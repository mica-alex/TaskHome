"""Data backups (MASTER_PLAN P6-2).

The plan sequenced this early -- "backup before touching storage" -- and it was
skipped. Two data-displacement incidents followed during development; both were
recoverable by luck and by the migration's never-delete rule, not by design.
These snapshots are the design.
"""
import json

import pytest

from taskhome import constants, state, storage


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, 'APP_ROOT', str(tmp_path / 'repo'))
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(constants, 'TASKS_FILE', str(tmp_path / 'tasks.json'))
    monkeypatch.setattr(state, 'tasks', [])
    monkeypatch.setattr(state, 'config', dict(constants.DEFAULT_CONFIG))
    state.load_failed.clear()
    yield tmp_path
    state.load_failed.clear()


def tasks_file(store):
    return store / 'tasks.json'


def test_no_backup_when_nothing_exists_yet(store):
    state.tasks[:] = [{'id': 'a'}]
    storage.save_tasks()
    assert storage.list_backups('tasks') == []


def test_overwriting_snapshots_the_previous_content(store):
    """The snapshot is a *pre-image*: backing up after writing would only ever
    preserve the new content, which is useless for recovery."""
    tasks_file(store).write_text(json.dumps([{'id': 'original'}]))
    state.tasks[:] = [{'id': 'replacement'}]
    storage.save_tasks()

    snapshots = storage.list_backups('tasks')
    assert len(snapshots) == 1
    saved = json.loads((store / 'backups' / 'tasks' / snapshots[0]).read_text())
    assert saved == [{'id': 'original'}]
    assert json.loads(tasks_file(store).read_text()) == [{'id': 'replacement'}]


def test_unchanged_content_is_not_snapshotted_again(store):
    """The scheduler rewrites tasks.json whenever anything moves; twenty
    identical copies would push the real history out of the window."""
    state.tasks[:] = [{'id': 'a'}]
    storage.save_tasks()      # creates the file; nothing to snapshot yet
    storage.save_tasks()      # snapshots the pre-image once
    settled = len(storage.list_backups('tasks'))
    assert settled == 1

    for _ in range(5):        # identical content: must not accumulate
        storage.save_tasks()
    assert len(storage.list_backups('tasks')) == settled


def test_each_distinct_version_is_kept(store):
    for n in range(4):
        state.tasks[:] = [{'id': f'v{n}'}]
        storage.save_tasks()
    assert len(storage.list_backups('tasks')) == 3   # 3 pre-images of 4 writes


def test_retention_prunes_oldest_first(store):
    state.config['backups'] = {'keep': 3}
    for n in range(8):
        state.tasks[:] = [{'id': f'v{n}'}]
        storage.save_tasks()

    snapshots = storage.list_backups('tasks')
    assert len(snapshots) == 3
    newest = json.loads((store / 'backups' / 'tasks' / snapshots[0]).read_text())
    assert newest == [{'id': 'v6'}]      # the pre-image of the final write


def test_backups_can_be_disabled(store):
    state.config['backups'] = {'enabled': False}
    tasks_file(store).write_text(json.dumps([{'id': 'a'}]))
    state.tasks[:] = [{'id': 'b'}]
    storage.save_tasks()
    assert storage.list_backups('tasks') == []


@pytest.mark.parametrize('keep', ['abc', None, 0, -1])
def test_invalid_retention_falls_back(store, keep):
    state.config['backups'] = {'keep': keep}
    assert storage.backup_keep() >= 1


def test_backup_failure_does_not_prevent_the_save(store, monkeypatch):
    """A backup problem must never block the write it precedes."""
    tasks_file(store).write_text(json.dumps([{'id': 'a'}]))
    monkeypatch.setattr(storage.os, 'makedirs',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('read-only')))
    state.tasks[:] = [{'id': 'b'}]
    assert storage.save_tasks() is True
    assert json.loads(tasks_file(store).read_text()) == [{'id': 'b'}]


def test_same_second_writes_do_not_overwrite_each_other(store):
    """Timestamps are second-resolution; two saves in one second must both
    survive rather than one silently replacing the other."""
    for n in range(3):
        tasks_file(store).write_text(json.dumps([{'id': f'v{n}'}]))
        storage.backup_store('tasks', str(tasks_file(store)))
    assert len(storage.list_backups('tasks')) == 3


def test_empty_file_is_not_snapshotted(store):
    """An empty file is the symptom of the failure we are protecting against,
    not something worth keeping a copy of."""
    tasks_file(store).write_text('')
    storage.backup_store('tasks', str(tasks_file(store)))
    assert storage.list_backups('tasks') == []


def test_a_recovered_snapshot_round_trips(store):
    """The whole point: the previous content can be read back and restored."""
    original = [{'id': 'x', 'title': 'Take Medicine'}]
    tasks_file(store).write_text(json.dumps(original))
    state.tasks[:] = []
    storage.save_tasks()                       # simulate the destructive write

    snapshot = storage.list_backups('tasks')[0]
    recovered = json.loads((store / 'backups' / 'tasks' / snapshot).read_text())
    assert recovered == original

    storage._save_json_file('tasks', str(tasks_file(store)), recovered)
    assert json.loads(tasks_file(store).read_text()) == original


def test_backups_live_under_the_data_dir(store):
    assert storage.backup_dir('tasks').startswith(str(store))


def test_snapshot_names_sort_newest_first(store):
    """Ordering is lexicographic, so the name format has to sort correctly.

    At second resolution two saves in the same second collided and the
    disambiguating suffix sorted *before* the plain name -- making the oldest
    look newest, which broke both the unchanged-content check and pruning.
    """
    for n in range(5):
        tasks_file(store).write_text(json.dumps([{'id': f'v{n}'}]))
        storage.backup_store('tasks', str(tasks_file(store)))

    snapshots = storage.list_backups('tasks')
    assert snapshots == sorted(snapshots, reverse=True)
    newest = json.loads((store / 'backups' / 'tasks' / snapshots[0]).read_text())
    assert newest == [{'id': 'v4'}]
