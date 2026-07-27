"""Recurrence maths and catch-up policy.

The invariant that matters: `calculate_next` returns its input unchanged to
mean "cannot advance", and `advance_schedule` refuses to loop on that. The two
hangs this replaced (a missed one-off, a custom rule with no weekdays) were
both instances of iterating without advancing, so the guard targets that
property rather than the two symptoms.
"""
from datetime import datetime, timedelta

from dateutil.relativedelta import relativedelta

from . import constants, printing, state, storage
from .logsetup import log


def calculate_next(next_time_str, recurring, days=None):
    """Return the next occurrence after next_time_str, as a naive local ISO string.

    Returns the input UNCHANGED when it cannot advance (unknown recurrence,
    'none', or a weekday rule that matches nothing). Callers must treat an
    unchanged return as "no next occurrence" and must never loop on it — see
    advance_schedule, which enforces that. Weekday searches are bounded to a
    single week: a rule that hasn't matched in 7 days never will (P0-2).
    """
    # Deliberately not logged per call: advance_schedule can invoke this
    # hundreds of times catching up a long-overdue task, and the noise
    # buried everything else (P1-5). advance_schedule logs the summary.
    next_time = datetime.fromisoformat(next_time_str)
    if recurring == 'daily':
        return (next_time + timedelta(days=1)).isoformat()
    elif recurring == 'weekly':
        return (next_time + timedelta(days=7)).isoformat()
    elif recurring == 'monthly':
        return (next_time + relativedelta(months=1)).isoformat()
    elif recurring == 'every_weekday':
        for _ in range(7):
            next_time += timedelta(days=1)
            if next_time.weekday() < 5:
                return next_time.isoformat()
        return next_time_str  # unreachable: some day in any 7 is a weekday
    elif recurring == 'first_day_month':
        return (next_time + relativedelta(months=1, day=1)).isoformat()
    elif recurring == 'custom':
        valid_days = {d for d in (days or []) if isinstance(d, int) and 0 <= d <= 6}
        if not valid_days:
            log.error(
                f"Custom recurrence with no valid days ({days!r}); cannot advance")
            return next_time_str
        for _ in range(7):
            next_time += timedelta(days=1)
            if next_time.weekday() in valid_days:
                return next_time.isoformat()
        return next_time_str
    return next_time_str


class ScheduleError(Exception):
    """A task's recurrence cannot be advanced. Never raised into the caller's
    loop condition — always caught per-task so one bad task can't stall the
    scheduler (P0-6)."""


