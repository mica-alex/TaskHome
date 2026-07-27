# Setup & Operation

## Dependencies

Direct dependencies are pinned in [`requirements.txt`](../requirements.txt);
transitives (jinja2, werkzeug, pillow, qrcode, python-barcode, pyyaml) resolve
via pip.

```
Flask 3.1.1        python-escpos 3.1     pyusb 1.3.1
requests 2.32.5    python-dateutil 2.9.0.post0
```

Python is **3.13** (Homebrew). The venv was previously built against a 3.9
interpreter inside an Xcode beta; see below.

## Environment setup

```sh
./scripts/setup-venv.sh          # create or repair .venv, install requirements
./scripts/setup-venv.sh --check  # read-only health report, exits 1 if unhealthy
./scripts/setup-venv.sh --force  # discard and rebuild from scratch
```

The script is idempotent and never touches the JSON data files.

### Self-repair

`setup-venv.sh` detects a broken environment and rebuilds it rather than
failing. It recognises three failure modes:

| Symptom | Detection |
|---|---|
| Interpreter deleted (dangling `.venv/bin/python` symlink) | `-e` test follows the symlink and fails |
| `.venv` missing entirely | directory check |
| Base interpreter removed while `bin/python` still runs | `home =` in `pyvenv.cfg` no longer exists |

`scripts/run.sh` calls `--check` before starting and repairs automatically, so
a machine that loses its interpreter recovers on the next start instead of
staying broken. Disable with `--no-repair` or `TASKHOME_NO_REPAIR=1`.

Interpreter selection prefers Homebrew Python (independent of Xcode), then any
`python3.N` on `PATH`, then Command Line Tools, and warns if it can only find
an Xcode-anchored interpreter.

> **Historical note.** The venv broke because it was built against
> `/Applications/Xcode-16.1.0-Beta.app/...`, which was later deleted. On macOS
> `/usr/bin/python3` dispatches through `xcode-select -p`, so building against
> it re-arms the same trap — hence the Homebrew preference.

## Running

```sh
./scripts/run.sh                 # repairs if needed, then starts
.venv/bin/python app.py          # direct, no health check
```

Run from the repo root either way — data files resolve relative to CWD.

- Serves on `http://0.0.0.0:5000` by default — reachable by the whole LAN, no auth.
- **Host and port are configurable**: `TASKHOME_HOST` / `TASKHOME_PORT`
  environment variables, else `host` / `port` in `config.json`, else
  `0.0.0.0:5000`. Invalid or out-of-range values log a warning and fall back to
  the default rather than refusing to start.
- On macOS, port 5000 is claimed by **AirPlay Receiver**. Either set
  `TASKHOME_PORT=5001` or turn AirPlay Receiver off in
  System Settings → General → AirDrop & Handoff. The committed IDE run
  configurations set `TASKHOME_PORT=5001` for this reason; deployments leave it
  unset and serve on 5000.
- The scheduler thread starts on import, before the server binds.
- The printer (Epson TM-T20III, USB `04b8:0e27`) may be absent; the app runs
  fine and logs "Printer not connected, skipping print". Occurrences are no
  longer lost: a task stays due and retries each tick, and an SCF issue is left
  out of the seen set so the next overlapping window picks it up. A durable
  queue that survives restarts is still MASTER_PLAN `P6-3`.
- Do not run under a multi-worker WSGI server or with the Flask reloader as-is:
  each import starts another scheduler → duplicate prints (MASTER_PLAN `P0-12`).
- macOS USB note: pyusb needs libusb (`brew install libusb`). No kernel-driver
  detach is needed on macOS for this printer.

## IDE setup

Run configurations are committed for both editors and work straight after a
clone + `./scripts/setup-venv.sh`. Both pin the interpreter by **path**
(`.venv/bin/python`) rather than by a global SDK name, so nothing needs
per-machine configuration.

**PyCharm** (`.idea/runConfigurations/`)

| Configuration | Does |
|---|---|
| `TaskHome` | Runs `app.py` on port 5001, debugger attached |
| `TaskHome (self-healing)` | Runs `scripts/run.sh` — repairs the venv first |
| `Setup Environment` | `scripts/setup-venv.sh` |
| `Environment Check` | `scripts/setup-venv.sh --check` |

The project SDK name in `misc.xml` / `TaskHome.iml` is `Python 3.13 (TaskHome)`.
PyCharm resolves SDK *names* from its own global table, so on a fresh machine it
will flag the interpreter as invalid until you point it at `.venv` once
(Settings → Project → Python Interpreter). The run configurations above work
regardless, because they bypass the SDK table.

**VS Code** (`.vscode/`) — `launch.json` (debug, plus a step-into-libraries
variant), `tasks.json` (setup / check / force-rebuild / run),
`settings.json` (interpreter path, Jinja2 associations for `templates/*.html`),
and `extensions.json`.

## Tests

```sh
.venv/bin/python -m pytest tests/ -q
```

Install dev dependencies first with `.venv/bin/pip install -r requirements-dev.txt`.

The suite needs **no printer and no network**. `tests/conftest.py` sets
`TASKHOME_NO_INIT=1` before importing `app`, so importing the module neither
reads your real JSON files nor starts the scheduler thread; fixtures then
substitute a fake printer and a fake HTTP layer. Nothing it does can emit paper
or touch `tasks.json`.

