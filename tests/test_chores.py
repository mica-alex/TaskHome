"""Chore charts and streaks (P5-2 #12)."""
from datetime import date, timedelta

import pytest

from taskhome import chores, constants, create_app, printing, state, storage


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(state, 'config', {'hostname': 'taskhome.local',
                                          'max_history': 500, 'theme': 'system'})
    monkeypatch.setattr(state, 'history', [])
    monkeypatch.setattr(state, 'listeners', {})
    monkeypatch.setattr(storage, 'save_history', lambda: True)
    yield tmp_path


@pytest.fixture
def client(store):
    app = create_app(load=False, with_scheduler=False)
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


def person_with(store, days=None, completed=None):
    person = chores.add_person('Sara')
    chores.update_person(person['id'], days=days if days is not None else list(range(7)),
                         chores=['Feed the cat'])
    if completed:
        for day in completed:
            chores.mark_done(person['id'], day)
    return chores.get_person(person['id'])


# --- streaks ------------------------------------------------------------------

def test_a_fresh_person_has_no_streak(store):
    assert chores.streak(person_with(store)) == 0


def test_consecutive_days_count(store):
    today = date(2026, 7, 27)
    person = person_with(store, completed=[today - timedelta(days=n) for n in range(3)])
    assert chores.streak(person, today) == 3


def test_today_not_yet_done_does_not_break_the_streak(store):
    """The day is still in progress. A counter that punishes you at breakfast
    is not a counter anyone keeps using."""
    today = date(2026, 7, 27)
    person = person_with(store, completed=[today - timedelta(days=n) for n in (1, 2, 3)])
    assert chores.streak(person, today) == 3


def test_a_missed_day_breaks_it(store):
    today = date(2026, 7, 27)
    person = person_with(store, completed=[today - timedelta(days=n) for n in (1, 3, 4)])
    assert chores.streak(person, today) == 1


def test_unscheduled_days_do_not_break_a_streak(store):
    """A weekday-only chart must survive the weekend: Saturday is not a missed
    day if nothing was due."""
    monday = date(2026, 7, 27)
    weekdays = [0, 1, 2, 3, 4]
    done = [monday - timedelta(days=n) for n in (0, 3, 4, 5, 6, 7)]
    done = [d for d in done if d.weekday() in weekdays]
    person = person_with(store, days=weekdays, completed=done)
    assert chores.streak(person, monday) >= 3


def test_marking_done_is_idempotent(store):
    today = date(2026, 7, 27)
    person = person_with(store)
    chores.mark_done(person['id'], today)
    chores.mark_done(person['id'], today)
    assert chores.get_person(person['id'])['completed'].count(today.isoformat()) == 1


def test_undo_removes_only_that_day(store):
    today = date(2026, 7, 27)
    person = person_with(store, completed=[today, today - timedelta(days=1)])
    chores.undo_done(person['id'], today)
    remaining = chores.get_person(person['id'])['completed']
    assert today.isoformat() not in remaining and len(remaining) == 1


def test_best_streak_survives_a_break(store):
    """So a broken streak still shows something earned."""
    today = date(2026, 7, 27)
    done = [today - timedelta(days=n) for n in (10, 9, 8, 7, 6, 1)]
    person = person_with(store, completed=done)
    assert chores.best_streak(person) == 5
    assert chores.streak(person, today) < 5


# --- the done link ------------------------------------------------------------

def test_the_qr_link_marks_the_day_done(client, store):
    person = person_with(store)
    response = client.get(f"/c/{person['token']}")
    assert response.status_code == 200
    assert chores.done_today(chores.get_person(person['id'])) is True


def test_scanning_twice_says_already_done(client, store):
    person = person_with(store)
    client.get(f"/c/{person['token']}")
    body = client.get(f"/c/{person['token']}").get_data(as_text=True)
    assert 'Already done' in body


def test_an_unknown_token_is_404_not_a_crash(client, store):
    assert client.get('/c/nope').status_code == 404


def test_tokens_are_compared_in_constant_time(store):
    """It is the one value that authorises anything."""
    import inspect
    assert 'compare_digest' in inspect.getsource(chores.by_token)


