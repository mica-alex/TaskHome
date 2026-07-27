"""Shared test fixtures.

Two things must be true throughout: no test may touch the user's real JSON
files, and no test may reach the physical printer. Both have gone wrong for
real during development -- a live tasks.json was overwritten, and a stray code
path emitted an actual receipt -- so both are enforced by autouse fixtures
rather than left to each test's discipline.

Importing the taskhome package is now inert: it reads no files, starts no
thread and touches no hardware. That is the point of the app factory (P0-12),
and it is why these fixtures can be this simple.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest  # noqa: E402

import taskhome  # noqa: E402
from taskhome import printing, state, storage  # noqa: E402

REAL_STATE_FILES = [REPO_ROOT / name for name in
                    ('config.json', 'tasks.json', 'history.json', 'listeners.json')]
REAL_DATA_FILES = [REPO_ROOT / 'data' / name for name in
                   ('config.json', 'tasks.json', 'history.json', 'listeners.json')]


class RealPrinterReached(AssertionError):
    """A test tried to open the physical printer."""


@pytest.fixture(autouse=True)
def never_touch_real_data():
    """Fail loudly if a test modifies the user's live JSON files.

    A backstop, not a nicety: a real tasks.json was destroyed during
    development by running code that wrote to the repo root.
    """
    watched = REAL_STATE_FILES + REAL_DATA_FILES

    def snapshot():
        return {p: (p.read_bytes() if p.exists() else None) for p in watched}

    before = snapshot()
    yield
    after = snapshot()
    changed = [p.name for p in watched if before[p] != after[p]]
    assert not changed, (
        f"test modified real data files: {changed}. "
        f"Point constants.DATA_DIR at tmp_path instead.")


@pytest.fixture(autouse=True)
def no_physical_printing(monkeypatch):
    """Make the real printer unreachable from every test, always.

    Patching individual print functions per test is the wrong layer: any code
    path reaching escpos Usb() prints, and it is easy to add one without
    noticing -- which is exactly how a test once emitted a real receipt.
    Replacing the device constructor covers all of them.
    """
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(True)
        raise RealPrinterReached(
            "a test tried to open the physical printer via escpos Usb(). "
            "Patch the print_* function you are exercising, or use the "
            "fake_printer fixture in tests/test_printing.py.")

    monkeypatch.setattr(printing, 'Usb', forbidden)
    monkeypatch.setattr(printing.usb.core, 'find', lambda *a, **k: None)

    yield

    # Raising is not enough on its own: the print functions catch broad
    # exceptions and return False, so a blocked attempt would be swallowed and
    # the test would pass while hiding that it would have printed.
    assert not attempts, (
        "this test reached escpos Usb(); on a machine with the printer "
        "connected it would have emitted real paper.")


class PrintLog(list):
    """Records what would have been printed. Set `.online = False` to simulate
    a disconnected printer (P0-4)."""
    online = True


@pytest.fixture
def clean_state(monkeypatch):
    """Isolate module state and neuter every path that writes or prints.

    Yields a PrintLog of what would have been printed.
    """
    printed = PrintLog()

    def fake_print(task):
        if not printed.online:
            return False
        printed.append(task)
        return True

    monkeypatch.setattr(state, 'tasks', [])
    monkeypatch.setattr(state, 'history', [])
    monkeypatch.setattr(state, 'listeners', {})
    monkeypatch.setattr(state, 'config', {
        'max_history': 500, 'hostname': 'localhost', 'theme': 'system',
    })
    for name in ('save_tasks', 'save_history', 'save_config', 'save_listeners'):
        monkeypatch.setattr(storage, name, lambda: True)
    monkeypatch.setattr(printing, 'print_task', fake_print)
    monkeypatch.setattr(printing, 'is_printer_connected', lambda: printed.online)

    yield printed


@pytest.fixture
def app():
    """A Flask app with no data loaded and no scheduler thread."""
    application = taskhome.create_app(load=False, with_scheduler=False)
    application.config['TESTING'] = True
    return application


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
