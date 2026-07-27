# TaskHome Documentation

System-level documentation for TaskHome — a Flask app that prints scheduled
task receipts, civic issues, weather alerts, calendar agendas, news digests and
more on a USB Epson TM-T20IIIL thermal printer. Written so a new contributor (or agent) can be productive without
reading `taskhome/README.md` first.

Everything here describes the **actual** behavior of the code. Where behavior
is surprising or arguably wrong, that is stated explicitly and cross-referenced
to the improvement roadmap.

**Status (2026-07-27):** the MASTER_PLAN is complete apart from `P2B-5` (Web
Push and Android installability), which is on long-term hold pending an HTTPS
decision. These documents describe current behaviour; the plan retains the
original bug descriptions for the record.

## Contents

| Document | Covers |
| --- | --- |
| [architecture.md](architecture.md) | Big picture: process model, request flow, scheduler thread lifecycle, data flow diagram |
| [data-model.md](data-model.md) | The SQLite schema, every record type, the two time frames, migrations applied on load, backup and export |
| [scheduling.md](scheduling.md) | Every recurrence mode, `calculate_next`, startup catch-up behavior, timezone caveats |
| [printing.md](printing.md) | ESC/POS layer, printer identity/profile, exact receipt layouts, what each `p.set(...)` does |
| [listeners.md](listeners.md) | The plugin interface, push vs poll, and all twelve listeners end to end |
| [design.md](design.md) | The Mica design language, component inventory, PWA visual layer |
| [routes.md](routes.md) | Every HTTP route, generated from the live URL map |
| [operations.md](operations.md) | Dependencies, venv, running as a service, logs, backups, recovering from a bad datastore |
| [agent-plans/MASTER_PLAN.md](agent-plans/MASTER_PLAN.md) | The "0 to 100" improvement roadmap: stack decision (`S-1`), bug catalogue (Phase 0), foundations incl. the `data/` migration, UI overhaul + Mica design system (2A) + iOS/Android PWA (2B), Receipt Style Studio, SCF integration, new listeners incl. the NOAA weather/EAS design (`P5-3`), operations/`deploy/`. Items have stable IDs (`P0-1`, `P2-3`, …) |

## Ten-second orientation

- `app.py` is a 20-line entry point; the application is the `taskhome`
  package. Importing it has **no side effects** — `create_app()` is an app
  factory, and the single scheduler daemon thread starts only when asked for.
  **SQLite** (`data/taskhome.db`) as the datastore, plus caches, backups and
  receipt templates (gitignored — they are the user's live data). ~996 tests
  that need no printer and no network.
- **Twelve listeners**, each off by default: SeeClickFix, NOAA weather, RSS
  digest, calendar agenda, morning brief, bin day, webhook (push), MQTT/Home
  Assistant (push), GitHub, transit, package tracking, chore charts. Plus
  tasks, printable checklists, catch-up summaries, and anything drained from
  the print queue.
- Every receipt goes through one renderer, so the preview cannot drift from the
  paper, and every listener's receipt is editable in the Receipt Studio.
- The web UI (port 5000, bound to `0.0.0.0`, no auth) manages tasks, lists,
  chore charts, settings, the listeners and their schema-driven settings pages,
  the Receipt Studio and the print queue, and shows paged print history. It
  installs as a home-screen web app on iOS, and there is a JSON API under
  `/api` for everything.
- Printing is a physical side effect. Do not invoke print paths casually.
