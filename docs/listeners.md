# Listeners

A "listener" polls an external source and prints new items as receipts. There
are two:

| Name | Source | Style | Built on |
| --- | --- | --- | --- |
| `scf` | SeeClickFix civic issues | poll | Bespoke — predates the plugin interface |
| `nws` | NOAA / National Weather Service alerts | poll | `base.Listener` |
| `feeds` | RSS / Atom digest | poll | `base.Listener` |
| `calendar` | ICS calendar agenda | poll | `base.Listener` |
| `brief` | Composes the others | poll | `base.Listener` |
| `binday` | Bin collection reminder | poll | `base.Listener` |
| `github` | Releases, builds, issues, PRs | poll | `base.Listener` |
| `transit` | Departures and service alerts | poll | `base.Listener` |
| `webhook` | Anything that can POST | **push** | `base.Listener` |
| `mqtt` | MQTT topics / Home Assistant | **push** | `base.Listener` (optional dep) |

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

### Filters (P4-5)

Rendered from `scf.FILTER_SCHEMA` through the same macro every other listener
uses; only the category picker is bespoke.

| Setting | Effect |
| --- | --- |
| `status` | Which statuses to fetch. Default open + acknowledged. An issue opened and closed between two polls is missed whatever this says. |
| `place_url` | A SeeClickFix place slug, e.g. `manchester`. |
| `bbox` | `min_lat, min_lng, max_lat, max_lng` — for an area no place slug matches. |
| `search` | Free-text. **Requires a place or a bbox.** |
| `muted_types` | Stop printing a category without unsubscribing. |

**The keyword rule is a hard constraint, not a preference.** Verified against
the live API: `search=pothole` alone does not return within 60 seconds — it
scans roughly 850,000 issues — while `search=pothole&place_url=manchester`
answers in about 6. Allowing a bare keyword would let someone configure a
listener that times out on every poll, backs off, and never prints again.

Muting is applied **after** the fetch rather than by removing the request-type
id, so unmuting restores the subscription without having to find the number
again. A muted issue is still marked seen — otherwise it is re-fetched and
re-evaluated on every poll for as long as it stays inside the window.

A filter absent from a submission keeps its stored value; only an explicit
empty selection clears it. For a multiselect those are the same on the wire,
which is why the macro emits a hidden marker field.

### Photos on receipts (P4-7)

Off by default: a photo roughly doubles the paper per issue (59 mm → 108 mm
for the default template) and adds a download to the print path.

When on, `media.image_full` is fetched at print time and reduced for the
printer in `taskhome/images.py`:

- **Floyd–Steinberg error diffusion**, not thresholding. A thermal printer has
  one ink level, so a photograph has to become pure black and white; plain
  thresholding produces a black smear, while error diffusion trades spatial
  resolution for apparent grey and keeps a scene recognisable at 203 dpi.
- **EXIF orientation is applied.** A phone photo is usually stored rotated with
  a tag saying which way is up; ignoring it prints the picture sideways.
- Scaled to fit, never cropped — a cropped photo of a pothole may not contain
  the pothole.
- Capped at 2 MB and 5 seconds, streamed and counted rather than trusting
  `Content-Length`. Fetches are throttled, because a catch-up burst of twenty
  issues would otherwise fire twenty downloads at someone else's CDN.
- Cached under `data/cache/media/`, since the print queue may retry a receipt
  several times. Derived data; safe to delete.
- **Every failure returns None**, and the receipt prints `[Photo unavailable]`.
  A receipt missing its photo is a good outcome; a receipt that failed to print
  because a CDN was down is not.

`image` is a template block type, so the placement is editable in the Studio:
`{"type": "image", "src": "{media_url}", "width": 384}`. `{media_url}` is empty
both when the issue has no photo and when photos are switched off, and `fill()`
drops the block entirely — so neither case leaves a gap.

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

### Receipts

Every registered listener is an editable kind in the Receipt Studio
(`styles.kinds()` is built from the registry). A listener supplies:

- **`PLACEHOLDERS`** — the placeholder names its templates may use, with
  realistic sample values. A template using anything else is refused at save
  time rather than printing the literal `{typo}` on paper.
