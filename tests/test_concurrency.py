"""Concurrent access to shared state (the remaining half of P0-5).

The scheduler thread and Flask request handlers mutate the same module-level
lists. `STATE_LOCK` guards structural mutation and serialisation. It is
deliberately NOT held across printing or HTTP fetches -- those take seconds and
would stall every page load.

**What these tests actually establish**, stated honestly: that the observable
behaviour is correct under thread pressure -- no exceptions, no lost or
duplicated history records, the cap respected, the file always parseable.

They do **not** demonstrate that the lock is load-bearing today. It was
verified by removing it: every test here still passes, because CPython's GIL
makes an individual list `insert`/`del`/`append` atomic, and `json.dumps` of
plain types runs entirely in C without releasing it. The races the lock
prevents are therefore not reachable on a stock CPython 3.13 build.

The lock is kept anyway, for reasons that are about the future rather than
today:

  * GIL atomicity is a CPython implementation detail, not a language
    guarantee. Free-threaded builds (PEP 703, available from 3.13) remove it,
    and this project would be a natural candidate to run on one.
  * It makes compound read-modify-write sequences correct by construction, so
    the next person to add one -- `record_history` already reads config,
    inserts, then trims -- does not have to reason about GIL boundaries.
  * The cost is negligible: it is never held across I/O.

If you are tempted to remove it because "the tests pass without it": they do,
and that is documented here precisely so the argument does not have to be
rediscovered.
"""
import json
import threading

import pytest

import app as taskhome


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(taskhome, 'APP_ROOT', str(tmp_path / 'repo'))
    monkeypatch.setattr(taskhome, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(taskhome, 'TASKS_FILE', str(tmp_path / 'tasks.json'))
    monkeypatch.setattr(taskhome, 'HISTORY_FILE', str(tmp_path / 'history.json'))
    monkeypatch.setattr(taskhome, 'tasks', [])
    monkeypatch.setattr(taskhome, 'history', [])
    monkeypatch.setattr(taskhome, 'config', dict(taskhome.DEFAULT_CONFIG))
    taskhome._load_failed.clear()
    yield tmp_path
    taskhome._load_failed.clear()


def task(n):
    return {'id': f'task-{n}', 'title': f'Task {n}',
            'next_time': '2026-03-05T09:00:00', 'recurring': 'daily',
            'enabled': True}


def test_saving_while_mutating_does_not_raise_or_tear(store):
    """Serialise repeatedly while another thread mutates the same list.

    Verifies the file on disk is always complete, parseable JSON and that no
    exception escapes. See the module docstring on what this does and does not
    prove about the lock.
    """
    errors = []
    stop = threading.Event()

    def mutate():
        n = 0
        while not stop.is_set():
            with taskhome.STATE_LOCK:
                taskhome.tasks.append(task(n))
                if len(taskhome.tasks) > 50:
                    del taskhome.tasks[:25]
            n += 1

    def save():
        try:
            for _ in range(200):
                taskhome.save_tasks()
        except Exception as e:  # noqa: BLE001 - recording it is the point
            errors.append(e)

    writer = threading.Thread(target=mutate, daemon=True)
    writer.start()
    try:
        save()
    finally:
        stop.set()
        writer.join(timeout=5)

    assert errors == []
    # And the file on disk is always complete, parseable JSON.
    assert isinstance(json.loads((store / 'tasks.json').read_text()), list)


def test_concurrent_history_writes_lose_nothing(store):
    """record_history from several threads drops and duplicates nothing."""
    threads = [
        threading.Thread(target=lambda i=i: [
            taskhome.record_history({'id': f'{i}-{j}', 'type': 'task'})
            for j in range(20)
        ])
        for i in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(taskhome.history) == 100
    assert len({record['id'] for record in taskhome.history}) == 100


def test_history_cap_holds_under_concurrency(store):
    taskhome.config['max_history'] = 30
    threads = [
        threading.Thread(target=lambda i=i: [
            taskhome.record_history({'id': f'{i}-{j}', 'type': 'task'})
            for j in range(40)
        ])
        for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(taskhome.history) == 30


def test_lock_is_reentrant(store):
    """save_* is called from inside already-locked sections (record_history
    does exactly this), so a non-reentrant Lock would deadlock. This one is
    load-bearing: swapping RLock for Lock hangs the suite."""
    with taskhome.STATE_LOCK:
        with taskhome.STATE_LOCK:
            assert taskhome.save_tasks() is True


def test_clear_history_mutates_in_place(store):
    """Rebinding the global would detach it from lists other code already
    holds, sending their writes to an orphan."""
    taskhome.history.extend([{'id': 'a', 'type': 'task'}])
    held = taskhome.history

    taskhome.app.config['TESTING'] = True
    with taskhome.app.test_client() as client:
        client.post('/settings', data={'clear_history': '1'})

    assert taskhome.history is held
    assert held == []
