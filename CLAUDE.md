# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

TaskHome is a single-file Flask app that drives a USB Epson TM-T20III thermal receipt printer. It prints scheduled/recurring "task" receipts (chore reminders with QR codes) and receipts for new SeeClickFix civic issues via a polling "listener". It runs as a LAN home appliance on port 5000.

## Commands

```sh
# Run (from repo root — data files are resolved relative to CWD)
./scripts/run.sh                   # repairs the venv if broken, then serves
.venv/bin/python app.py            # direct, no health check

# Environment
./scripts/setup-venv.sh            # create or repair .venv + install requirements
./scripts/setup-venv.sh --check    # read-only health report; exit 1 if unhealthy
./scripts/setup-venv.sh --force    # discard and rebuild
```

Serves on `0.0.0.0:5000` by default. Override with `TASKHOME_HOST` / `TASKHOME_PORT`, or `host` / `port` in `config.json`. **On macOS port 5000 is taken by AirPlay Receiver** — the committed IDE run configurations use 5001 for this reason.

Dependencies are pinned in `requirements.txt`. Python is **3.13** (Homebrew); 3.10+ syntax is fine. If `.venv` ever breaks again — it previously pointed into a deleted Xcode beta — `setup-venv.sh` detects and rebuilds it; prefer a Homebrew interpreter over `/usr/bin/python3`, which dispatches through `xcode-select` and can vanish the same way.

Tests: `.venv/bin/python -m pytest tests/ -q` (install `requirements-dev.txt` first). They need **no printer and no network** — `conftest.py` sets `TASKHOME_NO_INIT=1` before importing `app`, so the module reads no real data and starts no thread, and fixtures supply a fake printer and fake HTTP layer. No lint pipeline or CI yet (MASTER_PLAN `P1-6`).

## Architecture

Everything lives in `app.py` (~1252 lines):

- **Global mutable state**: `config`, `tasks`, `history`, `listeners` are module-level lists/dicts, loaded at import by `load_data()`. Writes are atomic (temp file + fsync + rename) and a store that failed to load is write-blocked, but there is **still no lock** — the scheduler thread and request handlers mutate the same objects (remaining half of `P0-5`, to land with `P1-2`).
- **Scheduler thread**: a daemon thread running `scheduler_loop()`, started at import unless `TASKHOME_NO_INIT=1`. It runs `run_catchup()` once (applying the configured catch-up policy), then loops every 60s: `run_due_tasks()` then `poll_scf_listener()`. `scf` is still the only listener and is called directly — no plugin abstraction yet (`P5-1`).
- **Printing**: `print_task()` / `print_scf_issue()` open the printer through the `open_printer()` context manager via `escpos.printer.Usb(0x04b8, 0x0e27, profile='TM-T20II')` (yes, the TM-T20**II** profile for a TM-T20**III** — intentional, it works). **Both return `True` only if paper actually came out**, and callers depend on that: the scheduler advances a schedule and the listener marks an issue seen only on success. History is written only after a successful print.
- **Web UI**: server-rendered Jinja templates styled with Materialize + flatpickr, **vendored under `static/vendor/`** so the UI works with no internet. Routes are classic form-POST-and-redirect; write routes validate and return 400 + `error.html` on bad input. `/test_print` and `/test_scf_print` signal outcome by status code (200/500/503), which `settings.html` reads via `resp.ok`.

`docs/` contains the full system documentation (architecture, data model, scheduling, printing, listeners, routes, operations). `docs/agent-plans/MASTER_PLAN.md` is the improvement roadmap with stable item IDs (P0-1 …) — check it before fixing bugs or adding features; known defects are catalogued there.

## Conventions & gotchas

- **`config.json`, `tasks.json`, `history.json`, `listeners.json` are the user's live data.** They are gitignored on purpose. Never commit them, never overwrite them with defaults, never "reset" them while testing. **Never run the app from the repo root to try something out** — the scheduler rewrites `tasks.json` and `listeners.json` on its own. Copy the four files to a scratch directory and run with that as CWD. (A real `tasks.json` was destroyed this way during development.) `load_data()` merges `config` over the in-code defaults, so a partial config file is safe.
- **Printing has physical side effects** — every print path call emits real paper from a real printer. Never call `print_task`, `print_scf_issue`, `/test_print`, or `/test_scf_print` casually, and never fire test prints unless the user explicitly asks.
- **Two time frames, on purpose**: task times are **naive local wall-clock** (`parse_task_time()` normalises anything aware); listener watermarks are **aware UTC** (`parse_utc()`). They are different kinds of value — a wall-clock reminder vs. an instant. Never compare across them. See `docs/scheduling.md`.
- **Invariants worth not breaking** (Phase 0 fixed these; the tests will catch regressions):
  - `calculate_next` returns its input unchanged to mean "cannot advance". Never loop on it — `advance_schedule` raises `ScheduleError` instead.
  - A schedule advances only after a successful print.
  - A store that failed to load is never written to.
  - Skipped/disabled tasks stay visible in the UI with a reason.
- `load_data()` performs implicit migrations on load: adds `enabled: true` to tasks, `type: 'task'` to old history records, converts theme `high-contrast` → `system`, and creates a default `listeners.json` if missing.
- Data file paths are relative to the process CWD — always run from the repo root. (Planned, not yet current: MASTER_PLAN `P1-9` moves all state into `data/` with a startup migration — until that lands, the four JSON files live in the repo root.)
- `app.log` is vestigial (empty): no file handler is configured; logs go to the console at DEBUG level (`P1-5` adds rotation).
- Git identity: this repo commits as `mica-alex <83238954+mica-alex@users.noreply.github.com>` (already set locally). Match existing commit style: short imperative subjects.
- `docs/agent-plans/` is gitignored (agent working documents, not project source).

## File map

| Path | What |
| --- | --- |
| `app.py` | Entire application: state, scheduler, printing, routes |
| `templates/base.html` | Layout, nav, CDN includes, theme JS, Materialize/flatpickr init |
| `templates/index.html` | Dashboard: printer status, task table, last-5 history |
| `templates/tasks.html` | Task CRUD (add/edit modals), full history table |
| `templates/settings.html` | Config form, printer info, async test-print buttons |
| `templates/listener.html` | SCF listener config form |
| `static/styles.css` | Theme variables (`[data-theme]`), Materialize overrides |
| `*.json` (gitignored) | Live datastore — see `docs/data-model.md` |
| `requirements.txt` | Pinned direct dependencies |
| `scripts/setup-venv.sh` | Creates/repairs `.venv`; detects a dead interpreter |
| `scripts/run.sh` | Self-healing launcher (used by IDE configs and services) |
| `.idea/runConfigurations/`, `.vscode/` | Committed run/debug configs for both editors |
| `templates/partials/` | Shared template fragments (task status badge) |
| `templates/error.html` | Validation-failure page |
| `static/vendor/` | Vendored Materialize/flatpickr/icons so the UI works offline |
| `tests/` | pytest suite; needs no printer and no network |
| `requirements-dev.txt` | Test dependencies |
| `docs/` | System documentation; `docs/agent-plans/MASTER_PLAN.md` is the roadmap |