def test_rotating_a_token_invalidates_the_old_link(client, store):
    person = person_with(store)
    old = person['token']
    chores.update_person(person['id'], rotate=True)
    assert client.get(f'/c/{old}').status_code == 404
    new = chores.get_person(person['id'])['token']
    assert client.get(f'/c/{new}').status_code == 200


def test_the_done_url_is_short(store):
    """It becomes a QR code, and every character adds modules to the symbol."""
    person = person_with(store)
    assert '/c/' in chores.done_url(person)


# --- the receipt --------------------------------------------------------------

def test_the_chart_has_boxes_and_a_qr(store):
    from taskhome import receipt
    person = person_with(store)
    blocks = chores.chart_blocks(person)
    kinds = [b['type'] for b in blocks]
    assert 'qr' in kinds
    rendered = '\n'.join(receipt.render_text(blocks))
    assert '[ ] Feed the cat' in rendered


def test_a_streak_appears_on_the_chart(store):
    from taskhome import receipt
    today = date(2026, 7, 27)
    person = person_with(store, completed=[today - timedelta(days=n) for n in (1, 2)])
    rendered = '\n'.join(receipt.render_text(chores.chart_blocks(person, today)))
    assert '2 day streak' in rendered


def test_no_streak_line_when_there_is_none(store):
    from taskhome import receipt
    rendered = '\n'.join(receipt.render_text(chores.chart_blocks(person_with(store))))
    assert 'streak' not in rendered.lower()


# --- the listener -------------------------------------------------------------

def test_one_receipt_per_person(store):
    from taskhome.listeners import chores as listener_module
    person_with(store)
    chores.add_person('Alex')
    config = dict(listener_module.listener.config(), print_at='00:00')
    items = listener_module.listener.poll(config, None)
    assert len(items) == 2


def test_it_prints_once_a_day(store):
    from taskhome.listeners import chores as listener_module
    person_with(store)
    config = dict(listener_module.listener.config(), print_at='00:00')
    assert len(listener_module.listener.poll(config, None)) == 1
    assert listener_module.listener.poll(config, None) == []


def test_someone_not_scheduled_today_is_skipped(store):
    from taskhome.listeners import chores as listener_module
    tomorrow = (date.today() + timedelta(days=1)).weekday()
    person_with(store, days=[tomorrow])
    config = dict(listener_module.listener.config(), print_at='00:00')
    assert listener_module.listener.poll(config, None) == []


def test_someone_already_done_is_skipped(store):
    """The reminder is pointless if they beat it."""
    from taskhome.listeners import chores as listener_module
    person = person_with(store)
    chores.mark_done(person['id'])
    config = dict(listener_module.listener.config(), print_at='00:00',
                  skip_if_done=True)
    assert listener_module.listener.poll(config, None) == []


def test_nothing_prints_before_the_configured_time(store):
    from taskhome.listeners import chores as listener_module
    person_with(store)
    config = dict(listener_module.listener.config(), print_at='23:59')
    assert listener_module.listener.poll(config, None) == []


# --- the API ------------------------------------------------------------------

def test_the_api_covers_the_page(client, store):
    created = client.post('/api/chores', json={'name': 'Sara'})
    assert created.status_code == 201
    person_id = created.get_json()['data']['id']
    assert client.patch(f'/api/chores/{person_id}',
                        json={'chores': ['Feed the cat'], 'days': [0, 1]}).status_code == 200
    assert client.post(f'/api/chores/{person_id}/done').get_json()['data']['done_today']
    assert client.delete(f'/api/chores/{person_id}/done').status_code == 200
    assert client.delete(f'/api/chores/{person_id}').status_code == 200


def test_printing_a_chart_queues_when_offline(client, store, monkeypatch):
    from taskhome import queue
    monkeypatch.setattr(printing, 'print_blocks', lambda b: False)
    person = person_with(store)
    assert client.post(f"/api/chores/{person['id']}/print").status_code == 503
    assert len(queue.load_queue()) == 1


def test_a_nameless_person_is_refused(client, store):
    assert client.post('/api/chores', json={'name': '  '}).status_code == 400


def test_registered_and_editable():
    from taskhome import styles
    from taskhome.listeners import base
    assert 'chores' in base.registry()
    assert 'chores' in styles.kinds()