- **`template_presets()`** → `[(name, blocks)]`, default first. Blocks carry
  `{placeholder}` markers. **Generate these from the same code that prints the
  fallback layout** — a preset that has drifted from what actually prints is
  worse than no preset, because it looks authoritative.
- **`template_name(config, item)`** — an optional per-item override. NWS
  returns the `style` chosen for that alert type, which is how one listener
  prints a tornado warning large and a wind advisory small.
- **`matrix_column_options(spec, column)`** — for a `select` column whose
  options are not knowable when the schema is declared. NWS's style column
  lists whatever templates exist right now, including ones just saved in the
  Studio.

If a template is missing or unusable at print time the listener falls back to
`receipt_blocks()` and logs it. A weather alert must not be lost because
someone deleted a template.

## Push listeners

A listener with `accepts_push = True` is handed items rather than fetching
them. The poll sweep skips it, and `base.deliver()` runs the same tail as a
polled listener — dedup, per-delivery cap, `should_print`, the active template,
history, and queueing on a failed print.

That sharing is the point. A push path that printed directly would have to
reimplement all of it, and would get the queueing wrong — which is the part
that loses receipts.

**Printing is serialised** by `printing.PRINT_LOCK`. Several threads can print
— the scheduler, a web request, and a push listener's own network thread — and
two of them opening the same USB device produces interleaved bytes or a claim
failure that leaks the interface (`P0-11`). The lock is held for a whole
receipt, because a receipt is the atomic unit: waiting behind another print is
fine, sharing paper with it is not.

### Webhook (`listeners/webhook.py`)

`POST /api/inbound/<token>` with `{"title": "...", "body": "..."}`, or just
plain text. One endpoint that makes TaskHome a printer for Apple Shortcuts,
Home Assistant, IFTTT, cron, or three lines of curl.

```sh
curl -X POST http://taskhome.local:5000/api/inbound/YOUR_TOKEN \
  -d '{"title": "Bins tonight", "body": "Green bin and recycling."}'
```

Honest about what the security is: a shared secret in a URL, over a LAN, with
no TLS. If the network is hostile the token is visible. What it buys is that a
stray request to a scanned port cannot print, and a leaked token rotates
without touching anything else.

- A wrong token returns **404, not 403**, so scanning cannot distinguish "no
  such endpoint" from "right endpoint, wrong secret".
- An empty configured token never matches, or enabling the listener before
  generating one would leave the endpoint open.
- **The rate limit matters more than the authentication.** The failure that
  actually costs something is a stuck script retrying every second overnight —
  thousands of receipts. Default 20/hour, sliding window, `429` with
  `Retry-After`.
- Titles and bodies are truncated rather than refused: a 4 MB log would print
  until the roll ran out, but refusing outright loses a legitimate long message.

### MQTT / Home Assistant (`listeners/mqtt.py`)

Subscribe to topics and print what arrives. Any Home Assistant automation can
print with one action:

```yaml
service: mqtt.publish
data:
  topic: taskhome/print/laundry
  payload: >-
    {"title": "Washing machine finished"}
```

**The dependency is optional.** `paho-mqtt` is not in `requirements.txt`; the
module imports without it, reports itself unavailable, and the settings page
shows the install command. A hard import would take the whole app down for
everyone who does not use MQTT, since the listener registry is imported at
startup.

The connection is long-lived and delivers on paho's own network thread.
`ensure_connected()` is driven from the scheduler tick rather than at import:
it doubles as the reconnect path, and it means a dev server started without a
scheduler holds no broker connection.

Two behaviours worth knowing:

- **Retained messages are ignored by default.** A retained message is
  redelivered on every reconnect, so printing them reprints the same receipt
  each time the connection blips.
- **An exception never escapes into paho's loop.** One that did would kill the
  network thread silently, leaving the listener looking connected while
  receiving nothing at all.

## The transit listener (`listeners/transit.py`)

Next departures from your stops, and a receipt when your line is disrupted.

Built around **providers**, because agencies differ too much for one client:

