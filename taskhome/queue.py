"""Durable print queue (MASTER_PLAN P6-3).

`P0-4` stopped the scheduler advancing past a failed print, so an offline
printer delays receipts rather than destroying them. That fix lives in memory:
a task stays due and retries next tick. It does not survive a restart, and it
does nothing at all for SeeClickFix issues, whose window has already moved on.

This closes the gap. A print becomes a queued job on disk, drained by the
scheduler. Nothing is lost to a power cut, a service restart, or a printer left
unplugged over a weekend.

Design notes:

* The queue holds **rendered blocks**, not a task id. By the time a job is
  drained the task may have been edited or deleted, and reprinting it with
  today's settings would be a different receipt from the one that was due.
  Rendering at enqueue time freezes what was meant.
* Jobs retry with backoff and are eventually **parked**, not dropped. A job
  that cannot print is a thing the owner needs to know about; silently
  discarding it after N attempts would recreate the bug this replaces.
* The queue is capped. An appliance left printerless for a month should not
  accumulate an unbounded file, and 500 receipts of backlog is already far
  past useful.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

from . import constants, receipt, storage
from .logsetup import log

QUEUE_FILENAME = 'queue.json'
MAX_QUEUE = 500
MAX_ATTEMPTS = 8          # ~4 hours of backoff before parking
MAX_BACKOFF_MINUTES = 30


def queue_path():
    return os.path.join(constants.DATA_DIR, QUEUE_FILENAME)


def load_queue():
    value, ok = storage._load_json_file('queue', queue_path(), [])
    if not ok or not isinstance(value, list):
        return []
    return value


def save_queue(jobs):
    return storage._save_json_file('queue', queue_path(), jobs)


def enqueue(kind, blocks, description='', history=None):
    """Add a rendered job to the queue. Returns the job.

    `history` is the record to write once it genuinely prints -- history is the
    record of paper that exists, so it is not written at enqueue time.
    """
    jobs = load_queue()
    job = {
        'id': str(uuid.uuid4()),
        'kind': kind,
        'description': description,
        'blocks': blocks,
        'history': history,
        'queued_at': datetime.now(timezone.utc).isoformat(),
        'attempts': 0,
        'next_attempt': None,
        'last_error': None,
        'parked': False,
    }
    jobs.append(job)

    if len(jobs) > MAX_QUEUE:
        # Drop the oldest, and say so. Silent truncation would misrepresent
        # what happened to a receipt someone was expecting.
        dropped = len(jobs) - MAX_QUEUE
        jobs = jobs[dropped:]
        log.error(f"Print queue exceeded {MAX_QUEUE}; dropped {dropped} oldest job(s)")

    save_queue(jobs)
    log.info(f"Queued {kind} print: {description or job['id']} ({len(jobs)} waiting)")
    return job


def _due(job, now):
    if job.get('parked'):
        return False
    nxt = job.get('next_attempt')
    if not nxt:
        return True
    try:
        return datetime.fromisoformat(nxt) <= now
    except (TypeError, ValueError):
        return True


def backoff_minutes(attempts):
    return min(2 ** max(attempts - 1, 0), MAX_BACKOFF_MINUTES)


def drain(printer_fn=None, now=None):
    """Attempt every due job, oldest first. Returns (printed, remaining).

    Stops at the first failure. The queue is ordered, and pushing past a job
    that just failed would print later receipts before earlier ones -- for a
    stack of paper someone reads top-down, order is the whole point.
    """
    from . import printing
    printer_fn = printer_fn or printing.print_blocks
    now = now or datetime.now(timezone.utc)

    jobs = load_queue()
    if not jobs:
        return 0, 0

    printed = 0
    changed = False
    remaining = []
    halted = False

    for job in jobs:
        if halted or not _due(job, now):
            remaining.append(job)
            continue

        if printer_fn(job['blocks']):
            printed += 1
            changed = True
            if job.get('history'):
                printing.record_history(job['history'])
            log.info(f"Printed queued job: {job.get('description') or job['id']}")
            continue

        job['attempts'] = job.get('attempts', 0) + 1
        job['last_error'] = 'printer unavailable'
        changed = True
        if job['attempts'] >= MAX_ATTEMPTS:
            # Parked, not dropped: someone needs to know a receipt never
            # printed, and deciding for them is how receipts got lost before.
            job['parked'] = True
            log.error(
                f"Parking print job after {job['attempts']} attempts: "
                f"{job.get('description') or job['id']}. It will not retry until "
                f"released from the queue page.")
        else:
            delay = backoff_minutes(job['attempts'])
            job['next_attempt'] = (now + timedelta(minutes=delay)).isoformat()
            log.warning(
                f"Print failed ({job['attempts']}/{MAX_ATTEMPTS}), retrying in "
                f"{delay}m: {job.get('description') or job['id']}")
        remaining.append(job)
        halted = True     # preserve order

    if changed:
        save_queue(remaining)
    return printed, len(remaining)


def stats():
    """Counts for the UI."""
    jobs = load_queue()
    return {
        'total': len(jobs),
        'parked': sum(1 for j in jobs if j.get('parked')),
        'waiting': sum(1 for j in jobs if not j.get('parked')),
        'oldest': jobs[0]['queued_at'] if jobs else None,
    }


def release_parked():
    """Un-park everything, so a fixed printer can drain the backlog."""
    jobs = load_queue()
    released = 0
    for job in jobs:
        if job.get('parked'):
            job['parked'] = False
            job['attempts'] = 0
            job['next_attempt'] = None
            released += 1
    if released:
        save_queue(jobs)
        log.info(f"Released {released} parked print job(s)")
    return released


def discard(job_id=None):
    """Remove one job, or every job when no id is given."""
    jobs = load_queue()
    if job_id is None:
        save_queue([])
        return len(jobs)
    remaining = [j for j in jobs if j.get('id') != job_id]
    removed = len(jobs) - len(remaining)
    if removed:
        save_queue(remaining)
    return removed


def describe(job):
    """A one-line summary for the queue page."""
    if job.get('description'):
        return job['description']
    for block in job.get('blocks', []):
        if block.get('type') == 'text' and block.get('value'):
            return str(block['value'])[:60]
    return job.get('kind', 'receipt')


def estimated_paper_mm(jobs=None):
    """How much paper the backlog represents, so the number means something."""
    jobs = load_queue() if jobs is None else jobs
    return round(sum(receipt.height_mm(j.get('blocks') or []) for j in jobs))
