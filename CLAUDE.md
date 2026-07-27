# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

TaskHome is a Flask app that drives a USB Epson TM-T20IIIL thermal receipt
printer. It prints scheduled/recurring **task** receipts (chore reminders with
QR codes), **SeeClickFix** civic issues, and **NOAA weather alerts**, via
polling "listeners". It runs as a LAN home appliance on port 5000.

The physical printer is the point, and most of the non-obvious design follows
from paper being irreversible: a receipt that prints twice cannot be
un-printed, and a receipt that never prints is silently gone unless something
remembers it.

## Commands

```sh
./scripts/run.sh                   # repairs the venv if broken, then serves
.venv/bin/python app.py            # direct, no health check

./scripts/setup-venv.sh            # create or repair .venv + install requirements
./scripts/setup-venv.sh --check    # read-only health report; exit 1 if unhealthy
./scripts/setup-venv.sh --force    # discard and rebuild

.venv/bin/python -m pytest -q      # ~570 tests, no printer and no network
.venv/bin/python -m pyflakes taskhome/
```

Serves on `0.0.0.0:5000` by default. Override with `TASKHOME_HOST` /
`TASKHOME_PORT`, or `host` / `port` in `config.json`. **On macOS port 5000 is
taken by AirPlay Receiver** — the committed IDE run configurations use 5001.

`TASKHOME_DEV=1` re-reads templates and CSS on every request. Python changes
still need a restart.

Dependencies are pinned in `requirements.txt`. Python is **3.13** (Homebrew).
If `.venv` breaks again — it previously pointed into a deleted Xcode beta —
`setup-venv.sh` detects and rebuilds it; prefer a Homebrew interpreter over
`/usr/bin/python3`, which dispatches through `xcode-select` and can vanish the
same way.

## Architecture

`app.py` is 20 lines of entry point. The application is the `taskhome` package;
`taskhome/README.md` has the module table. `create_app(load=, with_scheduler=)`
is an app factory — importing the package has **no side effects**, which is
what stops a script or a test from starting a scheduler against live data.

- **State** — `config`, `tasks`, `history`, `listeners` live in
  `taskhome/state.py` and are the only mutable globals. Writes are atomic
  (temp file + fsync + `os.replace`), a store that failed to load is
  write-blocked, and `state.STATE_LOCK` guards cross-thread mutation.
  Cross-module reads go through the module object (`state.tasks`), never
  `from .state import tasks` — otherwise a rebinding is invisible to everyone
  else.
- **Scheduler** — one daemon thread, started only by
  `create_app(with_scheduler=True)`; `start_scheduler()` refuses a second. It
  runs `run_catchup()` once under the configured policy, then every 60s:
  `queue.drain()` → `run_due_tasks()` → `scf.poll_scf_listener()` →
  `listener_base.run_all()`. Draining first means a backlog clears in order
  rather than newest-first.
- **Printing** — `printing.print_blocks()` is the lowest level and is what the
  queue drains through. `print_task()` / `print_scf_issue()` build blocks, then
  call it. All of them return `True` **only if paper actually came out**.
- **Receipts** — one block list drives the printer, the ASCII preview and the
  HTML preview (`taskhome/receipt.py`), so the preview cannot drift from the
  paper. Layouts are data in `layouts.py`; `styles.py` adds user-editable
  templates, edited in the Receipt Studio at `/settings/receipts`.
- **Listeners** — `listeners/base.py` is a plugin interface. A listener
  declares `CONFIG_SCHEMA` and implements `poll()`; the runtime provides
  interval gating, watermarks, dedup, per-poll caps, backoff and queueing.
  `nws.py` is built on it; `scf.py` predates it and is still bespoke.
- **Web** — two blueprints, `main` (`web/routes.py`) and `pwa` (`web/pwa.py`).
  Server-rendered Jinja, everything vendored under `static/vendor/` so the UI
  works with no internet. Materialize and flatpickr are gone; the UI is Mica
  components plus native `<dialog>` and `datetime-local`.

`docs/` contains the full system documentation. `docs/agent-plans/MASTER_PLAN.md`
is the roadmap with stable item IDs (`P0-1` …) and a decision log — check it
before fixing bugs or adding features.

## Ground rules