| Provider | Covers | Notes |
| --- | --- | --- |
| `mbta` | Boston | Native V3 JSON API. **No key needed.** Stop search, route names, headsigns, severity-scored alerts. |
| `gtfsrt` | NYC MTA, and any GTFS-RT feed | Read by `taskhome/gtfsrt.py`. Verified keyless against NYC. |

Adding an agency means adding a provider, not touching the listener.

### Granularity

Two matrices, so subscriptions go from one line to the whole system:

- **Per stop** — departure board, alerts here. A board for the stop you leave
  from and alerts for a different one is a normal combination, not an edge case.
- **Per route** — service alerts, and a minimum severity per line. MBTA scores
  alerts 1–10; 7 is shuttle-bus level. GTFS-RT carries no severity, so `any`
  applies there.

An alert usually names several routes. Only the **subscribed** ones are
consulted — otherwise a minor notice on a switched-off line prints anyway just
because it shares an alert with a line that is on. Among the subscribed lines
the most permissive threshold wins.

### Things found by probing the live APIs

- **Sorting MBTA predictions ascending by `departure_time` puts NULLs first**,
  and a cancelled trip has no times at all. An unfiltered board is therefore
  full of blanks during a disruption — exactly when someone is looking at it.
  Cancelled and skipped predictions are excluded.
- **The MBTA has no name-search endpoint** and thousands of stops. The finder
  searches parent stations (~276), which is what someone naming "North Station"
  actually means rather than each of its platforms.
- Boards print at configured times only, and only within 30 minutes of one, or
  a restart at 23:00 would print every board configured that day.

### GTFS-Realtime without protobuf

`taskhome/gtfsrt.py` reads the feed directly. GTFS-RT is protobuf and the usual
answer is `gtfs-realtime-bindings`, which pulls in `protobuf` and its C
extension — a lot to carry on an appliance meant to run untouched for years.

The wire format is self-describing enough to walk without a schema: every field
is a varint tag carrying a field number and a wire type. The reader decodes
generically and picks out the dozen field numbers that matter, which are
written down in the module docstring rather than left as magic. Unknown fields
are skipped, so a feed that gains one does not break it.

## The GitHub listener (`listeners/github.py`)

Releases, failed workflow runs, issues and pull requests for chosen repos.

**A token is optional.** Public repositories are readable unauthenticated at 60
requests an hour; a token raises that to 5,000 and allows private repos. That
tier is usable because of conditional requests — GitHub returns an `ETag` on
every list endpoint and **a 304 does not count against the rate limit**, so
polling a handful of repos costs nothing while nothing changes.

Two things found by testing against real repositories rather than reasoning
about the API:

- **`python/cpython` has zero Release objects.** It publishes tags, and the
  `releases.atom` feed reflects tags rather than releases. An empty result is
  correct there, not a bug.
- **Bot filtering applies only to issues and pull requests.** Every release in
  `pallets/flask` is published by `github-actions[bot]`; filtering releases by
  author means the listener silently prints nothing. Dependabot noise on issues
  is the case the setting is actually for.

Releases also routinely have an empty `name`, so the display falls back to
`tag_name`.

## The RSS digest listener (`listeners/feeds.py`)

**One receipt per digest, not one per article.** A feed with forty items a day
would otherwise bury the room in paper, and forty receipts are harder to read
than one list. `max_prints_per_poll = 1` enforces it.

Parsed with `xml.etree` rather than feedparser — the subset that matters is a
dozen lines, and an appliance that must keep working untouched for years is
better off without the dependency. Both RSS and Atom, verified against BBC News
(RSS), Reddit and GitHub releases (Atom).

Three behaviours worth knowing:

- **The first poll of a feed prints nothing.** Everything in it is "new" on
  first sight, and a busy feed carries 30–40 items — printed a few per digest,
  that is a week of catching up on old news. The backlog is marked seen and the
  digest starts from the next thing published. Same call SCF's catch-up policy
  makes, for the same reason.
- **Conditional requests.** ETag and Last-Modified are stored per feed and sent
  back; a feed polled hourly is almost always unchanged, and some publishers
  rate-limit clients that ignore this.
- **One dead feed does not stop the digest.** Failures are recorded and shown
  in the summary; the other feeds still print.

