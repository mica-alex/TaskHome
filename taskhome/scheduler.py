"""The scheduler thread: catch-up on start, then fire due tasks and poll
listeners every 60 seconds.

Every task is isolated in its own try, so one unusable task cannot abort the
pass or the listener poll (P0-6), and a task that cannot be advanced or parsed
is disabled with the reason recorded rather than retried forever.
"""
import threading
import time
from datetime import datetime, timezone

from . import printing, recurrence, state, storage
from .listeners import scf
from .logsetup import log


def fire_due_task(task, now, catchup_config=None):
    """Print a task if it is due and reschedule it. Returns True if it fired.

    The schedule advances ONLY on a successful print (P0-4). Previously an
    offline printer silently dropped the occurrence and moved on, so a task
    due while the printer was unplugged was lost forever. Now it stays due and
    retries each tick until the printer comes back.
    """
    if recurrence.parse_task_time(task['next_time']) > now:
        return False

    if not printing.print_task(task):
        # Leave next_time alone so the occurrence is retried, and record the
        # failure so the UI can show "waiting for printer" rather than looking
        # stuck for no visible reason.
        task['print_failures'] = task.get('print_failures', 0) + 1
        task['last_print_failure'] = now.isoformat()
        log.warning(
            f"Print failed for task {task.get('id')}; "
            f"leaving it due (attempt {task['print_failures']})")
        return False

    task.pop('print_failures', None)
    task.pop('last_print_failure', None)

    next_time, missed = recurrence.advance_schedule(task, now)
    # missed[0] is the occurrence just printed; anything after it came due
    # while the printer was offline and is governed by the catch-up policy.
    extra = missed[1:] if missed else []
    if extra:
        cfg = catchup_config or recurrence.get_catchup_config()
        policy = recurrence.resolve_catchup_policy(task, cfg)
        occurrences, dropped = recurrence.select_catchup_prints(extra, policy, cfg, now)
        log.info(
            f"Task {task.get('id')} recovered with {len(extra)} further missed "
            f"occurrence(s), policy={policy}, printing {len(occurrences)}")
        if occurrences:
            recurrence.print_catchup(task, occurrences, len(extra), dropped, policy)
        task['missed_count'] = task.get('missed_count', 0) + len(extra)
        task['last_missed_at'] = extra[-1]

    if next_time is None:
        with state.STATE_LOCK:
            if task in state.tasks:
                state.tasks.remove(task)
    else:
        task['next_time'] = next_time
    return True


def run_due_tasks(now):
    """Fire every due task. Each task is isolated: one unusable task cannot
    stall the others or the listener poll (P0-6). Returns state.tasks fired."""
    fired = 0
    changed = 0
    cfg = recurrence.get_catchup_config()
    for task in list(state.tasks):
        if not task.get('enabled', True):
            continue
        failures_before = task.get('print_failures')
        try:
            if fire_due_task(task, now, cfg):
                fired += 1
                changed += 1
            elif task.get('print_failures') != failures_before:
                changed += 1  # a failed print still needs persisting
        except (recurrence.ScheduleError, ValueError) as e:
            task['enabled'] = False
            task['schedule_error'] = str(e)
            changed += 1
            log.error(f"Disabling task {task.get('id')} - {e}")
        except Exception as e:
            log.error(
                f"Error firing task {task.get('id')}: {e}", exc_info=True)
    if changed:
        storage.save_tasks()
    return fired


def scheduler_loop():
    # Times are naive local wall-clock throughout (P0-3): the catch-up and the
    # steady-state loop must compare in the same frame as the stored values.
    recurrence.run_catchup(datetime.now())

    while True:
        log.debug("Scheduler loop iteration started")
        try:
            now = datetime.now()
            now_utc = datetime.now(timezone.utc)
            run_due_tasks(now)

            scf.poll_scf_listener(now_utc)
        except Exception as e:
            log.error(f"Scheduler loop error: {e}", exc_info=True)

        # Sleep for a minute before next check
        log.debug("Scheduler loop iteration complete, sleeping for 60 seconds")
        time.sleep(60)


_thread = None


def start_scheduler():
    """Start the scheduler thread, refusing to start a second one.

    The app factory only calls this when explicitly asked (P0-12), so the
    common accidental cases -- importing the package, running tests, a
    reloader importing twice -- cannot start a thread that prints. This guard
    covers the remaining case of two calls within one process.
    """
    global _thread
    if _thread is not None and _thread.is_alive():
        log.warning("Scheduler already running; not starting another")
        return _thread
    _thread = threading.Thread(target=scheduler_loop, daemon=True)
    _thread.start()
    log.info("Scheduler thread started")
    return _thread
