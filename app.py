import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from dateutil import parser

import usb.core
from dateutil.relativedelta import relativedelta
from escpos.printer import Usb
from flask import Flask, render_template, request, redirect, url_for
import requests  # New import for API calls

app = Flask(__name__)
app.logger.setLevel('DEBUG')  # Set to DEBUG for detailed logs

# Constants
DEFAULT_HOST = '0.0.0.0'
DEFAULT_PORT = 5000
VID = 0x04b8
PID = 0x0e27
# Where mutable state lives (P1-9). Anchored to the repo root rather than the
# process CWD, so TaskHome no longer has to be started from a particular
# directory to find its own data. TASKHOME_DATA_DIR overrides it -- tests and
# any throwaway run should set it rather than pointing at the real files.
APP_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('TASKHOME_DATA_DIR') or os.path.join(APP_ROOT, 'data')

STORE_FILENAMES = {
    'config': 'config.json',
    'tasks': 'tasks.json',
    'history': 'history.json',
    'listeners': 'listeners.json',
}


def data_path(filename):
    return os.path.join(DATA_DIR, filename)


CONFIG_FILE = data_path('config.json')
TASKS_FILE = data_path('tasks.json')
HISTORY_FILE = data_path('history.json')
LISTENERS_FILE = data_path('listeners.json')  # New file for listener configs

# Catch-up policy (P1-10): what happens to occurrences that came due while
# TaskHome was down. Resolution order is per-task 'catchup', then
# oneoff_policy for one-offs, then policy. See docs/scheduling.md.
RECURRENCE_MODES = ('none', 'daily', 'weekly', 'monthly', 'every_weekday',
                    'first_day_month', 'custom')
THEMES = ('system', 'light', 'dark')

CATCHUP_POLICIES = ('skip', 'print_once', 'print_all', 'print_if_recent')
DEFAULT_CATCHUP = {
    'policy': 'skip',              # recurring: losing one of many occurrences
    'oneoff_policy': 'print_once',  # one-off: skipping means it NEVER prints
    'recent_window_hours': 6,
    'max_prints': 20,
}

PRINTER_MANUFACTURER = 'Epson'
PRINTER_MODEL = 'TM-T20III'
PRINTER_CONNECTION = 'USB'

# Global data
DEFAULT_CONFIG = {'max_history': 500, 'hostname': 'localhost', 'theme': 'system'}
config = dict(DEFAULT_CONFIG)
tasks = []
history = []
listeners = {}  # New: e.g., {'scf': {'enabled': False, 'request_types': '6632,6634', 'interval': 10, 'last_check': None}}


# Guards structural mutation of the shared state and its serialisation.
# Deliberately NOT held across printing or HTTP fetches: those take seconds and
# would stall every page load. It protects the operations that can actually
# corrupt state -- appending/removing/rebinding, and json.dump reading a list
# while another thread mutates it. Reentrant because save_* is called from
# inside already-locked sections.
STATE_LOCK = threading.RLock()

# Stores whose load failed. Saving one of these would overwrite a file that is
# damaged but still recoverable by hand, turning a fixable problem into
# permanent data loss (P0-5). Saves for these names are refused.
_load_failed = set()


def migrate_legacy_data_files(legacy_dir=None, data_dir=None):
    """Move root-level JSON state into data/ (P1-9).

    TaskHome has historically been run straight out of a git clone with its
    four JSON files in the repo root, so existing installs must keep working
    across this change without anyone having to move files by hand.

    Idempotent. Returns a list of human-readable actions taken.

    Deliberate choices about the awkward cases:
      * Each file moves independently, so a failure part-way leaves the rest
        correct rather than half-migrated in an unpredictable way.
      * If a file exists in BOTH places we do not guess which is current. The
        data/ copy wins (it is what the app will read) and the legacy file is
        renamed aside rather than deleted, so nothing is destroyed.
      * A read-only or unwritable filesystem is reported and the app continues
        against the legacy location instead of refusing to start.
      * os.replace is atomic within a filesystem; a cross-device move falls
        back to copy-then-remove, which keeps the source until the copy lands.
    """
    legacy_dir = legacy_dir or APP_ROOT
    data_dir = data_dir or DATA_DIR
    actions = []

    pending = [
        (name, os.path.join(legacy_dir, filename), os.path.join(data_dir, filename))
        for name, filename in STORE_FILENAMES.items()
    ]
    if not any(os.path.exists(legacy) for _, legacy, _ in pending):
        return actions  # nothing to migrate; the common case after first run

    try:
        os.makedirs(data_dir, exist_ok=True)
    except OSError as e:
        app.logger.error(
            f"Cannot create {data_dir}: {e}. Continuing with the legacy "
            f"location; state will stay in {legacy_dir}.")
        return actions

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    for name, legacy, target in pending:
        if not os.path.exists(legacy):
            continue
        if os.path.exists(target):
            aside = f"{legacy}.superseded-{stamp}"
            try:
                os.replace(legacy, aside)
                actions.append(f"{name}: kept data/ copy, moved legacy file to {aside}")
                app.logger.warning(
                    f"{name} exists in both {legacy_dir} and {data_dir}. Using the "
                    f"data/ copy; the old one is preserved at {aside}.")
            except OSError as e:
                app.logger.error(f"Could not set aside legacy {name}: {e}")
            continue
        try:
            os.replace(legacy, target)
            actions.append(f"{name}: moved into {data_dir}")
        except OSError:
            # Different filesystem, or replace unsupported. Copy first, and
            # only unlink the source once the copy is safely in place.
            try:
                shutil.copy2(legacy, target)
                os.unlink(legacy)
                actions.append(f"{name}: copied into {data_dir}")
            except OSError as e:
                app.logger.error(
                    f"Failed to migrate {name} from {legacy} to {target}: {e}")

    if actions:
        app.logger.warning(
            f"Migrated state into {data_dir} (P1-9): " + '; '.join(actions))
        try:
            with open(os.path.join(legacy_dir, 'DATA_MOVED.txt'), 'w') as f:
                f.write(
                    "TaskHome state moved to the data/ directory.\n\n"
                    f"Migrated {stamp}. Files now live in:\n  {data_dir}\n\n"
                    "This file is only a breadcrumb and can be deleted.\n"
                    "Set TASKHOME_DATA_DIR to use a different location.\n")
        except OSError:
            pass  # breadcrumb is a nicety, never a failure
    return actions


