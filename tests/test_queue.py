"""Durable print queue (MASTER_PLAN P6-3).

P0-4 stopped the scheduler advancing past a failed print, so an offline printer
delays receipts rather than destroying them -- but that fix lives in memory. It
does not survive a restart, and it does nothing for SeeClickFix issues whose
window has already moved on. This is the durable half.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from taskhome import constants, printing, queue, state, storage


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, 'APP_ROOT', str(tmp_path / 'repo'))
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(constants, 'HISTORY_FILE', str(tmp_path / 'history.json'))
    monkeypatch.setattr(state, 'history', [])
    monkeypatch.setattr(state, 'config', dict(constants.DEFAULT_CONFIG))
    monkeypatch.setattr(storage, 'save_history', lambda: True)
    state.load_failed.clear()
    yield tmp_path
    state.load_failed.clear()


T0 = datetime(2026, 3, 5, 9, 0, tzinfo=timezone.utc)


def blocks(text='hello'):
    from taskhome import receipt
    return [receipt.text(text)]


def always_fails(_blocks):
    return False


def always_works(_blocks):
    return True


# --- enqueue ------------------------------------------------------------------

def test_enqueue_persists_to_disk(store):
    queue.enqueue('task', blocks(), description='Take Medicine')
    saved = json.loads((store / 'queue.json').read_text())
    assert len(saved) == 1
    assert saved[0]['description'] == 'Take Medicine'


def test_queue_survives_a_restart(store):
    """The whole point: in-memory retry does not."""
    queue.enqueue('task', blocks(), description='Feed cat')
    reloaded = queue.load_queue()
    assert [j['description'] for j in reloaded] == ['Feed cat']


def test_queue_stores_rendered_blocks_not_a_reference(store):
    """By drain time the task may have been edited or deleted; reprinting it
    from today's settings would be a different receipt from the one due."""
    job = queue.enqueue('task', blocks('frozen text'))
    assert job['blocks'][0]['value'] == 'frozen text'


def test_queue_is_capped_and_says_so(store, monkeypatch):
    monkeypatch.setattr(queue, 'MAX_QUEUE', 3)
    for n in range(6):
        queue.enqueue('task', blocks(f'job {n}'))
    jobs = queue.load_queue()
    assert len(jobs) == 3
    assert jobs[0]['blocks'][0]['value'] == 'job 3'   # oldest dropped


# --- draining -----------------------------------------------------------------

def test_drain_prints_and_clears(store):
    queue.enqueue('task', blocks())
    printed, remaining = queue.drain(printer_fn=always_works)
    assert (printed, remaining) == (1, 0)
    assert queue.load_queue() == []


def test_drain_writes_history_only_on_success(store):
    queue.enqueue('task', blocks(), history={'id': 'x', 'type': 'task'})
    queue.drain(printer_fn=always_fails, now=T0)
    assert state.history == [], 'history recorded for a receipt that never printed'
    # Past the backoff window.
    queue.drain(printer_fn=always_works, now=T0 + timedelta(hours=1))
    assert len(state.history) == 1


def test_failure_schedules_a_retry(store):
    queue.enqueue('task', blocks())
    queue.drain(printer_fn=always_fails)
    job = queue.load_queue()[0]
    assert job['attempts'] == 1
    assert job['next_attempt'] is not None


def test_backoff_grows_and_is_capped():
    delays = [queue.backoff_minutes(n) for n in range(1, 10)]
    assert delays == sorted(delays)
    assert max(delays) == queue.MAX_BACKOFF_MINUTES


def test_drain_preserves_order_by_stopping_at_a_failure(store):
    """A stack of paper is read top-down; printing later receipts before
    earlier ones would scramble it."""
    queue.enqueue('task', blocks('first'))
    queue.enqueue('task', blocks('second'))

    attempted = []

    def fail_all(b):
        attempted.append(b[0]['value'])
        return False

    queue.drain(printer_fn=fail_all)
    assert attempted == ['first'], 'drain pushed past a failed job'


def test_jobs_park_rather_than_being_dropped(store, monkeypatch):
    """Silently discarding after N attempts would recreate the very bug this
    replaces."""
    monkeypatch.setattr(queue, 'MAX_ATTEMPTS', 2)
    queue.enqueue('task', blocks())
    # Each drain an hour later, so every retry is genuinely due.
    for hour in range(4):
        queue.drain(printer_fn=always_fails, now=T0 + timedelta(hours=hour))

    jobs = queue.load_queue()
    assert len(jobs) == 1, 'the job was dropped instead of parked'
    assert jobs[0]['parked'] is True


def test_parked_jobs_are_not_retried(store, monkeypatch):
    monkeypatch.setattr(queue, 'MAX_ATTEMPTS', 1)
    queue.enqueue('task', blocks())
    queue.drain(printer_fn=always_fails, now=T0)
    attempted = []
    queue.drain(printer_fn=lambda b: attempted.append(1) or True,
                now=T0 + timedelta(days=1))
    assert attempted == [], 'a parked job was retried'


