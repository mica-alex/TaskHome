"""Checklists (P5-2 #11), bin day (P5-2 #6) and print stats (P6-4)."""
from datetime import date, datetime, timedelta

import pytest

from taskhome import constants, create_app, lists, printing, receipt, state, storage
from taskhome.listeners import binday


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(state, 'listeners', {})
    monkeypatch.setattr(state, 'history', [])
    monkeypatch.setattr(state, 'config', {'max_history': 500})
    monkeypatch.setattr(storage, 'save_listeners', lambda: True)
    monkeypatch.setattr(storage, 'save_history', lambda: True)
    yield tmp_path


@pytest.fixture
def client(store):
    app = create_app(load=False, with_scheduler=False)
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


# --- checklists ---------------------------------------------------------------

def test_a_list_round_trips(store):
    entry = lists.create_list('Groceries')
    lists.add_item(entry['id'], 'Milk')
    assert lists.get_list(entry['id'])['items'][0]['text'] == 'Milk'


def test_pasting_several_lines_adds_several_items(store):
    """Adding fifteen things one at a time is the fastest way to make someone
    stop using this."""
    entry = lists.create_list('Groceries')
    added = lists.add_item(entry['id'], 'Milk\nEggs\n\n  Bread  \n')
    assert [i['text'] for i in added] == ['Milk', 'Eggs', 'Bread']


def test_items_keep_the_order_they_were_added(store):
    """Usually the order of the aisles. Re-sorting is actively unhelpful."""
    entry = lists.create_list('Groceries')
    lists.add_item(entry['id'], 'Zucchini\nApples\nMilk')
    assert [i['text'] for i in lists.get_list(entry['id'])['items']] == \
        ['Zucchini', 'Apples', 'Milk']


def test_printing_does_not_clear_the_list(store, monkeypatch):
    """A shopping list is mostly the same things every week."""
    monkeypatch.setattr(printing, 'print_blocks', lambda b: True)
    monkeypatch.setattr(printing, 'record_history', lambda r: None)
    entry = lists.create_list('Groceries')
    lists.add_item(entry['id'], 'Milk\nEggs')
    lists.print_list(entry['id'])
    assert len(lists.get_list(entry['id'])['items']) == 2


def test_ticked_items_are_left_off_the_paper(store):
    entry = lists.create_list('Groceries')
    lists.add_item(entry['id'], 'Milk\nEggs')
    item = lists.get_list(entry['id'])['items'][0]
    lists.set_item(entry['id'], item['id'], done=True)
    rendered = '\n'.join(receipt.render_text(
        lists.list_blocks(lists.get_list(entry['id']))))
    assert 'Eggs' in rendered and 'Milk' not in rendered


def test_the_receipt_has_tickable_boxes(store):
    entry = lists.create_list('Groceries')
    lists.add_item(entry['id'], 'Milk')
    rendered = '\n'.join(receipt.render_text(
        lists.list_blocks(lists.get_list(entry['id']))))
    assert '[ ] Milk' in rendered


def test_a_failed_list_print_is_queued(store, monkeypatch):
    """Someone pressed a button and is waiting for paper, and there is no
    schedule to retry from."""
    from taskhome import queue
    monkeypatch.setattr(printing, 'print_blocks', lambda b: False)
    entry = lists.create_list('Groceries')
    lists.add_item(entry['id'], 'Milk')
    assert lists.print_list(entry['id']) is False
    assert len(queue.load_queue()) == 1


def test_clear_ticked_removes_only_ticked(store):
    entry = lists.create_list('Groceries')
    lists.add_item(entry['id'], 'Milk\nEggs')
    item = lists.get_list(entry['id'])['items'][0]
    lists.set_item(entry['id'], item['id'], done=True)
    assert lists.clear_done(entry['id']) == 1
    assert len(lists.get_list(entry['id'])['items']) == 1


@pytest.mark.parametrize('bad', ['', '   '])
def test_a_nameless_list_is_refused(store, bad):
    with pytest.raises(ValueError):
        lists.create_list(bad)


def test_the_api_covers_the_page(client, store):
    created = client.post('/api/lists', json={'name': 'Packing'})
    assert created.status_code == 201
    list_id = created.get_json()['data']['id']
    assert client.post(f'/api/lists/{list_id}/items',
                       json={'text': 'Charger'}).status_code == 201
    item_id = lists.get_list(list_id)['items'][0]['id']
    assert client.patch(f'/api/lists/{list_id}/items/{item_id}',
                        json={'done': True}).status_code == 200
    assert client.post(f'/api/lists/{list_id}/clear').get_json()['data']['removed'] == 1
    assert client.delete(f'/api/lists/{list_id}').status_code == 200


def test_an_unknown_list_is_404(client):
    assert client.post('/api/lists/nope/items', json={'text': 'x'}).status_code == 404


