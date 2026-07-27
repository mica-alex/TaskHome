# Package layout

`app.py` at the repo root only assembles the app and starts the server.
Everything else lives here.

| Module | Owns |
| --- | --- |
| `constants.py` | Paths and fixed values. Imports nothing from the package, so anything may import it. |
| `state.py` | The mutable globals: config, tasks, history, listeners, the lock. |
| `settings.py` | Runtime settings resolved from env + config (host, port). |
| `logsetup.py` | The `taskhome` logger and its configuration. |
| `storage.py` | Load, save, and the legacy `data/` migration. |
| `recurrence.py` | Recurrence maths and catch-up policy. |
| `printing.py` | The ESC/POS layer. |
| `receipt.py` | The shared renderer: blocks → printer or preview. |
| `layouts.py` | Receipt layouts as data. |
| `scheduler.py` | The background thread. |
| `listeners/` | External pollers. Only `scf` so far. |
| `web/` | Blueprint, routes, forms, pagination. |

## Two rules that keep this working

**Cross-module references go through the module object.** Write
`from . import printing` then `printing.print_task(...)`, never
`from .printing import print_task`. A direct import binds the object that
existed at import time, so a later reassignment — by `load_data`, or by a test
— is invisible to the importer. This is why the state lives in one module and
is always read as `state.tasks`.

**Importing the package has no side effects.** No files read, no threads
started, no hardware touched. `create_app()` does that, and only starts the
scheduler when explicitly asked. That is what makes the package safe to import
from scripts and tests, and it is the real fix for the duplicate-scheduler
problem (`P0-12`).

`tests/test_static_analysis.py` enforces both, plus pyflakes over the whole
tree. The split introduced an undefined name and two duplicate function
definitions that no behavioural test could catch; that is what those checks are
for.
