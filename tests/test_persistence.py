"""Storage safety (MASTER_PLAN P0-5) — atomic writes and refusing to clobber.

These exist because real data was actually lost: a load that failed left the
in-memory list empty, and the very next save wrote that empty list over the
user's tasks.json. Both halves of that chain are now tested.

Every test runs inside a tmp_path with the module's file constants pointed at
it, so the real data files are never touched.
"""
import json
import os

import pytest

import app as taskhome


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point every store at a temp directory and reset load state."""
    monkeypatch.setattr(taskhome, 'CONFIG_FILE', str(tmp_path / 'config.json'))
    monkeypatch.setattr(taskhome, 'TASKS_FILE', str(tmp_path / 'tasks.json'))
    monkeypatch.setattr(taskhome, 'HISTORY_FILE', str(tmp_path / 'history.json'))
    monkeypatch.setattr(taskhome, 'LISTENERS_FILE', str(tmp_path / 'listeners.json'))
    monkeypatch.setattr(taskhome, 'tasks', [])
    monkeypatch.setattr(taskhome, 'history', [])
    monkeypatch.setattr(taskhome, 'listeners', {})
    monkeypatch.setattr(taskhome, 'config', dict(taskhome.DEFAULT_CONFIG))
    taskhome._load_failed.clear()
    yield tmp_path
    taskhome._load_failed.clear()


REAL_TASKS = [
    {'id': 'a', 'title': 'Take Medicine', 'next_time': '2026-03-01T09:00:00',
     'recurring': 'daily', 'enabled': True},
]


# --- the data-loss chain ------------------------------------------------------

def test_corrupt_file_blocks_saving_over_it(store):
    """The exact chain that lost data: bad parse -> empty memory -> save."""
    tasks_file = store / 'tasks.json'
    tasks_file.write_text('{"truncated": ')

    taskhome.load_data()
    assert taskhome.tasks == []            # memory is empty...
    assert 'tasks' in taskhome._load_failed

    assert taskhome.save_tasks() is False  # ...but the save is refused
    assert tasks_file.read_text() == '{"truncated": '  # file untouched


def test_healthy_file_saves_normally(store):
    (store / 'tasks.json').write_text(json.dumps(REAL_TASKS))
    taskhome.load_data()
    assert len(taskhome.tasks) == 1

    taskhome.tasks.append(dict(REAL_TASKS[0], id='b'))
    assert taskhome.save_tasks() is True
    assert len(json.loads((store / 'tasks.json').read_text())) == 2


def test_missing_file_is_not_a_failure(store):
    """Absent is fine; unreadable is not. Only the latter blocks saves."""
    taskhome.load_data()
    assert 'tasks' not in taskhome._load_failed
    taskhome.tasks.extend(REAL_TASKS)
    assert taskhome.save_tasks() is True


def test_one_corrupt_store_does_not_block_the_others(store):
    (store / 'tasks.json').write_text('not json at all')
    (store / 'history.json').write_text(json.dumps([{'type': 'task'}]))

    taskhome.load_data()

    assert 'tasks' in taskhome._load_failed
    assert 'history' not in taskhome._load_failed
    assert taskhome.save_tasks() is False
    assert taskhome.save_history() is True


# --- atomicity ----------------------------------------------------------------

def test_write_is_atomic_via_rename(store, monkeypatch):
    """A failure mid-write must leave the previous file intact, not truncated.

    The old code opened the target with 'w', truncating it instantly; anything
    that went wrong after that point left an empty or partial file.
    """
    tasks_file = store / 'tasks.json'
    tasks_file.write_text(json.dumps(REAL_TASKS))
    taskhome.load_data()

    def explode(*args, **kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(taskhome.json, 'dump', explode)
    taskhome.tasks = []
    assert taskhome.save_tasks() is False

    # The original content survived.
    assert json.loads(tasks_file.read_text()) == REAL_TASKS


def test_failed_write_leaves_no_temp_files(store, monkeypatch):
    (store / 'tasks.json').write_text(json.dumps(REAL_TASKS))
    taskhome.load_data()
    monkeypatch.setattr(taskhome.json, 'dump',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('nope')))
    taskhome.save_tasks()
    leftovers = [p.name for p in store.iterdir() if p.name.endswith('.tmp')]
    assert leftovers == []


def test_saved_json_is_readable_back(store):
    taskhome.load_data()
    taskhome.tasks.extend(REAL_TASKS)
    taskhome.save_tasks()
    assert json.loads((store / 'tasks.json').read_text()) == REAL_TASKS


# --- config merging (P1-6) ----------------------------------------------------

def test_config_merges_over_defaults(store):
    """A config file missing keys must not break code that reads them."""
    (store / 'config.json').write_text(json.dumps({'hostname': 'printer.local'}))
    taskhome.load_data()
    assert taskhome.config['hostname'] == 'printer.local'
    assert taskhome.config['max_history'] == 500   # from defaults
    assert taskhome.config['theme'] == 'system'


def test_legacy_theme_is_migrated(store):
    (store / 'config.json').write_text(json.dumps({'theme': 'high-contrast'}))
    taskhome.load_data()
    assert taskhome.config['theme'] == 'system'


def test_tasks_get_enabled_default(store):
    (store / 'tasks.json').write_text(json.dumps([{'id': 'x', 'title': 'T',
                                                   'next_time': '2026-03-01T09:00:00',
                                                   'recurring': 'daily'}]))
    taskhome.load_data()
    assert taskhome.tasks[0]['enabled'] is True


def test_history_records_get_type_default(store):
    (store / 'history.json').write_text(json.dumps([{'id': 'x', 'title': 'Old'}]))
    taskhome.load_data()
    assert taskhome.history[0]['type'] == 'task'


def test_listeners_default_is_created_when_absent(store):
    taskhome.load_data()
    assert 'scf' in taskhome.listeners
    assert (store / 'listeners.json').exists()
