# Scheduling & Recurrence

Times are stored as **naive local ISO strings** in `task['next_time']`. That is
the system's time model: task times mean wall-clock local time, always.

The moving parts:

| Function | Role |
| --- | --- |
| `calculate_next()` | One step of a recurrence. Pure; no side effects. |
| `advance_schedule()` | Rolls a task past `now`, returning the occurrences stepped over. Enforces forward progress. |
| `run_catchup()` | Startup pass over all tasks. |
| `run_due_tasks()` | Steady-state pass, every 60s. |
| `fire_due_task()` | Print one task and reschedule it. |

## Recurrence modes

`calculate_next(next_time_str, recurring, days=None)` returns the next
occurrence as a naive local ISO string, or **the input unchanged** when it
cannot advance.

| `recurring` | Behavior | Notes |
| --- | --- | --- |
| `daily` | `+ timedelta(days=1)` | Wall-clock stable across DST (naive arithmetic) |
| `weekly` | `+ timedelta(days=7)` | |
| `monthly` | `+ relativedelta(months=1)` | dateutil clamps: Jan 31 → Feb 28/29 → **Mar 28/29 thereafter** (the original day-of-month is not remembered) |
| `every_weekday` | step forward to the next `weekday() < 5`, bounded to 7 days | Fri → Mon |
| `first_day_month` | `+ relativedelta(months=1, day=1)` | Always the 1st of the next month, correct from any date |
| `custom` | step forward to the next `weekday() in days`, bounded to 7 days | `days` uses 0=Mon…6=Sun. Invalid entries are discarded; if nothing valid remains it returns the input unchanged and logs |
| `none` or anything unrecognised | **returns the input unchanged** | Means "no next occurrence" |

### The invariant that matters

An unchanged return means *no next occurrence* and callers must never loop on
it. `advance_schedule` enforces this rather than trusting callers:

- unchanged return → `ScheduleError`
- candidate not strictly later than the current value → `ScheduleError`
- more than 4096 steps → `ScheduleError`

This is deliberately general. The two hangs that motivated it (`P0-1`, a missed
one-off; `P0-2`, a `custom` recurrence with no weekdays) were both instances of
"the loop iterated without advancing", so the fix targets that property instead
of the two symptoms — no future recurrence mode can reintroduce it.

## Startup catch-up

`run_catchup()` runs once, before the loop, comparing **naive local to naive
local** — the same frame as the stored values and the steady-state loop.

For each enabled task it rolls `next_time` forward past now and collects the
occurrences it stepped over. What happens to those is the **catch-up policy**
(MASTER_PLAN `P1-10`):

| Policy | Behavior |
| --- | --- |
| `skip` | Print nothing; roll forward |
| `print_once` | One receipt summarising the gap |
| `print_all` | One receipt per missed occurrence, oldest first |
| `print_if_recent` | Only those within `recent_window_hours` |

Resolution order, first match wins: `task['catchup']` (unless `"inherit"`) →
`catchup.oneoff_policy` for one-offs → `catchup.policy`.

Defaults are `skip` globally and `print_once` for one-offs. The asymmetry is
deliberate: skipping a recurring task loses one occurrence of many, while
skipping a one-off means it **never prints at all**.

Printing policies are capped at `catchup.max_prints` (default 20) and emit a
final receipt naming how many were dropped — a silent truncation would
misrepresent what happened.

### Skipping is recorded, not silent

A skipped occurrence sets `missed_count` and `last_missed_at` on the task. A
missed one-off has no future occurrence, so leaving it enabled would make the
steady-state loop fire it seconds later, contradicting the policy — it is
marked `missed: true` and disabled, and the UI shows it as *Missed*, distinct
from *user-disabled*.

A task whose recurrence cannot advance, or whose `next_time` will not parse, is
disabled with the reason in `schedule_error` rather than retried every tick
forever.

## Steady-state firing (every 60s)

The tick order is `queue.drain()` → `run_due_tasks()` → `scf.poll_scf_listener()` → `listener_base.run_all()`. Draining first means a backlog clears in order rather than newest-first.

`run_due_tasks(now)` compares `next_time <= now`, both naive local, then:

1. Print. **If the print fails, nothing else happens** — `next_time` is left
   alone so the occurrence is retried next tick, and `print_failures` /
   `last_print_failure` are recorded. An unplugged printer therefore delays
   receipts rather than destroying them.
2. On success, advance from the *scheduled* time, not from now — a task due
   21:00 that fires at 21:00:40 next fires exactly 24h after 21:00.
3. One-off tasks are removed after a successful print.
4. If further occurrences elapsed during an outage, the catch-up policy governs
   them, so recovery doesn't dump a stack of paper by default.

Each task is isolated in its own `try`. One unusable task cannot abort the pass
or the listener poll.

## Timezone summary

| Value | Frame |
| --- | --- |
| `task.next_time` | Naive local |
| Startup catch-up comparison | Naive local vs naive local |
| Steady-state comparison | Naive local vs naive local |
| Task `print_time` (history) | Naive local |
| SCF `last_check` / `after` | Real UTC (`...Z`) |
| SCF `reported_at` (history) | Verbatim API value (offset-aware, e.g. `-04:00`) |

Task times and listener watermarks are genuinely different kinds of value — one
is wall-clock, the other an instant — so they use different frames on purpose.
`parse_task_time()` normalises any stray timezone-aware value to local naive, so
comparisons never mix frames.

DST: naive day-stepping keeps wall-clock times stable across transitions (a
21:00 task stays 21:00), which is the desirable behavior for reminders. A
`next_time` inside the spring-forward gap fires at the next tick after the
nonexistent time.

## Input canonicalisation

`next_time` submitted through a form is parsed and re-serialised, so the stored
value is canonical regardless of what the browser sent. Previously `':00'` was
appended blindly, producing `'...T21:00:00:00'` when the browser already
included seconds.

Worth knowing: on Python 3.11+ `datetime.fromisoformat` *accepts* that
malformed value, silently dropping the trailing `:00`. On 3.9 it raised. Since
`D-3` moved this project to 3.13, that input is a silent-acceptance bug rather
than the denial-of-service originally catalogued as `P0-6`; validation at
ingress is the real defence. `tests/test_recurrence.py` pins the behavior.