- **The user's live data is in `data/`**: `config.json`, `tasks.json`,
  `history.json`, `listeners.json`, `queue.json`, plus `cache/`, `backups/`
  and `styles/`. Gitignored on purpose. `queue.json` holds receipts that have
  not printed yet, so deleting it loses paper. **Set `TASKHOME_DATA_DIR` to a
  scratch copy for any experimental run.** An override means *the data lives
  here*, not *go and fetch it from the repo root* — getting that backwards
  displaced the live install once. A real `tasks.json` was destroyed during
  development; treat these files as irreplaceable.
- **Printing has physical side effects.** Never call `print_task`,
  `print_scf_issue`, `/test_print` or `/test_scf_print` casually, and never
  fire a test print unless the user explicitly asks.
- **Commit to master; do not push.** Work stays local.

## Conventions & gotchas

- **Two time frames, on purpose**: task times and print history are **naive
  local wall-clock** (`parse_task_time()` normalises anything aware); listener
  watermarks and queue timestamps are **aware UTC** (`parse_utc()`). A
  wall-clock reminder and an instant are different kinds of value. Comparing
  across them does not raise — it is just wrong by your UTC offset.
- **A failed print is handled differently by kind.** A *listener* receipt is
  queued: its polling window has already moved past the item, so without the
  queue it is gone. A *task* receipt is **not** queued, because the task
  staying due is already a durable retry. Doing both gave one occurrence two
  retry mechanisms and printed it twice when the printer came back.
