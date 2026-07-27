"""Legacy data migration into data/ (MASTER_PLAN P1-9).

TaskHome has historically run straight out of a git clone with its JSON files
in the repo root. Existing installs must keep working across the move without
anyone relocating files by hand — and, more importantly, without losing
anything if the migration goes sideways.
"""
import json
import os

import pytest

import app as taskhome

STORES = ('config.json', 'tasks.json', 'history.json', 'listeners.json')


@pytest.fixture
def dirs(tmp_path):
    legacy = tmp_path / 'repo'
    data = legacy / 'data'
    legacy.mkdir()
    return legacy, data


def write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def test_nothing_to_do_is_a_no_op(dirs):
    legacy, data = dirs
    assert taskhome.migrate_legacy_data_files(str(legacy), str(data)) == []
    assert not data.exists()


def test_legacy_files_move_into_data(dirs):
    legacy, data = dirs
    for name in STORES:
        write(legacy / name, {'store': name})

    actions = taskhome.migrate_legacy_data_files(str(legacy), str(data))

    assert len(actions) == 4
    for name in STORES:
        assert (data / name).exists()
        assert not (legacy / name).exists()
        assert json.loads((data / name).read_text()) == {'store': name}


def test_content_survives_exactly(dirs):
    """The whole point: no data may be altered in transit."""
    legacy, data = dirs
    payload = [{'id': 'abc', 'title': 'Take Medicine', 'extra': 'MISS KITTY TIME'}]
    write(legacy / 'tasks.json', payload)

    taskhome.migrate_legacy_data_files(str(legacy), str(data))

    assert json.loads((data / 'tasks.json').read_text()) == payload


def test_partial_migration_moves_what_it_can(dirs):
    """Only some files present — the rest must not be invented."""
    legacy, data = dirs
    write(legacy / 'tasks.json', [1])

    taskhome.migrate_legacy_data_files(str(legacy), str(data))

    assert (data / 'tasks.json').exists()
    assert not (data / 'history.json').exists()


def test_is_idempotent(dirs):
    legacy, data = dirs
    write(legacy / 'tasks.json', [1])

    taskhome.migrate_legacy_data_files(str(legacy), str(data))
    second = taskhome.migrate_legacy_data_files(str(legacy), str(data))

    assert second == []
    assert json.loads((data / 'tasks.json').read_text()) == [1]


def test_file_in_both_places_keeps_data_copy_and_preserves_legacy(dirs):
    """Ambiguous state: don't guess, and above all don't delete."""
    legacy, data = dirs
    write(legacy / 'tasks.json', ['legacy'])
    write(data / 'tasks.json', ['current'])

    actions = taskhome.migrate_legacy_data_files(str(legacy), str(data))

    # data/ wins, because that is what the app reads.
    assert json.loads((data / 'tasks.json').read_text()) == ['current']
    # The legacy file is preserved under a new name, not removed.
    assert not (legacy / 'tasks.json').exists()
    superseded = list(legacy.glob('tasks.json.superseded-*'))
    assert len(superseded) == 1
    assert json.loads(superseded[0].read_text()) == ['legacy']
    assert 'kept data/ copy' in actions[0]


def test_breadcrumb_is_left_behind(dirs):
    legacy, data = dirs
    write(legacy / 'tasks.json', [1])
    taskhome.migrate_legacy_data_files(str(legacy), str(data))
    note = legacy / 'DATA_MOVED.txt'
    assert note.exists()
    assert 'data/' in note.read_text()


def test_no_breadcrumb_when_nothing_moved(dirs):
    legacy, data = dirs
    taskhome.migrate_legacy_data_files(str(legacy), str(data))
    assert not (legacy / 'DATA_MOVED.txt').exists()


def test_unwritable_data_dir_leaves_legacy_intact(dirs, monkeypatch):
    """A read-only filesystem must not destroy anything or crash startup."""
    legacy, data = dirs
    write(legacy / 'tasks.json', ['important'])

    def refuse(*a, **k):
        raise OSError('read-only file system')

    monkeypatch.setattr(taskhome.os, 'makedirs', refuse)
    actions = taskhome.migrate_legacy_data_files(str(legacy), str(data))

    assert actions == []
    assert json.loads((legacy / 'tasks.json').read_text()) == ['important']


