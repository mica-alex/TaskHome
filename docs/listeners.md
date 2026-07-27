# Listeners

A "listener" polls an external source and prints new items as receipts. There
are two:

| Name | Source | Built on |
| --- | --- | --- |
| `scf` | SeeClickFix civic issues | Bespoke — predates the plugin interface |
| `nws` | NOAA / National Weather Service alerts | `listeners/base.Listener` |

`listeners/base.py` is the plugin interface (`P5-1`). SeeClickFix still runs
through `poll_scf_listener()` because it was written first; everything new
should subclass `Listener`. Both are called from the scheduler tick —
`scf.poll_scf_listener(now_utc)` then `listener_base.run_all(now_utc)`.

`listeners.json` is a dict keyed by listener name, holding both user settings
and runtime state (watermarks, seen ids, backoff) for each.

## SeeClickFix listener, end to end

### Config (`listeners.json`, edited via `/listener/scf`)

```json
{"scf": {"enabled": true, "request_types": "6632,6634,6630,6628,20840",
         "interval": 5, "last_check": "2025-08-26T13:35:21Z"}}
```

See [data-model.md](data-model.md#listenersjson) for field semantics. The
request-type IDs are raw SCF category ids (e.g. 6632 = "Signal Repair", City of
Manchester DPW). They are still *stored* as a comma string, but the UI resolves
and displays them as named chips — see "Request-type names and discovery" below.

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

### The `/listener/scf` page

SeeClickFix keeps a hand-written page because its category picker needs a live
location lookup that no schema field type expresses. That is the honest
boundary — everything that *can* come from a schema does.

GET renders the form from `listeners.get('scf', {})`. POST validates: the
interval must parse as an integer in 1–1440, and `request_types` is normalised
(whitespace trimmed, empty entries dropped). The existing `last_check` is
preserved via `listeners.get('scf') or {}` — indexing it directly raised
`KeyError` → HTTP 500 on a fresh install before the listener had ever been
configured.

The SCF form does not surface `seen`, `backoff_until`, `consecutive_failures`
or `last_error`. Schema-driven listeners do — `listener_settings.html` has a
Status card showing last check, seen count, last error and backoff window, and
the index cards show last-check and error. A full feed view is `P4-6`.

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

## The plugin interface (`listeners/base.py`)

A listener declares what it needs and fetches items. Everything else is
provided once, rather than rediscovered per listener.

### What you implement

```python
class MyListener(base.Listener):
    name = 'mine'
    title = 'My Source'
    description = 'What it prints.'
    default_interval = 10
    max_prints_per_poll = 20

    CONFIG_SCHEMA = (
        base.field('enabled', 'Enabled', 'bool', default=False),
        base.field('interval', 'Check every (minutes)', 'int',
                   default=10, min=1, max=1440, group='Where'),
    )
    PLACEHOLDERS = {'title': 'Sample'}     # sample values for receipt previews

    def poll(self, config, since):
        """Return items created after `since`. Raise to trigger backoff."""

    def context(self, item):
        """Placeholder values for the receipt template."""
```

Optional overrides: `dedup_key`, `sort_key`, `describe`, `summary`,
`should_print`, `matrix_rows`, `matrix_row_default`, `receipt_blocks`,
`history_record`.

`poll()` must not print, save or dedup. A listener that prints directly cannot
be capped, queued or retried.

### What you get

`base.run(listener, now_utc)` provides, in order:

1. **Interval gate** — skip unless `now - last_check >= interval`.
2. **Backoff gate** — skip if inside a `backoff_until` window.
3. **A pre-fetch watermark.** Stamped *before* the request, because a watermark
   stamped afterwards skips anything created while the fetch was in flight.
4. **Backoff on failure** — `min(2**min(n, 6), 60)` minutes, and it does *not*
   advance the watermark, so nothing is skipped by an outage.
5. **Dedup** against `seen` (trimmed to the last 2000 keys).
6. **`should_print()`** per item — see below.
7. **A per-poll cap** (`max_prints_per_poll`). Suppressed items are marked seen
   and logged, not silently dropped.
8. **Queueing on print failure**, since the polling window has already moved on.

`run_all()` iterates the registry and isolates each listener, so one broken
listener cannot stop the others.

### `should_print(config, item)` → `(bool, reason)`

This is where per-item policy goes. The runtime calls it for every fresh item,
logs the reason when it returns False, and **still marks the item seen** — the
alternative is re-fetching and re-evaluating the same item on every poll
forever. A filter that raises fails *open*: failing closed prints nothing and
looks identical to "nothing happened", which is the one failure mode a weather
alerter must not have.

Filtering inside `poll()` instead makes the decisions invisible to the log and
untestable apart from the network.

> This hook was defined, unit-tested, and wired to nothing for a while, so the
> whole NWS configuration surface silently did nothing. Test a filter through
> `base.run()`, not by calling the method directly.

### The settings schema

```python
base.field(key, label, type='text', default=None, help='', group=None,
           depends_on=None, **extra)
```

`FIELD_TYPES` — `bool`, `int`, `text`, `secret`, `select`, `multiselect`,
`duration`, `time_range`, `matrix`.

`group` collects fields under a heading; `depends_on` hides a field until
another is set. Both keep a long settings page short for someone who only wants
the common options.

The schema is rendered by `templates/partials/setting_field.html` and bound by
`static/settings.js`, both entirely generic. Adding a setting costs a schema
entry — no markup, no handler, no validation code. **If a listener needs
bespoke settings markup, the schema is missing a field type. Add the type.**

`coerce_field()` validates per type and phrases errors with the field's
*label*, since the message is shown to whoever is filling the form.
`parse_form()` unpacks the three types a flat form cannot round-trip on its
own:

- a **bool** is absent rather than false when unchecked;
- a **time_range** is two inputs, `key.start` and `key.end`;
- a **matrix** is a grid of `key[row][column]` names, with rows discovered from
  hidden `key[]` inputs — an all-unchecked row submits nothing at all, and
  without the marker it would silently revert to its defaults.

### Registry and history

`register()` / `registry()` / `get()`. Registration also drives:

- the listeners index and the settings route (`/listener/settings/<name>`);
- the **history type filter and badge label** (`web/pagination.py`), so a new
  listener is filterable and correctly labelled without touching a template.

### Known gap

`styles.KINDS = ('task', 'scf')`, so a plugin listener falls back to its own
`receipt_blocks()` and its receipts are **not** editable in the Receipt Studio.

## The NOAA weather listener (`listeners/nws.py`)

Enter ZIP codes, get a receipt when the NWS issues an alert for them. Endpoint
shapes were verified against the live API rather than taken from docs.

### Zone resolution

```
api.zippopotam.us/us/03101          ZIP   -> 42.9929, -71.4633
api.weather.gov/points/{lat},{lng}  point -> forecastZone NHZ012,
                                              county NHC011,
                                              timeZone America/New_York
api.weather.gov/alerts/active?zone= zone  -> alert features
```

`api.weather.gov` requires a contactable `User-Agent` and rate-limits anonymous
use.

**Both the forecast zone and the county zone are queried.** Some products are
issued against one and some against the other; subscribing to only one silently
misses half of them.

The result is cached in `data/cache/nws_zones.json` **without a TTL** — a ZIP's
forecast zone does not move, and re-deriving it would cost two extra requests
per ZIP on every poll. The file is safe to delete; it re-derives.

### Filtering

`status != 'Actual'` is dropped unless `include_test` is on.

Per-event-type control is the design, not a nicety. NWS issues roughly 120
event types and a household cares about a handful, so no single severity
threshold can express "tornado warnings always, wind advisories never, and wake
me for a flash flood". Each event type is a row in the `events` matrix with
columns:

| Column | Meaning |
| --- | --- |
| `enabled` | Print this event type at all |
| `print_updates` | Print `messageType: Update` as well as the original |
| `print_cancels` | Print cancellations |
| `quiet_hours` | `respect` / `override` / `digest` |

Rows come from `COMMON_EVENTS` (21 entries) unioned with anything seen live, so
the table is useful on a fresh install and grows to match the area.
`default_matrix_row()` seeds defaults from severity — an event ending in
"Warning" gets updates, cancels and `quiet_hours: override`; anything else does
not. Seeding only ever fills blanks; an explicit choice is never overwritten.

**Extreme severity always prints, quiet hours or not.** That is the point of
the listener, and it is tested as such. Quiet hours wrap past midnight
(22:00–07:00 is the default and the case naive comparisons get wrong).

`default_interval = 2` because alerts are time-critical;
`max_prints_per_poll = 10`.

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
