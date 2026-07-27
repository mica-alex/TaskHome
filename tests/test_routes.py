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


def test_index_shows_printer_status(client):
    assert 'Not connected' in client.get('/').get_data(as_text=True)
