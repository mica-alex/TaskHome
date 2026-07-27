# HTTP Routes Reference

All routes are unauthenticated and CSRF-unprotected. POST handlers mutate the
global state, persist via `save_*()`, and redirect (except the two test-print
routes, which return raw HTML strings). Form field access uses
`request.form['x']` in several places — a missing field is a `KeyError` → HTTP
500 (noted per route).

| Route | Methods | Renders / Returns |
| --- | --- | --- |
| `/` | GET | `index.html` |
| `/task_page` | GET | `tasks.html` |
| `/settings` | GET, POST | `settings.html` / redirect |
| `/test_print` | POST | raw HTML string |
| `/test_scf_print` | POST | raw HTML string |
| `/add_task` | POST | redirect → `/task_page` |
| `/edit_task/<task_id>` | GET, POST | `tasks.html` (edit context) / redirect |
| `/delete_task` | POST | redirect → `/task_page` |
| `/listener` | GET, POST | `listener.html` / redirect |

## `GET /` — `index()`

Probes the printer (`is_printer_connected()` — one `usb.core.find` per page
load), passes `status`, `config`, **all** tasks, and `history[:5]` to
`index.html`.

## `GET /task_page` — `task_page()`

All tasks + the **entire** history list, unpaginated (`tasks.html` renders
every record; MASTER_PLAN `P2-1` fixes this).

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

## `/listener` — `listener()`

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