def _load_json_file(name, path, default):
    """Load one JSON file. Returns (value, ok).

    A missing file is fine — it yields the default and ok=True. A file that
    exists but won't parse is NOT fine: it returns ok=False so that saves are
    blocked rather than silently overwriting the user's data with defaults.
    """
    if not os.path.exists(path):
        app.logger.warning(f"{name} file not found: {path}")
        return default, True
    try:
        with open(path, 'r') as f:
            return json.load(f), True
    except (json.JSONDecodeError, OSError) as e:
        app.logger.error(
            f"FAILED to load {name} from {path}: {e}. "
            f"Refusing to overwrite it; fix or remove the file and restart. "
            f"A copy of the current in-memory state will NOT be saved over it.")
        _load_failed.add(name)
        return default, False


def load_data():
    global config, tasks, history, listeners
    app.logger.debug("Entering load_data")
    _load_failed.clear()

    # Must run before anything reads or writes: it decides where the files are.
    migrate_legacy_data_files()
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except OSError as e:
        app.logger.error(f"Cannot create data directory {DATA_DIR}: {e}")

    loaded_config, ok = _load_json_file('config', os.path.abspath(CONFIG_FILE), None)
    if ok and loaded_config is not None:
        # Merge over defaults rather than replacing, so a file missing a key
        # can't break code that reads config['hostname'] (P1-6).
        merged = dict(DEFAULT_CONFIG)
        if isinstance(loaded_config, dict):
            merged.update(loaded_config)
        if merged.get('theme') == 'high-contrast':
            merged['theme'] = 'system'
            app.logger.debug("Converted high-contrast theme to system")
        config = merged
        app.logger.debug(f"Loaded config with {len(config)} keys")

    loaded_tasks, ok = _load_json_file('tasks', os.path.abspath(TASKS_FILE), None)
    if ok and loaded_tasks is not None:
        tasks = loaded_tasks
        for task in tasks:
            if 'enabled' not in task:
                task['enabled'] = True
        app.logger.debug(f"Loaded {len(tasks)} tasks")

    loaded_history, ok = _load_json_file('history', os.path.abspath(HISTORY_FILE), None)
    if ok and loaded_history is not None:
        history = loaded_history
        for item in history:  # Add type to existing history if missing
            if 'type' not in item:
                item['type'] = 'task'
        app.logger.debug(f"Loaded {len(history)} history records")

    listeners_path = os.path.abspath(LISTENERS_FILE)
    loaded_listeners, ok = _load_json_file('listeners', listeners_path, None)
    if ok:
        if loaded_listeners is not None:
            listeners = loaded_listeners
            app.logger.debug(f"Loaded listeners: {list(listeners)}")
        else:
            listeners = {'scf': {'enabled': False, 'request_types': '6632,6634',
                                 'interval': 10, 'last_check': None}}
            save_listeners()
            app.logger.warning(f"Listeners file not found, created default: {listeners_path}")

    if _load_failed:
        app.logger.error(
            f"Startup completed with unreadable stores: {sorted(_load_failed)}. "
            f"Writes to these are disabled until the files are fixed and TaskHome restarts.")
    app.logger.debug("Exiting load_data")


def _save_json_file(name, path, data):
    """Write JSON atomically, refusing to clobber a store that failed to load.

    Non-atomic whole-file rewrites were losing data on interruption: the file
    is truncated the moment it's opened for writing, so a crash or a kill
    between truncate and flush leaves nothing (P0-5). Write to a temp file in
    the same directory, fsync, then rename — rename is atomic, so a reader
    sees either the old file or the new one, never a partial one.
    """
    if name in _load_failed:
        app.logger.error(
            f"Refusing to save {name}: its file failed to load, so writing "
            f"would destroy recoverable data. Fix {path} and restart.")
        return False
    directory = os.path.dirname(os.path.abspath(path)) or '.'
    tmp_path = None
    try:
        with STATE_LOCK:
            # Serialise inside the lock: json.dump iterates the live list, and
            # a concurrent append would raise or emit a torn file.
            payload = json.dumps(data, indent=2)
        fd, tmp_path = tempfile.mkstemp(prefix=f'.{name}-', suffix='.tmp', dir=directory)
        with os.fdopen(fd, 'w') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        return True
    except OSError as e:
        app.logger.error(f"Failed to save {name} to {path}: {e}")
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return False


