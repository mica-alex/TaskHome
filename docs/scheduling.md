# Scheduling & Recurrence

All logic lives in `calculate_next()` (`app.py:149-172`) and `scheduler_loop()`
(`app.py:303-397`). Times are stored as **naive local ISO strings** in
`task['next_time']`.

## Recurrence modes

`calculate_next(next_time_str, recurring, days=None)` parses the string with
`datetime.fromisoformat` and returns a new ISO string:

| `recurring` | Behavior | Notes |
| --- | --- | --- |
| `daily` | `+ timedelta(days=1)` | Wall-clock stable across DST (naive arithmetic) |
| `weekly` | `+ timedelta(days=7)` | |
| `monthly` | `+ relativedelta(months=1)` | dateutil clamps: Jan 31 → Feb 28/29 → **Mar 28/29 thereafter** (the original day-of-month is not remembered) |
| `every_weekday` | advance one day at a time until `weekday() < 5` | Fri → Mon, correct |
| `first_day_month` | `+ relativedelta(months=1, day=1)` | Always lands on the 1st of the next month, correct from any date |
| `custom` | advance one day at a time until `weekday() in days` | `days` uses 0=Mon…6=Sun (matches the form checkboxes). **If `days` is empty or None the `while True` loop never terminates** — the scheduler thread hangs forever (MASTER_PLAN `P0-2`) |
| `none` or anything else | **returns the input string unchanged** | This is the fall-through at `app.py:172` and the root cause of `P0-1` below |

## Steady-state firing (every 60s)

For each enabled task: `datetime.fromisoformat(next_time) <= datetime.now()`
(both naive local) → print, then:

- `recurring == 'none'` → task removed from `tasks`.
- otherwise → `next_time = calculate_next(...)`. Note this advances **from the
  scheduled time**, not from now — so a task due 21:00 that fires at 21:00:40
  next fires exactly 24h after 21:00. Good.

Two caveats:

1. The print result is ignored — if the printer is disconnected `print_task`
   returns without printing, but the schedule still advances / the one-off task
   is still deleted. The occurrence is silently lost (MASTER_PLAN `P0-4`).
2. The whole task pass + SCF check shares one `try/except` (`app.py:322-393`).
   One task with an unparseable `next_time` throws at `app.py:328` and aborts
   the remainder of the tick, every tick (MASTER_PLAN `P0-6`).

## Startup catch-up (runs once, before the loop)

`app.py:304-318`. Intent: after downtime, fast-forward each task's `next_time`
past "now" so stale occurrences don't all fire at once. Actual semantics:

- **Missed occurrences are skipped, not printed.** If the machine was off at
  21:00, the 21:00 task simply doesn't print that day. This is hardcoded and
  unconfigurable today; MASTER_PLAN `P1-10` makes it a setting (global default
  plus per-task override, with `skip` / `print_once` / `print_all` /
  `print_if_recent`), and the miss becomes visible in the UI rather than
  silent. Note that a missed **one-off** task doesn't just skip a day — it
  never prints at all.
- The comparison is done in a pseudo-UTC frame: the naive local `next_time` is
  force-tagged UTC (`.replace(tzinfo=timezone.utc)`, `app.py:311`) and compared
  to real UTC now. For a local zone at UTC-5, local `12:00` is treated as
  `12:00Z` = `07:00` local — i.e. the catch-up believes tasks are due **5 hours
  earlier than they are**. Consequence: any restart silently skips a full day of
  every task whose `next_time` falls within the next |UTC-offset| hours
  (MASTER_PLAN `P0-3`). The steady-state loop, by contrast, compares naive-local
  to naive-local and is internally consistent.
- **Hang #1:** a one-off (`recurring: "none"`) task whose `next_time` is in the
  past never advances (`calculate_next` returns it unchanged), so
  `while next_time < now` spins forever at 100% CPU and the scheduler thread
  never reaches its main loop — nothing ever prints again until the process is
  restarted with the task fixed (MASTER_PLAN `P0-1`). This happens any time the
  app restarts after a one-off task's time passed unprinted.
- **Hang #2:** `custom` with empty `days` — same infinite loop, inside
  `calculate_next` itself (`P0-2`). This one can also fire in steady state, the
  moment such a task comes due.
- The per-task `try/except` (`app.py:309-317`) does NOT protect against either
  hang — infinite loops are not exceptions.

## Timezone summary (the honest version)

| Value | Frame |
| --- | --- |
| `task.next_time` | Naive local (from `<input>` / flatpickr, or `datetime.now().isoformat()`) |
| Steady-state comparison | Naive local vs naive local — consistent |
| Startup catch-up comparison | Naive local **reinterpreted as UTC** vs real UTC — wrong by the UTC offset |
| Task `print_time` (history) | Naive local |
| SCF `last_check` / `after` | Real UTC (`...Z`) — handled correctly with dateutil parsing (`app.py:347-352`) |
| SCF `reported_at` (history) | Verbatim API value (offset-aware, e.g. `-04:00`) |

DST: naive day-stepping keeps wall-clock times stable across DST transitions
(a 21:00 task stays 21:00), which is the desirable behavior for reminders.
A `next_time` falling inside the spring-forward gap is compared naively and
fires at the next tick after the (nonexistent) time — acceptable.

Fix direction (see MASTER_PLAN `P0-3`/`P1-2`): pick one frame. Recommended:
keep naive local for task wall-clock semantics, and make the catch-up loop use
`datetime.now()` (naive local) instead of UTC — a two-line change — plus a
guard that `calculate_next` made progress.