- **Printer line spacing is clamped.** `ESC 3 n` uses the vertical motion unit
  (1/203" here, so 1 unit = 1 dot) and the printer silently floors spacing at
  character height (~34 dots). Smaller values are ignored, which reads as "my
  change did nothing". Hence `receipt.MIN_LINE_DOTS = 40`.
- **Column widths are measured, not assumed**: font A is 48 columns, font B is
  64, verified on hardware. The printer hard-wraps mid-word at the column
  limit, so text is pre-wrapped with the same `wrap()` the preview uses.
- **Adding a listener should touch nothing outside `listeners/`.** Settings
  render from `CONFIG_SCHEMA` (`templates/partials/setting_field.html`);
  history badges and type filters come from the registry. Editing a template
  to add a listener means something is hardcoded that should not be — NWS
  alerts were badged "Task" for exactly that reason.
- **A listener that filters inside `poll()` is invisible.** Override
  `should_print(config, item)`; the runtime calls it, logs the reason, and
  still marks the item seen. `should_print` was defined and unit-tested but
  never called for a while, so the entire NWS configuration surface did
  nothing. Test through `base.run()`, not the method alone.
- **Invariants worth not breaking** (the tests will catch regressions):
  - `calculate_next` returns its input unchanged to mean "cannot advance".
    Never loop on it — `advance_schedule` raises `ScheduleError` instead.
  - A schedule advances only after a successful print.
  - A store that failed to load is never written to.
  - Skipped/disabled tasks stay visible in the UI with a reason.
  - Queued jobs are **parked**, never dropped, and drain in order.
- `load_data()` performs implicit migrations: adds `enabled: true` to tasks,
  `type: 'task'` to old history records, converts theme `high-contrast` →
  `system`, creates a default `listeners.json`, and moves legacy root-level
  JSON into `data/` idempotently.
- Logs go to `logs/taskhome.log` through a rotating file handler at INFO by
  default (`log_level` in config), and to the console.
- **Tests**: `tests/conftest.py` has two autouse fixtures — one fails the test
  if any real JSON file changes, one replaces the escpos device constructor
  and *fails the test* if it is reached. Raising alone is not enough; the print
  paths have broad excepts that swallow it. `tests/test_static_analysis.py`
  runs pyflakes, and exists because the package split introduced three defects
  no behavioural test could catch.
- Git identity: `mica-alex <83238954+mica-alex@users.noreply.github.com>`.
  Short imperative subjects.
- `docs/agent-plans/` is gitignored (agent working documents, not source).
- Comments explain *why*, especially where the obvious approach is wrong — a
  lot of this code looks arbitrary until you know which failure it avoids.
  House style: `/Users/ahawk/GitProjects/dev-configurations`.

## File map

| Path | What |
| --- | --- |
| `app.py` | 20-line entry point; the app is `taskhome/` |
| `taskhome/__init__.py` | `create_app()`, blueprint registration, dev template reload |
| `taskhome/constants.py` | Paths, `DATA_DIR`, `VERSION`, `DEFAULT_CONFIG`, USB ids |
| `taskhome/state.py` | The only mutable state, plus `STATE_LOCK` |
| `taskhome/storage.py` | Atomic writes, load-failure tracking, migrations, backups |
| `taskhome/scheduler.py` | The 60-second tick and catch-up |
| `taskhome/recurrence.py` | `calculate_next`, `advance_schedule`, catch-up policy |
| `taskhome/queue.py` | Durable print queue: backoff, parking, ordered drain |
| `taskhome/printing.py` | ESC/POS layer; `print_blocks` is the lowest level |
| `taskhome/receipt.py` | Shared renderer: ESC/POS, ASCII and HTML from one block list |
| `taskhome/layouts.py` | Receipt layouts as data |
| `taskhome/styles.py` | User-editable receipt templates; kinds come from the registry |
| `taskhome/settings.py`, `logsetup.py` | Port/host resolution; rotating log setup |
| `taskhome/listeners/base.py` | Plugin interface: `CONFIG_SCHEMA`, `run()`, registry |
| `taskhome/listeners/scf.py` | SeeClickFix — predates the interface, still bespoke |
| `taskhome/listeners/nws.py` | NOAA weather alerts |
| `taskhome/listeners/feeds.py` | RSS/Atom digest |
| `taskhome/listeners/calendar.py` | ICS agenda (recurrence via `dateutil.rrule`) |
| `taskhome/listeners/brief.py` | Morning brief — composes the others |
| `taskhome/listeners/binday.py` | Bin collection reminder |
| `taskhome/listeners/webhook.py` | Inbound `POST` — the one **push** listener |
| `taskhome/lists.py` | Checklists: a mini-app, not a listener |
| `taskhome/web/routes.py` | The `main` blueprint |
| `taskhome/web/pwa.py` | The `pwa` blueprint: manifest, service worker |
| `taskhome/web/forms.py`, `pagination.py` | Validation helpers; paging, search, history kinds |
| `taskhome/templates/base.html` | Layout, Mica appbar, theme JS, PWA meta, SW registration |
| `taskhome/templates/index.html` | Dashboard: printer status, task table, recent history |
| `taskhome/templates/tasks.html` | Task CRUD, paged/filtered history table |
| `taskhome/templates/settings.html` | Config form, printer info, async test prints |
| `taskhome/templates/listener.html` | Listeners index (cards) |
| `taskhome/templates/listener_scf.html` | SeeClickFix form + category browser |
| `taskhome/templates/listener_settings.html` | Generic settings page, rendered from a schema |
| `taskhome/templates/receipt_studio.html` | Receipt Studio with live preview |
| `taskhome/templates/queue.html` | Print queue: retry, release, discard |
| `taskhome/templates/service-worker.js` | Offline shell (secure contexts only) |
| `taskhome/templates/partials/` | `setting_field.html`, `history_row.html`, `pager.html`, `receipt_rows.html`, `task_form.html`, `task_status.html`, `timestamp.html` |
| `taskhome/static/mica.css` | The whole stylesheet — Mica design language |
| `taskhome/static/ui.js` | Toasts, dialogs, timestamp localisation |
| `taskhome/static/settings.js` | Generic bindings for the schema renderer |
| `taskhome/static/listener.js`, `studio.js` | SCF category browser; Receipt Studio |
| `taskhome/static/vendor/` | Inter, Material Icons, `mica-tokens.css` — no CDNs |
| `taskhome/static/icons/` | PWA icon set (`any`, `maskable`, apple-touch, favicon) |
| `data/*.json` (gitignored) | Live datastore — see `docs/data-model.md` |
| `deploy/` | systemd unit, launchd plist, udev rules, `install.sh`, healthcheck |
| `scripts/` | `setup-venv.sh`, `run.sh`, `dry_run.py`, `backups.py`, `calibrate_printer.py`, `print_sample.py` |
| `.idea/runConfigurations/`, `.vscode/` | Committed run/debug configs |
| `tests/` | pytest suite; needs no printer and no network |
| `docs/` | System documentation; `docs/agent-plans/MASTER_PLAN.md` is the roadmap |
