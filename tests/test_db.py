"""The SQLite backend and its migration (P1-2)."""
import json

import pytest

from taskhome import constants, db, state, storage


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, 'APP_ROOT', str(tmp_path / 'repo'))
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    for name in ('CONFIG', 'TASKS', 'HISTORY', 'LISTENERS'):
        monkeypatch.setattr(constants, f'{name}_FILE',
                            str(tmp_path / f'{name.lower()}.json'))
    monkeypatch.setattr(state, 'tasks', [])
    monkeypatch.setattr(state, 'history', [])
    monkeypatch.setattr(state, 'listeners', {})
    monkeypatch.setattr(state, 'config', dict(constants.DEFAULT_CONFIG))
    db.forget()
    state.load_failed.clear()
    yield tmp_path
    db.forget()
    state.load_failed.clear()


def test_sqlite_needs_no_installation():
    """The whole reason this is acceptable on an appliance."""
    import sqlite3
    assert sqlite3.sqlite_version_info >= (3, 24)   # ON CONFLICT support


def test_wal_is_enabled(store):
    conn = db.connect()
    mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
    assert mode.lower() == 'wal', 'readers would block the writer'


def test_tasks_round_trip_and_keep_order(store):
    tasks = [{'id': str(n), 'title': f'Task {n}'} for n in range(5)]
    db.set_tasks(tasks)
    assert [t['title'] for t in db.get_tasks()] == [t['title'] for t in tasks]


def test_history_comes_back_newest_first(store):
    db.set_history([
        {'uid': 'a', 'type': 'task', 'print_time': '2026-07-01T09:00:00'},
        {'uid': 'b', 'type': 'task', 'print_time': '2026-07-03T09:00:00'},
        {'uid': 'c', 'type': 'task', 'print_time': '2026-07-02T09:00:00'},
    ])
    assert [r['uid'] for r in db.get_history()] == ['b', 'c', 'a']


def test_appending_history_does_not_rewrite_everything(store):
    """The reason history has its own table."""
    db.set_history([{'uid': str(n), 'type': 'task',
                     'print_time': f'2026-07-{n + 1:02d}T09:00:00'} for n in range(5)])
    db.add_history({'uid': 'new', 'type': 'task', 'print_time': '2026-08-01T09:00:00'})
    assert db.count_history() == 6
    assert db.get_history()[0]['uid'] == 'new'


def test_the_history_cap_is_enforced_on_append(store):
    for n in range(10):
        db.add_history({'uid': str(n), 'type': 'task',
                        'print_time': f'2026-07-{n + 1:02d}T09:00:00'}, cap=5)
    assert db.count_history() == 5


def test_kv_stores_round_trip(store):
    db.set_kv('config', {'hostname': 'printer.local', 'nested': {'a': [1, 2]}})
    assert db.get_kv('config')['nested']['a'] == [1, 2]


def test_counting_by_kind(store):
    db.set_history([{'uid': 'a', 'type': 'task', 'print_time': '2026-07-01T09:00:00'},
                    {'uid': 'b', 'type': 'scf', 'print_time': '2026-07-02T09:00:00'}])
    assert db.count_history('scf') == 1


# --- migration ----------------------------------------------------------------

def test_it_finds_json_in_the_data_directory(store):
    (store / 'tasks.json').write_text('[]')
    assert db.find_json_source() == str(store)


def test_it_finds_json_in_the_old_repo_root(store):
    """The pre-P1-9 layout, from when the checkout was the working directory."""
    root = store / 'repo'
    root.mkdir(exist_ok=True)
    (root / 'tasks.json').write_text('[]')
    assert db.find_json_source() == str(root)


def test_migration_imports_every_store(store):
    (store / 'tasks.json').write_text(json.dumps([{'id': 'a', 'title': 'Bins'}]))
    (store / 'config.json').write_text(json.dumps({'hostname': 'x'}))
    (store / 'history.json').write_text(json.dumps(
        [{'uid': 'h', 'type': 'task', 'print_time': '2026-07-01T09:00:00'}]))
    (store / 'lists.json').write_text(json.dumps([{'id': 'l', 'name': 'Shop'}]))

    report = db.migrate_from_json(str(store))
    assert report['imported']['tasks'] == 1
    assert db.get_tasks()[0]['title'] == 'Bins'
    assert db.get_kv('config')['hostname'] == 'x'
    assert db.get_kv('lists')[0]['name'] == 'Shop'
    assert db.count_history() == 1


def test_originals_are_renamed_never_deleted(store):
    """A bad migration has to be recoverable by hand."""
    (store / 'tasks.json').write_text('[]')
    db.migrate_from_json(str(store))
    assert not (store / 'tasks.json').exists()
    assert any(p.name.startswith('tasks.json.imported-') for p in store.iterdir())


def test_nothing_is_renamed_when_a_store_fails_to_parse(store):
    """Part-way is the dangerous state; the whole set stays put."""
    (store / 'tasks.json').write_text('{"truncated": ')
    (store / 'config.json').write_text('{}')
    report = db.migrate_from_json(str(store))
    assert report['failed']
    assert report['renamed'] == []
    assert (store / 'config.json').exists()


def test_a_missing_store_is_simply_skipped(store):
    (store / 'tasks.json').write_text('[]')
    report = db.migrate_from_json(str(store))
    assert 'history' not in report['imported']


def test_export_writes_every_store(store):
    db.set_tasks([{'id': 'a', 'title': 'Bins'}])
    db.set_kv('config', {'hostname': 'x'})
    written = db.export_json(str(store / 'out'))
    names = {p.rsplit('/', 1)[-1] for p in written}
    assert 'tasks.json' in names and 'config.json' in names
    assert json.loads((store / 'out' / 'tasks.json').read_text())[0]['title'] == 'Bins'


def test_discard_removes_the_database_and_its_sidecars(store):
    db.connect()
    assert db.exists()
    db.discard()
    assert not db.exists()
    assert not (store / 'taskhome.db-wal').exists()


def test_a_fresh_install_gets_a_database_without_migrating(store):
    storage.load_data()
    assert db.exists()
    assert state.tasks == []
