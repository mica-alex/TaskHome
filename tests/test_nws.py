"""NOAA weather alerts and the listener plugin interface (P5-1, P5-3).

Endpoint shapes were verified live before these were written: a ZIP resolves
through zippopotam to a lat/lng, /points turns that into a forecast zone and a
county zone, and /alerts/active takes either.
"""
import pytest

from taskhome import constants, state, storage
from taskhome.listeners import base, nws


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(state, 'listeners', {})
    monkeypatch.setattr(storage, 'save_listeners', lambda: True)
    state.load_failed.clear()
    yield tmp_path
    state.load_failed.clear()


def alert(event='Wind Advisory', severity='Minor', **extra):
    data = {'id': f'urn:{event}', 'event': event, 'severity': severity,
            'urgency': 'Expected', 'messageType': 'Alert', 'status': 'Actual',
            'areaDesc': 'Hillsborough, NH', 'effective': '2026-07-27T10:00:00-04:00',
            'expires': '2026-07-27T14:00:00-04:00'}
    data.update(extra)
    return data


# --- the plugin interface -----------------------------------------------------

def test_listener_is_registered():
    assert 'nws' in base.registry()


def test_config_merges_over_schema_defaults(store):
    """A stored blob missing a key must not break code that reads it (P1-6)."""
    state.listeners['nws'] = {'enabled': True}
    config = nws.listener.config()
    assert config['enabled'] is True
    assert config['min_severity'] == 'Moderate'      # from the schema
    assert config['interval'] == 2


@pytest.mark.parametrize('spec_type,value,expected', [
    ('bool', 'on', True), ('bool', '', False), ('bool', 'false', False),
    ('int', '5', 5), ('multiselect', '03101, 03102', ['03101', '03102']),
])
def test_field_coercion(spec_type, value, expected):
    spec = base.field('k', 'Label', spec_type)
    assert base.coerce_field(spec, value) == expected


def test_int_bounds_are_enforced_with_a_readable_message():
    spec = base.field('interval', 'Check every (minutes)', 'int', min=1, max=60)
    with pytest.raises(ValueError, match='Check every'):
        base.coerce_field(spec, 999)


def test_unknown_field_type_is_refused():
    with pytest.raises(ValueError):
        base.field('k', 'Label', 'hologram')


def test_schema_declares_a_matrix_for_event_types():
    """D-6: per-event-type control is the whole design, and it must be
    expressible in the schema rather than needing a bespoke page."""
    events = next(f for f in nws.NWSListener.CONFIG_SCHEMA if f['key'] == 'events')
    assert events['type'] == 'matrix'
    assert {c['key'] for c in events['columns']} >= {
        'enabled', 'print_updates', 'print_cancels', 'quiet_hours'}


# --- zone resolution ----------------------------------------------------------

def test_zone_resolution_is_cached(store, monkeypatch):
    """Two requests per ZIP, for a value that never changes."""
    calls = []

    class Resp:
        def __init__(self, payload): self._p = payload
        def raise_for_status(self): pass
        def json(self): return self._p

    def fake_get(url, **kwargs):
        calls.append(url)
        if 'zippopotam' in url:
            return Resp({'places': [{'latitude': '42.99', 'longitude': '-71.46',
                                     'place name': 'Manchester',
                                     'state abbreviation': 'NH'}]})
        return Resp({'properties': {'forecastZone': 'https://x/zones/forecast/NHZ012',
                                    'county': 'https://x/zones/county/NHC011',
                                    'timeZone': 'America/New_York'}})

    monkeypatch.setattr(nws.requests, 'get', fake_get)
    first = nws.resolve_zip('03101')
    assert first['zone'] == 'NHZ012' and first['county'] == 'NHC011'
    assert len(calls) == 2

    nws.resolve_zip('03101')
    assert len(calls) == 2, 'the cache was not used'


def test_both_forecast_and_county_zones_are_queried(store, monkeypatch):
    """Some products are issued against one and some the other; subscribing to
    only one silently misses half of them."""
    monkeypatch.setattr(nws, 'resolve_zip', lambda z: {
        'zip': z, 'zone': 'NHZ012', 'county': 'NHC011', 'place': 'Manchester, NH'})
    zones = nws.listener.zones({'zips': ['03101']})
    assert set(zones) == {'NHZ012', 'NHC011'}