def test_failed_move_keeps_the_source(dirs, monkeypatch):
    """If both replace and copy fail, the original must still be there."""
    legacy, data = dirs
    write(legacy / 'tasks.json', ['important'])

    monkeypatch.setattr(taskhome.os, 'replace',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('xdev')))
    monkeypatch.setattr(taskhome.shutil, 'copy2',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('nope')))

    taskhome.migrate_legacy_data_files(str(legacy), str(data))
    assert (legacy / 'tasks.json').exists()


def test_cross_device_move_falls_back_to_copy(dirs, monkeypatch):
    legacy, data = dirs
    write(legacy / 'tasks.json', ['payload'])

    real_replace = taskhome.os.replace

    def replace_fails_for_stores(src, dst, *a, **k):
        if str(dst).endswith('tasks.json'):
            raise OSError('cross-device link')
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(taskhome.os, 'replace', replace_fails_for_stores)
    actions = taskhome.migrate_legacy_data_files(str(legacy), str(data))

    assert json.loads((data / 'tasks.json').read_text()) == ['payload']
    assert not (legacy / 'tasks.json').exists()
    assert 'copied' in actions[0]


def test_one_failure_does_not_block_the_others(dirs, monkeypatch):
    legacy, data = dirs
    write(legacy / 'tasks.json', ['a'])
    write(legacy / 'history.json', ['b'])

    real_replace = taskhome.os.replace

    def fail_tasks_only(src, dst, *a, **k):
        if str(dst).endswith('tasks.json'):
            raise OSError('boom')
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(taskhome.os, 'replace', fail_tasks_only)
    monkeypatch.setattr(taskhome.shutil, 'copy2',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('boom')))

    taskhome.migrate_legacy_data_files(str(legacy), str(data))

    assert (legacy / 'tasks.json').exists()     # failed, preserved
    assert (data / 'history.json').exists()     # succeeded independently


def test_load_data_migrates_then_reads(tmp_path, monkeypatch):
    """End to end: an existing install starts up and finds its data."""
    legacy = tmp_path / 'repo'
    data = legacy / 'data'
    legacy.mkdir()
    write(legacy / 'tasks.json', [{'id': 'x', 'title': 'Feed cat',
                                   'next_time': '2026-03-05T09:00:00',
                                   'recurring': 'daily', 'enabled': True}])
    write(legacy / 'config.json', {'hostname': 'printer.local'})

    monkeypatch.setattr(taskhome, 'APP_ROOT', str(legacy))
    monkeypatch.setattr(taskhome, 'DATA_DIR', str(data))
    for const, name in [('CONFIG_FILE', 'config.json'), ('TASKS_FILE', 'tasks.json'),
                        ('HISTORY_FILE', 'history.json'),
                        ('LISTENERS_FILE', 'listeners.json')]:
        monkeypatch.setattr(taskhome, const, str(data / name))
    monkeypatch.setattr(taskhome, 'tasks', [])
    monkeypatch.setattr(taskhome, 'config', dict(taskhome.DEFAULT_CONFIG))
    monkeypatch.setattr(taskhome, 'history', [])
    monkeypatch.setattr(taskhome, 'listeners', {})

    taskhome.load_data()

    assert len(taskhome.tasks) == 1
    assert taskhome.tasks[0]['title'] == 'Feed cat'
    assert taskhome.config['hostname'] == 'printer.local'
    assert taskhome.config['max_history'] == 500  # merged from defaults
    assert (data / 'tasks.json').exists()
    assert not (legacy / 'tasks.json').exists()


def test_data_dir_honours_environment_override():
    """TASKHOME_DATA_DIR is how tests and throwaway runs stay off real data."""
    assert 'TASKHOME_DATA_DIR' in open(
        os.path.join(os.path.dirname(__file__), '..', 'app.py')).read()