def test_releasing_parked_jobs_lets_them_print(store, monkeypatch):
    monkeypatch.setattr(queue, 'MAX_ATTEMPTS', 1)
    queue.enqueue('task', blocks())
    queue.drain(printer_fn=always_fails, now=T0)
    assert queue.release_parked() == 1
    printed, remaining = queue.drain(printer_fn=always_works)
    assert (printed, remaining) == (1, 0)


def test_a_job_not_yet_due_is_skipped(store):
    queue.enqueue('task', blocks())
    queue.drain(printer_fn=always_fails, now=T0)       # sets next_attempt
    attempted = []
    queue.drain(printer_fn=lambda b: attempted.append(1) or True,
                now=T0 + timedelta(seconds=1))
    assert attempted == [], 'retried before its backoff elapsed'


# --- integration with the print path ------------------------------------------

def test_offline_task_print_is_not_queued(store, monkeypatch):
    """This test asserted the opposite until it was found to encode a bug.

    A task already retries durably -- the scheduler leaves next_time alone on
    failure (P0-4) and persists it -- so queueing the receipt as well gave the
    occurrence two retry mechanisms. When the printer came back, the queue
    drained the receipt and the still-due task printed a second copy.
    """
    monkeypatch.setattr(printing, 'is_printer_connected', lambda: False)
    task = {'id': 'x', 'title': 'Take Medicine', 'recurring': 'daily',
            'next_time': '2026-03-01T09:00:00', 'enabled': True}
    assert printing.print_task(task) is False
    assert queue.load_queue() == []


def test_offline_scf_print_is_queued(store, monkeypatch):
    monkeypatch.setattr(printing, 'is_printer_connected', lambda: False)
    issue = {'id': 42, 'html_url': 'https://x/42', 'request_type': {'title': 'Pothole'},
             'address': '1 Main St', 'created_at': '2026-03-01T09:00:00Z', 'status': 'Open'}
    assert printing.print_scf_issue(issue) is False
    assert 'Pothole' in queue.load_queue()[0]['description']


def test_a_successful_print_is_not_queued(store, monkeypatch):
    monkeypatch.setattr(printing, 'print_blocks', lambda b: True)
    task = {'id': 'x', 'title': 'T', 'recurring': 'daily',
            'next_time': '2026-03-01T09:00:00', 'enabled': True}
    assert printing.print_task(task) is True
    assert queue.load_queue() == []


# --- reporting ----------------------------------------------------------------

def test_stats_separate_waiting_from_parked(store, monkeypatch):
    monkeypatch.setattr(queue, 'MAX_ATTEMPTS', 1)
    queue.enqueue('task', blocks('a'))
    queue.drain(printer_fn=always_fails, now=T0)
    queue.enqueue('task', blocks('b'))
    s = queue.stats()
    assert s['total'] == 2 and s['parked'] == 1 and s['waiting'] == 1


def test_paper_estimate_is_reported(store):
    """A backlog count means little; centimetres of paper mean something."""
    queue.enqueue('task', blocks())
    assert queue.estimated_paper_mm() > 0


def test_discard_removes_one_or_all(store):
    a = queue.enqueue('task', blocks('a'))
    queue.enqueue('task', blocks('b'))
    assert queue.discard(a['id']) == 1
    assert len(queue.load_queue()) == 1
    assert queue.discard() == 1
    assert queue.load_queue() == []


def test_corrupt_queue_file_does_not_crash(store):
    (store / 'queue.json').write_text('{not json')
    assert queue.load_queue() == []


def test_a_failed_task_print_is_not_queued(tmp_path, monkeypatch):
    """A task has a durable retry already -- the scheduler leaves next_time
    alone and saves it (P0-4). Queueing as well gave it two retry mechanisms,
    and when the printer came back the queue drained the receipt AND the
    still-due task printed it again.
    """
    from taskhome import constants, printing, queue
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(printing, 'print_blocks', lambda blocks: False)

    assert printing.print_task({'id': 'a', 'title': 'Bins', 'recurring': 'none'}) is False
    assert queue.load_queue() == [], 'a task print was queued as well as left due'


def test_a_failed_scf_print_is_queued(tmp_path, monkeypatch):
    """The counterpart. A listener's polling window has already moved past the
    issue, so without the queue the receipt is simply gone."""
    from taskhome import constants, printing, queue
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(printing, 'print_blocks', lambda blocks: False)

    printing.print_scf_issue({'id': 7, 'address': 'Elm St', 'status': 'Open',
                              'request_type': {'title': 'Pothole'}})
    assert len(queue.load_queue()) == 1
