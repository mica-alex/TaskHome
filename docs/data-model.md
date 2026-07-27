# Data Model & Storage

The datastore is four JSON files in the process working directory (paths are the
bare constants at `app.py:23-26`, resolved relative to CWD — always run from the
repo root). All four are gitignored and contain the user's real data.

> **Planned change (not current behavior):** MASTER_PLAN `P1-9` relocates all
> mutable state into a `data/` directory (`data/config.json`, …, plus
> `data/styles/`, `data/cache/`, `data/push/`, `data/backups/` and a sibling
> `logs/`), with an idempotent startup migration that moves root-level legacy
> files. Until that lands, everything below describes the root-level layout.

Writes are whole-file, non-atomic rewrites (`save_config/tasks/history/listeners`,
`app.py:96-113`): `open(path, 'w')` + `json.dump`. A crash mid-write truncates
the file; on next start `load_data()` catches the JSON error, logs it, and
continues with defaults — silently discarding data (MASTER_PLAN `P0-5`).

## config.json

```json
{"max_history": 505, "hostname": "localhost", "theme": "system"}
```

| Key | Type | Meaning | Used at |
| --- | --- | --- | --- |
| `max_history` | int | History cap; history is truncated to this length after every print and on settings save | `app.py:224,297,426` |
| `hostname` | string | Host used to build the QR fallback URL on task receipts (`http://<hostname>:5000/task_page#<id>`) | `app.py:185,449` |
| `theme` | string | `system` \| `light` \| `dark`; stamped on `<html data-theme>` | `templates/base.html:2` |

**Caveat:** `load_data()` REPLACES `config` with the file's contents — it does
not merge with the in-code defaults (`app.py:50`). A `config.json` missing
`hostname` makes `print_task` raise `KeyError` before printing; missing
`max_history` makes the post-print history truncation raise, so the print
succeeds physically but is never saved to history (MASTER_PLAN `P0-9`).

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
| `id` | string (UUID4) | yes | Generated at `app.py:491` |
| `title` | string | yes | Printed large/bold on the receipt |
| `next_time` | string | yes | **Naive local** ISO 8601, no timezone suffix. From the add form: flatpickr's `Y-m-d H:i` value + `":00"` appended server-side (`app.py:493`). Empty form value → `datetime.now().isoformat()` (includes microseconds) |
| `recurring` | string | yes | `none` \| `daily` \| `weekly` \| `monthly` \| `every_weekday` \| `first_day_month` \| `custom` |
| `enabled` | bool | yes (migrated in) | Disabled tasks are skipped by the scheduler and hidden from every page |
| `extra` | string | no | Second line on receipt; key is absent (not empty) when unset |
| `url` | string | no | Overrides the QR code target; absent when unset |
| `days` | int array | only when `recurring == "custom"` | Weekdays, Python convention: 0=Mon … 6=Sun. **Can legally be empty** (form allows zero checked boxes) — an empty list makes `calculate_next` loop forever (MASTER_PLAN `P0-2`) |

One-off tasks (`recurring: "none"`) are **deleted from tasks.json** after they
print (`app.py:331-332`); their record survives only in history.

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
| `request_types` | string | Comma-separated SCF request-type IDs, passed verbatim as the API's `request_types` param. No validation, no friendly names cached |
| `interval` | int | Minimum minutes between polls (checked against `last_check`; the scheduler still ticks every 60s) |
| `last_check` | string \| null | UTC `YYYY-MM-DDTHH:MM:SSZ` of the last poll; also used as the `after` param of the next poll. Dual-purpose — see [listeners.md](listeners.md) |

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
