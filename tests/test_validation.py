"""Form input validation (MASTER_PLAN P0-9).

Unvalidated form data used to reach the datastore directly: int() on arbitrary
strings raised 500s, a missing field raised KeyError, and a custom recurrence
with no weekdays produced a task that could never be scheduled (P0-2).
"""
import pytest
from werkzeug.datastructures import MultiDict

import taskhome


@pytest.fixture
def client(app, clean_state):
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


def form(**kwargs):
    """Build a form. List values become repeated fields, as a browser sends."""
    md = MultiDict()
    for key, value in kwargs.items():
        if isinstance(value, (list, tuple)):
            for item in value:
                md.add(key, item)
        else:
            md.add(key, value)
    return md


# --- task_from_form -----------------------------------------------------------

def test_valid_task_is_built(clean_state):
    task = taskhome.web.forms.task_from_form(
        form(title='Water plants', next_time='2026-03-05T09:00',
             recurring='daily', enabled='on'))
    assert task['title'] == 'Water plants'
    assert task['next_time'] == '2026-03-05T09:00:00'
    assert task['recurring'] == 'daily'
    assert task['enabled'] is True
    assert 'id' in task


@pytest.mark.parametrize('title', ['', '   ', None])
def test_blank_title_is_rejected(clean_state, title):
    with pytest.raises(taskhome.web.forms.ValidationError, match='Title'):
        taskhome.web.forms.task_from_form(form(title=title or '', next_time='2026-03-05T09:00',
                                     recurring='daily'))


def test_unknown_recurrence_is_rejected(clean_state):
    with pytest.raises(taskhome.web.forms.ValidationError, match='recurrence'):
        taskhome.web.forms.task_from_form(form(title='X', next_time='2026-03-05T09:00',
                                     recurring='hourly'))


def test_custom_without_weekdays_is_rejected(clean_state):
    """Accepting this produced a task whose schedule could never advance."""
    with pytest.raises(taskhome.web.forms.ValidationError, match='at least one weekday'):
        taskhome.web.forms.task_from_form(form(title='X', next_time='2026-03-05T09:00',
                                     recurring='custom'))


@pytest.mark.parametrize('days', [['9'], ['-1'], ['abc'], ['1', '99']])
def test_invalid_weekdays_are_rejected(clean_state, days):
    with pytest.raises(taskhome.web.forms.ValidationError):
        taskhome.web.forms.task_from_form(form(title='X', next_time='2026-03-05T09:00',
                                     recurring='custom', days=days))


def test_weekdays_are_deduped_and_sorted(clean_state):
    task = taskhome.web.forms.task_from_form(
        form(title='X', next_time='2026-03-05T09:00', recurring='custom',
             days=['3', '1', '1']))
    assert task['days'] == [1, 3]


def test_unparseable_next_time_is_rejected(clean_state):
    with pytest.raises(taskhome.web.forms.ValidationError, match='not a valid date'):
        taskhome.web.forms.task_from_form(form(title='X', next_time='whenever',
                                     recurring='daily'))


def test_next_time_is_canonicalised(clean_state):
    """The old code appended ':00' blindly, yielding '...T21:00:00:00' when
    the browser already sent seconds. Re-serialising from the parsed value
    makes the stored form canonical regardless of input."""
    task = taskhome.web.forms.task_from_form(
        form(title='X', next_time='2026-03-05T09:00:00', recurring='daily'))
    assert task['next_time'] == '2026-03-05T09:00:00'


def test_days_dropped_when_recurrence_changes_away_from_custom(clean_state):
    existing = {'id': 'x', 'title': 'X', 'next_time': '2026-03-05T09:00:00',
                'recurring': 'custom', 'days': [1, 2], 'enabled': True}
    updated = taskhome.web.forms.task_from_form(
        form(title='X', next_time='2026-03-05T09:00', recurring='daily'), existing=existing)
    assert 'days' not in updated


def test_editing_clears_prior_schedule_error(clean_state):
    existing = {'id': 'x', 'title': 'X', 'next_time': '2026-03-05T09:00:00',
                'recurring': 'custom', 'days': [], 'enabled': False,
                'schedule_error': 'did not advance', 'missed': True}
    updated = taskhome.web.forms.task_from_form(
        form(title='X', next_time='2026-03-05T09:00', recurring='daily', enabled='on'),
        existing=existing)
    assert 'schedule_error' not in updated
    assert 'missed' not in updated


def test_omitted_enabled_means_disabled(clean_state):
    task = taskhome.web.forms.task_from_form(
        form(title='X', next_time='2026-03-05T09:00', recurring='daily'))
    assert task['enabled'] is False


# --- routes -------------------------------------------------------------------

def test_add_task_rejects_bad_input_without_500(client, clean_state):
    resp = client.post('/add_task', data={'title': '', 'next_time': '2026-03-05T09:00',
                                          'recurring': 'daily'})
    assert resp.status_code == 400
    assert taskhome.state.tasks == []