def parse_task_time(value):
    """Parse a task timestamp into a naive local datetime.

    Task times are naive local wall-clock (see docs/scheduling.md). Aware
    values that somehow reach us are converted to local and stripped, so
    comparisons stay in one frame (P0-3).
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def get_catchup_config():
    """Catch-up settings merged over defaults, with invalid values dropped.

    Never raises: a malformed state.config degrades to defaults with a warning
    rather than taking down the scheduler (X-4).
    """
    resolved = dict(constants.DEFAULT_CATCHUP)
    stored = state.config.get('catchup')
    if isinstance(stored, dict):
        for key, value in stored.items():
            if key in resolved:
                resolved[key] = value

    if resolved['policy'] not in constants.CATCHUP_POLICIES:
        log.warning(
            f"Invalid catchup.policy {resolved['policy']!r}, using {constants.DEFAULT_CATCHUP['policy']!r}")
        resolved['policy'] = constants.DEFAULT_CATCHUP['policy']
    if resolved['oneoff_policy'] not in constants.CATCHUP_POLICIES:
        log.warning(
            f"Invalid catchup.oneoff_policy {resolved['oneoff_policy']!r}, "
            f"using {constants.DEFAULT_CATCHUP['oneoff_policy']!r}")
        resolved['oneoff_policy'] = constants.DEFAULT_CATCHUP['oneoff_policy']
    for key in ('recent_window_hours', 'max_prints'):
        try:
            resolved[key] = int(resolved[key])
        except (TypeError, ValueError):
            log.warning(f"Invalid catchup.{key} {resolved[key]!r}, using default")
            resolved[key] = constants.DEFAULT_CATCHUP[key]
        if resolved[key] < 0:
            log.warning(f"Negative catchup.{key}, using default")
            resolved[key] = constants.DEFAULT_CATCHUP[key]
    return resolved


def resolve_catchup_policy(task, catchup_config=None):
    """Which catch-up policy applies to this task. First match wins:
    explicit per-task setting, then the one-off default, then the global.
    """
    cfg = catchup_config or get_catchup_config()
    explicit = task.get('catchup', 'inherit')
    if explicit != 'inherit':
        if explicit in constants.CATCHUP_POLICIES:
            return explicit
        log.warning(
            f"Task {task.get('id')} has invalid catchup {explicit!r}; falling back to inherit")
    if task.get('recurring') == 'none':
        return cfg['oneoff_policy']
    return cfg['policy']


def advance_schedule(task, now, max_iterations=4096):
    """Roll a task's schedule forward past `now`.

    Returns (next_time_str_or_None, missed) where `missed` lists the
    occurrences stepped over, oldest first, and None means the task has no
    future occurrence (a one-off that has already come due).

    Guarantees it never iterates without advancing — the fix for P0-1 and the
    backstop for any future recurrence mode. Raises ScheduleError instead of
    spinning.
    """
    recurring = task.get('recurring', 'none')
    current_str = task['next_time']
    current = parse_task_time(current_str)

    if current > now:
        return current_str, []
    if recurring == 'none':
        return None, [current_str]

    missed = []
    for _ in range(max_iterations):
        candidate_str = calculate_next(current_str, recurring, task.get('days'))
        if candidate_str == current_str:
            raise ScheduleError(
                f"recurrence {recurring!r} did not advance from {current_str}")
        candidate = parse_task_time(candidate_str)
        if candidate <= current:
            raise ScheduleError(
                f"recurrence {recurring!r} moved backwards: {current_str} -> {candidate_str}")
        missed.append(current_str)
        current_str, current = candidate_str, candidate
        if current > now:
            if len(missed) > 1:
                log.debug(
                    f"Advanced {recurring} schedule over {len(missed)} occurrence(s): "
                    f"{missed[0]} -> {current_str}")
            return current_str, missed
    raise ScheduleError(
        f"recurrence {recurring!r} exceeded {max_iterations} steps from {task['next_time']}")


def select_catchup_prints(missed, policy, cfg, now):
    """Which missed occurrences to print, and how many were dropped.

    Returns (occurrences, dropped). For print_once the caller emits a single
    summary receipt rather than one per occurrence.
    """
    if policy == 'skip' or not missed:
        return [], 0
    if policy == 'print_once':
        return missed[-1:], 0

    candidates = missed
    if policy == 'print_if_recent':
        cutoff = now - timedelta(hours=cfg['recent_window_hours'])
        candidates = [occ for occ in missed if parse_task_time(occ) >= cutoff]

    cap = cfg['max_prints']
    if len(candidates) > cap:
        # Keep the most recent; they are the ones still worth acting on.
        return candidates[-cap:], len(candidates) - cap
    return candidates, 0


def _format_occurrence(occurrence):
    try:
        return parse_task_time(occurrence).strftime('%a %b %d, %I:%M %p')
    except (ValueError, TypeError):
        return str(occurrence)


def _with_note(task, note):
    """Copy of the task with `note` appended to its extra line, so catch-up
    receipts are visibly distinct from on-time ones."""
    copy = dict(task)
    existing = copy.get('extra')
    copy['extra'] = f"{existing}\n{note}" if existing else note
    copy['catchup'] = True
    return copy


def print_catchup(task, occurrences, missed_total, dropped, policy):
    """Emit receipts for missed occurrences per the resolved policy."""
    if policy == 'print_once':
        note = (f"MISSED {missed_total}x while offline"
                f" - most recent {_format_occurrence(occurrences[0])}")
        printing.print_task(_with_note(task, note))
        return
    for occurrence in occurrences:
        printing.print_task(_with_note(task, f"MISSED occurrence - was due {_format_occurrence(occurrence)}"))
    if dropped:
        # Never truncate silently (X-4).
        printing.print_task(_with_note(
            task, f"... and {dropped} older missed occurrence(s) not printed"))


def apply_catchup(task, now, catchup_config=None):
    """Bring one task up to date. Returns True if the task was modified.

    Raises ScheduleError if the recurrence is unusable; callers isolate that
    per task.
    """
    cfg = catchup_config or get_catchup_config()
    next_time, missed = advance_schedule(task, now)
    if not missed:
        return False

    policy = resolve_catchup_policy(task, cfg)
    occurrences, dropped = select_catchup_prints(missed, policy, cfg, now)
    log.info(
        f"Catch-up for task {task.get('id')}: {len(missed)} missed, policy={policy}, "
        f"printing {len(occurrences)}, dropping {dropped}")
    if occurrences:
        print_catchup(task, occurrences, len(missed), dropped, policy)

    task['missed_count'] = task.get('missed_count', 0) + len(missed)
    task['last_missed_at'] = missed[-1]
    if next_time is None:
        # A one-off with no future occurrence. Leaving it enabled would make
        # the steady-state loop fire it immediately, contradicting the policy;
        # mark it missed so the UI can show it instead of it vanishing.
        task['enabled'] = False
        task['missed'] = True
    else:
        task['next_time'] = next_time
    return True


def run_catchup(now=None):
    """Startup catch-up across all enabled state.tasks. Returns state.tasks changed."""
    now = now or datetime.now()
    cfg = get_catchup_config()
    changed = 0
    for task in list(state.tasks):
        if not task.get('enabled', True):
            continue
        try:
            if apply_catchup(task, now, cfg):
                changed += 1
        except (ScheduleError, ValueError) as e:
            # Disable rather than leave a task that can never advance or whose
            # next_time can't be parsed: it would otherwise be retried, and
            # logged, every tick forever.
            task['enabled'] = False
            task['schedule_error'] = str(e)
            changed += 1
            log.error(f"Disabling task {task.get('id')} - {e}")
        except Exception as e:
            log.error(f"Catch-up failed for task {task.get('id')}: {e}", exc_info=True)
    if changed:
        storage.save_tasks()
    return changed
