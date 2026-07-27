#!/usr/bin/env python
"""Report what TaskHome would print on its next start. Prints nothing.

Starting the app after a long gap is the moment you most want to know how much
paper is about to come out -- from task catch-up, and from a SeeClickFix poll
whose window may be wide. This answers that without committing to it.

Read-only: no receipts, no saves, no migration. Safe to run any time.

    ./scripts/dry_run.py              # tasks only (no network)
    ./scripts/dry_run.py --check-scf  # also query SeeClickFix for the real count
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the package, never `app` -- importing the entry point builds the Flask
# app AND starts the scheduler thread, which for a read-only tool means running
# a scheduler against live data. That regression is why this comment exists.
import taskhome  # noqa: E402

# Keep this tool's report readable regardless of the configured level: the
# user may have set DEBUG, and this is a report, not a log.
taskhome.log.setLevel('WARNING')


def load_readonly():
    """Load state without migrating or writing anything."""
    for name, filename in taskhome.constants.STORE_FILENAMES.items():
        # Prefer data/, fall back to the legacy root location.
        candidate = taskhome.constants.data_path(filename)
        if not os.path.exists(candidate):
            legacy = os.path.join(taskhome.constants.APP_ROOT, filename)
            if os.path.exists(legacy):
                candidate = legacy
        value, ok = taskhome.storage._load_json_file(name, candidate, None)
        if ok and value is not None:
            if name == 'config':
                merged = dict(taskhome.constants.DEFAULT_CONFIG)
                merged.update(value if isinstance(value, dict) else {})
                taskhome.state.config = merged
            elif name == 'tasks':
                taskhome.state.tasks = value
            elif name == 'history':
                taskhome.state.history = value
            elif name == 'listeners':
                taskhome.state.listeners = value


def report_tasks(now):
    cfg = taskhome.recurrence.get_catchup_config()
    print(f"\nCatch-up policy: recurring={cfg['policy']!r} "
          f"one-off={cfg['oneoff_policy']!r} cap={cfg['max_prints']}")
    print(f"Now: {now:%Y-%m-%d %H:%M:%S} (local)\n")

    total = 0
    for task in taskhome.state.tasks:
        title = task.get('title', '?')
        if not task.get('enabled', True):
            why = ('schedule error' if task.get('schedule_error')
                   else 'missed' if task.get('missed') else 'disabled')
            print(f"  - {title:<20} skipped ({why})")
            continue
        try:
            next_time, missed = taskhome.recurrence.advance_schedule(dict(task), now)
        except taskhome.recurrence.ScheduleError as e:
            print(f"  ! {title:<20} WOULD BE DISABLED: {e}")
            continue
        except ValueError as e:
            print(f"  ! {title:<20} unparseable next_time: {e}")
            continue

        if not missed:
            print(f"  . {title:<20} not due (next {next_time})")
            continue

        policy = taskhome.recurrence.resolve_catchup_policy(task, cfg)
        chosen, dropped = taskhome.recurrence.select_catchup_prints(missed, policy, cfg, now)
        count = len(chosen) + (1 if dropped else 0)
        total += count
        detail = f"{len(missed)} missed, policy={policy}"
        if dropped:
            detail += f", {dropped} suppressed"
        print(f"  > {title:<20} WOULD PRINT {count:>3}   ({detail})")
        print(f"    {'':<20} then next at {next_time}")

    print(f"\n  Task receipts on next start: {total}")
    return total


def report_scf(now_utc, check_network):
    scf = taskhome.state.listeners.get('scf')
    if not scf:
        print("\nSeeClickFix: not configured")
        return 0
    if not scf.get('enabled'):
        print("\nSeeClickFix: disabled")
        return 0

    cap = scf.get('max_prints_per_poll', taskhome.listeners.scf.SCF_MAX_PRINTS_PER_POLL)
    last_check = taskhome.listeners.scf.parse_utc(scf.get('last_check'))
    seen = scf.get('seen') or []

    print(f"\nSeeClickFix: enabled, types={scf.get('request_types')}")
    print(f"  last_check : {scf.get('last_check')}")
    print(f"  seen ids   : {len(seen)}")
    print(f"  per-poll cap: {cap}")

    if last_check is None:
        window = "one hour (no last_check recorded)"
        after = now_utc - timedelta(hours=1)
    else:
        delta = now_utc - last_check
        hours = delta.total_seconds() / 3600
        window = f"{hours:.1f} hours since last_check"
        after = last_check
    print(f"  window     : {window}")

    if not check_network:
        print("  (pass --check-scf to query the API for the real count)")
        return None

    try:
        issues = taskhome.listeners.scf.fetch_scf_issues(
            scf.get('request_types', ''), after.strftime('%Y-%m-%dT%H:%M:%SZ'))
    except Exception as e:
        print(f"  fetch failed: {e}")
        return None

    fresh = [i for i in issues if i.get('id') not in set(seen)]
    would_print = min(len(fresh), cap) if isinstance(cap, int) else len(fresh)
    suppressed = max(len(fresh) - would_print, 0)
    print(f"  in window  : {len(issues)} issues, {len(fresh)} not yet seen")
    print(f"  WOULD PRINT: {would_print}" +
          (f"  (+1 notice, {suppressed} suppressed)" if suppressed else ""))
    return would_print + (1 if suppressed else 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--check-scf', action='store_true',
                    help='query the SeeClickFix API for the real issue count')
    args = ap.parse_args()

    load_readonly()
    print("=" * 62)
    print("DRY RUN - nothing will be printed and nothing will be saved")
    print("=" * 62)

    tasks_total = report_tasks(datetime.now())
    scf_total = report_scf(datetime.now(timezone.utc), args.check_scf)

    print("\n" + "=" * 62)
    if scf_total is None:
        print(f"Total known: {tasks_total} task receipt(s); SCF not counted")
    else:
        print(f"Total on next start: {tasks_total + scf_total} receipt(s)")
    print("=" * 62)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