def save_config():
    return _save_json_file('config', CONFIG_FILE, config)


def save_tasks():
    return _save_json_file('tasks', TASKS_FILE, tasks)


def save_history():
    return _save_json_file('history', HISTORY_FILE, history)


def save_listeners():  # New
    return _save_json_file('listeners', LISTENERS_FILE, listeners)


def get_host():
    """Resolve the bind address: TASKHOME_HOST env var > config.json 'host' > default."""
    return os.environ.get('TASKHOME_HOST') or config.get('host') or DEFAULT_HOST


def get_port():
    """Resolve the listen port: TASKHOME_PORT env var > config.json 'port' > default.

    Port 5000 is claimed by AirPlay Receiver on macOS, so an override is needed
    there. Falls back to DEFAULT_PORT on anything unparseable rather than
    refusing to start.
    """
    raw = os.environ.get('TASKHOME_PORT') or config.get('port') or DEFAULT_PORT
    try:
        port = int(raw)
    except (TypeError, ValueError):
        app.logger.warning(f"Invalid port {raw!r}, falling back to {DEFAULT_PORT}")
        return DEFAULT_PORT
    if not 1 <= port <= 65535:
        app.logger.warning(f"Port {port} out of range, falling back to {DEFAULT_PORT}")
        return DEFAULT_PORT
    return port


def is_printer_connected():
    try:
        dev = usb.core.find(idVendor=VID, idProduct=PID)
        return dev is not None
    except Exception as e:
        app.logger.error(f"USB detection error: {e}")
        return False


