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

There is no lint or test pipeline and no CI (MASTER_PLAN `P1-6` proposes one).

## Architecture

Everything lives in `app.py` (~544 lines):

- **Global mutable state**: `config`, `tasks`, `history`, `listeners` are module-level Python lists/dicts, loaded from JSON files at import time by `load_data()` and rewritten wholesale by `save_*()` helpers. There is **no locking and no atomic writes** — the scheduler thread and Flask request handlers mutate the same objects.
- **Scheduler thread**: a daemon thread running `scheduler_loop()` is started at import time (module level, line ~537). It first fast-forwards overdue tasks (without printing them), then loops every 60s: fires due tasks, then polls the SeeClickFix API if the `scf` listener is enabled and its interval has elapsed. `scf` is hardcoded in the loop — there is no listener plugin abstraction.
- **Printing**: `print_task()` / `print_scf_issue()` open the printer fresh each time via `escpos.printer.Usb(0x04b8, 0x0e27, profile='TM-T20II')` (yes, the TM-T20**II** profile for a TM-T20**III** — intentional, it works). Successful prints are prepended to `history` and truncated to `config['max_history']`.
- **Web UI**: server-rendered Jinja templates (`templates/`) styled with Materialize + flatpickr **loaded from CDNs** — the UI degrades badly offline (`styles.css` hides native `<select>`s until Materialize replaces them). Routes are classic form-POST-and-redirect; `/test_print` and `/test_scf_print` return raw HTML strings consumed by `fetch()` in `settings.html` via `txt.includes('successful')` string matching.

`docs/` contains the full system documentation (architecture, data model, scheduling, printing, listeners, routes, operations). `docs/agent-plans/MASTER_PLAN.md` is the improvement roadmap with stable item IDs (P0-1 …) — check it before fixing bugs or adding features; known defects are catalogued there.

## Conventions & gotchas

- **`config.json`, `tasks.json`, `history.json`, `listeners.json` are the user's live data.** They are gitignored on purpose. Never commit them, never overwrite them with defaults, never "reset" them while testing. `load_data()` REPLACES `config` with file contents (no merge with defaults) — a config file missing a key breaks code that uses `config['hostname']` / `config['max_history']`.
- **Printing has physical side effects** — every print path call emits real paper from a real printer. Never call `print_task`, `print_scf_issue`, `/test_print`, or `/test_scf_print` casually, and never fire test prints unless the user explicitly asks.
- **Timezones are inconsistent by design-accident**: task `next_time` values are naive local ISO strings; the steady-state loop compares them against naive `datetime.now()`, but the startup catch-up compares them against UTC via `.replace(tzinfo=timezone.utc)`. SCF `last_check` is UTC (`...Z`). Documented in `docs/scheduling.md`; fixes tracked as P0 items in the master plan.
- **Known landmines** (see MASTER_PLAN Phase 0 before touching): a missed one-off task or a `custom` task with an empty `days` list makes `calculate_next`/the catch-up loop spin forever and kills the scheduler thread; a malformed `next_time` poisons every scheduler iteration; prints are silently dropped (and schedules still advanced) when the printer is disconnected.
- `load_data()` performs implicit migrations on load: adds `enabled: true` to tasks, `type: 'task'` to old history records, converts theme `high-contrast` → `system`, and creates a default `listeners.json` if missing.
- Data file paths are relative to the process CWD — always run from the repo root. (Planned, not yet current: MASTER_PLAN `P1-9` moves all state into `data/` with a startup migration — until that lands, the four JSON files live in the repo root.)
- `app.log` is vestigial (empty): no file handler is configured; logs go to the console at DEBUG level and include full task/history payloads.
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
| `docs/` | System documentation; `docs/agent-plans/MASTER_PLAN.md` is the roadmap |
