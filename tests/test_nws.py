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
    """Three requests per ZIP, for a value that never changes."""
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
        if '/zones/county/' in url:
            return Resp({'properties': {'name': 'Hillsborough', 'state': 'NH'}})
        return Resp({'properties': {'forecastZone': 'https://x/zones/forecast/NHZ012',
                                    'county': 'https://x/zones/county/NHC011',
                                    'timeZone': 'America/New_York',
                                    'relativeLocation': {'properties': {
                                        'city': 'Manchester', 'state': 'NH'}}}})

    monkeypatch.setattr(nws.requests, 'get', fake_get)
    first = nws.resolve_zip('03101')
    assert first['zone'] == 'NHZ012' and first['county'] == 'NHC011'
    assert first['county_label'] == 'Hillsborough County, NH'
    assert len(calls) == 3

    nws.resolve_zip('03101')
    assert len(calls) == 3, 'the cache was not used'


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

def test_severe_alerts_print_longer_than_routine_ones():
    """An Extreme alert carries its full description; a routine advisory does
    not cost a hand of paper.

    Note what is NOT asserted: that the routine one has a smaller title. Both
    get the large headline -- an advisory you cannot read at a glance is not
    much use either. Length is what varies.
    """
    from taskhome import receipt
    loud = nws.listener.receipt_blocks(
        alert('Tornado Warning', 'Extreme', instruction='Take shelter.',
              description='A tornado was observed near Goffstown.'))
    quiet = nws.listener.receipt_blocks(
        alert(description='Winds 20 to 30 mph expected.'))
    assert receipt.height_mm(loud) > receipt.height_mm(quiet)
    assert loud[0]['height'] == quiet[0]['height'] == 2


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


# --- the runtime must actually apply the filter --------------------------------
#
# Every filtering test above exercises should_print() directly. That is exactly
# how the filter came to be wired to nothing at all: the unit tests passed while
# base.run() printed every fetched alert, so a configured "wind advisories never"
# still printed. These go through run().

def run_with(monkeypatch, store, alerts, **config):
    """Run the real listener runtime over a fixed set of alerts."""
    from taskhome import printing, storage as storage_module
    from taskhome.listeners import base

    printed = []
    monkeypatch.setattr(printing, 'print_blocks', lambda blocks: printed.append(blocks) or True)
    monkeypatch.setattr(printing, 'record_history', lambda record: None)
    monkeypatch.setattr(storage_module, 'save_listeners', lambda: True)
    monkeypatch.setattr(nws.NWSListener, 'poll', lambda self, c, since: alerts)

    state.listeners['nws'] = dict({'enabled': True, 'interval': 1}, **config)
    from datetime import datetime, timezone
    base.run(nws.listener, datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc))
    return printed


def test_a_switched_off_event_does_not_reach_the_printer(store, monkeypatch):
    printed = run_with(monkeypatch, store, [alert('Wind Advisory')],
                       events={'Wind Advisory': {'enabled': False}})
    assert printed == [], 'a disabled event type still printed'


def test_an_enabled_event_does_reach_the_printer(store, monkeypatch):
    printed = run_with(monkeypatch, store, [alert('Wind Advisory')],
                       events={'Wind Advisory': {'enabled': True}})
    assert len(printed) == 1


def test_a_suppressed_alert_is_still_marked_seen(store, monkeypatch):
    """Otherwise it is re-fetched and re-evaluated on every poll forever, and
    the log fills with the same skip line every two minutes."""
    run_with(monkeypatch, store, [alert('Wind Advisory')],
             events={'Wind Advisory': {'enabled': False}})
    assert nws.listener.dedup_key(alert('Wind Advisory')) in state.listeners['nws']['seen']


def test_a_broken_filter_fails_open(store, monkeypatch):
    """Failing closed prints nothing and looks exactly like "no alerts", which
    is the one failure mode a weather alerter must not have."""
    monkeypatch.setattr(nws.NWSListener, 'should_print',
                        lambda self, c, i, now=None: 1 / 0)
    printed = run_with(monkeypatch, store, [alert('Tornado Warning', 'Extreme')])
    assert len(printed) == 1


# --- county labelling ----------------------------------------------------------
#
# The API returns bare names -- 'Hillsborough', not 'Hillsborough County' --
# and a bare name is genuinely ambiguous: Hillsborough is also a town in NH.
# Appending "County" unconditionally is wrong in four different ways, each
# verified against the live zone API.

@pytest.mark.parametrize('name,state,expected', [
    ('Hillsborough', 'NH', 'Hillsborough County, NH'),
    ('Los Angeles', 'CA', 'Los Angeles County, CA'),
    ('Orleans', 'LA', 'Orleans Parish, LA'),           # Louisiana has parishes
    ('City of Alexandria', 'VA', 'City of Alexandria, VA'),   # already says City
    ('Baltimore City', 'MD', 'Baltimore City, MD'),
    ('Anchorage', 'AK', 'Anchorage, AK'),              # borough/municipality/census area
    ('', 'NH', ''),
])
def test_county_label(name, state, expected):
    assert nws.county_label(name, state) == expected


def test_a_bare_county_name_is_never_printed_alone():
    """The whole point of the change: 'Hillsborough, NH' could be the town."""
    assert nws.county_label('Hillsborough', 'NH') != 'Hillsborough, NH'


