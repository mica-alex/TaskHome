# Data Model & Storage

The datastore is **SQLite**, in `data/taskhome.db`. `sqlite3` is in the Python
standard library, so this adds nothing anyone has to install.

`data/` is resolved relative to the repo root, not the process working
directory. Override with `TASKHOME_DATA_DIR` or `taskhome --data-dir` — which
is what tests and any throwaway run should use. An override means *the data
lives here*, never *go and fetch it from the repo root*.

Before `P1-2` this was four JSON files. On any install that predates the
migration they survive as `*.imported-<timestamp>` — never deleted, so a bad
migration is recoverable by hand.

```
data/
  taskhome.db              the datastore
  taskhome.db-wal          write-ahead log; normal, do not delete while running
  taskhome.db-shm          shared-memory index; likewise
  backups/                 pre-image snapshots (P6-2)
  cache/                   derived data, safe to delete at any time
    scf_request_types.json SeeClickFix category names
    nws_zones.json         ZIP -> forecast zone
    media/                 dithered SeeClickFix photos
  styles/<kind>/*.json     user-edited receipt templates
  *.json.imported-<stamp>  the pre-SQLite datastore, kept
```

Everything here is gitignored and contains the user's real data.

## Schema

Deliberately under-normalised. `tasks` and `history` get real tables because
they are queried; everything else is read and written whole, so columns would
buy nothing.

```sql
CREATE TABLE meta    (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE kv      (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE tasks   (id TEXT PRIMARY KEY, position INTEGER, payload TEXT);
CREATE TABLE history (rowid INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT UNIQUE,
                      type TEXT NOT NULL, print_time TEXT NOT NULL,
                      payload TEXT NOT NULL);
CREATE INDEX history_time ON history(print_time DESC);
CREATE INDEX history_type ON history(type);
```

`kv` holds `config`, `listeners`, `queue`, `lists` and `chores` as JSON blobs.

WAL mode is on, so readers do not block the writer — which matters because the
scheduler writes while a page is being served. `taskhome/db.py` keeps one
connection **per thread**, since a `sqlite3` connection is not safe to share
and this app has several: the scheduler, request handlers, and a push
listener's own network thread.

History has its own table because it is append-heavy and query-shaped: adding
one receipt used to rewrite the entire file.

## Access

Nothing outside `storage.py` and `db.py` touches the database. The rest of the
app reads module-level state:

| Name | Type | Holds |
| --- | --- | --- |
| `state.config` | dict | Settings, merged over `constants.DEFAULT_CONFIG` |
| `state.tasks` | list | Every task, in order |
| `state.history` | list | Print records, newest first, capped at `max_history` |
| `state.listeners` | dict | Per-listener settings **and** runtime state |

**Cross-module access goes through the module object** (`state.tasks`), never
`from .state import tasks` — rebinding a name imported the second way is
invisible to every other module.

`state.STATE_LOCK` guards mutation shared between threads, and mutations are
made in place (`state.tasks[:] = remaining`, `del state.history[:]`) rather
than by rebinding, for the same reason.

## Config

```json
{"max_history": 500, "hostname": "localhost", "theme": "system",
 "app_name": "TaskHome", "catchup": {...}, "styles": {...},
 "log_level": "INFO", "backups": {"keep": 20}, "host": "0.0.0.0", "port": 5000}
```

| Key | Meaning |
| --- | --- |
| `max_history` | Cap on print records |
| `hostname` | Used to build QR links on receipts |
| `theme` | `system`, `light` or `dark` |
| `app_name` | Shown in the browser tab and the PWA manifest |
| `catchup` | Policy for occurrences missed while down (`P1-10`) |
| `styles` | `{kind: template_name}` — the active receipt template per kind |
| `log_level`, `backups`, `host`, `port` | Operations; see operations.md |

`load_data()` merges over `constants.DEFAULT_CONFIG`, so a partial config is
safe and a new key gets its default without a migration (`P1-6`).

## Records

### Task

```json
{"id": "uuid", "title": "Take Medicine", "next_time": "2026-03-01T09:00:00",
 "recurring": "daily", "enabled": true,
 "extra": "with water", "url": "https://…", "days": [0, 2, 4],
 "catchup": "print_once",
 "print_failures": 0, "last_print_failure": "…", "schedule_error": "…"}
```

`next_time` is **naive local wall-clock**. `days` applies only to
`recurring: "custom"`; without it the schedule can never advance (`P0-2`).
`schedule_error` and `print_failures` are set by the scheduler and cleared by a
successful edit or print — a task carrying either stays visible in the UI with
a reason rather than silently vanishing (`P0-13`).

