"""The JSON API (P2-3), task actions (P2-2) and the CLI (P6-5)."""
import pytest

from taskhome import constants, create_app, printing, state, storage


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(state, 'config', {'theme': 'system', 'max_history': 500})
    monkeypatch.setattr(state, 'tasks', [])
    monkeypatch.setattr(state, 'history', [])
    monkeypatch.setattr(state, 'listeners', {})
    for name in ('save_tasks', 'save_config', 'save_history', 'save_listeners'):
        monkeypatch.setattr(storage, name, lambda: True)
    app = create_app(load=False, with_scheduler=False)
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


def make(client, **extra):
    payload = {'title': 'Bins', 'recurring': 'daily',
               'next_time': '2026-07-28T09:00', 'enabled': True}
    payload.update(extra)
    return client.post('/api/tasks', json=payload)


# --- the envelope -------------------------------------------------------------

def test_success_and_failure_share_one_shape(client):
    good = make(client).get_json()
    assert good['ok'] is True and 'data' in good
    bad = client.post('/api/tasks', json={'title': ''}).get_json()
    assert bad['ok'] is False and isinstance(bad['error'], str)


def test_errors_are_phrased_for_a_person(client):
    """These reach a toast, so 'invalid' is not good enough."""
    error = client.post('/api/tasks', json={'title': ''}).get_json()['error']
    assert 'Title' in error


# --- tasks --------------------------------------------------------------------

def test_task_crud_round_trip(client):
    created = make(client)
    assert created.status_code == 201
    tid = created.get_json()['data']['id']

    assert client.get(f'/api/tasks/{tid}').status_code == 200
    assert len(client.get('/api/tasks').get_json()['data']) == 1

    updated = client.put(f'/api/tasks/{tid}', json={
        'title': 'Recycling', 'recurring': 'weekly',
        'next_time': '2026-07-29T09:00', 'enabled': True})
    assert updated.get_json()['data']['title'] == 'Recycling'

    assert client.delete(f'/api/tasks/{tid}').status_code == 200
    assert client.get(f'/api/tasks/{tid}').status_code == 404


def test_patch_only_changes_what_it_names(client):
    """A PATCH that sets `enabled` must not blank the title -- the validator
    needs a whole task, so the existing values have to be the defaults."""
    tid = make(client, extra='Kerbside').get_json()['data']['id']
    patched = client.patch(f'/api/tasks/{tid}', json={'enabled': False}).get_json()['data']
    assert patched['enabled'] is False
    assert patched['title'] == 'Bins' and patched['extra'] == 'Kerbside'


def test_a_false_checkbox_value_is_respected(client):
    """An HTML checkbox is absent when unchecked, so the form code tests
    `'enabled' in form`. JSON sends {"enabled": false} -- key present -- and
    the naive test would read it as True."""
    tid = make(client).get_json()['data']['id']
    client.patch(f'/api/tasks/{tid}', json={'enabled': False})
    assert state.tasks[0]['enabled'] is False


def test_a_rejected_edit_leaves_the_task_untouched(client):
    tid = make(client).get_json()['data']['id']
    assert client.patch(f'/api/tasks/{tid}', json={'recurring': 'hourly'}).status_code == 400
    assert state.tasks[0]['recurring'] == 'daily'
    assert state.tasks[0]['title'] == 'Bins'


def test_custom_recurrence_still_needs_days(client):
    """The same rule as the form -- P0-2, a custom schedule with no days can
    never advance. One validator means it cannot be fixed in one place only."""
    assert client.post('/api/tasks', json={
        'title': 'x', 'recurring': 'custom', 'next_time': '2026-07-28T09:00'}
    ).status_code == 400


def test_the_api_and_the_form_agree(client):
    """Both go through forms.task_from_form. If they diverge, one of them is
    letting something invalid into the datastore."""
    from taskhome.web import forms
    for payload in ({'title': '', 'recurring': 'daily'},
                    {'title': 'x', 'recurring': 'nope'},
                    {'title': 'x', 'recurring': 'custom'}):
        api_status = client.post('/api/tasks', json=payload).status_code
        try:
            forms.task_from_form(forms.JsonForm(payload))
            form_ok = True
        except forms.ValidationError:
            form_ok = False
        assert (api_status == 201) == form_ok, payload


# --- print now and duplicate --------------------------------------------------

def test_print_now_does_not_advance_the_schedule(client, monkeypatch):
    """Printing one now is not the occurrence coming due; advancing next_time
    would silently skip the real reminder."""
    monkeypatch.setattr(printing, 'print_blocks', lambda b: True)
    tid = make(client).get_json()['data']['id']
    before = state.tasks[0]['next_time']
    assert client.post(f'/api/tasks/{tid}/print').status_code == 200
    assert state.tasks[0]['next_time'] == before


def test_print_now_reports_an_offline_printer(client, monkeypatch):
    monkeypatch.setattr(printing, 'print_blocks', lambda b: False)
    tid = make(client).get_json()['data']['id']
    assert client.post(f'/api/tasks/{tid}/print').status_code == 503


