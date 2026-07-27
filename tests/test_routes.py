"""Route smoke tests — every page must render for every task state.

Template errors don't show up in the logic tests, and a Jinja failure on a
task with an unusual status would take down the whole page.
"""
import pytest

import app as taskhome


@pytest.fixture
def client(clean_state):
    taskhome.app.config['TESTING'] = True
    with taskhome.app.test_client() as c:
        yield c


@pytest.mark.parametrize('route', ['/', '/task_page', '/settings', '/listener'])
def test_pages_render_when_empty(client, route):
    assert client.get(route).status_code == 200


@pytest.mark.parametrize('route', ['/', '/task_page'])
def test_pages_render_every_task_state(client, clean_state, make_task, route):
    """All four states must render, including the ones only the scheduler
    creates (missed, schedule_error)."""
    taskhome.tasks.extend([
        make_task('2026-04-01T09:00:00', 'daily'),
        make_task('2026-04-01T09:00:00', 'daily', enabled=False),
        make_task('2026-03-01T09:00:00', 'none',
                  enabled=False, missed=True, last_missed_at='2026-03-01T09:00:00'),
        make_task('2026-03-01T09:00:00', 'custom', days=[],
                  enabled=False, schedule_error='recurrence did not advance'),
        make_task('2026-04-01T09:00:00', 'daily', missed_count=3,
                  last_missed_at='2026-03-01T09:00:00'),
    ])
    body = client.get(route).get_data(as_text=True)
    assert 'Missed' in body
    assert 'Disabled' in body
    assert 'Error' in body


def test_disabled_tasks_are_visible(client, clean_state, make_task):
    """P0-13: a disabled task used to disappear entirely, making a missed
    one-off unrecoverable from the UI."""
    taskhome.tasks.append(
        make_task('2026-03-01T09:00:00', 'none', title='Vanishing Act', enabled=False))
    assert 'Vanishing Act' in client.get('/task_page').get_data(as_text=True)


def test_schedule_error_detail_is_shown(client, clean_state, make_task):
    taskhome.tasks.append(make_task(
        '2026-03-01T09:00:00', 'custom', days=[], enabled=False,
        schedule_error='recurrence did not advance from 2026-03-01T09:00:00'))
    assert 'did not advance' in client.get('/task_page').get_data(as_text=True)


def test_task_titles_are_escaped(client, clean_state, make_task):
    taskhome.tasks.append(make_task(
        '2026-04-01T09:00:00', 'daily', title='<script>alert(1)</script>'))
    body = client.get('/task_page').get_data(as_text=True)
    assert '<script>alert(1)</script>' not in body
    assert '&lt;script&gt;' in body


@pytest.mark.parametrize('route', ['/test_print', '/test_scf_print'])
def test_test_print_disconnected_is_not_a_success(client, clean_state, monkeypatch, route):
    """The front end trusts the status code, so 'Printer not connected' must
    not come back as 200 or it renders as a success toast."""
    monkeypatch.setattr(taskhome, 'is_printer_connected', lambda: False)
    assert client.post(route).status_code == 503


def test_test_print_reports_failure_honestly(client, clean_state, monkeypatch):
    """P0-10: this used to return 'Test print successful!' even when nothing
    printed, because print_task swallowed its own exceptions."""
    monkeypatch.setattr(taskhome, 'is_printer_connected', lambda: True)
    monkeypatch.setattr(taskhome, 'print_task', lambda task: False)
    resp = client.post('/test_print')
    assert resp.status_code == 500
    assert 'failed' in resp.get_data(as_text=True)


def test_test_print_reports_success(client, clean_state, monkeypatch):
    monkeypatch.setattr(taskhome, 'is_printer_connected', lambda: True)
    monkeypatch.setattr(taskhome, 'print_task', lambda task: True)
    resp = client.post('/test_print')
    assert resp.status_code == 200
    assert 'successful' in resp.get_data(as_text=True)


def test_test_scf_print_reports_failure_honestly(client, clean_state, monkeypatch):
    monkeypatch.setattr(taskhome, 'is_printer_connected', lambda: True)
    monkeypatch.setattr(taskhome, 'print_scf_issue', lambda issue: False)
    assert client.post('/test_scf_print').status_code == 500


def test_index_shows_printer_connected(client, clean_state):
    clean_state.online = True
    assert 'Connected' in client.get('/').get_data(as_text=True)


def test_index_shows_printer_disconnected(client, clean_state):
    clean_state.online = False
    assert 'Not connected' in client.get('/').get_data(as_text=True)
