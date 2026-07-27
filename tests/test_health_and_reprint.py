"""Health/status endpoints (P6-4) and reprint / poll-now (P4-6)."""
import pytest

from taskhome import constants, create_app, printing, scheduler, state, storage


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(state, 'config', {'theme': 'system', 'max_history': 500})
    monkeypatch.setattr(state, 'history', [])
    monkeypatch.setattr(state, 'listeners', {})
    monkeypatch.setattr(storage, 'save_history', lambda: True)
    monkeypatch.setattr(storage, 'save_listeners', lambda: True)
    state.load_failed.clear()
    app = create_app(load=False, with_scheduler=False)
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c
    state.load_failed.clear()


# --- health -------------------------------------------------------------------

def test_health_is_200_when_nothing_is_wrong(client, monkeypatch):
    monkeypatch.setattr(printing, 'is_printer_connected', lambda: True)
    response = client.get('/api/health')
    assert response.status_code == 200
    assert response.get_json()['ok'] is True


def test_an_unplugged_printer_is_not_unhealthy(client, monkeypatch):
    """Normal for this appliance -- the queue makes it survivable. A monitor
    that pages at 2am because someone moved the printer gets muted."""
    monkeypatch.setattr(printing, 'is_printer_connected', lambda: False)
    response = client.get('/api/health')
    assert response.status_code == 200
    assert response.get_json()['printer']['connected'] is False


def test_a_write_blocked_store_is_unhealthy(client, monkeypatch):
    """Silently read-only is the worst failure this app has; it must surface."""
    monkeypatch.setattr(printing, 'is_printer_connected', lambda: True)
    state.load_failed.add('tasks')
    response = client.get('/api/health')
    assert response.status_code == 503
    assert any('tasks' in p for p in response.get_json()['problems'])


def test_parked_jobs_are_unhealthy(client, monkeypatch):
    from taskhome import queue
    monkeypatch.setattr(printing, 'is_printer_connected', lambda: True)
    monkeypatch.setattr(queue, 'stats', lambda: {
        'total': 1, 'parked': 1, 'waiting': 0, 'oldest': None})
    response = client.get('/api/health')
    assert response.status_code == 503


def test_a_stalled_scheduler_is_unhealthy(client, monkeypatch):
    """The failure this endpoint exists for: the thread dies or wedges, the web
    UI keeps serving perfectly, and receipts simply stop."""
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr(printing, 'is_printer_connected', lambda: True)
    stale = datetime.now(timezone.utc) - timedelta(seconds=600)
    monkeypatch.setattr(scheduler, 'heartbeat', lambda: (stale, 42))
    monkeypatch.setattr(scheduler, 'is_alive', lambda: True)
    response = client.get('/api/health')
    assert response.status_code == 503
    assert any('not ticked' in p for p in response.get_json()['problems'])


def test_a_deliberately_absent_scheduler_is_not_unhealthy(client, monkeypatch):
    """Running the UI without a scheduler is a normal development setup."""
    monkeypatch.setattr(printing, 'is_printer_connected', lambda: True)
    monkeypatch.setattr(scheduler, 'is_alive', lambda: False)
    assert client.get('/api/health').status_code == 200


def test_status_is_always_200(client, monkeypatch):
    """A status widget that vanishes when something is wrong is worse than
    useless."""
    monkeypatch.setattr(printing, 'is_printer_connected', lambda: True)
    state.load_failed.add('tasks')
    assert client.get('/api/status').status_code == 200
    assert client.get('/api/status').get_json()['problems']


def test_the_heartbeat_advances_with_ticks():
    before = scheduler.heartbeat()
    assert isinstance(before[1], int)


# --- reprint ------------------------------------------------------------------

def record(uid='abc', **extra):
    return {'uid': uid, 'type': 'task', 'title': 'Bins', 'id': 't1',
            'recurring': 'weekly', 'print_time': '2026-07-27T08:00:00', **extra}