If you add a test that exercises persistence, point the file constants at
`tmp_path` as `tests/test_persistence.py` does — never at the repo root.

## Logs

Level resolves `TASKHOME_LOG_LEVEL` > `config['log_level']` > **INFO**. Output
goes to the console *and* to a rotating file at `logs/taskhome.log`
(2 MB × 5 backups, override the directory with `TASKHOME_LOG_DIR`). If the log
directory cannot be created the app warns and carries on — an appliance must
still run when it cannot write a log.

```sh
TASKHOME_LOG_LEVEL=DEBUG ./scripts/run.sh   # verbose, for a specific problem
```

Previously the level was hardcoded to DEBUG with no file handler. That was not
merely untidy: `calculate_next` logged once per step, so catching up a
year-old task emitted several hundred lines, none of it survived a restart, and
the volume buried the output of any tool importing the module. The per-step
line is gone; `advance_schedule` logs one summary instead.

The `app.log` file in the repo root is vestigial from before this and can be
deleted.

## Dry run: what would print?

Before starting after a long gap — or after changing catch-up settings — ask
what it is about to do:

```sh
./scripts/dry_run.py              # tasks only, no network
./scripts/dry_run.py --check-scf  # also query SeeClickFix for the real count
```

Read-only: no receipts, no saves, no migration. It reports per task how many
occurrences were missed, which policy applies, and how many receipts that
means, plus the SeeClickFix window and how many issues sit in it.

## Running as a service

```sh
./deploy/install.sh            # detects the platform, asks before changing anything
./deploy/healthcheck.sh        # exits 0 when healthy
./scripts/backups.py list      # what snapshots exist
```

See [deploy/README.md](../deploy/README.md). On Linux the udev rule is not
optional: without it libusb can only claim the printer as root, and the service
reports "Printer not connected" with no other symptom.

## Backups

Every write snapshots the file it is about to replace into
`data/backups/<store>/`, keeping the newest 20 (configurable via
`config['backups']`). Identical content is not snapshotted twice.

```sh
./scripts/backups.py list tasks
./scripts/backups.py show tasks 20260727T132012
./scripts/backups.py restore tasks 20260727T132012 --confirm
```

Stop TaskHome before restoring — the scheduler rewrites `tasks.json` and
`listeners.json` on its own and would overwrite what you just put back. A
restore snapshots the current file first, so it is itself undoable.

## Printer calibration

```sh
./scripts/calibrate_printer.py --confirm            # ~15cm of paper
./scripts/calibrate_printer.py --confirm --minimal  # ~5cm, rulers only
./scripts/calibrate_printer.py --confirm --width 96 # wider ruler for font B
```

**Emits physical paper** and refuses to run without `--confirm`. Prints a
column ruler per font; the wrap point is the printer's width. Measured values
for the unit in use are recorded in [printing.md](printing.md).

## Data files & recovery

`config.json`, `tasks.json`, `history.json`, `listeners.json` live in the repo
root, gitignored, and are the only persistent state. Schemas in
[data-model.md](data-model.md).

**Back them up** — there is still no second copy (MASTER_PLAN `P6-2`), though
the two mechanisms that turned a small problem into a large one are fixed:
writes are atomic (temp file + fsync + rename), and a store that fails to load
is **write-blocked**, so a corrupt-but-repairable file is never overwritten with
defaults. Watch for `Refusing to save` in the log — it means a file needs
attention and that store is frozen until you fix it and restart.

Recovering from a bad JSON file:

1. Stop the app first (the scheduler rewrites `tasks.json`/`listeners.json` on
   its own).
2. Validate: `python3 -m json.tool tasks.json`. A truncated file usually fails
   at the very end — often repairable by closing brackets by hand.
3. If unrepairable, restore from backup, or delete the file:
   - `config.json` missing → in-code defaults are used. A *partial* file is
     also safe now: `load_data()` merges it over the defaults rather than
     replacing them.
   - `listeners.json` missing → recreated with defaults (listener disabled).
   - `tasks.json` / `history.json` missing → start empty.
4. Restart and re-check `/settings` and `/listener` values.

Sanity checks after any manual edit of `tasks.json`:

- Every `next_time` parses. A bad one no longer poisons the tick — the task is
  disabled with `schedule_error` recorded — but it stops firing until fixed.
- Every `custom` task has a non-empty `days` list. An empty one is now rejected
  at ingress and disabled at runtime rather than hanging the scheduler.
- A one-off with `next_time` in the past is handled by the catch-up policy on
  next start; it no longer hangs anything.

## Known operational limits (honest posture)

- Flask dev server on `0.0.0.0`, no auth/CSRF/TLS: acceptable only on a trusted
  home LAN. Do not port-forward it. Hardening options in MASTER_PLAN `X-1`.
- UI assets are vendored, so the interface works with no internet.
- No service wrapper: run it in a terminal/tmux today. Planned: a committed
  `deploy/` directory with a systemd unit, udev rule for the printer
  (04b8:0e27 without root), launchd plist, and install scripts — specified in
  MASTER_PLAN `P6-1`. Also planned: state moves from the repo root into
  `data/` with an automatic startup migration (`P1-9`).
- Restarting applies the configured catch-up policy (default: skip recurring,
  print one summary receipt for a missed one-off). The timezone bug that made
  restarts additionally skip tasks due within the UTC offset is fixed.
