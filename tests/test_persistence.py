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
    taskhome.state.load_failed.clear()
    yield tmp_path
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


def test_healthy_file_saves_normally(store):
    (store / 'tasks.json').write_text(json.dumps(REAL_TASKS))
    taskhome.storage.load_data()
    assert len(taskhome.state.tasks) == 1

    taskhome.state.tasks.append(dict(REAL_TASKS[0], id='b'))
    assert taskhome.storage.save_tasks() is True
    assert len(json.loads((store / 'tasks.json').read_text())) == 2


def test_missing_file_is_not_a_failure(store):
    """Absent is fine; unreadable is not. Only the latter blocks saves."""
    taskhome.storage.load_data()
    assert 'tasks' not in taskhome.state.load_failed
    taskhome.state.tasks.extend(REAL_TASKS)
    assert taskhome.storage.save_tasks() is True


def test_one_corrupt_store_does_not_block_the_others(store):
    (store / 'tasks.json').write_text('not json at all')
    (store / 'history.json').write_text(json.dumps([{'type': 'task'}]))

    taskhome.storage.load_data()

    assert 'tasks' in taskhome.state.load_failed
    assert 'history' not in taskhome.state.load_failed
    assert taskhome.storage.save_tasks() is False
    assert taskhome.storage.save_history() is True


# --- atomicity ----------------------------------------------------------------

@pytest.mark.parametrize('failure_point', ['serialise', 'write', 'rename'])
def test_failure_at_any_stage_leaves_the_previous_file_intact(
        store, monkeypatch, failure_point):
    """The whole point of the temp-file dance.

    The old code opened the target with 'w', truncating it instantly, so
    anything that went wrong after that left an empty or partial file. Now a
    failure at any stage must leave the previous content untouched.
    """
    tasks_file = store / 'tasks.json'
    tasks_file.write_text(json.dumps(REAL_TASKS))
    taskhome.storage.load_data()

    def explode(*args, **kwargs):
        raise OSError('injected failure')

    if failure_point == 'serialise':
        monkeypatch.setattr(taskhome.storage.json, 'dumps', explode)
    elif failure_point == 'write':
        monkeypatch.setattr(taskhome.storage.os, 'fsync', explode)
    else:
        monkeypatch.setattr(taskhome.storage.os, 'replace', explode)

    taskhome.state.tasks[:] = []
    taskhome.storage.save_tasks()

    assert json.loads(tasks_file.read_text()) == REAL_TASKS


def test_successful_write_replaces_content(store):
    tasks_file = store / 'tasks.json'
    tasks_file.write_text(json.dumps(REAL_TASKS))
    taskhome.storage.load_data()
    taskhome.state.tasks[:] = []
    assert taskhome.storage.save_tasks() is True
    assert json.loads(tasks_file.read_text()) == []


@pytest.mark.parametrize('failure_point', ['write', 'rename'])
def test_failed_write_leaves_no_temp_files(store, monkeypatch, failure_point):
    (store / 'tasks.json').write_text(json.dumps(REAL_TASKS))
    taskhome.storage.load_data()

    def explode(*args, **kwargs):
        raise OSError('injected failure')

    monkeypatch.setattr(taskhome.storage.os,
                        'fsync' if failure_point == 'write' else 'replace', explode)
    taskhome.storage.save_tasks()
    leftovers = [p.name for p in store.iterdir() if p.name.endswith('.tmp')]
    assert leftovers == []


def test_saved_json_is_readable_back(store):
    taskhome.storage.load_data()
    taskhome.state.tasks.extend(REAL_TASKS)
    taskhome.storage.save_tasks()
    assert json.loads((store / 'tasks.json').read_text()) == REAL_TASKS


# --- config merging (P1-6) ----------------------------------------------------

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
    assert (store / 'listeners.json').exists()