The source line under each headline uses a `-` prefix rather than indentation,
because `wrap()` strips leading whitespace — spaces would vanish and the source
would read as a second headline.

## The calendar listener (`listeners/calendar.py`)

Today's events from any ICS URL — Google, iCloud, Outlook, and most municipal
bin-day calendars. One agenda receipt per day, not one per event.

ICS is parsed here, but **recurrence is expanded with `dateutil.rrule`**, which
was already a dependency. That split is deliberate: unfolding lines and
splitting properties is thirty lines of obvious code, while RRULE is a genuinely
hard specification — `BYSETPOS`, `BYDAY` with ordinals, interval arithmetic
across DST — and hand-rolling it would be a slow-burning source of "why did my
Tuesday meeting print on Wednesday".

Details that matter, each verified against a real Google calendar feed:

- **Line unfolding is not optional.** A `SUMMARY` over 75 octets is split
  mid-word with the continuation indented; parsing line-by-line silently
  truncates it.
- **All-day events are calendar squares, not instants.** They carry
  `VALUE=DATE` and are kept at local midnight — converting from UTC shifts them
  a day for anyone west of Greenwich.
- `webcal://` is rewritten to `https://`, because that is what Apple hands you
  when you copy a calendar link.
- `STATUS:CANCELLED` events and `EXDATE` exclusions are dropped.
- An empty day still marks itself done, or the listener retries every interval
  until midnight looking for events that are not there.

## The morning brief (`listeners/brief.py`)

The only listener that **composes other listeners** rather than fetching
anything itself: date, weather, today's tasks, today's calendar, headlines —
one receipt, once a day.

Composition is the whole design:

- **Configuration is not duplicated.** The brief uses the ZIP codes, calendar
  URLs and feeds already configured on those listeners. Its own schema
  deliberately contains none of them, and a test asserts that.
- **A section with nothing configured is absent**, rather than printing an
  empty heading.
- **A failing section leaves the rest intact.** A brief missing its headlines
  is useful; one that refused to print because a feed 502'd is not.
- The source listeners do **not** have to be enabled for printing. Someone may
  want weather in their brief without a receipt for every advisory.

`receipt_blocks` is built from the sections that actually have content rather
than from the template, because a brief's shape changes daily and an empty
heading with nothing under it is worse than no heading. The editable template
still exists for people who want a fixed layout.

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

### The area line

`areaDesc` from the API is a bare county name — `Hillsborough, NH` — which is
ambiguous, because Hillsborough is also a town in New Hampshire. That collision
is common across most of the country, so the receipt builds its own line from
the ZIPs that were actually configured:

```
Manchester (03102) - Hillsborough County, NH
```

Two details this needs:

- **The right noun.** The zone API returns bare names, and "County" is wrong in
  several states — Louisiana has parishes; Virginia and Maryland have
  independent cities whose names already say so (`City of Alexandria`,
  `Baltimore City`); Alaska mixes boroughs, municipalities and census areas, so
  `county_label()` leaves those bare rather than guessing. Resolving the name
  costs one extra request per ZIP, cached forever with the rest.
- **Only the ZIPs the alert covers.** `affectedZones` is matched against the
  configured zones, so a warning does not claim a ZIP three counties away that
  merely happens to be in the settings. When no zone matches exactly the line
  falls back to the NWS wording rather than overstating precision.

ZIPs sharing a county are grouped under it; ZIPs in different counties are
listed separately. The raw text stays available to templates as `{area_nws}`.

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

### Receipt layouts

Two presets, both generated from `blocks_from_context()`:

| Preset | For |
| --- | --- |
| `nws-default` | Warnings — large title, full description |
| `nws-compact` | Advisories — large title, no description |
| `nws-minimal` | Single-size title, no description |

Title size and description are separate knobs (`blocks_from_context(big_title=,
description=)`). They used to be one "loud" flag, which meant an advisory could
not have a readable headline without also printing 600 characters of forecast
discussion.

The per-event `style` column selects between them (and any template saved in
the Studio). Left blank, an alert uses whichever template is active for the
`nws` kind. `default_matrix_row()` seeds warnings to the large layout.

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
