"""Storage safety (MASTER_PLAN P0-5) — atomic writes and refusing to clobber.

These exist because real data was actually lost: a load that failed left the
in-memory list empty, and the very next save wrote that empty list over the
user's tasks.json. Both halves of that chain are now tested.

Every test runs inside a tmp_path with the module's file constants pointed at
it, so the real data files are never touched.
"""
import json

import pytest

import taskhome


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point every store at a temp directory and reset load state.

    APP_ROOT and DATA_DIR are redirected too, not just the file constants:
    load_data() runs the legacy migration, which would otherwise operate on the
    real repo root and move the user's actual JSON files.
    """
    monkeypatch.setattr(taskhome.constants, 'APP_ROOT', str(tmp_path / 'repo'))
    monkeypatch.setattr(taskhome.constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(taskhome.constants, 'CONFIG_FILE', str(tmp_path / 'config.json'))
    monkeypatch.setattr(taskhome.constants, 'TASKS_FILE', str(tmp_path / 'tasks.json'))
    monkeypatch.setattr(taskhome.constants, 'HISTORY_FILE', str(tmp_path / 'history.json'))
    monkeypatch.setattr(taskhome.constants, 'LISTENERS_FILE', str(tmp_path / 'listeners.json'))
    monkeypatch.setattr(taskhome.state, 'tasks', [])
    monkeypatch.setattr(taskhome.state, 'history', [])
    monkeypatch.setattr(taskhome.state, 'listeners', {})
    monkeypatch.setattr(taskhome.state, 'config', dict(taskhome.constants.DEFAULT_CONFIG))
    # Each test gets its own database; the connection cache is per-thread and
    # would otherwise point at the previous test's temp directory.
    taskhome.db.forget()
    taskhome.state.load_failed.clear()
    yield tmp_path
    taskhome.db.forget()
    taskhome.state.load_failed.clear()


REAL_TASKS = [
    {'id': 'a', 'title': 'Take Medicine', 'next_time': '2026-03-01T09:00:00',
     'recurring': 'daily', 'enabled': True},
]


# --- the data-loss chain ------------------------------------------------------

def test_corrupt_file_blocks_saving_over_it(store):
    """The exact chain that lost data: bad parse -> empty memory -> save."""
    tasks_file = store / 'tasks.json'
    tasks_file.write_text('{"truncated": ')

    taskhome.storage.load_data()
    assert taskhome.state.tasks == []            # memory is empty...
    assert 'tasks' in taskhome.state.load_failed

    assert taskhome.storage.save_tasks() is False  # ...but the save is refused
    assert tasks_file.read_text() == '{"truncated": '  # file untouched


def test_healthy_data_saves_normally(store):
    (store / 'tasks.json').write_text(json.dumps(REAL_TASKS))
    taskhome.storage.load_data()
    assert len(taskhome.state.tasks) == 1

    taskhome.state.tasks.append(dict(REAL_TASKS[0], id='b'))
    assert taskhome.storage.save_tasks() is True

    taskhome.state.tasks[:] = []
    taskhome.storage.load_data()
    assert len(taskhome.state.tasks) == 2


def test_missing_file_is_not_a_failure(store):
    """Absent is fine; unreadable is not. Only the latter blocks saves."""
    taskhome.storage.load_data()
    assert 'tasks' not in taskhome.state.load_failed
    taskhome.state.tasks.extend(REAL_TASKS)
    assert taskhome.storage.save_tasks() is True


def test_one_corrupt_store_does_not_block_the_others(store):
    (store / 'tasks.json').write_text('not json at all')
    (store / 'history.json').write_text(json.dumps(
        [{'type': 'task', 'print_time': '2026-01-01T00:00:00'}]))

    taskhome.storage.load_data()

    assert 'tasks' in taskhome.state.load_failed
    assert 'history' not in taskhome.state.load_failed
    assert taskhome.storage.save_tasks() is False
    assert taskhome.storage.save_history() is True


def test_a_migration_that_cannot_finish_leaves_no_database(store):
    """A half-migrated database is worse than none: it exists, so the backend
    switches to it, the store that failed to import reads as empty, nothing is
    marked failed, and the next save writes that emptiness over the only
    surviving copy. That is the P0-5 chain at the migration boundary.
    """
    (store / 'tasks.json').write_text('{"truncated": ')
    (store / 'history.json').write_text(json.dumps([]))

    taskhome.storage.load_data()

    assert taskhome.db.exists() is False, 'a half-migrated database survived'
    assert 'tasks' in taskhome.state.load_failed
    assert taskhome.storage.save_tasks() is False
    assert (store / 'tasks.json').read_text() == '{"truncated": '


def test_a_clean_migration_imports_everything_and_keeps_the_originals(store):
    (store / 'tasks.json').write_text(json.dumps(REAL_TASKS))
    (store / 'config.json').write_text(json.dumps({'hostname': 'printer.local'}))

    taskhome.storage.load_data()

    assert taskhome.db.exists() is True
    assert len(taskhome.state.tasks) == 1
    assert taskhome.state.config['hostname'] == 'printer.local'
    # Renamed, never deleted -- a bad migration must be recoverable by hand.
    assert not (store / 'tasks.json').exists()
    assert any(p.name.startswith('tasks.json.imported-') for p in store.iterdir())


def test_the_json_export_round_trips(store):
    """Backup symmetry: a database must be readable without sqlite to hand."""
    (store / 'tasks.json').write_text(json.dumps(REAL_TASKS))
    taskhome.storage.load_data()

    target = store / 'export'
    written = taskhome.db.export_json(str(target))
    assert written
    exported = json.loads((target / 'tasks.json').read_text())
    assert [t['title'] for t in exported] == ['Take Medicine']


# --- atomicity ----------------------------------------------------------------

# The atomic-write machinery still backs every store that is NOT in the
# database -- the request-type and weather-zone caches. These exercise it
# through a cache-shaped store name, since the core stores now go to SQLite.
CACHE_STORE = 'nws-zones'


@pytest.mark.parametrize('failure_point', ['serialise', 'write', 'rename'])
def test_failure_at_any_stage_leaves_the_previous_file_intact(
        store, monkeypatch, failure_point):
    """The whole point of the temp-file dance.

    The old code opened the target with 'w', truncating it instantly, so
    anything that went wrong after that left an empty or partial file. A
    failure at any stage must leave the previous content untouched.

    Exercised through a cache store, because the core stores now live in
    SQLite -- but this machinery still backs the request-type and weather-zone
    caches, so it still has to work.
    """
    cache_file = store / 'cache.json'
    cache_file.write_text(json.dumps(REAL_TASKS))

    def explode(*args, **kwargs):
        raise OSError('injected failure')

    if failure_point == 'serialise':
        monkeypatch.setattr(taskhome.storage.json, 'dumps', explode)
    elif failure_point == 'write':
        monkeypatch.setattr(taskhome.storage.os, 'fsync', explode)
    else:
        monkeypatch.setattr(taskhome.storage.os, 'replace', explode)

    taskhome.storage._save_json_file(CACHE_STORE, str(cache_file), [])
    assert json.loads(cache_file.read_text()) == REAL_TASKS


def test_successful_write_replaces_content(store):
    cache_file = store / 'cache.json'
    cache_file.write_text(json.dumps(REAL_TASKS))
    assert taskhome.storage._save_json_file(CACHE_STORE, str(cache_file), []) is True
    assert json.loads(cache_file.read_text()) == []


@pytest.mark.parametrize('failure_point', ['write', 'rename'])
def test_failed_write_leaves_no_temp_files(store, monkeypatch, failure_point):
    cache_file = store / 'cache.json'
    cache_file.write_text(json.dumps(REAL_TASKS))

    def explode(*args, **kwargs):
        raise OSError('injected failure')

    monkeypatch.setattr(taskhome.storage.os,
                        'fsync' if failure_point == 'write' else 'replace', explode)
    taskhome.storage._save_json_file(CACHE_STORE, str(cache_file), [])
    leftovers = [p.name for p in store.iterdir() if p.name.endswith('.tmp')]
    assert leftovers == []


# --- the same properties, against the database backend ------------------------

def test_a_saved_task_survives_a_reload(store):
    """The property the file-shaped tests used to assert, stated in terms of
    the datastore rather than the mechanism."""
    taskhome.storage.load_data()
    taskhome.state.tasks.extend(REAL_TASKS)
    assert taskhome.storage.save_tasks() is True

    taskhome.state.tasks[:] = []
    taskhome.storage.load_data()
    assert [t['title'] for t in taskhome.state.tasks] == ['Take Medicine']


def test_the_database_is_where_the_data_goes(store):
    taskhome.storage.load_data()
    assert taskhome.storage.use_db() is True
    assert (store / 'taskhome.db').exists()


def test_a_store_that_failed_to_load_is_never_written(store, monkeypatch):
    """Unchanged by the backend swap: a store that would not read must not be
    overwritten, or a transient failure destroys recoverable data."""
    taskhome.storage.load_data()
    taskhome.state.load_failed.add('tasks')
    assert taskhome.storage.save_tasks() is False


def test_an_unreadable_database_blocks_writes(store, monkeypatch):
    """The SQLite equivalent of a corrupt JSON file."""
    taskhome.storage.load_data()

    def explode(*args, **kwargs):
        raise RuntimeError('database is locked')

    monkeypatch.setattr(taskhome.db, 'get_tasks', explode)
    value, ok = taskhome.storage._load_json_file('tasks', 'ignored', None)
    assert ok is False
    assert 'tasks' in taskhome.state.load_failed


def test_config_merges_over_defaults(store):
    """A config file missing keys must not break code that reads them."""
    (store / 'config.json').write_text(json.dumps({'hostname': 'printer.local'}))
    taskhome.storage.load_data()
    assert taskhome.state.config['hostname'] == 'printer.local'
    assert taskhome.state.config['max_history'] == 500   # from defaults
    assert taskhome.state.config['theme'] == 'system'


def test_legacy_theme_is_migrated(store):
    (store / 'config.json').write_text(json.dumps({'theme': 'high-contrast'}))
    taskhome.storage.load_data()
    assert taskhome.state.config['theme'] == 'system'


def test_tasks_get_enabled_default(store):
    (store / 'tasks.json').write_text(json.dumps([{'id': 'x', 'title': 'T',
                                                   'next_time': '2026-03-01T09:00:00',
                                                   'recurring': 'daily'}]))
    taskhome.storage.load_data()
    assert taskhome.state.tasks[0]['enabled'] is True


def test_history_records_get_type_default(store):
    (store / 'history.json').write_text(json.dumps([{'id': 'x', 'title': 'Old'}]))
    taskhome.storage.load_data()
    assert taskhome.state.history[0]['type'] == 'task'


def test_listeners_default_is_created_when_absent(store):
    taskhome.storage.load_data()
    assert 'scf' in taskhome.state.listeners
    # Persisted, wherever the backend keeps it.
    taskhome.state.listeners.clear()
    taskhome.storage.load_data()
    assert 'scf' in taskhome.state.listeners
