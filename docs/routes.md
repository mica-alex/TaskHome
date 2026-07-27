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

## `GET /` — `index()` (`app.py:400-405`)

Probes the printer (`is_printer_connected()` — one `usb.core.find` per page
load), passes `status`, `config`, enabled tasks, and `history[:5]` to
`index.html`. The history table header says "Last 5 Prints" and matches.

## `GET /task_page` — `task_page()` (`app.py:408-411`)

Enabled tasks + the **entire** history list, unpaginated (`tasks.html` renders
every record; MASTER_PLAN `P2-1`). Note disabled tasks are invisible here too —
there is no UI to re-enable a disabled task except editing... which is also
impossible since edit modals only render for visible tasks. Disabling a task
via its edit modal makes it unreachable from the UI (MASTER_PLAN `P0-13`).

## `/settings` — `settings()` (`app.py:414-435`)

- GET: renders config form + printer info block
  (manufacturer/model/connection constants + live status probe).
- POST with `clear_history` present (the "Clear History" submit button):
  empties history, saves, redirects. **No confirmation step.** Other form
  values in the same submission are ignored.
- POST otherwise: `config['max_history'] = int(request.form['max_history'])`
  (ValueError/KeyError → 500; values ≤ 0 pass the server untouched — the form's
  `min="1"` is client-side only — and break history truncation),
  `hostname` and `theme` stored verbatim, config saved, history truncated and
  saved, redirect.

## `POST /test_print` — `test_print()` (`app.py:438-457`)

Builds a hardcoded sample task and calls `print_task()` — real paper, real
history record. Returns `'Test print successful! <a href="/settings">Back</a>'`
etc. as a bare string. Because `print_task` swallows its own exceptions, the
"successful" branch is returned even when printing failed; the only honest
failure is the up-front `is_printer_connected()` check
(MASTER_PLAN `P0-10`). `settings.html` calls this via `fetch()` and decides
which toast to show by `txt.includes('successful')`.

## `POST /test_scf_print` — `test_scf_print()` (`app.py:460-485`)

Same pattern with a hardcoded SCF issue (`media.image_full: null`, exercising
the "Has Media: No" path). Same misleading-success caveat.

## `POST /add_task` — `add_task()` (`app.py:488-505`)

Consumes: `title` (required — KeyError → 500 if absent), `next_time`,
`recurring`, `enabled` (checkbox presence), `extra`, `url`, `days` (multi,
only kept when `recurring == 'custom'`).

- `next_time` handling: `request.form['next_time'] + ':00'` when non-empty
  (flatpickr submits `Y-m-d H:i`, so this appends seconds), else
  `datetime.now().isoformat()`. No server-side validation that the result
  parses — garbage in the field becomes a stored `next_time` that poisons the
  scheduler tick (MASTER_PLAN `P0-6`).
- `days`: `[int(d) for d in request.form.getlist('days')]` — non-numeric →
  500; **empty list accepted** for `custom` → infinite loop when due
  (MASTER_PLAN `P0-2`).

## `/edit_task/<task_id>` — `edit_task()` (`app.py:508-533`)

- GET: renders `tasks.html` with `edit_task` context (note: the template never
  uses `edit_task` — editing actually happens through the per-task modals
  rendered on `/task_page`; this GET branch is effectively dead code).
- POST: same fields as add. Two extra hazards:
  - The edit modal pre-fills `next_time` with the stored ISO value
    (`2025-08-26T21:00:00`). flatpickr normally rewrites it to `Y-m-d H:i` on
    init; if flatpickr fails to load (CDN offline), submitting appends `:00`
    to the raw ISO → `2025-08-26T21:00:00:00`, unparseable → scheduler tick
    poisoned (MASTER_PLAN `P0-6`).
  - Absent/empty `extra`/`url` **delete** those fields (differs from add, which
    just omits them). Empty `next_time` keeps the old value.

## `POST /delete_task` — `delete_task()` (`app.py:536-542`)

Consumes `id` (`request.form['id']` — 500 if absent). Rebinds the global
`tasks` list (filter-out); no confirmation, no undo (history is unaffected).

## `/listener` — `listener()` (`app.py:546-557`)

- GET: renders form from `listeners.get('scf', {})` — safe when key missing.
- POST: rebuilds `listeners['scf']` from the form; hazards:
  `listeners['scf'].get('last_check')` → **KeyError/500 when the `scf` key is
  absent** from a hand-edited `listeners.json`, and
  `int(request.form.get('interval', 10))` → 500 on empty/non-numeric input
  (MASTER_PLAN `P0-9`). `request_types` is stored verbatim with no validation.

## Template/front-end notes

- Jinja autoescaping is on (Flask default) — user-entered titles etc. are XSS-safe.
- `base.html` pulls Materialize, Material Icons, and flatpickr from CDNs; with
  no internet the selects are invisible (`styles.css` hides native selects) and
  the datetime picker (and the `+ ':00'` normalization it provides) is gone
  (MASTER_PLAN `P0-15`).
- Theme: `data-theme="system"` is resolved to `dark`/`light` client-side on
  load; because the attribute is overwritten, the `matchMedia` change listener
  never re-fires meaningfully — OS theme flips mid-session are ignored until
  reload (MASTER_PLAN `P2-8`).
- Edit-modal bug: the recurring-select change handler looks for
  `[id^="custom-days"]` but edit modals use ids `custom_days_<uuid>`
  (underscore) — choosing "Custom" in an edit modal throws a TypeError and
  never reveals the weekday checkboxes (`tasks.html:186`, MASTER_PLAN `P0-14`).
- History listing quirk: for SCF rows both `index.html:55` and `tasks.html:54`
  render `SCF: {{ item.category }} - {{ item.id }}` when `item.summary` exists —
  the id is shown where the summary was presumably intended (MASTER_PLAN `P2-10`).
- The test-print buttons don't disable while a request is in flight —
  double-click = double paper.
