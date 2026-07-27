"""Listener receipts in the Receipt Studio (P3-5).

The Studio used to be hardcoded to ('task', 'scf'), so a listener built on the
plugin interface had an uneditable receipt. These protect the general property
-- every registered listener is editable -- rather than the NWS special case.
"""
import json

import pytest

from taskhome import constants, create_app, receipt, state, styles
from taskhome.listeners import base, nws


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(state, 'config', {'theme': 'system'})
    app = create_app(load=False, with_scheduler=False)
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


# --- the kind list follows the registry ---------------------------------------

def test_every_registered_listener_is_an_editable_kind():
    for name in base.registry():
        assert name in styles.kinds(), f'{name} has no editable receipt'


def test_kinds_still_include_the_builtins():
    assert styles.kinds()[:2] == ('task', 'scf')


def test_each_kind_has_a_human_label():
    for kind in styles.kinds():
        label = styles.kind_label(kind)
        assert label and label != kind, f'{kind} shows a raw key as its tab'


def test_the_studio_offers_a_tab_per_kind(client):
    body = client.get('/settings/receipts').get_data(as_text=True)
    for kind in styles.kinds():
        assert f'kind={kind}' in body
        assert styles.kind_label(kind) in body


# --- presets ------------------------------------------------------------------

def test_a_listener_kind_has_at_least_one_preset():
    for name in base.registry():
        assert styles.builtin_templates(name), f'{name} ships no preset'


def test_presets_use_placeholders_not_baked_in_values():
    """A preset with a real value baked in prints that value forever."""
    for name, listener in base.registry().items():
        for preset in styles.builtin_templates(name):
            rendered = json.dumps(preset['blocks'])
            for key, sample in listener.PLACEHOLDERS.items():
                if isinstance(sample, str) and len(sample) > 12:
                    assert sample not in rendered, \
                        f'{preset["name"]} has {key} baked in'


def test_every_placeholder_a_preset_uses_is_declared():
    """An undeclared placeholder renders as literal text on paper."""
    for name, listener in base.registry().items():
        for preset in styles.builtin_templates(name):
            used = set(styles._PLACEHOLDER_RE.findall(json.dumps(preset['blocks'])))
            assert used <= set(listener.PLACEHOLDERS), \
                f'{preset["name"]} uses {used - set(listener.PLACEHOLDERS)}'


def test_presets_are_generated_from_the_printing_code():
    """Not duplicated. A preset that drifted from what actually prints would
    be worse than none, because it would look authoritative."""
    alert = dict(nws.listener.PLACEHOLDERS)
    printed = nws.listener.blocks_from_context(
        alert, big_title=True, description=True)
    preset = dict(nws.listener.template_presets())['nws-default']
    assert [b.get('type') for b in printed] == [b.get('type') for b in preset]


def test_nws_ships_a_loud_and_a_compact_layout():
    """A tornado warning and a wind advisory are not the same kind of event."""
    names = [t['name'] for t in styles.builtin_templates('nws')]
    assert 'nws-default' in names and 'nws-compact' in names
    heights = {}
    for name in names:
        blocks = styles.fill(styles.get_template('nws', name),
                             styles.sample_context('nws'))
        heights[name] = receipt.height_mm(blocks)
    assert heights['nws-default'] > heights['nws-compact']


def test_a_builtin_preset_cannot_be_deleted():
    for name in [t['name'] for t in styles.builtin_templates('nws')]:
        with pytest.raises(styles.TemplateError):
            styles.delete_template('nws', name)


# --- the studio round trip ----------------------------------------------------

def test_the_studio_renders_a_listener_kind(client):
    response = client.get('/settings/receipts?kind=nws')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'nws-default' in body and 'nws-compact' in body


def test_previewing_a_listener_template_works(client):
    response = client.post('/api/receipt/preview', json={
        'template': styles.get_template('nws', 'nws-default')})
    assert response.status_code == 200
    assert response.get_json()['ok'] is True