def test_area_line_names_the_place_the_zip_and_the_county():
    resolved = {'zip': '03102', 'city': 'Manchester', 'state': 'NH',
                'county_label': 'Hillsborough County, NH'}
    line = nws.listener.area_label(
        {'_zones': {'NHC011': resolved}, '_zones_exact': True})
    assert line == 'Manchester (03102) - Hillsborough County, NH'


def test_zips_sharing_a_county_are_grouped():
    zones = {
        'NHC011': {'zip': '03102', 'city': 'Manchester', 'county_label': 'Hillsborough County, NH'},
        'NHZ012': {'zip': '03110', 'city': 'Bedford', 'county_label': 'Hillsborough County, NH'},
    }
    line = nws.listener.area_label({'_zones': zones, '_zones_exact': True})
    assert line == 'Bedford (03110), Manchester (03102) - Hillsborough County, NH'
    assert line.count('Hillsborough County') == 1, 'the county was repeated'


def test_zips_in_different_counties_are_listed_separately():
    zones = {
        'a': {'zip': '03102', 'city': 'Manchester', 'county_label': 'Hillsborough County, NH'},
        'b': {'zip': '03301', 'city': 'Concord', 'county_label': 'Merrimack County, NH'},
    }
    line = nws.listener.area_label({'_zones': zones, '_zones_exact': True})
    assert 'Hillsborough County' in line and 'Merrimack County' in line


def test_the_raw_nws_area_is_still_available_to_templates():
    """Someone may want the authoritative text; it should not be lost."""
    located = dict(alert(), _zones={'NHC011': {
        'zip': '03102', 'city': 'Manchester',
        'county_label': 'Hillsborough County, NH'}}, _zones_exact=True)
    context = nws.listener.context(located)
    assert context['area_nws'] == 'Hillsborough, NH'
    assert context['area'] == 'Manchester (03102) - Hillsborough County, NH'


def test_an_alert_only_claims_the_zips_it_actually_covers(store, monkeypatch):
    """Attaching every configured zone made the receipt claim a tornado warning
    for a ZIP three counties away that merely happened to be in the settings."""
    monkeypatch.setattr(nws, 'resolve_zip', lambda z: {
        '03102': {'zip': '03102', 'city': 'Manchester', 'zone': 'NHZ012',
                  'county': 'NHC011', 'county_label': 'Hillsborough County, NH'},
        '03301': {'zip': '03301', 'city': 'Concord', 'zone': 'NHZ008',
                  'county': 'NHC013', 'county_label': 'Merrimack County, NH'},
    }[z])

    class Resp:
        def raise_for_status(self): pass
        def json(self):
            return {'features': [{'properties': dict(
                alert(), affectedZones=['https://api.weather.gov/zones/county/NHC011'])}]}

    monkeypatch.setattr(nws.requests, 'get', lambda *a, **k: Resp())
    alerts = nws.listener.poll(dict(nws.listener.config(),
                                    zips=['03102', '03301']), None)
    line = nws.listener.area_label(alerts[0])
    assert 'Manchester' in line
    assert 'Concord' not in line, 'claimed a ZIP the alert does not cover'


def test_an_unmatched_alert_falls_back_to_the_nws_wording(store):
    """Never overstate how precisely we know where an alert applies."""
    line = nws.listener.area_label(
        {'areaDesc': 'Coastal Rockingham, NH', '_zones': {'x': {'zip': '03102'}},
         '_zones_exact': False})
    assert line == 'Coastal Rockingham, NH'


def test_a_failed_county_lookup_does_not_lose_the_alert(monkeypatch):
    """A slightly less precise area line beats no weather alert."""
    def boom(url, **kwargs):
        raise RuntimeError('weather.gov down')
    monkeypatch.setattr(nws.requests, 'get', boom)
    name, state = nws._county_identity('NHC011', {}, 'NH')
    assert name == '' and state == 'NH'


def test_a_stale_cache_entry_is_re_resolved(store, monkeypatch):
    """The zone cache has no TTL, so an old entry shape would persist forever."""
    monkeypatch.setattr(nws, 'load_zone_cache',
                        lambda: {'03102': {'zip': '03102', 'zone': 'NHZ012'}})
    calls = []

    class Resp:
        def raise_for_status(self): pass
        def json(self):
            return {'places': [{'latitude': '1', 'longitude': '2',
                                'place name': 'Manchester',
                                'state abbreviation': 'NH'}],
                    'properties': {'forecastZone': 'x/NHZ012',
                                   'county': 'x/NHC011', 'name': 'Hillsborough',
                                   'state': 'NH'}}

    monkeypatch.setattr(nws.requests, 'get',
                        lambda url, **k: calls.append(url) or Resp())
    monkeypatch.setattr(nws, 'save_zone_cache', lambda c: True)
    result = nws.resolve_zip('03102')
    assert calls, 'a version-1 entry was used as-is'
    assert result['cache_version'] == nws.CACHE_VERSION


# --- layout presets ------------------------------------------------------------

def test_the_title_is_large_in_every_preset_but_minimal():
    """Title size and description are separate knobs. They used to be one
    flag, so an advisory could not have a readable headline without also
    printing 600 characters of forecast discussion."""
    presets = dict(nws.listener.template_presets())
    for name in ('nws-default', 'nws-compact'):
        assert presets[name][0]['height'] == 2, f'{name} lost its large title'
    assert presets['nws-minimal'][0]['height'] == 1


def test_only_the_default_preset_carries_the_description():
    presets = dict(nws.listener.template_presets())
    assert '{description}' in str(presets['nws-default'])
    assert '{description}' not in str(presets['nws-compact'])
