"""Shared test fixtures.

Two things must be true before `app` is imported: it must not read the user's
real JSON data files, and it must not start the scheduler thread (which prints
to a physical printer). `TASKHOME_NO_INIT=1` handles both, and is set here
before the import happens.
"""
import os
import sys
from pathlib import Path

os.environ['TASKHOME_NO_INIT'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import app as taskhome  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_STATE_FILES = [REPO_ROOT / name for name in
                    ('config.json', 'tasks.json', 'history.json', 'listeners.json')]


@pytest.fixture(autouse=True)
def never_touch_real_data():
    """Fail loudly if any test modifies the user's live JSON files.

    This is a backstop, not a nicety: a real tasks.json was destroyed during
    development by running code that wrote to the repo root. Tests are the most
    likely place for that to happen again, so the suite checks itself.
    """
    def snapshot():
        return {p: (p.read_bytes() if p.exists() else None) for p in REAL_STATE_FILES}

    before = snapshot()
    yield
    after = snapshot()
    changed = [p.name for p in REAL_STATE_FILES if before[p] != after[p]]
    assert not changed, (
        f"test modified real data files: {changed}. "
        f"Point DATA_DIR/APP_ROOT at tmp_path instead.")


class PrintLog(list):
    """Records what would have been printed, and can simulate an offline
    printer by setting `.online = False` (used to test P0-4).

    Subclasses list so tests can assert on it directly.
    """
    online = True


@pytest.fixture
def clean_state(monkeypatch):
    """Isolate module-level globals and neuter every path that writes to disk
    or to the printer. Yields a PrintLog of what *would* have been printed.

    This is a minimal stand-in for the fake printer backend in MASTER_PLAN
    P1-4 — it proves the scheduler logic without hardware.
    """
    printed = PrintLog()

    def fake_print(task):
        if not printed.online:
            return False
        printed.append(task)
        return True

    monkeypatch.setattr(taskhome, 'tasks', [])
    monkeypatch.setattr(taskhome, 'history', [])
    monkeypatch.setattr(taskhome, 'listeners', {})
    monkeypatch.setattr(taskhome, 'config', {
        'max_history': 500, 'hostname': 'localhost', 'theme': 'system',
    })
    monkeypatch.setattr(taskhome, 'save_tasks', lambda: None)
    monkeypatch.setattr(taskhome, 'save_history', lambda: None)
    monkeypatch.setattr(taskhome, 'save_config', lambda: None)
    monkeypatch.setattr(taskhome, 'save_listeners', lambda: None)
    monkeypatch.setattr(taskhome, 'print_task', fake_print)
    monkeypatch.setattr(taskhome, 'is_printer_connected', lambda: printed.online)

    yield printed


@pytest.fixture
def make_task():
    """Build a task dict with sensible defaults."""
    counter = {'n': 0}

    def _make(next_time, recurring='daily', **overrides):
        counter['n'] += 1
        task = {
            'id': f"task-{counter['n']}",
            'title': f"Task {counter['n']}",
            'next_time': next_time,
            'recurring': recurring,
            'enabled': True,
        }
        task.update(overrides)
        return task

    return _make