def test_reprint_prints_and_reports_success(client, monkeypatch):
    printed = []
    monkeypatch.setattr(printing, 'print_blocks', lambda b: printed.append(b) or True)
    state.history.append(record())
    response = client.post('/api/history/reprint/abc')
    assert response.status_code == 200 and len(printed) == 1


def test_a_reprint_is_not_added_to_history(client, monkeypatch):
    """Otherwise the list grows every time someone re-runs a row, and the
    second copy looks like a second occurrence."""
    monkeypatch.setattr(printing, 'print_blocks', lambda b: True)
    state.history.append(record())
    client.post('/api/history/reprint/abc')
    assert len(state.history) == 1


def test_reprint_reports_an_offline_printer_honestly(client, monkeypatch):
    monkeypatch.setattr(printing, 'print_blocks', lambda b: False)
    state.history.append(record())
    assert client.post('/api/history/reprint/abc').status_code == 503


def test_an_unknown_uid_is_404(client):
    assert client.post('/api/history/reprint/nope').status_code == 404


def test_reprint_is_addressed_by_uid_not_position(client, monkeypatch):
    """Position stops being an identity the moment the table is filtered,
    searched, paged or trimmed by max_history."""
    printed = []
    monkeypatch.setattr(printing, 'print_blocks', lambda b: printed.append(b) or True)
    state.history.extend([record('one', title='First'), record('two', title='Second')])
    client.post('/api/history/reprint/two')
    rendered = str(printed[0])
    assert 'Second' in rendered and 'First' not in rendered


@pytest.mark.parametrize('kind,extra', [
    ('task', {'title': 'Bins'}),
    ('scf', {'category': 'Pothole', 'address': 'Elm St', 'status': 'Open'}),
    ('nws', {'event': 'Tornado Warning', 'severity': 'Extreme', 'category': 'Tornado Warning'}),
])
def test_every_history_kind_can_be_reprinted(client, monkeypatch, kind, extra):
    printed = []
    monkeypatch.setattr(printing, 'print_blocks', lambda b: printed.append(b) or True)
    state.history.append(dict(record('k', type=kind), **extra))
    response = client.post('/api/history/reprint/k')
    assert response.status_code == 200, response.get_json()
    assert printed and printed[0], f'{kind} produced an empty receipt'


def test_history_records_get_a_uid_when_written(monkeypatch, tmp_path):
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(state, 'history', [])
    monkeypatch.setattr(state, 'config', {'max_history': 10})
    monkeypatch.setattr(storage, 'save_history', lambda: True)
    printing.record_history({'type': 'task', 'title': 'x'})
    assert state.history[0]['uid']


def test_old_history_records_get_a_uid_on_load(tmp_path, monkeypatch):
    """Back-filled, or every pre-existing row would have an unusable button."""
    import json
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    for name in ('config', 'tasks', 'listeners'):
        (tmp_path / f'{name}.json').write_text('{}' if name != 'tasks' else '[]')
    (tmp_path / 'history.json').write_text(json.dumps([{'title': 'old'}]))
    monkeypatch.setattr(constants, 'HISTORY_FILE', str(tmp_path / 'history.json'))
    monkeypatch.setattr(constants, 'CONFIG_FILE', str(tmp_path / 'config.json'))
    monkeypatch.setattr(constants, 'TASKS_FILE', str(tmp_path / 'tasks.json'))
    monkeypatch.setattr(constants, 'LISTENERS_FILE', str(tmp_path / 'listeners.json'))
    monkeypatch.setattr(storage, 'migrate_legacy_data_files', lambda: None)
    storage.load_data()
    assert state.history[0].get('uid'), 'an old record has no reprint handle'
    assert state.history[0]['type'] == 'task'


# --- poll now -----------------------------------------------------------------

