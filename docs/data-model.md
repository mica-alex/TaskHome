# Data Model & Storage

The datastore is four JSON files in **`data/`**, resolved relative to the repo
root (the location of `app.py`), not the process working directory. Override
with `TASKHOME_DATA_DIR` — which is what tests and any throwaway run should use.

```
data/config.json   data/tasks.json   data/history.json   data/listeners.json
```

All four are gitignored and contain the user's real data.

### Migration from the old root-level layout

TaskHome was historically run straight out of a git clone with these files in
the repo root. On startup, `migrate_legacy_data_files()` moves any it finds
there into `data/`, so existing installs keep working with no manual step. It
is idempotent and runs before anything reads or writes.

| Situation | Behavior |
| --- | --- |
| File only in the root | Moved into `data/` |
| File only in `data/` | Left alone |
| File in **both** | `data/` wins (it is what the app reads); the root copy is renamed `<name>.superseded-<timestamp>`, never deleted |
| Data dir not creatable (read-only FS) | Logged; the app continues against the legacy location rather than refusing to start |
| Move fails (cross-device) | Falls back to copy-then-remove, so the source survives until the copy lands |
| Any single file fails | The others still migrate; failures are logged individually |

A `DATA_MOVED.txt` breadcrumb is left in the root after a migration. It is
purely informational and can be deleted.

## Write safety

Writes go through `_save_json_file()`: write to a temp file in the same
directory, `fsync`, then `os.replace()`. Rename is atomic, so a reader or a
crash sees either the whole old file or the whole new one — never a truncated
one.

Loads are per-store. A file that is **missing** is fine and yields defaults. A
file that **exists but will not parse** marks that store failed, and every
subsequent save to it is refused with a loud log line until the file is fixed
and TaskHome restarts.

That refusal matters more than it looks. The original code wrapped the whole
load in one `try/except`, so a parse error left the in-memory list empty, and
the very next save wrote that empty list over the user's file — turning a
hand-repairable JSON error into permanent data loss. This is not hypothetical:
it destroyed a real `tasks.json` during development. See `P0-5`.

### Locking

`STATE_LOCK` (an `RLock`) guards structural mutation and serialisation. It is
reentrant because `record_history` holds it and calls `save_history()`, which
acquires it again — a plain `Lock` deadlocks there.

It is deliberately **not** held across printing or HTTP fetches, which take
seconds and would stall every page load. It covers the operations that can
corrupt state: append/remove/clear, and `json.dumps` reading a list another
thread is mutating.

Worth knowing before anyone decides it is redundant: on stock CPython 3.13 the
GIL already makes individual list operations and C-level `json.dumps` atomic,
so the races it prevents are not reachable today, and the test suite passes
without it. It is kept because that atomicity is a CPython implementation
detail rather than a language guarantee (free-threaded builds remove it), and
because it makes compound read-modify-write sequences correct by construction.
`tests/test_concurrency.py` records this in full.

## config.json

```json
{"max_history": 500, "hostname": "localhost", "theme": "system",
 "app_name": "TaskHome"}
```

| Key | Type | Meaning | Used at |
| --- | --- | --- | --- |
| `max_history` | int | History cap; history is truncated after every print and on settings save |
| `hostname` | string | Host used to build the QR fallback URL on task receipts (`http://<hostname>:<port>/task_page#<id>`) |
| `theme` | string | `system` \| `light` \| `dark` |
| `host` | string | *Optional.* Bind address. Overridden by `TASKHOME_HOST`; defaults to `0.0.0.0` |
| `port` | int | *Optional.* Listen port. Overridden by `TASKHOME_PORT`; defaults to `5000` |
| `catchup` | object | *Optional.* Catch-up policy — see [scheduling.md](scheduling.md) |

```jsonc
"catchup": {
  "policy":              "skip",        // recurring tasks
  "oneoff_policy":       "print_once",  // recurring == "none"
  "recent_window_hours": 6,             // for print_if_recent
  "max_prints":          20             // cap for printing policies
}
```