def test_saving_a_custom_listener_template_round_trips(client):
    template = styles.get_template('nws', 'nws-default')
    template = dict(template, name='my-alerts', builtin=False)
    response = client.post('/api/receipt/templates/nws', json={'template': template})
    assert response.status_code == 200, response.get_data(as_text=True)
    assert 'my-alerts' in [t['name'] for t in styles.list_templates('nws')]


def test_a_template_with_an_unknown_placeholder_is_refused(client):
    template = {'name': 'bad', 'kind': 'nws', 'version': 1,
                'blocks': [{'type': 'text', 'value': '{not_a_field}'}]}
    with pytest.raises(styles.TemplateError):
        styles.validate_template(template)


def test_an_unregistered_kind_is_still_refused(client):
    template = {'name': 'x', 'kind': 'nope', 'version': 1,
                'blocks': [{'type': 'text', 'value': 'x'}]}
    with pytest.raises(styles.TemplateError):
        styles.validate_template(template)


# --- per-item style selection -------------------------------------------------

def test_an_event_can_choose_its_own_receipt_style():
    config = {'events': {'Tornado Warning': {'style': 'nws-compact'}}}
    assert nws.listener.template_name(config, {'event': 'Tornado Warning'}) == 'nws-compact'


def test_no_style_falls_back_to_the_active_template():
    assert nws.listener.template_name({'events': {}}, {'event': 'Wind Advisory'}) is None


def test_the_style_column_offers_the_templates_that_exist_now():
    """A fixed option list goes stale the moment someone saves a template."""
    spec = next(f for f in nws.NWSListener.CONFIG_SCHEMA if f['key'] == 'events')
    column = next(c for c in spec['columns'] if c['key'] == 'style')
    options = nws.listener.matrix_column_options(spec, column)
    assert 'nws-compact' in options
    assert '' in options, 'there is no way back to the default'


def test_the_runtime_uses_the_per_event_template(tmp_path, monkeypatch):
    """The whole point: a chosen style must reach the paper."""
    from datetime import datetime, timezone
    from taskhome import printing, storage
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(storage, 'save_listeners', lambda: True)
    monkeypatch.setattr(state, 'listeners', {})

    used = []
    monkeypatch.setattr(styles, 'fill', lambda tpl, ctx: used.append(tpl['name']) or [])
    monkeypatch.setattr(printing, 'print_blocks', lambda blocks: True)
    monkeypatch.setattr(printing, 'record_history', lambda r: None)
    monkeypatch.setattr(nws.NWSListener, 'poll', lambda self, c, s: [
        {'id': 'x', 'event': 'Tornado Warning', 'severity': 'Extreme',
         'messageType': 'Alert', 'status': 'Actual'}])

    state.listeners['nws'] = {
        'enabled': True, 'interval': 1,
        'events': {'Tornado Warning': {'enabled': True, 'style': 'nws-compact'}}}
    base.run(nws.listener, datetime(2026, 7, 27, 12, tzinfo=timezone.utc))
    assert used == ['nws-compact']


def test_an_unusable_template_falls_back_rather_than_dropping_the_alert(tmp_path, monkeypatch):
    """A weather alert must not be lost because a template was deleted."""
    from datetime import datetime, timezone
    from taskhome import printing, storage
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(storage, 'save_listeners', lambda: True)
    monkeypatch.setattr(state, 'listeners', {})

    printed = []
    monkeypatch.setattr(styles, 'get_template',
                        lambda k, n=None: (_ for _ in ()).throw(RuntimeError('gone')))
    monkeypatch.setattr(printing, 'print_blocks', lambda blocks: printed.append(blocks) or True)
    monkeypatch.setattr(printing, 'record_history', lambda r: None)
    monkeypatch.setattr(nws.NWSListener, 'poll', lambda self, c, s: [
        {'id': 'x', 'event': 'Tornado Warning', 'severity': 'Extreme',
         'messageType': 'Alert', 'status': 'Actual'}])

    state.listeners['nws'] = {'enabled': True, 'interval': 1}
    base.run(nws.listener, datetime(2026, 7, 27, 12, tzinfo=timezone.utc))
    assert len(printed) == 1 and printed[0], 'the alert was dropped'
