# TaskHome Documentation

System-level documentation for TaskHome — a Flask app that prints scheduled task
receipts and SeeClickFix civic-issue receipts on a USB Epson TM-T20III thermal
printer. Written so a new contributor (or agent) can be productive without
reading `app.py` first.

Everything here describes the **actual** behavior of the code. Where behavior
is surprising or arguably wrong, that is stated explicitly and cross-referenced
to the improvement roadmap.

**Status:** MASTER_PLAN Phase 0 (the bug catalogue) is complete — see the status
table at the top of that phase for what landed and the two items deliberately
left for Phase 1. These documents describe the post-fix behavior; the plan
retains the original bug descriptions for the record.

## Contents

| Document | Covers |
| --- | --- |
| [architecture.md](architecture.md) | Big picture: process model, request flow, scheduler thread lifecycle, data flow diagram |
| [data-model.md](data-model.md) | Exact schema of `config.json`, `tasks.json`, `history.json` (both record types), `listeners.json`; implicit migrations in `load_data()` |
| [scheduling.md](scheduling.md) | Every recurrence mode, `calculate_next`, startup catch-up behavior, timezone caveats |
| [printing.md](printing.md) | ESC/POS layer, printer identity/profile, exact receipt layouts, what each `p.set(...)` does |
| [listeners.md](listeners.md) | The SeeClickFix listener end to end: config, polling, `last_check` semantics, what adding a new listener takes today |
| [routes.md](routes.md) | Every HTTP route: method, form fields consumed, response |
| [operations.md](operations.md) | Dependencies, venv, running, logs, recovering from bad JSON |
| [agent-plans/MASTER_PLAN.md](agent-plans/MASTER_PLAN.md) | The "0 to 100" improvement roadmap: stack decision (`S-1`), bug catalogue (Phase 0), foundations incl. the `data/` migration, UI overhaul + Mica design system (2A) + iOS/Android PWA (2B), Receipt Style Studio, SCF integration, new listeners incl. the NOAA weather/EAS design (`P5-3`), operations/`deploy/`. Items have stable IDs (`P0-1`, `P2-3`, …) |

## Ten-second orientation

- One file: `app.py` (~1252 lines). One background daemon thread
  (`scheduler_loop`) started at import time, suppressible with
  `TASKHOME_NO_INIT=1`. Four JSON files as the datastore (gitignored — they
  are the user's live data). A pytest suite that needs no printer and no
  network.
- Two things get printed: **tasks** (user-scheduled reminders) and **SCF issues**
  (new SeeClickFix reports matching configured request-type IDs).
- The web UI (port 5000, bound to `0.0.0.0`, no auth) manages tasks, settings,
  and the SCF listener, and shows print history.
- Printing is a physical side effect. Do not invoke print paths casually.