`load_data()` **merges** the file over the in-code defaults rather than
replacing them, so a `config.json` missing a key no longer breaks code that
reads it. Previously a missing `hostname` made `print_task` raise before
printing, and a missing `max_history` made the post-print truncation raise — so
the receipt physically printed but was never recorded in history.

Invalid values degrade rather than crash: an unparseable port, a negative
`max_prints`, or an unknown `catchup.policy` logs a warning and falls back to
the default.

## tasks.json

A JSON array of task objects. Real example:

```json
[
  {"id": "6ef1b365-8971-49d4-b1d0-705ddc446133", "title": "Play with Sara",
   "next_time": "2025-08-26T21:00:00", "recurring": "daily", "enabled": true,
   "extra": "MISS KITTY TIME"},
  {"id": "517e6806-72a4-4355-a8b8-af42414b9dac", "title": "Take Medicine",
   "next_time": "2025-08-27T12:00:00", "recurring": "daily", "enabled": true}
]
```

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `id` | string (UUID4) | yes | Generated on add |
| `title` | string | yes | Printed large/bold on the receipt |
| `next_time` | string | yes | **Naive local** ISO 8601, no timezone suffix. Form input is parsed and re-serialised, so the stored value is canonical whatever the browser sends. Empty form value → now |
| `recurring` | string | yes | `none` \| `daily` \| `weekly` \| `monthly` \| `every_weekday` \| `first_day_month` \| `custom` |
| `enabled` | bool | yes (migrated in) | Disabled tasks are skipped by the scheduler. They remain **visible** in the UI with a status badge — hiding them made a missed one-off unrecoverable (`P0-13`) |
| `extra` | string | no | Second line on receipt; key is absent (not empty) when unset |
| `url` | string | no | Overrides the QR code target; absent when unset |
| `days` | int array | only when `recurring == "custom"` | Weekdays, Python convention: 0=Mon … 6=Sun. Deduped and sorted. An empty list is rejected at ingress, since it yields a schedule that can never advance (`P0-2`) |
| `catchup` | string | no | Per-task catch-up policy override; `"inherit"` (or absent) defers to config. See [scheduling.md](scheduling.md) |

### Scheduler-written fields

These are set by the scheduler, not the user, and are absent until something
happens. They exist so that a task going quiet is never silent:

| Field | Type | Meaning |
| --- | --- | --- |
| `missed_count` | int | Cumulative occurrences skipped while TaskHome was down |
| `last_missed_at` | string | The most recent skipped occurrence |
| `missed` | bool | A one-off whose time passed unprinted. Also sets `enabled: false`, since it has no future occurrence and would otherwise fire immediately |
| `schedule_error` | string | Why the task was disabled — an unadvanceable recurrence or an unparseable `next_time`. Cleared when the task is edited |
| `print_failures` | int | Consecutive failed print attempts; cleared on success |
| `last_print_failure` | string | Timestamp of the most recent failure |

One-off tasks (`recurring: "none"`) are **deleted from tasks.json** after a
successful print; their record survives only in history. A one-off whose print
*fails* is kept and retried, so an offline printer cannot silently consume it.

## history.json

A JSON array, newest first, capped at `config['max_history']`. Contains two
record shapes distinguished by `type`.

### `type: "task"` — a printed task

The full task object at print time, plus `print_time` and `type`
(`app.py:222`). Real example:

```json
{
  "id": "5c1c7096-77d7-4e72-ae2e-de4ab7a097e5",
  "title": "Test Task Print",
  "extra": "This is a test print from TaskHome",
  "url": "http://localhost:5000/task_page#test",
  "next_time": "2025-08-26T09:36:53.841688",
  "recurring": "none",
  "enabled": true,
  "print_time": "2025-08-26T09:36:54.106869",
  "type": "task"
}
```

- `print_time` is naive local ISO (`datetime.now().isoformat()`).
- Optional task fields (`extra`, `url`, `days`) appear only if the task had them.
- `/test_print` receipts land here too, indistinguishable from real tasks except
  by their `Test Task Print` title.

