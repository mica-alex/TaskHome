# HTTP Routes Reference

All routes are unauthenticated and CSRF-unprotected. Form POST handlers mutate
the global state, persist via `save_*()`, and redirect; the `/api/` routes
return JSON; the two test-print routes signal outcome by status code.

Two blueprints: `main` (`web/routes.py`) and `pwa` (`web/pwa.py`).

### Pages

| Route | Methods | Renders / Returns |
| --- | --- | --- |
| `/` | GET | `index.html` — printer status, tasks, recent history |
| `/task_page` | GET | `tasks.html` — task CRUD, paged/filtered history |
| `/edit_task/<task_id>` | GET, POST | `tasks.html` (edit context) / redirect |
| `/settings` | GET, POST | `settings.html` / redirect |
| `/settings/receipts` | GET | `receipt_studio.html` — live preview editor |
| `/listener` | **GET** | `listener.html` — the listeners **index** |
| `/listener/scf` | GET, POST | `listener_scf.html` / redirect |
| `/listener/settings/<name>` | GET, POST | `listener_settings.html` — rendered from `CONFIG_SCHEMA`; 404 for an unregistered name |
| `/queue` | GET | `queue.html` — the print queue |

### Form POSTs

| Route | Methods | Returns |
| --- | --- | --- |
| `/add_task` | POST | redirect → `/task_page` |
| `/delete_task` | POST | redirect → `/task_page` |
| `/test_print` | POST | status code (200 / 500 / 503) |
| `/test_scf_print` | POST | status code (200 / 500 / 503) |

### JSON

| Route | Methods | Purpose |
| --- | --- | --- |
| `/api/receipt/preview` | POST | Render a template to preview rows |
| `/api/receipt/templates/<kind>` | POST | Create/update a template |
| `/api/receipt/templates/<kind>/<name>` | DELETE | Delete a template |
| `/api/receipt/activate/<kind>/<name>` | POST | Make a template active |
| `/api/receipt/test_print/<kind>` | POST | Print a sample |
| `/api/scf/browse` | GET | Request types available at a lat/lng |
| `/api/scf/names` | POST | Resolve request-type ids to names |
| `/api/queue/retry` | POST | Release parked jobs and drain |
| `/api/queue/<job_id>` | DELETE | Discard one job, or all when `job_id == 'all'` |

### PWA (`pwa` blueprint)

| Route | Methods | Purpose |
| --- | --- | --- |
| `/manifest.webmanifest` | GET | Web app manifest; `application/manifest+json` |
| `/service-worker.js` | GET | Offline shell. Served from the **root** so its scope covers the app, and `no-cache` on itself so a stale copy cannot permanently stick |

## `GET /` — `index()`

Probes the printer (`is_printer_connected()` — one `usb.core.find` per page
load), passes `status`, `config`, **all** tasks, and `history[:5]` to
`index.html`.

## `GET /task_page` — `task_page()`

All tasks, plus **one page** of history. Filtering and paging are server-side,
so the browser is never handed the whole list.

| Query param | Meaning | Behaviour on bad input |
| --- | --- | --- |
| `page` | 1-based page number | Clamped into range — an out-of-range bookmark shows the last page, not a blank table |
| `per_page` | 25 / 50 / 100 / 250 | Anything else falls back to 25 |
| `q` | Free text; all terms must match | Searches title, extra, category, address, summary, description, status, id |
| `type` | `task` or `scf` | Unknown values are ignored rather than matching nothing |

Every pager link carries the current `q` and `type`, so paging never silently
drops a search, and every view is bookmarkable. The dashboard's five-item list
at `/` stays deliberately unpaginated.

Both pages render disabled tasks rather than filtering them out, with a status
badge distinguishing *Missed*, *Error* and *Disabled*. Hiding them previously
made a disabled task unreachable from the UI — including one the scheduler had
disabled itself — so there was no way to re-enable it (`P0-13`).

## `/settings` — `settings()`

- GET: renders config form + printer info block (constants + live status probe).
- POST with `clear_history`: empties history, saves, redirects. The button
  confirms client-side first. Other form values in that submission are ignored.
- POST otherwise: validates `max_history` (integer, 0–100000) and `theme`
  (one of `system`/`light`/`dark`); a blank `hostname` falls back to the
  default. Invalid input returns **400** with `error.html` rather than a 500.

## `POST /test_print`, `POST /test_scf_print`

Build a hardcoded sample payload and call the real print function — **real
paper, real history record**.

Status codes are honest: `200` on success, `500` when the print failed, `503`
when the printer is absent. `settings.html` decides its toast from `resp.ok`,
having previously matched the body against the substring `"successful"` — which
reported success unconditionally, since the print functions swallow their own
exceptions (`P0-10`). Buttons disable in flight so a double click cannot emit
two receipts.

## `POST /add_task`, `POST /edit_task/<id>`

Both build the task through `task_from_form()`, which validates and raises
`ValidationError` (→ 400 + `error.html`) rather than letting bad input reach the
datastore:

| Field | Rule |
| --- | --- |
| `title` | Required, non-blank after stripping |
| `recurring` | Must be one of the seven known modes |
| `next_time` | Parsed and re-serialised; unparseable is rejected. Empty → now (add) or unchanged (edit) |
| `days` | Integers 0–6, deduped and sorted. **Required and non-empty** when `recurring == 'custom'`, since an empty list yields a schedule that can never advance (`P0-2`) |
| `enabled` | Checkbox presence |
| `extra`, `url` | Optional; blank removes the key |

Edits validate against a **copy**, and the live task is only replaced once
everything passes — a rejected edit leaves it untouched instead of
half-applied. A successful edit also clears `schedule_error` and `missed`,
since the user has just restated what the schedule should be.

`GET /edit_task/<id>` renders `tasks.html` with an `edit_task` context that the
template never uses — editing happens through the per-task modals on
`/task_page`. This branch is effectively dead code.

## `POST /delete_task`

Consumes `id`. Missing id → 400; unknown id → 404. Mutates the task list in
place rather than rebinding the global. No confirmation, no undo (history is
unaffected).

## `/listener/scf` — `listener_scf()`

- GET: renders the form from `listeners.get('scf', {})`.
- POST: validates `interval` (integer, 1–1440) and normalises `request_types`
  (trims whitespace, drops empty entries). Preserves the existing `last_check`
  via `listeners.get('scf') or {}` — indexing it directly raised `KeyError` →
  500 on a fresh install (`P0-9`).

## Error responses

Validation failures render `error.html` with a message and a back link, at the
appropriate 4xx status. This is deliberately plain: the submitted values are
lost on the way there, which is why MASTER_PLAN `P2-4` replaces it with inline
field errors and toasts once the `/api/` layer exists.

## Template/front-end notes

- Jinja autoescaping is on (Flask default) — user-entered titles are XSS-safe,
  and `tests/test_routes.py` asserts it.
- All front-end assets are **vendored** under `static/vendor/`; no page makes an
  external request, so the UI works with no internet (`P0-15`).
- Theme: the user's setting lives in `data-theme-mode` and the resolved theme in
  `data-theme`, so OS light/dark flips are followed for the whole session. The
  script runs in `<head>` before first paint, avoiding a flash of the wrong
  theme (`P0-16`).
- History listing quirk: for SCF rows both `index.html` and `tasks.html` render
  `SCF: {{ item.category }} - {{ item.id }}` when `item.summary` exists — the id
  appears where the summary was presumably intended (MASTER_PLAN `P2-10`).