def test_poll_now_runs_the_listener(client, monkeypatch):
    from taskhome.listeners import base
    monkeypatch.setattr(base, 'run', lambda listener, now: 3)
    state.listeners['nws'] = {'enabled': True}
    response = client.post('/api/listeners/nws/poll')
    assert response.status_code == 200
    assert response.get_json()['printed'] == 3


def test_poll_now_ignores_the_interval_gate(client, monkeypatch):
    """'Check now' has to mean now, or the button does nothing most of the
    time and looks broken."""
    seen = {}

    def fake_run(listener, now):
        seen['last_check'] = listener.state().get('last_check')
        return 0

    from taskhome.listeners import base
    monkeypatch.setattr(base, 'run', fake_run)
    state.listeners['nws'] = {'enabled': True, 'last_check': '2099-01-01T00:00:00Z'}
    client.post('/api/listeners/nws/poll')
    assert seen['last_check'] is None


def test_poll_now_on_a_disabled_listener_is_refused(client):
    state.listeners['nws'] = {'enabled': False}
    assert client.post('/api/listeners/nws/poll').status_code == 400


def test_poll_now_on_an_unknown_listener_is_404(client):
    assert client.post('/api/listeners/nope/poll').status_code == 404


def test_a_failed_poll_restores_the_watermark(client, monkeypatch):
    """Losing the watermark on a failed manual poll would replay the backlog
    on the next scheduled one."""
    from taskhome.listeners import base

    def boom(listener, now):
        raise RuntimeError('weather.gov down')

    monkeypatch.setattr(base, 'run', boom)
    state.listeners['nws'] = {'enabled': True, 'last_check': '2026-07-27T10:00:00Z'}
    assert client.post('/api/listeners/nws/poll').status_code == 502
    assert state.listeners['nws']['last_check'] == '2026-07-27T10:00:00Z'


def test_uids_survive_a_restart(tmp_path, monkeypatch):
    """They are random, so leaving them in memory only mints a different set
    on every start. A uid rendered into a page would 404 the moment the app
    restarted -- and the page would look completely normal until it did.
    """
    import json
    from taskhome import constants as c
    monkeypatch.setattr(c, 'DATA_DIR', str(tmp_path))
    for name, blank in (('config', '{}'), ('tasks', '[]'), ('listeners', '{}')):
        (tmp_path / f'{name}.json').write_text(blank)
        monkeypatch.setattr(c, f'{name.upper()}_FILE', str(tmp_path / f'{name}.json'))
    history = tmp_path / 'history.json'
    history.write_text(json.dumps([{'title': 'Bins', 'type': 'task'}]))
    monkeypatch.setattr(c, 'HISTORY_FILE', str(history))
    monkeypatch.setattr(storage, 'migrate_legacy_data_files', lambda: None)

    storage.load_data()
    first = state.history[0]['uid']
    assert first

    storage.load_data()
    assert state.history[0]['uid'] == first, 'a restart invalidated every reprint link'
    assert json.loads(history.read_text())[0]['uid'] == first, 'uid was not persisted'


def test_an_scf_reprint_formats_the_timestamp(client, monkeypatch):
    """History stores reported_at raw, as the API returned it, while the
    receipt shows it formatted."""
    from taskhome import receipt
    printed = []
    monkeypatch.setattr(printing, 'print_blocks', lambda b: printed.append(b) or True)
    state.history.append({
        'uid': 'scf1', 'type': 'scf', 'id': 42, 'category': 'Pothole',
        'address': 'Elm St', 'status': 'Open', 'description': 'Big one.',
        'reported_at': '2025-08-26T13:36:42Z', 'print_time': '2025-08-26T09:36:42'})
    client.post('/api/history/reprint/scf1')
    rendered = '\n'.join(receipt.render_text(printed[0]))
    assert '2025-08-26T13:36:42Z' not in rendered, 'printed a raw ISO timestamp'
    assert 'AM' in rendered or 'PM' in rendered