def calculate_next(next_time_str, recurring, days=None):
    """Return the next occurrence after next_time_str, as a naive local ISO string.

    Returns the input UNCHANGED when it cannot advance (unknown recurrence,
    'none', or a weekday rule that matches nothing). Callers must treat an
    unchanged return as "no next occurrence" and must never loop on it — see
    advance_schedule, which enforces that. Weekday searches are bounded to a
    single week: a rule that hasn't matched in 7 days never will (P0-2).
    """
    app.logger.debug(f"Calculating next time from {next_time_str} with recurring={recurring} and days={days}")
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
            app.logger.error(
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

    Never raises: a malformed config degrades to defaults with a warning
    rather than taking down the scheduler (X-4).
    """
    resolved = dict(DEFAULT_CATCHUP)
    stored = config.get('catchup')
    if isinstance(stored, dict):
        for key, value in stored.items():
            if key in resolved:
                resolved[key] = value

    if resolved['policy'] not in CATCHUP_POLICIES:
        app.logger.warning(
            f"Invalid catchup.policy {resolved['policy']!r}, using {DEFAULT_CATCHUP['policy']!r}")
        resolved['policy'] = DEFAULT_CATCHUP['policy']
    if resolved['oneoff_policy'] not in CATCHUP_POLICIES:
        app.logger.warning(
            f"Invalid catchup.oneoff_policy {resolved['oneoff_policy']!r}, "
            f"using {DEFAULT_CATCHUP['oneoff_policy']!r}")
        resolved['oneoff_policy'] = DEFAULT_CATCHUP['oneoff_policy']
    for key in ('recent_window_hours', 'max_prints'):
        try:
            resolved[key] = int(resolved[key])
        except (TypeError, ValueError):
            app.logger.warning(f"Invalid catchup.{key} {resolved[key]!r}, using default")
            resolved[key] = DEFAULT_CATCHUP[key]
        if resolved[key] < 0:
            app.logger.warning(f"Negative catchup.{key}, using default")
            resolved[key] = DEFAULT_CATCHUP[key]
    return resolved


def resolve_catchup_policy(task, catchup_config=None):
    """Which catch-up policy applies to this task. First match wins:
    explicit per-task setting, then the one-off default, then the global.
    """
    cfg = catchup_config or get_catchup_config()
    explicit = task.get('catchup', 'inherit')
    if explicit != 'inherit':
        if explicit in CATCHUP_POLICIES:
            return explicit
        app.logger.warning(
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
        print_task(_with_note(task, note))
        return
    for occurrence in occurrences:
        print_task(_with_note(task, f"MISSED occurrence - was due {_format_occurrence(occurrence)}"))
    if dropped:
        # Never truncate silently (X-4).
        print_task(_with_note(
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
    app.logger.info(
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
    """Startup catch-up across all enabled tasks. Returns tasks changed."""
    now = now or datetime.now()
    cfg = get_catchup_config()
    changed = 0
    for task in list(tasks):
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
            app.logger.error(f"Disabling task {task.get('id')} - {e}")
        except Exception as e:
            app.logger.error(f"Catch-up failed for task {task.get('id')}: {e}", exc_info=True)
    if changed:
        save_tasks()
    return changed


def fire_due_task(task, now, catchup_config=None):
    """Print a task if it is due and reschedule it. Returns True if it fired.

    The schedule advances ONLY on a successful print (P0-4). Previously an
    offline printer silently dropped the occurrence and moved on, so a task
    due while the printer was unplugged was lost forever. Now it stays due and
    retries each tick until the printer comes back.
    """
    if parse_task_time(task['next_time']) > now:
        return False

    if not print_task(task):
        # Leave next_time alone so the occurrence is retried, and record the
        # failure so the UI can show "waiting for printer" rather than looking
        # stuck for no visible reason.
        task['print_failures'] = task.get('print_failures', 0) + 1
        task['last_print_failure'] = now.isoformat()
        app.logger.warning(
            f"Print failed for task {task.get('id')}; "
            f"leaving it due (attempt {task['print_failures']})")
        return False

    task.pop('print_failures', None)
    task.pop('last_print_failure', None)

    next_time, missed = advance_schedule(task, now)
    # missed[0] is the occurrence just printed; anything after it came due
    # while the printer was offline and is governed by the catch-up policy.
    extra = missed[1:] if missed else []
    if extra:
        cfg = catchup_config or get_catchup_config()
        policy = resolve_catchup_policy(task, cfg)
        occurrences, dropped = select_catchup_prints(extra, policy, cfg, now)
        app.logger.info(
            f"Task {task.get('id')} recovered with {len(extra)} further missed "
            f"occurrence(s), policy={policy}, printing {len(occurrences)}")
        if occurrences:
            print_catchup(task, occurrences, len(extra), dropped, policy)
        task['missed_count'] = task.get('missed_count', 0) + len(extra)
        task['last_missed_at'] = extra[-1]

    if next_time is None:
        with STATE_LOCK:
            if task in tasks:
                tasks.remove(task)
    else:
        task['next_time'] = next_time
    return True


@contextmanager
def open_printer():
    """Open the printer, guaranteeing the USB handle is released (P0-11).

    The previous code only called close() on the success path, so any
    exception mid-receipt leaked the claimed interface; enough of those and
    the device stops opening until it is physically replugged.
    """
    printer = Usb(VID, PID, profile='TM-T20II')
    try:
        yield printer
    finally:
        try:
            printer.close()
        except Exception as e:  # closing must never mask the original error
            app.logger.warning(f"Error closing printer: {e}")


def record_history(record):
    """Prepend a print record and trim to the configured cap."""
    with STATE_LOCK:
        history.insert(0, record)
        max_history = config.get('max_history', DEFAULT_CONFIG['max_history'])
        try:
            max_history = int(max_history)
        except (TypeError, ValueError):
            max_history = DEFAULT_CONFIG['max_history']
        del history[max(max_history, 0):]
    save_history()


def print_task(task):
    """Print a task receipt. Returns True only if paper actually came out.

    The return value matters: the scheduler must not advance a task's schedule
    for a print that never happened (P0-4), and the test-print routes must not
    claim success when the print failed (P0-10).
    """
    if not is_printer_connected():
        app.logger.warning("Printer not connected, skipping print")
        return False
    try:
        with open_printer() as p:
            # p.profile.media_width_mm = 80  # Set paper width to 80mm
            # QR code at the top
            p.set(align='center', density=4)
            hostname = config.get('hostname', DEFAULT_CONFIG['hostname'])
            qr_url = task.get('url', '') or f"http://{hostname}:{get_port()}/task_page#{task['id']}"
            p.qr(qr_url, size=5, model=2)

            # Title: bold, large, centered
            p.set(align='center', font='a', bold=True, custom_size=True, width=3, height=3, density=4)
            p.text(task['title'] + '\n')

            # Extra info: regular, left-aligned
            if 'extra' in task and task['extra']:
                # Blank line
                p.text('\n')
                p.set(align='center', font='b', bold=False, custom_size=True, width=2, height=2)
                p.text(task['extra'] + '\n')

            # Blank line
            p.text('\n')

            # Timestamp: italic, left-aligned
            print_time = datetime.now().strftime('%I:%M %p, %m/%d/%Y')
            p.set(align='center', font='b', bold=False, custom_size=True, width=1, height=1)
            p.text(f'Printed at {print_time}\n')

            # Blank line
            p.text('\n')

            # Task Type: italic, left-aligned
            recurring = task.get('recurring', 'none')
            task_type = 'Non-recurring' if recurring == 'none' else f"Recurring ({recurring.capitalize()})"
            p.set(align='center', font='b', bold=False, custom_size=True, width=1, height=1)
            p.text(f'Task Type: {task_type}\n')

            # Task ID: italic, left-aligned
            p.text(f'Task ID: {task["id"]}\n')

            # Disable italics and cut
            p.cut()

        # Only recorded once the receipt is out and the handle is closed.
        record_history({**task, 'print_time': datetime.now().isoformat(), 'type': 'task'})
        return True
    except Exception as e:
        app.logger.error(f"Print error: {e}", exc_info=True)
        return False


def scf_has_media(issue):
    """Whether an issue carries a full-size image.

    `media` may be absent, null, or present-without-image_full depending on
    the issue; indexing it blindly raised mid-receipt, wasting paper on a
    half-printed job (P0-8).
    """
    media = issue.get('media')
    if not isinstance(media, dict):
        return False
    return bool(media.get('image_full'))


def scf_category(issue):
    request_type = issue.get('request_type')
    if isinstance(request_type, dict) and request_type.get('title'):
        return request_type['title']
    return 'Unknown Category'


def scf_reported_at(issue):
    created = issue.get('created_at')
    if not created:
        return 'Unknown'
    try:
        return datetime.fromisoformat(created.replace('Z', '+00:00')).strftime('%I:%M %p, %m/%d/%Y')
    except (ValueError, AttributeError):
        app.logger.warning(f"Unparseable SCF created_at {created!r}")
        return str(created)


def print_scf_issue(issue):  # New: Custom print for SCF issues
    """Print an SCF issue receipt. Returns True only if it actually printed."""
    if not is_printer_connected():
        app.logger.warning("Printer not connected, skipping SCF issue print")
        return False

    # Resolve every field BEFORE opening the printer, so a malformed payload
    # fails without wasting paper on a partial receipt (P0-8).
    category = scf_category(issue)
    address = issue.get('address', 'Unknown Location')
    reported_at = scf_reported_at(issue)
    status = issue.get('status', 'Unknown')
    has_media = 'Yes' if scf_has_media(issue) else 'No'
    html_url = issue.get('html_url', '')
    issue_id = issue.get('id', 'unknown')
    description = issue.get('description') or ''

    try:
        with open_printer() as p:
            # QR code at the top (to issue HTML URL)
            if html_url:
                p.set(align='center', density=4)
                p.qr(html_url, size=5, model=2)

            # Category: bold, large, centered (like title)
            p.set(align='center', font='a', bold=True, custom_size=True, width=3, height=3, density=4)
            p.text(category + '\n')

            # Blank line
            p.text('\n')

            # Location, reported timestamp, status (smaller text)
            p.set(align='center', font='b', bold=False, custom_size=True, width=1, height=1)
            p.text(f'Location: {address}\n')
            p.text(f'Reported: {reported_at}\n')
            p.text(f'Status: {status}\n')
            p.text(f'Has Media: {has_media}\n')

            # Description (if present)
            if description:
                p.text('\nDescription:\n')
                p.text(description + '\n')

            # Blank line
            p.text('\n')

            # Print timestamp
            print_time = datetime.now().strftime('%I:%M %p, %m/%d/%Y')
            p.text(f'Printed at {print_time}\n')

            # Issue ID
            try:
                p.barcode(str(issue_id), 'CODE39', width=2, height=60, pos='below', align_ct=True)
            except Exception as e:
                p.text(f'Issue ID: {issue_id}\n')
                app.logger.error(f"Barcode print error: {e}")

            # Cut
            p.cut()

        # Add to history
        record_history({
            'type': 'scf',
            'id': issue_id,
            'category': category,
            'summary': issue.get('summary', ''),
            'address': address,
            'reported_at': issue.get('created_at', ''),
            'status': status,
            'description': description,
            'url': html_url,
            'print_time': datetime.now().isoformat()
        })
        return True
    except Exception as e:
        app.logger.error(f"SCF issue print error: {e}", exc_info=True)
        return False


SCF_ISSUES_URL = "https://seeclickfix.com/api/v2/issues"
SCF_PER_PAGE = 100
SCF_MAX_PAGES = 20          # 2000 issues per poll; a guard, not a real limit
SCF_SEEN_LIMIT = 2000       # bounded dedup memory
SCF_MAX_BACKOFF_MINUTES = 60


def parse_utc(value, default=None):
    """Parse an ISO 8601 timestamp to an aware UTC datetime, or return default."""
    if not value:
        return default
    try:
        parsed = parser.parse(str(value).strip())
    except (ValueError, TypeError, OverflowError) as e:
        app.logger.warning(f"Failed to parse timestamp {value!r}: {e}")
        return default
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fetch_scf_issues(request_types, after):
    """Fetch every page of SCF issues created after `after`.

    The old code requested one page of 100 and ignored
    metadata.pagination entirely, so a busy interval silently dropped
    everything past the first page (P0-7). Raises on transport/HTTP errors so
    the caller can back off without advancing its watermark.
    """
    collected = []
    page = 1
    while page <= SCF_MAX_PAGES:
        params = {
            'status': 'open,acknowledged',
            'request_types': request_types,
            'after': after,
            'per_page': str(SCF_PER_PAGE),
            'page': str(page),
        }
        app.logger.info(f"Fetching SCF issues after {after}, page {page}")
        resp = requests.get(SCF_ISSUES_URL, params=params, timeout=15)
        resp.raise_for_status()
        payload = resp.json()

        issues = payload.get('issues') or []
        collected.extend(issues)

        pagination = (payload.get('metadata') or {}).get('pagination') or {}
        total_pages = pagination.get('pages')
        if total_pages is None:
            # Fall back to "a short page means the last page".
            if len(issues) < SCF_PER_PAGE:
                break
        elif page >= total_pages:
            break
        page += 1
    else:
        app.logger.warning(
            f"SCF pagination hit the {SCF_MAX_PAGES}-page guard; "
            f"some issues were not fetched this cycle")
    return collected


def scf_due(scf, now_utc):
    """Whether the SCF listener should poll now."""
    backoff_until = parse_utc(scf.get('backoff_until'))
    if backoff_until and now_utc < backoff_until:
        return False
    last_check = parse_utc(scf.get('last_check'))
    if last_check is None:
        return True
    try:
        interval_minutes = int(scf.get('interval', 10))
    except (TypeError, ValueError):
        interval_minutes = 10
    return (now_utc - last_check) >= timedelta(minutes=max(interval_minutes, 1))


def poll_scf_listener(now_utc):
    """Poll SeeClickFix and print new issues. Returns the number printed.

    Fixes the three P0-7 defects together, because they interact:
      * dedup by issue id, since `after` is inclusive and windows overlap
      * follow pagination rather than silently truncating at 100
      * take the watermark BEFORE the request, so issues created while the
        request is in flight are caught next cycle instead of being skipped
    """
    scf = listeners.get('scf')
    if not scf or not scf.get('enabled'):
        return 0

    request_types = (scf.get('request_types') or '').strip()
    if not request_types:
        app.logger.warning("SCF listener enabled but request_types empty; skipping check")
        return 0

    if not scf_due(scf, now_utc):
        return 0

    app.logger.debug("Checking SCF listener")
    last_check = parse_utc(scf.get('last_check'))
    # The watermark for the NEXT poll is taken now, before the request. Using
    # a timestamp captured after the fetch would skip anything created while
    # the fetch was running.
    watermark = now_utc
    after_dt = last_check if last_check else (now_utc - timedelta(hours=1))
    after = after_dt.strftime('%Y-%m-%dT%H:%M:%SZ')

    try:
        issues = fetch_scf_issues(request_types, after)
    except Exception as e:
        failures = scf.get('consecutive_failures', 0) + 1
        # Exponential backoff, capped. last_check is deliberately NOT advanced,
        # so the window is retried rather than skipped.
        delay = min(2 ** min(failures, 6), SCF_MAX_BACKOFF_MINUTES)
        scf['consecutive_failures'] = failures
        scf['backoff_until'] = (now_utc + timedelta(minutes=delay)).strftime('%Y-%m-%dT%H:%M:%SZ')
        scf['last_error'] = str(e)
        save_listeners()
        app.logger.error(
            f"SCF listener error (failure {failures}, retrying in {delay}m): {e}")
        return 0

    seen = scf.get('seen') or []
    seen_set = set(seen)

    fresh = []
    for issue in issues:
        issue_id = issue.get('id')
        if issue_id is None or issue_id in seen_set:
            continue
        seen_set.add(issue_id)
        fresh.append(issue)

    fresh.sort(key=lambda i: i.get('created_at') or '')

    printed = 0
    for issue in fresh:
        if print_scf_issue(issue):
            seen.append(issue.get('id'))
            printed += 1
        else:
            # Printing failed (offline printer). Leave it out of `seen` so the
            # next overlapping window picks it up again (P0-4).
            app.logger.warning(
                f"SCF issue {issue.get('id')} not printed; will retry next cycle")

    if len(seen) > SCF_SEEN_LIMIT:
        del seen[:-SCF_SEEN_LIMIT]  # keep the most recent ids
    scf['seen'] = seen
    scf['last_check'] = watermark.strftime('%Y-%m-%dT%H:%M:%SZ')
    scf['consecutive_failures'] = 0
    scf.pop('backoff_until', None)
    scf.pop('last_error', None)
    save_listeners()
    app.logger.info(
        f"SCF listener checked at {scf['last_check']}: "
        f"{len(issues)} fetched, {len(fresh)} new, {printed} printed")
    return printed


def run_due_tasks(now):
    """Fire every due task. Each task is isolated: one unusable task cannot
    stall the others or the listener poll (P0-6). Returns tasks fired."""
    fired = 0
    changed = 0
    cfg = get_catchup_config()
    for task in list(tasks):
        if not task.get('enabled', True):
            continue
        failures_before = task.get('print_failures')
        try:
            if fire_due_task(task, now, cfg):
                fired += 1
                changed += 1
            elif task.get('print_failures') != failures_before:
                changed += 1  # a failed print still needs persisting
        except (ScheduleError, ValueError) as e:
            task['enabled'] = False
            task['schedule_error'] = str(e)
            changed += 1
            app.logger.error(f"Disabling task {task.get('id')} - {e}")
        except Exception as e:
            app.logger.error(
                f"Error firing task {task.get('id')}: {e}", exc_info=True)
    if changed:
        save_tasks()
    return fired


def scheduler_loop():
    # Times are naive local wall-clock throughout (P0-3): the catch-up and the
    # steady-state loop must compare in the same frame as the stored values.
    run_catchup(datetime.now())

    while True:
        app.logger.debug("Scheduler loop iteration started")
        try:
            now = datetime.now()
            now_utc = datetime.now(timezone.utc)
            run_due_tasks(now)

            poll_scf_listener(now_utc)
        except Exception as e:
            app.logger.error(f"Scheduler loop error: {e}", exc_info=True)

        # Sleep for a minute before next check
        app.logger.debug("Scheduler loop iteration complete, sleeping for 60 seconds")
        time.sleep(60)


@app.route('/')
def index():
    status = 'Connected' if is_printer_connected() else 'Not connected'
    recent_history = history[:5]
    # All tasks, not just enabled ones: a task disabled by a schedule error or
    # a missed one-off must stay visible (P0-13, and P1-10's promise that
    # skipping isn't vanishing). The template renders the status.
    return render_template('index.html', status=status, config=config, tasks=tasks, history=recent_history)


@app.route('/task_page')
def task_page():
    return render_template('tasks.html', config=config, tasks=tasks, history=history)


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        if 'clear_history' in request.form:
            # Mutate in place rather than rebinding the global: rebinding
            # detaches the module-level name from the list other code is
            # already holding, so their writes would go to an orphan.
            with STATE_LOCK:
                del history[:]
            save_history()
            return redirect(url_for('settings'))
        raw_max = request.form.get('max_history', '')
        try:
            max_history = int(raw_max)
        except (TypeError, ValueError):
            return reject(f"'{raw_max}' is not a valid history size.")
        if not 0 <= max_history <= 100000:
            return reject('History size must be between 0 and 100000.')

        theme = request.form.get('theme', 'system')
        if theme not in THEMES:
            return reject(f"'{theme}' is not a valid theme.")

        config['max_history'] = max_history
        config['hostname'] = (request.form.get('hostname') or '').strip() or \
            DEFAULT_CONFIG['hostname']
        config['theme'] = theme
        save_config()
        del history[max_history:]
        save_history()
        return redirect(url_for('settings'))
    printer_info = {
        'manufacturer': PRINTER_MANUFACTURER,
        'model': PRINTER_MODEL,
        'connection': PRINTER_CONNECTION,
        'status': 'Connected' if is_printer_connected() else 'Not connected'
    }
    return render_template('settings.html', config=config, printer_info=printer_info)


@app.route('/test_print', methods=['POST'])
def test_print():
    if not is_printer_connected():
        # 503, not 200: the front end trusts the status code, so a "not
        # connected" reply must not read as success (P0-10).
        return 'Printer not connected. <a href="/settings">Back</a>', 503
    try:
        # Create a test task with example data
        test_task = {
            'id': str(uuid.uuid4()),
            'title': 'Test Task Print',
            'extra': 'This is a test print from TaskHome',
            'url': f"http://{config.get('hostname', DEFAULT_CONFIG['hostname'])}:{get_port()}/task_page#test",
            'next_time': datetime.now().isoformat(),
            'recurring': 'none',
            'enabled': True
        }
        # print_task swallows its own errors, so the return value is the only
        # honest signal of whether paper came out (P0-10).
        if print_task(test_task):
            return 'Test print successful! <a href="/settings">Back</a>'
        return ('Test print failed - see the log for details. '
                '<a href="/settings">Back</a>'), 500
    except Exception as e:
        app.logger.error(f"Test print error: {e}")
        return f'Test print failed: {e}. <a href="/settings">Back</a>', 500


@app.route('/test_scf_print', methods=['POST'])
def test_scf_print():
    if not is_printer_connected():
        return 'Printer not connected. <a href="/settings">Back</a>', 503
    try:
        # Example SCF issue data
        test_issue = {
            'id': 12345678,
            'html_url': 'https://seeclickfix.com/issues/12345678',
            'request_type': {'title': 'Streetlight Out'},
            'address': '123 Main St, Springfield',
            'created_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'status': 'Open',
            'description': 'The streetlight in front of my house is not working.',
            'summary': 'Streetlight outage reported',
            'media': {
                # 'image_full': 'https://seeclickfix.com/media/issues/12345678/full.jpg',
                'image_full': None,
                'image_square_100x100': 'https://seeclickfix.com/media/issues/12345678/thumb.jpg'
            }
        }
        if print_scf_issue(test_issue):
            return 'Test SCF issue print successful! <a href="/settings">Back</a>'
        return ('Test SCF issue print failed - see the log for details. '
                '<a href="/settings">Back</a>'), 500
    except Exception as e:
        app.logger.error(f"Test SCF print error: {e}")
        return f'Test SCF print failed: {e}. <a href="/settings">Back</a>', 500


class ValidationError(Exception):
    """A form submission was rejected. Carries a user-facing message."""


def normalize_next_time(raw, fallback=None):
    """Turn a datetime-local form value into a stored naive-local ISO string.

    The form yields 'YYYY-MM-DDTHH:MM'; seconds were previously appended
    blindly, producing '...T21:00:00:00' when the browser already included
    them. Parse first, then re-serialise from the parsed value, so the stored
    form is always canonical regardless of what the browser sent (P0-9).
    """
    raw = (raw or '').strip()
    if not raw:
        if fallback is not None:
            return fallback
        return datetime.now().replace(microsecond=0).isoformat()
    try:
        return parse_task_time(raw).isoformat()
    except (ValueError, TypeError):
        raise ValidationError(f"'{raw}' is not a valid date and time.")


def parse_days(raw_days):
    """Weekday indices from the form, deduped and ordered. 0=Mon .. 6=Sun."""
    days = set()
    for value in raw_days:
        try:
            day = int(value)
        except (TypeError, ValueError):
            raise ValidationError(f"'{value}' is not a valid weekday.")
        if not 0 <= day <= 6:
            raise ValidationError(f"Weekday {day} is out of range.")
        days.add(day)
    return sorted(days)


def task_from_form(form, existing=None):
    """Build or update a task from form data, validating as we go.

    Raises ValidationError with a message suitable for showing the user. The
    task dict is only mutated once every field has validated, so a rejected
    edit can't leave a task half-updated.
    """
    title = (form.get('title') or '').strip()
    if not title:
        raise ValidationError('Title is required.')

    recurring = form.get('recurring') or 'none'
    if recurring not in RECURRENCE_MODES:
        raise ValidationError(f"'{recurring}' is not a valid recurrence.")

    next_time = normalize_next_time(
        form.get('next_time'), fallback=existing['next_time'] if existing else None)

    days = parse_days(form.getlist('days')) if recurring == 'custom' else None
    if recurring == 'custom' and not days:
        # Without this the schedule can never advance (P0-2).
        raise ValidationError('Pick at least one weekday for a custom recurrence.')

    task = existing if existing is not None else {'id': str(uuid.uuid4())}
    task['title'] = title
    task['next_time'] = next_time
    task['recurring'] = recurring
    task['enabled'] = 'enabled' in form

    for field in ('extra', 'url'):
        value = (form.get(field) or '').strip()
        if value:
            task[field] = value
        else:
            task.pop(field, None)

    if days:
        task['days'] = days
    else:
        task.pop('days', None)

    # A successful edit clears any prior failure state, since the user has
    # just told us what the schedule should be.
    task.pop('schedule_error', None)
    task.pop('missed', None)
    return task


def reject(message, status=400):
    """Render a validation failure. Kept plain until P2-4 brings in toasts."""
    app.logger.info(f"Rejected form submission: {message}")
    return render_template('error.html', message=message), status


@app.route('/add_task', methods=['POST'])
def add_task():
    try:
        task = task_from_form(request.form)
    except ValidationError as e:
        return reject(str(e))
    with STATE_LOCK:
        tasks.append(task)
    save_tasks()
    return redirect(url_for('task_page'))


@app.route('/edit_task/<task_id>', methods=['GET', 'POST'])
def edit_task(task_id):
    task = next((t for t in tasks if t['id'] == task_id), None)
    if not task:
        return 'Task not found', 404
    if request.method == 'POST':
        try:
            # Validate against a copy so a rejected edit leaves the live task
            # untouched rather than partially applied.
            candidate = task_from_form(request.form, existing=dict(task))
        except ValidationError as e:
            return reject(str(e))
        with STATE_LOCK:
            task.clear()
            task.update(candidate)
        save_tasks()
        return redirect(url_for('task_page'))
    return render_template('tasks.html', config=config, tasks=tasks,
                           history=history, edit_task=task)


@app.route('/delete_task', methods=['POST'])
def delete_task():
    task_id = request.form.get('id')
    if not task_id:
        return reject('No task specified.')
    with STATE_LOCK:
        remaining = [t for t in tasks if t.get('id') != task_id]
        if len(remaining) == len(tasks):
            return reject('That task no longer exists.', status=404)
        tasks[:] = remaining
    save_tasks()
    return redirect(url_for('task_page'))


# New route for listeners page
@app.route('/listener', methods=['GET', 'POST'])  # Note: singular as per your request
def listener():
    if request.method == 'POST':
        # listeners['scf'] may not exist yet; the old code indexed it directly
        # to preserve last_check and raised KeyError on a fresh install (P0-9).
        existing = listeners.get('scf') or {}

        raw_interval = request.form.get('interval', '')
        try:
            interval = int(raw_interval)
        except (TypeError, ValueError):
            return reject(f"'{raw_interval}' is not a valid interval.")
        if not 1 <= interval <= 1440:
            return reject('Interval must be between 1 and 1440 minutes.')

        request_types = ','.join(
            part.strip() for part in (request.form.get('request_types') or '').split(',')
            if part.strip())

        listeners['scf'] = {
            'enabled': 'enabled' in request.form,
            'request_types': request_types,
            'interval': interval,
            'last_check': existing.get('last_check'),  # Preserve existing last_check
        }
        save_listeners()
        return redirect(url_for('listener'))
    return render_template('listener.html', config=config, scf=listeners.get('scf', {}))


scheduler_thread = None


def start_scheduler():
    """Start the scheduler thread, refusing to start a second one.

    Partial mitigation for P0-12: under the Flask reloader or a multi-worker
    server the module can be imported more than once. This guard only covers
    repeat starts within one process; the full fix is an app factory (P1-3).
    """
    global scheduler_thread
    if scheduler_thread is not None and scheduler_thread.is_alive():
        app.logger.warning("Scheduler already running; not starting another")
        return scheduler_thread
    scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
    scheduler_thread.start()
    return scheduler_thread


def init_app(load=True, scheduler=True):
    if load:
        load_data()
    if scheduler:
        start_scheduler()
    app.logger.debug("Initialization complete: Data loaded and scheduler started")


# Initialize app. TASKHOME_NO_INIT lets tests import this module without
# reading the user's real JSON files or starting a thread that prints.
if os.environ.get('TASKHOME_NO_INIT') != '1':
    init_app(scheduler=os.environ.get('TASKHOME_NO_SCHEDULER') != '1')

if __name__ == '__main__':
    app.logger.debug("Running directly via python app.py")
    app.run(host=get_host(), port=get_port())
