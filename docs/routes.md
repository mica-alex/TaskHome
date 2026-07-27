# HTTP Routes Reference

Generated against the live URL map, so it cannot drift silently — if a route is
missing here, regenerate it rather than hand-editing.

All routes are unauthenticated and CSRF-unprotected, on the assumption of a
trusted LAN. The one exception is `/c/<token>`, which carries its own secret
because it is reached from a printed QR code.

Three response conventions, by design rather than accident:

* **Form POSTs** validate and redirect, returning `400` with `error.html` on
  bad input. They keep working with JavaScript disabled.
* **`/api/` routes** return `{"ok": true, "data": ...}` or
  `{"ok": false, "error": "..."}`. The uniform envelope is what retired the
  contract where a page sniffed HTML for the word "successful" (`P0-10`).
* **`/test_print` and `/test_scf_print`** signal by status code (200/500/503),
  which predates the API layer and is kept because `settings.html` reads it.

Blueprints: `main` (`web/routes.py`), `api` (`web/api.py`), `health`
(`web/health.py`), `pwa` (`web/pwa.py`).

### Pages

| Route | Methods | Endpoint |
| --- | --- | --- |
| `/` | GET | `main.index` |
| `/chores` | GET | `main.chore_charts` |
| `/edit_task/<task_id>` | GET, POST | `main.edit_task` |
| `/listener` | GET | `main.listener` |
| `/listener/scf` | GET, POST | `main.listener_scf` |
| `/listener/settings/<name>` | GET, POST | `main.listener_settings` |
| `/lists` | GET | `main.checklists` |
| `/manifest.webmanifest` | GET | `pwa.manifest` |
| `/queue` | GET | `main.print_queue` |
| `/service-worker.js` | GET | `pwa.service_worker` |
| `/settings` | GET, POST | `main.settings` |
| `/settings/receipts` | GET | `main.receipt_studio` |
| `/task_page` | GET | `main.task_page` |

### Form POSTs

| Route | Methods | Endpoint |
| --- | --- | --- |
| `/add_task` | POST | `main.add_task` |
| `/delete_task` | POST | `main.delete_task` |
| `/test_print` | POST | `main.test_print` |
| `/test_scf_print` | POST | `main.test_scf_print` |

### Public links

| Route | Methods | Endpoint |
| --- | --- | --- |
| `/c/<token>` | GET | `main.chore_done` |

### JSON API

| Route | Methods | Endpoint |
| --- | --- | --- |
| `/api/chores` | POST | `api.create_person` |
| `/api/chores/<person_id>` | DELETE, PATCH | `api.modify_person` |
| `/api/chores/<person_id>/done` | DELETE, POST | `api.set_person_done` |
| `/api/chores/<person_id>/print` | POST | `api.print_chore_chart` |
| `/api/config` | GET | `api.get_config` |
| `/api/health` | GET | `health.api_health` |
| `/api/history` | GET | `api.list_history` |
| `/api/history/reprint/<uid>` | POST | `main.api_history_reprint` |
| `/api/inbound/<token>` | POST | `api.inbound` |
| `/api/listeners` | GET | `api.list_listeners` |
| `/api/listeners/<name>` | GET | `api.get_listener` |
| `/api/listeners/<name>/poll` | POST | `main.api_listener_poll` |
| `/api/lists` | GET | `api.get_lists` |
| `/api/lists/<list_id>` | DELETE, PATCH | `api.modify_list` |
| `/api/lists/<list_id>/clear` | POST | `api.clear_list_done` |
| `/api/lists/<list_id>/items` | POST | `api.add_list_item` |
| `/api/lists/<list_id>/items/<item_id>` | DELETE, PATCH | `api.modify_list_item` |
| `/api/lists/<list_id>/print` | POST | `api.print_list` |
| `/api/queue/<job_id>` | DELETE | `main.api_queue_discard` |
| `/api/queue/retry` | POST | `main.api_queue_retry` |
| `/api/receipt/activate/<kind>/<name>` | POST | `main.api_activate_template` |
| `/api/receipt/preview` | POST | `main.api_receipt_preview` |
| `/api/receipt/templates/<kind>` | POST | `main.api_save_template` |
| `/api/receipt/templates/<kind>/<name>` | DELETE | `main.api_delete_template` |
| `/api/receipt/test_print/<kind>` | POST | `main.api_template_test_print` |
| `/api/scf/browse` | GET | `main.api_scf_browse` |
| `/api/scf/names` | POST | `main.api_scf_names` |
| `/api/scheduler` | GET | `api.scheduler_info` |
| `/api/stats` | GET | `health.api_stats` |
| `/api/status` | GET | `health.api_status` |
| `/api/tasks` | GET | `api.list_tasks` |
| `/api/tasks/<task_id>` | GET | `api.get_task` |
| `/api/tasks/<task_id>/duplicate` | POST | `api.duplicate_task` |
| `/api/tasks/<task_id>/print` | POST | `api.print_task_now` |
| `/api/test_print` | POST | `api.test_print` |
| `/api/webhook/token` | POST | `api.rotate_webhook_token` |


## Notes on particular routes

### `GET /` — `main.index`

Probes the printer (one `usb.core.find` per load), and passes tasks enriched by
`api.task_view` so the page and the API cannot disagree about when a task last
printed.

### `POST /api/inbound/<token>` — the webhook

A wrong token returns **404, not 403**, so scanning cannot distinguish "no such
endpoint" from "right endpoint, wrong secret". Rate limited per hour with a
`429` and `Retry-After`; the limit exists for stuck loops, not attackers.
Accepts JSON, a form, or bare text.

### `GET /c/<token>` — chore chart done-link

Deliberately unauthenticated beyond the token: it is scanned from paper by a
child on a home LAN. Marking done is idempotent and non-destructive. The path
is short because it becomes a QR code, and every character adds modules.

### `POST /api/history/reprint/<uid>`

Addressed by `uid`, not list position — position stops being an identity the
moment the table is filtered, searched or paged — and not by the record's own
id, because the record types draw ids from different namespaces and collide.
Re-renders from today's active template rather than replaying stored blocks.

### `POST /api/tasks/<id>/print`

Prints **without advancing the schedule**. Printing one now is not the
occurrence coming due, and advancing `next_time` would silently skip the real
reminder.

### `GET /api/health` vs `GET /api/status`

`/api/health` returns **503** when something needs a human, so a monitor does
not have to parse the body. `/api/status` is always 200, because a status
widget that vanishes when something is wrong is worse than useless. An
unplugged printer is deliberately *not* unhealthy.

### `POST /test_print`, `POST /test_scf_print`

Report the real outcome (`P0-10`). Note that a *failed* test print still
enqueues a durable job, so a 500 does not mean nothing will ever print.
