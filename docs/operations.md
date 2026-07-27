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
  fine and logs "Printer not connected, skipping print" — but note that due
  tasks and fetched SCF issues that occur while the printer is unplugged are
  **permanently dropped**, not queued (MASTER_PLAN `P0-4`).
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

## Logs

- `app.logger` is set to DEBUG (`app.py:16`) with **no file handler configured**
  — output goes to the console. The `app.log` file in the repo root is
  vestigial (0 bytes, nothing writes it).
- DEBUG logging includes **full dumps of tasks and history** on every
  `load_data()` — noisy and mildly sensitive (MASTER_PLAN `P1-5`).
- One stray `print("Checking SCF listener...")` writes to stdout each poll
  (`app.py:341`).

## Data files & recovery

`config.json`, `tasks.json`, `history.json`, `listeners.json` live in the repo
root, gitignored, and are the only persistent state. Schemas in
[data-model.md](data-model.md).

**Back them up** — there is no other copy, writes are non-atomic, and
`load_data()` silently falls back to defaults when a file fails to parse
(then the next save overwrites the corrupt-but-recoverable file with defaults).

Recovering from a bad JSON file:

1. Stop the app first (the scheduler rewrites `tasks.json`/`listeners.json` on
   its own).
2. Validate: `python3 -m json.tool tasks.json`. A truncated file usually fails
   at the very end — often repairable by closing brackets by hand.
3. If unrepairable, restore from backup, or delete the file:
   - `config.json` missing → in-code defaults are used, **but note** several
     code paths require keys to exist once a file IS present
     (see [data-model.md](data-model.md#configjson)); a deleted file is safer
     than a partial one.
   - `listeners.json` missing → recreated with defaults (listener disabled).
   - `tasks.json` / `history.json` missing → start empty.
4. Restart and re-check `/settings` and `/listener` values.

Sanity checks after any manual edit of `tasks.json`:

- Every `next_time` parses with `datetime.fromisoformat` (a bad one poisons
  every scheduler tick — `P0-6`).
- No enabled one-off (`recurring: "none"`) task has `next_time` in the past
  (hangs the scheduler at startup — `P0-1`).
- Every `custom` task has a non-empty `days` list (`P0-2`).

## Known operational limits (honest posture)

- Flask dev server on `0.0.0.0`, no auth/CSRF/TLS: acceptable only on a trusted
  home LAN. Do not port-forward it. Hardening options in MASTER_PLAN `X-1`.
- UI requires internet (CDN assets) even though the app itself is LAN-local.
- No service wrapper: run it in a terminal/tmux today. Planned: a committed
  `deploy/` directory with a systemd unit, udev rule for the printer
  (04b8:0e27 without root), launchd plist, and install scripts — specified in
  MASTER_PLAN `P6-1`. Also planned: state moves from the repo root into
  `data/` with an automatic startup migration (`P1-9`).
- Restarting the app skips (does not print) any occurrences missed while down,
  and — due to `P0-3` — may additionally skip tasks due in the next few hours.
