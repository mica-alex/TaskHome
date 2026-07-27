"""The schema-driven settings renderer (P2-11).

The contract these protect: a listener contributes a schema and gets a working
settings page, and no handler, template or test may contain per-listener code.
"""
import pytest

from taskhome import state, storage
from taskhome.listeners import base, nws


@pytest.fixture
def client(tmp_path, monkeypatch):
    from taskhome import constants, create_app
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(storage, 'save_listeners', lambda: True)
    monkeypatch.setattr(state, 'listeners', {})
    app = create_app(load=False, with_scheduler=False)
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


def test_index_lists_every_registered_listener(client):
    body = client.get('/listener').get_data(as_text=True)
    assert 'SeeClickFix' in body
    for listener in base.registry().values():
        assert listener.title in body


def test_settings_page_renders_every_declared_field(client):
    body = client.get('/listener/settings/nws').get_data(as_text=True)
    for spec in nws.NWSListener.CONFIG_SCHEMA:
        assert spec['label'] in body, f"{spec['key']} was not rendered"


def test_matrix_renders_a_row_per_known_event(client):
    body = client.get('/listener/settings/nws').get_data(as_text=True)
    for event in nws.COMMON_EVENTS:
        assert f'data-row="{event}"' in body


def test_unknown_listener_is_404_not_500(client):
    assert client.get('/listener/settings/nope').status_code == 404


def test_saving_round_trips_through_the_schema(client):
    client.post('/listener/settings/nws', data={
        'enabled': 'on', 'zips': '03101,03102', 'interval': '5',
        'min_severity': 'Severe', 'min_urgency': 'Expected',
        'quiet_hours.start': '23:00', 'quiet_hours.end': '06:30',
        'events[]': ['Wind Advisory'],
        'events[Wind Advisory][enabled]': 'on',
        'events[Wind Advisory][quiet_hours]': 'override',
    })
    config = nws.listener.config()
    assert config['enabled'] is True
    assert config['zips'] == ['03101', '03102']
    assert config['interval'] == 5
    assert config['quiet_hours'] == {'start': '23:00', 'end': '06:30'}
    assert config['events']['Wind Advisory']['enabled'] is True
    assert config['events']['Wind Advisory']['print_updates'] is False
    assert config['events']['Wind Advisory']['quiet_hours'] == 'override'


def test_unchecking_a_box_actually_turns_it_off(client):
    """An unchecked checkbox submits nothing, so the naive `key in form` read
    silently preserves the old value forever."""
    client.post('/listener/settings/nws', data={'enabled': 'on', 'interval': '5'})
    assert nws.listener.config()['enabled'] is True
    client.post('/listener/settings/nws', data={'interval': '5'})
    assert nws.listener.config()['enabled'] is False


def test_an_all_unchecked_matrix_row_is_not_mistaken_for_an_absent_one(client):
    """Same failure one level down: a row where every box is off submits no
    inputs at all, and without the hidden row marker it would reappear with its
    defaults restored."""
    client.post('/listener/settings/nws', data={
        'interval': '5', 'events[]': ['Tornado Warning'],
    })
    row = nws.listener.config()['events']['Tornado Warning']
    assert row['enabled'] is False and row['print_updates'] is False


def test_a_bad_value_is_a_message_not_a_500(client):
    response = client.post('/listener/settings/nws',
                           data={'interval': 'soon', 'enabled': 'on'})
    assert response.status_code == 200
    assert 'Check every' in response.get_data(as_text=True)


def test_a_rejected_save_does_not_partially_apply(client):
    client.post('/listener/settings/nws', data={'interval': '5'})
    client.post('/listener/settings/nws', data={'interval': '99999', 'enabled': 'on'})
    assert nws.listener.config()['interval'] == 5
    assert nws.listener.config()['enabled'] is False


def test_runtime_state_survives_a_settings_save(client):
    """Watermarks and seen-ids live in the same blob as the settings; a save
    that dropped them would replay the whole backlog."""
    state.listeners['nws'] = {'last_check': '2026-07-27T10:00:00Z', 'seen': ['a']}
    client.post('/listener/settings/nws', data={'interval': '5', 'enabled': 'on'})
    assert state.listeners['nws']['last_check'] == '2026-07-27T10:00:00Z'
    assert state.listeners['nws']['seen'] == ['a']


def test_every_field_type_in_the_schema_has_a_renderer():
    """A field type the macro does not know falls through to a text input,
    which would look fine and quietly corrupt the value on save."""
    import pathlib
    macro = pathlib.Path('taskhome/templates/partials/setting_field.html').read_text()
    used = {spec['type'] for listener in base.registry().values()
            for spec in listener.CONFIG_SCHEMA}
    for kind in used:
        assert f"kind == '{kind}'" in macro, f'no branch renders {kind}'


def test_groups_preserve_declaration_order(client):
    headings = [heading for heading, _ in nws.listener.groups()]
    assert headings == list(dict.fromkeys(
        spec.get('group') for spec in nws.NWSListener.CONFIG_SCHEMA))


def test_every_field_type_is_coercible():
    """The companion to the renderer check. A type the macro renders but
    coerce_field does not know falls through to str(), which turned a
    time_range dict into the literal text "{'start': '23:00', ...}"."""
    samples = {'bool': True, 'int': 3, 'text': 'x', 'secret': 'x',
               'select': 'a', 'multiselect': ['a'], 'duration': 15,
               'time_range': {'start': '22:00', 'end': '07:00'},
               'matrix': {'row': {'enabled': True}}}
    for kind in base.FIELD_TYPES:
        spec = base.field('k', 'Label', kind, options=['a'])
        result = base.coerce_field(spec, samples[kind])
        assert not (isinstance(samples[kind], (dict, list)) and isinstance(result, str)), \
            f'{kind} was stringified'


@pytest.mark.parametrize('bad', ['25:00', '7:00', 'evening', '', '22:60'])
def test_malformed_times_are_refused(bad):
    spec = base.field('quiet_hours', 'Quiet hours', 'time_range')
    with pytest.raises(ValueError, match='Quiet hours'):
        base.coerce_field(spec, {'start': bad, 'end': '07:00'})