### `type: "scf"` — a printed SeeClickFix issue

A **projection** of the API issue (not the raw payload), built at
`app.py:284-295`. Real example:

```json
{
  "type": "scf",
  "id": 12345678,
  "category": "Streetlight Out",
  "summary": "Streetlight outage reported",
  "address": "123 Main St, Springfield",
  "reported_at": "2025-08-26T13:36:42Z",
  "status": "Open",
  "description": "The streetlight in front of my house is not working.",
  "url": "https://seeclickfix.com/issues/12345678",
  "print_time": "2025-08-26T09:36:42.921326"
}
```

- `id` is the SCF numeric issue id (an **int**, unlike task string UUIDs).
- `reported_at` is the API's `created_at` verbatim. The live API emits offset
  timestamps like `2026-07-23T18:53:48-04:00`; the test route emits `...Z`.
- `category` is `request_type.title`, or `"Unknown Category"` if absent.
- `print_time` is naive local, same as tasks.
- There is **no dedup key usage**: nothing prevents the same SCF `id` appearing
  multiple times in history (MASTER_PLAN `P0-7`).

## listeners.json

```json
{"scf": {"enabled": true, "request_types": "6632,6634,6630,6628,20840",
         "interval": 5, "last_check": "2025-08-26T13:35:21Z"}}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `enabled` | bool | Master switch for the SCF poll |
| `request_types` | string | Comma-separated SCF request-type IDs, passed as the API's `request_types` param. Normalised on save (whitespace trimmed, empties dropped) but **not** validated against the API, and no friendly names are cached yet — MASTER_PLAN `P4-1`/`P4-2` |
| `interval` | int | Minimum minutes between polls, 1–1440 (checked against `last_check`; the scheduler still ticks every 60s) |
| `last_check` | string \| null | UTC `YYYY-MM-DDTHH:MM:SSZ`, taken **before** the fetch. Doubles as the `after` param of the next poll — see [listeners.md](listeners.md) |

Runtime state, written by the poller and not exposed in the form:

| Field | Type | Meaning |
| --- | --- | --- |
| `seen` | int array | Recently printed issue ids, oldest first, trimmed to 2000. Makes the deliberately-overlapping poll windows harmless. An issue that failed to print is deliberately absent so it retries |
| `consecutive_failures` | int | Failed fetches in a row; reset on success |
| `backoff_until` | string | UTC instant before which polling is skipped. Exponential, capped at 60 minutes. Removed on success |
| `last_error` | string | Most recent fetch error. Removed on success |

If `listeners.json` is missing, `load_data()` creates it with defaults
(`enabled: false`, `request_types: "6632,6634"`, `interval: 10`) — this is the
only file it creates (`app.py:88-90`).

## Implicit migrations in `load_data()`

Performed in memory on every load; only persisted when the next `save_*()`
happens to run:

| Migration | Where | Effect |
| --- | --- | --- |
| Theme `high-contrast` → `system` | `app.py:52-54` | Legacy theme value converted |
| Task `enabled` default | `app.py:63-66` | Tasks missing `enabled` get `true` |
| History `type` default | `app.py:75-77` | History records missing `type` get `"task"` (pre-SCF records) |
| Default `listeners.json` | `app.py:83-90` | Created (and saved immediately) if the file is absent |

## Consistency hazards (documented, not fixed)

- No lock: the scheduler thread and request threads mutate the same lists and
  call the same `save_*()` functions concurrently; interleaved writes can
  corrupt a file (MASTER_PLAN `P0-5`).
- `delete_task` and the settings "clear history" REBIND the globals
  (`tasks = [...]`, `history = []`) while the scheduler may be iterating the old
  object (`app.py:417-419,539-540`).
- `history[:config['max_history']]` with `max_history <= 0` (settable via a
  hand-edited config or a crafted POST; the form's `min="1"` is client-side
  only) empties or mis-truncates history (MASTER_PLAN `P0-9`).