# --- filtering ----------------------------------------------------------------

def test_switched_off_event_does_not_print(store):
    config = dict(nws.listener.config(), events={'Wind Advisory': {'enabled': False}})
    ok, reason = nws.listener.should_print(config, alert())
    assert ok is False and 'switched off' in reason


def test_extreme_always_prints_even_in_quiet_hours(store):
    """That behaviour is the product, not a nicety."""
    config = dict(nws.listener.config(),
                  quiet_hours={'start': '00:00', 'end': '23:59'})
    ok, _ = nws.listener.should_print(
        config, alert('Tornado Warning', 'Extreme'))
    assert ok is True


def test_routine_alert_waits_out_quiet_hours(store):
    config = dict(nws.listener.config(),
                  quiet_hours={'start': '00:00', 'end': '23:59'})
    ok, reason = nws.listener.should_print(config, alert())
    assert ok is False and reason == 'quiet hours'


def test_quiet_hours_window_wraps_midnight(store):
    """22:00-07:00 is the normal case and the one naive comparisons break on."""
    from datetime import datetime
    config = dict(nws.listener.config(), quiet_hours={'start': '22:00', 'end': '07:00'})
    assert nws.listener.in_quiet_hours(config, datetime(2026, 3, 5, 23, 0)) is True
    assert nws.listener.in_quiet_hours(config, datetime(2026, 3, 5, 3, 0)) is True
    assert nws.listener.in_quiet_hours(config, datetime(2026, 3, 5, 12, 0)) is False


def test_updates_and_cancels_are_separately_controllable(store):
    config = dict(nws.listener.config(),
                  events={'Wind Advisory': {'enabled': True, 'print_updates': False,
                                            'print_cancels': True}})
    ok, _ = nws.listener.should_print(config, alert(messageType='Update'))
    assert ok is False
    ok, _ = nws.listener.should_print(config, alert(messageType='Cancel'))
    assert ok is True


def test_warnings_default_to_louder_settings_than_advisories():
    """Seeded from severity so a fresh install behaves sensibly (X-4)."""
    warning = nws.default_matrix_row('Tornado Warning')
    advisory = nws.default_matrix_row('Wind Advisory')
    assert warning['print_updates'] and not advisory['print_updates']
    assert warning['quiet_hours'] == 'override'
    assert advisory['quiet_hours'] == 'respect'


def test_test_alerts_are_excluded_by_default(store, monkeypatch):
    monkeypatch.setattr(nws, 'resolve_zip', lambda z: {
        'zip': z, 'zone': 'NHZ012', 'county': 'NHC011', 'place': 'x'})

    class Resp:
        def raise_for_status(self): pass
        def json(self):
            return {'features': [
                {'properties': dict(alert(), status='Test')},
                {'properties': dict(alert(), status='Actual')}]}

    monkeypatch.setattr(nws.requests, 'get', lambda *a, **k: Resp())
    config = dict(nws.listener.config(), zips=['03101'])
    assert len(nws.listener.poll(config, None)) == 1
    assert len(nws.listener.poll(dict(config, include_test=True), None)) == 2


# --- receipts -----------------------------------------------------------------

def test_severe_alerts_print_larger_than_routine_ones():
    """An Extreme alert should read from across the room; a routine advisory
    should not cost a hand of paper."""
    from taskhome import receipt
    loud = nws.listener.receipt_blocks(alert('Tornado Warning', 'Extreme',
                                             instruction='Take shelter.'))
    quiet = nws.listener.receipt_blocks(alert())
    assert receipt.height_mm(loud) > receipt.height_mm(quiet)
    assert loud[0]['height'] == 2 and quiet[0]['height'] == 1


def test_receipt_contains_the_essentials():
    from taskhome import receipt
    blocks = nws.listener.receipt_blocks(
        alert('Flash Flood Warning', 'Severe', instruction='Move to high ground.'))
    rendered = '\n'.join(receipt.render_text(blocks))
    for expected in ('Flash Flood Warning', 'Severe', 'Hillsborough', 'high ground'):
        assert expected in rendered


def test_context_fills_every_declared_placeholder():
    context = nws.listener.context(alert())
    missing = set(nws.NWSListener.PLACEHOLDERS) - set(context)
    assert not missing, f'context is missing {missing}'