def test_a_duplicate_starts_paused(client):
    """Copying a task usually means editing it next, and a half-edited chore
    printing at 6am is a bad surprise."""
    tid = make(client).get_json()['data']['id']
    copy = client.post(f'/api/tasks/{tid}/duplicate').get_json()['data']
    assert copy['enabled'] is False
    assert copy['id'] != tid and 'copy' in copy['title']


def test_a_duplicate_does_not_inherit_a_schedule_error(client):
    tid = make(client).get_json()['data']['id']
    state.tasks[0]['schedule_error'] = 'stuck'
    copy = client.post(f'/api/tasks/{tid}/duplicate').get_json()['data']
    assert 'schedule_error' not in copy


# --- derived fields -----------------------------------------------------------

def test_last_printed_comes_from_history(client):
    """Derived, not stored: a copy on the task would be a second source of
    truth the print path has to keep in step."""
    tid = make(client).get_json()['data']['id']
    state.history.append({'type': 'task', 'id': tid, 'print_time': '2026-07-27T08:00:00'})
    assert client.get(f'/api/tasks/{tid}').get_json()['data']['last_printed'] \
        == '2026-07-27T08:00:00'


def test_custom_recurrence_gets_a_readable_label(client):
    tid = make(client, recurring='custom', days=[0, 2, 4]).get_json()['data']['id']
    assert client.get(f'/api/tasks/{tid}').get_json()['data']['recurrence_label'] \
        == 'Mon/Wed/Fri'


# --- config -------------------------------------------------------------------

def test_config_write_is_allow_listed(client):
    """`styles` has its own endpoints and its own validation; a blanket merge
    would let this endpoint corrupt it."""
    assert client.put('/api/config', json={'styles': {'task': 'x'}}).status_code == 400


@pytest.mark.parametrize('payload', [
    {'theme': 'neon'}, {'max_history': 'lots'}, {'max_history': -1},
    {'catchup': {'policy': 'nope'}}, {'catchup': {'max_prints': -5}},
    {'catchup': 'skip'}, {'catchup': {'unknown_key': 1}},
])
def test_bad_config_is_refused_not_silently_corrected(client, payload):
    """recurrence.get_catchup_config() deliberately degrades rather than
    raising, so the scheduler survives a bad value. An API must not: storing
    something different from what was sent is worse than refusing it."""
    assert client.put('/api/config', json=payload).status_code == 400


def test_a_valid_catchup_patch_merges(client):
    state.config['catchup'] = {'policy': 'skip', 'max_prints': 20}
    client.put('/api/config', json={'catchup': {'policy': 'print_once'}})
    assert state.config['catchup'] == {'policy': 'print_once', 'max_prints': 20}


# --- history and listeners ----------------------------------------------------

def test_history_uses_the_same_contract_as_the_page(client):
    """A client must not get a different answer from the same query."""
    state.history.extend([{'type': 'task', 'title': f't{i}',
                           'print_time': '2026-07-27T08:00:00'} for i in range(30)])
    data = client.get('/api/history?per_page=25&page=2').get_json()['data']
    assert data['page'] == 2 and data['total'] == 30 and len(data['records']) == 5


def test_history_filter_matches_the_page(client):
    state.history.extend([{'type': 'task', 'title': 'a'}, {'type': 'nws', 'category': 'b'}])
    assert len(client.get('/api/history?kind=nws').get_json()['data']['records']) == 1


def test_listeners_expose_their_schema(client):
    """So a client can render settings without hardcoding a listener."""
    data = client.get('/api/listeners').get_json()['data']
    assert data and all('schema' in l and 'config' in l for l in data)


def test_writing_a_listener_uses_its_own_validator(client):
    assert client.put('/api/listeners/nws', json={'interval': 999}).status_code == 400
    assert client.put('/api/listeners/nws', json={'interval': 5}).status_code == 200


def test_unknown_listener_is_404(client):
    assert client.get('/api/listeners/nope').status_code == 404


# --- CLI (P6-5) ---------------------------------------------------------------

def test_data_dir_flag_actually_repoints_the_store(tmp_path, monkeypatch):
    """Setting TASKHOME_DATA_DIR in main() is too late -- reaching the CLI
    imports the package, which has already resolved every path. The flag was
    silently ignored, which means writing to the real datastore while claiming
    to use a scratch one.
    """
    from taskhome import cli
    original = constants.DATA_DIR
    try:
        cli.build_parser()          # import check
        constants.set_data_dir(str(tmp_path))
        assert constants.DATA_DIR == str(tmp_path)
        assert constants.TASKS_FILE.startswith(str(tmp_path))
        assert constants.HISTORY_FILE.startswith(str(tmp_path))
        assert constants.DATA_DIR_IS_DEFAULT is False
    finally:
        constants.set_data_dir(original)


def test_version_flag(capsys):
    from taskhome import cli
    assert cli.main(['--version']) == 0
    assert constants.VERSION in capsys.readouterr().out


def test_the_cli_parser_covers_the_documented_flags():
    from taskhome import cli
    args = cli.build_parser().parse_args(
        ['--host', '127.0.0.1', '--port', '5050', '--data-dir', '/tmp/x', '--no-scheduler'])
    assert args.host == '127.0.0.1' and args.port == 5050
    assert args.data_dir == '/tmp/x' and args.no_scheduler is True
