# Listeners

A "listener" polls an external source and prints new items as receipts. Today
there is exactly one — SeeClickFix (`scf`) — and it is **hardcoded** inside
`scheduler_loop()` (`app.py:337-391`); `listeners.json` is a dict keyed by
listener name to allow more, but nothing else is generic.

## SeeClickFix listener, end to end

### Config (`listeners.json`, edited via `/listener`)

```json
{"scf": {"enabled": true, "request_types": "6632,6634,6630,6628,20840",
         "interval": 5, "last_check": "2025-08-26T13:35:21Z"}}
```

See [data-model.md](data-model.md#listenersjson) for field semantics. The
request-type IDs are raw SCF category ids (e.g. 6632 = "Signal Repair",
City of Manchester DPW); the UI stores and displays them as an opaque comma
string with no name lookup (MASTER_PLAN Phase 4 redesigns this).

### Poll cycle (every scheduler tick, gated by `interval`)

1. Skip unless `scf.enabled` and `request_types` is non-blank (`app.py:340`).
2. Parse `last_check` with `dateutil.parser` (tolerant), assume UTC if naive
   (`app.py:344-355`). Unparseable → treated as never-checked.
3. Gate: poll only if `now_utc - last_check >= interval` minutes. Note
   `now_utc` was captured at the **top of the tick** (`app.py:324`), before task
   printing — so slow prints widen the effective window slightly.
4. Build the request (`app.py:365-373`):

   ```
   GET https://seeclickfix.com/api/v2/issues
       ?status=open,acknowledged
       &request_types=<verbatim config string>
       &after=<last_check, or now-1h on first run>
       &per_page=100
   ```

5. Print every returned issue in `created_at` ascending order
   (`app.py:380-381`).
6. Set `last_check = now_utc` (strict `%Y-%m-%dT%H:%M:%SZ`) and
   `save_listeners()` (`app.py:384-385`).

### `after`/`last_check` semantics — what actually happens

Verified against the live API and its docs (github.com/SeeClickFix/dev.seeclickfix.com):

- `after` filters `created_at >= after` — **inclusive**. Combined with
  `last_check` being stamped from the tick-start time (before the fetch), the
  windows *overlap* rather than gap: an issue created at exactly `last_check`,
  or between tick-start and the fetch, appears in two consecutive polls. Since
  there is **no dedup by issue id**, that means a duplicate physical receipt
  (MASTER_PLAN `P0-7`).
- Issues that enter the feed late with an earlier `created_at` (moderation
  holds; SCF timestamps are report-time) fall behind the advancing window and
  are missed forever.
- The `status=open,acknowledged` filter means an issue opened and closed
  between polls is never printed.
- If the fetch or the print loop throws, `last_check` is NOT advanced (it is set
  after the loop, inside the same `try`) — good, the window is retried — but
  individual print failures are swallowed *inside* `print_scf_issue`, so a
  printer-offline poll still advances `last_check` and permanently drops every
  issue in the window (MASTER_PLAN `P0-4`).

### Pagination — silent loss above 100

`per_page=100` is the API maximum; the response's `metadata.pagination`
(`entries`, `page`, `pages`, `next_page`, `next_page_url`) is ignored
(`app.py:376-377` even has a comment admitting the assumption). More than 100
new issues in a window → only the first page (default sort `created_at DESC`,
i.e. the **newest** 100) is processed; the oldest overflow issues are lost
(MASTER_PLAN `P0-7`).

### Rate limits & errors

The public API allows ~20 requests/minute. One poll = one request, so the
current design is safely inside it, but there is no handling for 429/5xx beyond
"log and retry next interval" (which is fine, because `last_check` doesn't
advance on failure).

### The `/listener` page

GET renders the form from `listeners.get('scf', {})` (safe). POST rebuilds the
dict wholesale and reads `listeners['scf'].get('last_check')` to preserve the
watermark — a `KeyError` → HTTP 500 if the `scf` key is absent (hand-edited
file), and `int(request.form.get('interval', 10))` → 500 on non-numeric/empty
input (MASTER_PLAN `P0-9`).

## SeeClickFix API reference (verified 2026-07)

Docs are published at dev.seeclickfix.com (source:
`github.com/SeeClickFix/dev.seeclickfix.com`). The endpoints relevant to
TaskHome, all working unauthenticated as of this writing:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v2/issues` | List issues. Params: `page`, `per_page` (max 100), `status` (`open,acknowledged,closed,archived`), `sort`(`created_at`/`updated_at`/`rating`/`distance`), `sort_direction`, `after`/`before` (created_at, ISO 8601, `after` inclusive), `updated_at_after`/`updated_at_before`, `search`, `request_types` (comma ids), `details=true`, plus geography (`lat`+`lng`[+`zoom`], `place_url`, `min_lat/min_lng/max_lat/max_lng` bbox, `watcher_token`) |
| `GET /api/v2/issues/:id` | Single issue (`details=true` for comments etc.) |
| `GET /api/v2/issues/new?lat=..&lng=..` | **Request-type discovery**: the report form for a point — returns every request type available at that location, each with title + organization. This is the lookup the Phase 4 picker should use; there is no "list request types by place name" endpoint, but `place_url` works as an area filter on `/issues` |
| `GET /api/v2/request_types/:id` | Details for one request type: `title`, `organization`, questions. Ideal for resolving configured ids to friendly names |

Issue object fields actually returned (verified live): `id`, `status`,
`summary`, `description` (nullable), `rating`, `lat`, `lng`, `address`,
`created_at`/`acknowledged_at`/`closed_at`/`reopened_at`/`updated_at`
(offset-aware ISO), `private_visibility`, `html_url`, `url`, `point`,
`comment_url`, `flag_url`, `transitions`, `reporter`,
`media {video_url, image_full, image_square_100x100, representative_image_url}`
(keys always present, values nullable), `request_type {id, title, organization,
url, related_issues_url}`.

## What adding a new listener takes today

There is no abstraction. You must:

1. Add a config blob under a new key in `listeners.json` and defaults in
   `load_data()`.
2. Add an `if '<name>' in listeners:` block inside `scheduler_loop()` with your
   own interval gating, watermarking, and fetch.
3. Write a `print_<name>_item()` sibling of `print_scf_issue()` with its own
   history projection (and add a `type` value the templates understand —
   `index.html`/`tasks.html` special-case `item.type == 'scf'` in two places
   each).
4. Extend `/listener`'s GET/POST and `listener.html` by hand.

MASTER_PLAN Phase 5 (`P5-1`) specifies a proper plugin interface to replace
steps 1–4.
