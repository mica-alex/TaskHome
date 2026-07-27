# Listeners

A "listener" polls an external source and prints new items as receipts. Today
there is exactly one — SeeClickFix (`scf`) — implemented in
`poll_scf_listener()` and called from the scheduler loop. `listeners.json` is a
dict keyed by listener name to allow more, but nothing else is generic: adding
a second listener still means writing another bespoke function and another
call. MASTER_PLAN `P5-1` introduces the plugin interface.

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

### Poll cycle

`poll_scf_listener(now_utc)`, called once per scheduler tick:

1. Skip unless `scf.enabled` and `request_types` is non-blank.
2. Skip if inside a `backoff_until` window, or if `now_utc - last_check` is
   less than `interval` minutes. An unparseable `last_check` is treated as
   never-checked, so a corrupt watermark polls rather than stalling forever.
3. **Take the watermark for the next poll before making any request.** This is
   the fix for the window gap: the old code stamped `last_check` from a
   timestamp captured at the top of the tick and wrote it after the fetch, so
   anything created while the request was in flight fell between windows and
   was never seen.
4. Fetch every page:

   ```
   GET https://seeclickfix.com/api/v2/issues
       ?status=open,acknowledged
       &request_types=<normalised comma string>
       &after=<last_check, or now-1h on first run>
       &per_page=100&page=N
   ```

   Paging continues until `metadata.pagination.pages` is reached, or a short
   page arrives if that metadata is missing, or the `SCF_MAX_PAGES` guard (20)
   trips — which logs that it truncated rather than truncating silently.
5. Drop issues whose id is already in `scf['seen']`, sort the rest by
   `created_at` ascending, and print.
6. Record ids of issues that **actually printed**, trim `seen` to the most
   recent 2000, advance `last_check`, clear failure state, save.

### `after` / `last_check` semantics

Verified against the live API and its docs (github.com/SeeClickFix/dev.seeclickfix.com):

- `after` filters `created_at >= after` — **inclusive** — so consecutive
  windows deliberately overlap rather than risk a gap. Dedup by issue id is
  what makes the overlap harmless; without it, an issue landing exactly on the
  watermark reprinted every single cycle.
- An issue that fails to print is **not** added to `seen`, so the next
  overlapping window retries it. An offline printer delays SCF receipts instead
  of destroying them.
- Issues entering the feed late with an earlier `created_at` (moderation holds;
  SCF timestamps are report-time) still fall behind the advancing window. This
  is a genuine remaining limitation — see MASTER_PLAN `P4-4` for the
  overlap-window proposal that addresses it.
- The `status=open,acknowledged` filter means an issue opened and closed
  between polls is never printed.

### Rate limits & errors

The public API allows ~20 requests/minute. A poll is now one request *per page*,
so a large backlog can burst; the page guard bounds it at 20.

A failed fetch does **not** advance `last_check` — the window is retried rather
than skipped. Consecutive failures back off exponentially
(`2^n` minutes, capped at 60) via `backoff_until`, and the counter resets on
success. `last_error` records the most recent failure.

### The `/listener` page

GET renders the form from `listeners.get('scf', {})`. POST validates: the
interval must parse as an integer in 1–1440, and `request_types` is normalised
(whitespace trimmed, empty entries dropped). The existing `last_check` is
preserved via `listeners.get('scf') or {}` — indexing it directly raised
`KeyError` → HTTP 500 on a fresh install before the listener had ever been
configured.

Note the form does not surface `seen`, `backoff_until`, `consecutive_failures`
or `last_error`; they are runtime state, visible only in `listeners.json` and
the log until MASTER_PLAN `P4-6` adds a feed view.

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


## Request-type names and discovery (P4-1/P4-2/P4-3)

`request_types` is stored as a comma string of numeric ids, which says nothing
about what is actually subscribed. The settings page now shows named chips.

Names come from `GET /api/v2/request_types/:id` and are cached in
`data/cache/scf_request_types.json` for 30 days — titles are effectively
static, and a LAN appliance should not need the network to render its own
settings page.

Deliberate behaviours:

- An id whose lookup **404s** is remembered as missing rather than retried, so
  a dead subscription does not mean a network round trip on every page load.
  It still appears in the list, marked, rather than silently vanishing.
- A **network failure keeps the stale name** — an out-of-date title beats none.
- The cache is written directly rather than through `_save_json_file`: it is
  derived data, so it needs neither backups nor the write-block protection that
  guards real state.

### Discovery

There is **no search-by-name endpoint**. Categories are discovered by location
through the report-a-problem form, `GET /api/v2/issues/new?lat=&lng=`, which
lists everything reportable at a point — 37 for Manchester NH. The picker
groups them by organization and filters client-side.
