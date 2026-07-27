"""Transit departures and alerts (P5-2 #10), and the GTFS-RT reader."""
from datetime import datetime, timedelta, timezone

import pytest

from taskhome import constants, gtfsrt, receipt, state, storage
from taskhome.listeners import base, transit


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(state, 'listeners', {})
    monkeypatch.setattr(storage, 'save_listeners', lambda: True)
    yield


# --- the GTFS-RT reader -------------------------------------------------------

def varint(n):
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def field(number, wire, payload):
    tag = varint((number << 3) | wire)
    if wire == 2:
        return tag + varint(len(payload)) + payload
    return tag + payload


def test_varints_and_nesting_decode():
    message = field(1, 0, varint(150)) + field(2, 2, b'hello')
    decoded = gtfsrt.decode(message)
    assert decoded[1] == [150] and decoded[2] == [b'hello']


def test_a_trip_update_is_read():
    trip = field(1, 2, b'trip-7') + field(5, 2, b'A')
    departure = field(2, 0, varint(1700000000))
    stu = field(3, 2, departure) + field(4, 2, b'H11S')
    update = field(1, 2, trip) + field(2, 2, stu)
    entity = field(1, 2, b'e1') + field(3, 2, update)
    feed = gtfsrt.parse_feed(field(2, 2, entity))

    assert len(feed['trip_updates']) == 1
    parsed = feed['trip_updates'][0]
    assert parsed['route_id'] == 'A' and parsed['trip_id'] == 'trip-7'
    assert parsed['stops'][0]['stop_id'] == 'H11S'
    assert parsed['stops'][0]['departure'] == 1700000000


def test_an_alert_is_read():
    def translated(text):
        return field(1, 2, field(1, 2, text))
    alert = (field(5, 2, field(3, 2, b'Red'))
             + field(10, 2, translated(b'Delays'))
             + field(11, 2, translated(b'Signal problem')))
    feed = gtfsrt.parse_feed(field(2, 2, field(5, 2, alert)))
    assert feed['alerts'][0]['header'] == 'Delays'
    assert feed['alerts'][0]['routes'] == ['Red']


def test_unknown_fields_are_skipped():
    """A feed that gains a field must not break the reader."""
    update = field(1, 2, field(1, 2, b't')) + field(99, 0, varint(1))
    feed = gtfsrt.parse_feed(field(2, 2, field(3, 2, update)))
    assert len(feed['trip_updates']) == 1


def test_truncated_data_raises_rather_than_returning_nonsense():
    with pytest.raises(ValueError):
        gtfsrt.parse_feed(b'\x0a\xff')


def test_an_alert_with_no_header_is_dropped():
    """Nothing useful to print."""
    alert = field(5, 2, field(3, 2, b'Red'))
    assert gtfsrt.parse_feed(field(2, 2, field(5, 2, alert)))['alerts'] == []


# --- wrap: column alignment and indentation -----------------------------------

def test_runs_of_spaces_survive_wrapping():
    """Text is pre-wrapped before it reaches the device, so collapsing spaces
    meant the printer could not receive an aligned column layout at all."""
    line = 'Orange   Forest Hills      4 min'
    assert receipt.wrap(line, 64) == [line]


def test_leading_indentation_survives_wrapping():
    assert receipt.wrap('   - BBC News', 64) == ['   - BBC News']


def test_wrapping_still_happens_at_the_column_limit():
    wrapped = receipt.wrap('word ' * 20, 20)
    assert all(len(line) <= 20 for line in wrapped)
    assert len(wrapped) > 1


def test_an_overlong_word_is_still_broken():
    wrapped = receipt.wrap('x' * 70, 64)
    assert len(wrapped) == 2 and len(wrapped[0]) == 64


def test_trailing_padding_is_dropped():
    """Invisible on paper, and it only risks wrapping early."""
    assert receipt.wrap('abc     ', 64) == ['abc']


# --- per-stop and per-route granularity ---------------------------------------

@pytest.fixture
def configured(store):
    return dict(transit.listener.config(), provider='mbta',
                stops=['place-north', 'place-sstat'],
                stop_settings={'place-north': {'departures': True, 'alerts': False},
                               'place-sstat': {'departures': False, 'alerts': True}},
                routes=['Red', 'Orange'],
                route_settings={'Red': {'alerts': True, 'min_severity': '7'},
                                'Orange': {'alerts': False, 'min_severity': 'any'}})