`recurring` is one of `constants.RECURRENCE_MODES`: `none`, `daily`, `weekly`,
`monthly`, `every_weekday`, `first_day_month`, `custom`.

### History

Every record has `uid`, `type` and `print_time`. `uid` is assigned when written
and **back-filled on load** for older records; it is what the reprint button
addresses, because list position stops being an identity once the table is
filtered or paged, and per-type ids collide across namespaces.

`print_time` is naive local. Records are capped at `config.max_history`.

| `type` | Written by | Notable fields |
| --- | --- | --- |
| `task` | `printing.print_task` | the whole task |
| `scf` | `printing.print_scf_issue` | `category`, `address`, `status`, `has_media` |
| `nws` | NOAA weather | `category` is the event, plus `severity` |
| `feeds` | RSS digest | `description` holds the headlines |
| `calendar` | Calendar agenda | `description` holds the events |
| `brief` | Morning brief | `description` lists the sections |
| `binday` | Bin day | `description` lists the bins |
| `webhook` | Webhook | `category` is the source |
| `mqtt` | MQTT | `category` is the topic |
| `github` | GitHub | `category` is "kind — repo" |
| `transit` | Transit | `category` is Departures or Alert |
| `packages` | 17TRACK | `category` is the carrier |
| `chores` | Chore charts | `title` is the person |
| `list` | A printed checklist | `title` is the list name |

The kinds offered in the UI come from `web/pagination.history_kinds()`, built
from the listener registry — so a new listener is filterable and correctly
badged without a template being touched.

### Listener blob

`state.listeners[name]` mixes user settings with runtime state. Settings come
from the listener's `CONFIG_SCHEMA`; runtime keys are written by
`listeners/base.run` and are never user-edited:

`last_check`, `seen` (trimmed to 2000), `consecutive_failures`,
`backoff_until`, `last_error`, plus per-listener extras such as `etags`,
`known_feeds`, `validators`, `last_agenda`, `last_board`, `recent`.

**A save merges rather than replaces**, because settings and runtime share the
blob — a save that dropped the watermark would replay the entire backlog.

### Queue job

```json
{"id": "uuid", "kind": "scf", "description": "…", "blocks": [...],
 "history": {...}, "queued_at": "…Z", "attempts": 0,
 "next_attempt": null, "last_error": null, "parked": false}
```

Holds **rendered blocks**, not a reference to the source. By the time a job
drains, the task may have been edited or deleted; rendering at enqueue time
freezes what was meant. The `history` record is attached but written only when
the job genuinely prints.

### Lists and chore charts

```json
{"id": "uuid", "name": "Groceries",
 "items": [{"id": "…", "text": "Milk", "done": false}]}

{"id": "uuid", "name": "Sara", "token": "…", "days": [0,1,2,3,4],
 "chores": ["Feed the cat"], "completed": ["2026-07-27"]}
```

A chore `token` authorises `/c/<token>`; it is compared in constant time and
can be rotated, which invalidates any chart already printed.

## Two time frames, on purpose

Task times and print history are **naive local wall-clock**; listener
watermarks and queue timestamps are **aware UTC**. A chore reminder is a
wall-clock time and a listener watermark is an instant — different kinds of
value. Comparing across them does not raise, it is simply wrong by your UTC
offset (`P0-3`). See [scheduling.md](scheduling.md).

## Migrations on load

`storage.load_data()` applies these every start, idempotently:

1. **Legacy file move** (`P1-9`) — root-level JSON into `data/`.
2. **SQLite import** (`P1-2`) — JSON into the database, from `data/` or the
   old repo root. **All-or-nothing**: any parse failure discards the partial
   database, leaves the JSON untouched, and retries next start. A half-migrated
   database would exist, switch the backend over, read as empty, and let the
   next save overwrite the only surviving copy.
3. `enabled: true` added to tasks lacking it.
4. `type: 'task'` added to old history records; `uid` back-filled and persisted.
5. Theme `high-contrast` → `system` (`P0-16`).
6. Default listener settings created if absent.

## Invariants

- A store that failed to load is **never written to**. That is the `P0-5`
  chain — bad parse, empty memory, save over it — and it holds across the
  SQLite boundary too.
- Writes are atomic: a transaction for the database, temp file plus `rename`
  for the caches that are still files.
- Backups are **pre-image** snapshots of what is about to be overwritten, named
  with microsecond precision so ordering is lexicographic.
- Nothing is deleted on migration — only renamed.

## Backup and export

`data/backups/` holds pre-image snapshots (`P6-2`), pruned to
`config.backups.keep`. `scripts/backups.py` lists and restores them.

`taskhome --export-json DIR` writes every store back out as JSON, so a backup
stays readable without sqlite to hand.