def test_checklists_appear_in_the_history_filter():
    """A list print is not a listener, so it needs adding alongside tasks."""
    from taskhome.web import pagination
    assert 'list' in {key for key, _ in pagination.history_kinds()}


# --- bin day ------------------------------------------------------------------

def test_the_next_collection_is_found_from_any_day():
    config = dict(binday.listener.config(), collection_day='Tuesday')
    assert binday.listener.next_collection(config, date(2026, 7, 27)) == date(2026, 7, 28)
    assert binday.listener.next_collection(config, date(2026, 7, 28)) == date(2026, 7, 28)
    assert binday.listener.next_collection(config, date(2026, 7, 29)) == date(2026, 8, 4)


def test_alternating_bins_follow_the_anchor():
    """Anchored to a date the user supplies, and counted in whole weeks. ISO
    week parity flips at new year and would silently invert the schedule."""
    config = dict(binday.listener.config(), bins=['Trash'],
                  alternating_bins=['Recycling'], alternating_anchor='2026-07-28')
    assert binday.listener.bins_for(config, date(2026, 7, 28)) == ['Trash', 'Recycling']
    assert binday.listener.bins_for(config, date(2026, 8, 4)) == ['Trash']
    assert binday.listener.bins_for(config, date(2026, 8, 11)) == ['Trash', 'Recycling']


def test_the_anchor_works_backwards_too():
    config = dict(binday.listener.config(), bins=['Trash'],
                  alternating_bins=['Recycling'], alternating_anchor='2026-07-28')
    assert binday.listener.bins_for(config, date(2026, 7, 14)) == ['Trash', 'Recycling']
    assert binday.listener.bins_for(config, date(2026, 7, 21)) == ['Trash']


def test_alternating_bins_without_an_anchor_are_left_off():
    """Listing them every time is wrong half the time; leaving them off is
    wrong half the time and obviously wrong, which is better."""
    config = dict(binday.listener.config(), bins=['Trash'],
                  alternating_bins=['Recycling'], alternating_anchor='')
    assert binday.listener.bins_for(config, date(2026, 7, 28)) == ['Trash']


def test_a_malformed_anchor_does_not_raise():
    config = dict(binday.listener.config(), bins=['Trash'],
                  alternating_bins=['Recycling'], alternating_anchor='last tuesday')
    assert binday.listener.bins_for(config, date(2026, 7, 28)) == ['Trash']


def test_the_reminder_is_the_evening_before(store):
    """A receipt on collection morning arrives after the lorry."""
    spec = next(f for f in binday.BinDayListener.CONFIG_SCHEMA
                if f['key'] == 'remind_days_before')
    assert spec['default'] == 1


def test_the_reminder_fires_once_per_collection(store, monkeypatch):
    config = dict(binday.listener.config(), collection_day='Tuesday',
                  bins=['Trash'], remind_at='00:00', remind_days_before=0)
    monkeypatch.setattr(binday.listener, 'next_collection',
                        lambda c, today: today)
    assert len(binday.listener.poll(config, None)) == 1
    assert binday.listener.poll(config, None) == []


def test_nothing_prints_when_no_bins_are_configured(store, monkeypatch):
    config = dict(binday.listener.config(), bins=[], alternating_bins=[],
                  remind_at='00:00', remind_days_before=0)
    monkeypatch.setattr(binday.listener, 'next_collection', lambda c, today: today)
    assert binday.listener.poll(config, None) == []


# --- print stats --------------------------------------------------------------

def test_stats_are_derived_from_history_not_counters(store):
    """A counter would be a second source of truth that every print path has
    to remember to increment."""
    from taskhome.web import health
    today = datetime.now()
    state.history.extend([
        {'type': 'task', 'print_time': today.isoformat()},
        {'type': 'task', 'print_time': (today - timedelta(days=1)).isoformat()},
        {'type': 'scf', 'print_time': today.isoformat()},
    ])
    stats = health.print_stats(days=14)
    assert stats['series'][-1] == 2 and stats['series'][-2] == 1
    assert stats['by_kind'] == {'task': 2, 'scf': 1}
    assert stats['total_window'] == 3


def test_stats_survive_malformed_timestamps(store):
    from taskhome.web import health
    state.history.extend([
        {'type': 'task', 'print_time': 'not a date'},
        {'type': 'task'},
    ])
    assert health.print_stats()['total_window'] == 0


def test_stats_say_when_the_window_is_capped(store):
    """History is capped, so 'all time' is not all time -- and a chart that
    quietly under-reports is worse than none."""
    from taskhome.web import health
    state.config['max_history'] = 2
    state.history.extend([{'type': 'task', 'print_time': datetime.now().isoformat()}] * 2)
    assert health.print_stats()['window_limited'] is True


def test_the_stats_endpoint_works(client, store):
    response = client.get('/api/stats')
    assert response.status_code == 200
    assert len(response.get_json()['data']['series']) == 14