def test_add_task_accepts_good_input(client, clean_state):
    resp = client.post('/add_task', data={'title': 'Feed cat',
                                          'next_time': '2026-03-05T09:00',
                                          'recurring': 'daily', 'enabled': 'on'})
    assert resp.status_code == 302
    assert len(taskhome.state.tasks) == 1


def test_add_task_missing_fields_entirely(client, clean_state):
    """A form with no fields at all used to raise KeyError -> 500."""
    assert client.post('/add_task', data={}).status_code == 400


def test_rejected_edit_leaves_the_task_untouched(client, clean_state, make_task):
    task = make_task('2026-03-05T09:00:00', 'daily', title='Original')
    taskhome.state.tasks.append(task)

    resp = client.post(f"/edit_task/{task['id']}",
                       data={'title': '', 'next_time': '2026-03-06T09:00',
                             'recurring': 'daily'})

    assert resp.status_code == 400
    assert task['title'] == 'Original'
    assert task['next_time'] == '2026-03-05T09:00:00'


def test_successful_edit_applies(client, clean_state, make_task):
    task = make_task('2026-03-05T09:00:00', 'daily', title='Original')
    taskhome.state.tasks.append(task)

    resp = client.post(f"/edit_task/{task['id']}",
                       data={'title': 'Renamed', 'next_time': '2026-03-06T10:30',
                             'recurring': 'weekly', 'enabled': 'on'})

    assert resp.status_code == 302
    assert task['title'] == 'Renamed'
    assert task['next_time'] == '2026-03-06T10:30:00'
    assert task['recurring'] == 'weekly'


def test_delete_missing_task_is_404_not_500(client, clean_state):
    assert client.post('/delete_task', data={'id': 'nope'}).status_code == 404


def test_delete_without_id_is_rejected(client, clean_state):
    assert client.post('/delete_task', data={}).status_code == 400


def test_delete_removes_the_task(client, clean_state, make_task):
    task = make_task('2026-03-05T09:00:00', 'daily')
    taskhome.state.tasks.append(task)
    assert client.post('/delete_task', data={'id': task['id']}).status_code == 302
    assert taskhome.state.tasks == []


# --- settings -----------------------------------------------------------------

@pytest.mark.parametrize('value', ['abc', '', '-5', '999999999'])
def test_settings_rejects_bad_max_history(client, clean_state, value):
    resp = client.post('/settings', data={'max_history': value,
                                          'hostname': 'localhost', 'theme': 'system'})
    assert resp.status_code == 400


def test_settings_rejects_unknown_theme(client, clean_state):
    resp = client.post('/settings', data={'max_history': '100',
                                          'hostname': 'localhost', 'theme': 'neon'})
    assert resp.status_code == 400


def test_settings_accepts_valid_input(client, clean_state):
    resp = client.post('/settings', data={'max_history': '50',
                                          'hostname': 'printer.local', 'theme': 'dark'})
    assert resp.status_code == 302
    assert taskhome.state.config['max_history'] == 50
    assert taskhome.state.config['hostname'] == 'printer.local'


def test_blank_hostname_falls_back_to_default(client, clean_state):
    client.post('/settings', data={'max_history': '50', 'hostname': '  ',
                                   'theme': 'system'})
    assert taskhome.state.config['hostname'] == taskhome.constants.DEFAULT_CONFIG['hostname']


# --- listener -----------------------------------------------------------------

def test_listener_post_on_fresh_install(client, clean_state):
    """listeners['scf'] was indexed directly to preserve last_check, raising
    KeyError before the listener had ever been configured."""
    assert taskhome.state.listeners == {}
    resp = client.post('/listener', data={'request_types': '6632', 'interval': '10'})
    assert resp.status_code == 302
    assert taskhome.state.listeners['scf']['interval'] == 10


def test_listener_preserves_last_check(client, clean_state):
    taskhome.state.listeners['scf'] = {'enabled': True, 'request_types': '1',
                                 'interval': 5, 'last_check': '2026-03-05T09:00:00Z'}
    client.post('/listener', data={'request_types': '6632', 'interval': '10',
                                   'enabled': 'on'})
    assert taskhome.state.listeners['scf']['last_check'] == '2026-03-05T09:00:00Z'


@pytest.mark.parametrize('interval', ['abc', '0', '-1', '99999', ''])
def test_listener_rejects_bad_interval(client, clean_state, interval):
    assert client.post('/listener', data={'request_types': '6632',
                                          'interval': interval}).status_code == 400


def test_listener_normalises_request_types(client, clean_state):
    client.post('/listener', data={'request_types': ' 6632 , ,6634 , ',
                                   'interval': '10'})
    assert taskhome.state.listeners['scf']['request_types'] == '6632,6634'