def test_a_board_and_alerts_can_be_on_different_stops(configured):
    """Departure board where you leave from, alerts where they would change
    your plans -- not necessarily the same place."""
    assert transit.listener.wanted_stops(configured, 'departures') == ['place-north']
    assert transit.listener.wanted_stops(configured, 'alerts') == ['place-sstat']


def test_alerts_can_be_on_for_one_line_and_off_for_another(configured):
    assert transit.listener.wanted_routes(configured) == ['Red']


@pytest.mark.parametrize('severity,routes,expected', [
    (7, ['Red'], True),                  # meets Red's threshold
    (3, ['Red'], False),                 # below it
    (9, ['Orange'], False),              # Orange is switched off entirely
    (8, ['Red', 'Orange'], True),        # shared alert, Red wants it
    (None, ['Red'], True),               # GTFS-RT carries no severity
])
def test_alert_filtering(configured, severity, routes, expected):
    alert = {'severity': severity, 'routes': routes}
    assert transit.listener.alert_passes(configured, alert) is expected


def test_an_alert_on_an_unsubscribed_line_does_not_ride_along(configured):
    """Otherwise a minor notice prints just because it shares an alert with a
    line that is switched on."""
    assert transit.listener.alert_passes(
        configured, {'severity': 10, 'routes': ['Blue']}) is False


def test_matrix_rows_follow_the_configured_lists(configured):
    """So the two lists and their settings cannot drift apart."""
    rows = transit.listener.matrix_rows({'key': 'stop_settings'}, configured)
    assert rows == ['place-north', 'place-sstat']
    rows = transit.listener.matrix_rows({'key': 'route_settings'}, configured)
    assert rows == ['Red', 'Orange']


def test_a_stop_added_but_not_configured_gets_a_board(store):
    """Adding a stop and having it do nothing would be baffling."""
    config = dict(transit.listener.config(), stops=['place-north'], stop_settings={})
    assert transit.listener.wanted_stops(config, 'departures') == ['place-north']
    assert transit.listener.wanted_stops(config, 'alerts') == []


# --- the MBTA provider --------------------------------------------------------

def test_cancelled_predictions_are_excluded(store, monkeypatch):
    """Sorting ascending by departure_time puts NULLs first, and a cancelled
    trip has no times -- so an unfiltered board is full of blanks exactly when
    someone is looking at it during a disruption."""
    soon = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    payload = {'data': [
        {'attributes': {'schedule_relationship': 'CANCELLED',
                        'departure_time': None}, 'relationships': {}},
        {'attributes': {'schedule_relationship': None, 'departure_time': soon},
         'relationships': {}},
    ], 'included': []}
    monkeypatch.setattr(transit.MBTAProvider, '_get',
                        lambda self, c, p, params: payload)
    found = transit.PROVIDERS['mbta'].departures({'stops': ['place-north']}, 5)
    assert len(found) == 1


def test_departures_already_gone_are_excluded(store, monkeypatch):
    past = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    payload = {'data': [{'attributes': {'departure_time': past},
                         'relationships': {}}], 'included': []}
    monkeypatch.setattr(transit.MBTAProvider, '_get',
                        lambda self, c, p, params: payload)
    assert transit.PROVIDERS['mbta'].departures({'stops': ['x']}, 5) == []


def test_an_api_key_is_optional(store):
    spec = next(f for f in transit.TransitListener.CONFIG_SCHEMA
                if f['key'] == 'api_key')
    assert spec['default'] == ''


def test_known_feeds_are_offered():
    assert 'nyc-ace' in transit.KNOWN_FEEDS
    assert all(url.startswith('http') for _, url in transit.KNOWN_FEEDS.values())


def test_a_board_prints_once_per_configured_time(store):
    now = datetime.now()
    config = dict(transit.listener.config(),
                  board_times=[now.strftime('%H:%M')])
    assert transit.listener._board_due(config, now) is True
    assert transit.listener._board_due(config, now) is False


def test_a_restart_late_in_the_day_does_not_print_every_board(store):
    """Otherwise restarting at 23:00 prints every board configured that day."""
    now = datetime.now().replace(hour=23, minute=0)
    config = dict(transit.listener.config(), board_times=['08:00'])
    assert transit.listener._board_due(config, now) is False


def test_registered_and_editable():
    from taskhome import styles
    assert 'transit' in base.registry()
    assert 'transit' in styles.kinds()
    assert styles.builtin_templates('transit')
