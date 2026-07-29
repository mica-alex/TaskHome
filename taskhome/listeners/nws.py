"""NOAA / National Weather Service alerts (MASTER_PLAN P5-3).

Enter ZIP codes, get a receipt when the NWS issues an alert for them.

Everything here was verified against the live API rather than taken from docs:

    api.zippopotam.us/us/03101          ZIP  -> 42.9929, -71.4633
    api.weather.gov/points/{lat},{lng}  point -> forecast zone NHZ012,
                                                 county NHC011,
                                                 timeZone America/New_York
    api.weather.gov/alerts/active?zone= zone  -> alert features

Zone resolution is cached without a TTL: a ZIP's forecast zone effectively
never changes, and re-deriving it on every poll would be two extra requests per
ZIP for a value that is already known.

The per-event-type control matrix (`D-6`) is the point of the design. NWS
issues roughly 120 event types and a household cares about a handful; a single
severity threshold cannot express "tornado warnings always, wind advisories
never, and wake me for a flash flood". Each event type gets its own row.
"""
from datetime import datetime

import requests

from . import base
from .. import constants, layouts, receipt, storage
from ..logsetup import log

ZIP_URL = 'https://api.zippopotam.us/us/{zip}'
POINTS_URL = 'https://api.weather.gov/points/{lat},{lng}'
ALERTS_URL = 'https://api.weather.gov/alerts/active'
#: The one human-readable page in this API's orbit -- it redirects to the point
#: forecast for a ZIP, with any active alerts listed at the top. See alert_url.
FORECAST_URL = 'https://forecast.weather.gov/zipcity.php'

#: The API asks for a contactable User-Agent and rate-limits anonymous use.
USER_AGENT = '(TaskHome receipt printer, https://github.com/mica-alex/TaskHome)'

SEVERITIES = ('Extreme', 'Severe', 'Moderate', 'Minor', 'Unknown')
URGENCIES = ('Immediate', 'Expected', 'Future', 'Past', 'Unknown')

#: Common event types, so the settings matrix has rows before anything has
#: been seen live. Anything the API returns that is not listed is added to the
#: matrix on first sight, so the list never has to be exhaustive.
COMMON_EVENTS = (
    'Tornado Warning', 'Severe Thunderstorm Warning', 'Flash Flood Warning',
    'Flood Warning', 'Flood Advisory', 'Winter Storm Warning',
    'Winter Weather Advisory', 'Blizzard Warning', 'Ice Storm Warning',
    'High Wind Warning', 'Wind Advisory', 'Extreme Heat Warning',
    'Heat Advisory', 'Extreme Cold Warning', 'Cold Weather Advisory',
    'Dense Fog Advisory', 'Air Quality Alert', 'Red Flag Warning',
    'Special Weather Statement', 'Hurricane Warning', 'Tropical Storm Warning',
)

CACHE_FILENAME = 'nws_zones.json'

#: Bumped when a cached entry gains a field. An entry from an older version is
#: re-resolved rather than used half-empty -- the cache has no TTL, so a stale
#: shape would otherwise persist forever.
CACHE_VERSION = 2

ZONE_URL = 'https://api.weather.gov/zones/{kind}/{code}'

#: Names come back bare -- 'Hillsborough', not 'Hillsborough County' -- and the
#: right noun is not always "County". Louisiana has parishes; Alaska has
#: boroughs, municipalities and census areas; Virginia and Maryland have
#: independent cities whose names already say so.
COUNTY_NOUN_BY_STATE = {'LA': 'Parish'}
NOUNS_ALREADY_PRESENT = ('County', 'Parish', 'Borough', 'Municipality', 'City',
                         'Census Area', 'Island', 'Municipio', 'District')
#: States where the subdivision type varies enough that guessing is worse than
#: saying nothing.
STATES_WITHOUT_A_SAFE_NOUN = {'AK', 'DC', 'PR', 'VI', 'GU', 'AS', 'MP'}


def county_label(name, state):
    """'Hillsborough', 'NH' -> 'Hillsborough County, NH'.

    The point is disambiguation: Hillsborough is also a town in New Hampshire,
    and that collision is common enough across the country that a bare name on
    a receipt is genuinely ambiguous.
    """
    name = (name or '').strip()
    if not name:
        return ''
    state = (state or '').strip().upper()

    if any(noun in name for noun in NOUNS_ALREADY_PRESENT):
        label = name                      # 'City of Alexandria', 'Baltimore City'
    elif state in STATES_WITHOUT_A_SAFE_NOUN:
        label = name                      # better bare than confidently wrong
    else:
        label = f"{name} {COUNTY_NOUN_BY_STATE.get(state, 'County')}"
    return f'{label}, {state}' if state else label


def cache_path():
    import os
    return os.path.join(constants.DATA_DIR, 'cache', CACHE_FILENAME)


def _headers():
    return {'User-Agent': USER_AGENT, 'Accept': 'application/geo+json'}


def load_zone_cache():
    value, ok = storage._load_json_file('nws-zones', cache_path(), {})
    return value if ok and isinstance(value, dict) else {}


def save_zone_cache(cache):
    import os
    try:
        os.makedirs(os.path.dirname(cache_path()), exist_ok=True)
    except OSError:
        return False
    return storage._save_json_file('nws-zones', cache_path(), cache)


def resolve_zip(zipcode):
    """ZIP -> {zone, county, place, timezone}. Cached permanently.

    Two requests per ZIP, which is why the result is kept: a forecast zone does
    not move.
    """
    zipcode = str(zipcode).strip()[:5]
    cache = load_zone_cache()
    cached = cache.get(zipcode)
    if cached and cached.get('cache_version') == CACHE_VERSION:
        return cached

    place = requests.get(ZIP_URL.format(zip=zipcode), timeout=15)
    place.raise_for_status()
    location = (place.json().get('places') or [{}])[0]
    lat, lng = location.get('latitude'), location.get('longitude')
    if not lat or not lng:
        raise ValueError(f'No location found for ZIP {zipcode}.')

    point = requests.get(POINTS_URL.format(lat=lat, lng=lng),
                         headers=_headers(), timeout=20)
    point.raise_for_status()
    props = point.json().get('properties', {})

    county_code = (props.get('county') or '').rsplit('/', 1)[-1]
    county_name, state = _county_identity(
        county_code, props, location.get('state abbreviation', ''))

    resolved = {
        'cache_version': CACHE_VERSION,
        'zip': zipcode,
        'city': location.get('place name', '') or
                (props.get('relativeLocation', {}).get('properties', {}) or {}).get('city', ''),
        'state': state,
        'place': f"{location.get('place name', '')}, {location.get('state abbreviation', '')}".strip(', '),
        'zone': (props.get('forecastZone') or '').rsplit('/', 1)[-1],
        'county': county_code,
        'county_name': county_name,
        'county_label': county_label(county_name, state),
        'timezone': props.get('timeZone'),
    }
    cache[zipcode] = resolved
    save_zone_cache(cache)
    log.info(f"Resolved ZIP {zipcode} -> {resolved['place']} zone {resolved['zone']}")
    return resolved


def _county_identity(county_code, point_props, fallback_state):
    """The county's display name and state.

    One extra request per ZIP, for a value that never changes and is cached
    forever. Failing here must not fail the whole resolution -- an alert with a
    slightly less precise area line is much better than no alert.
    """
    relative = (point_props.get('relativeLocation', {}).get('properties', {}) or {})
    state = relative.get('state') or fallback_state or ''
    if not county_code:
        return '', state
    try:
        response = requests.get(
            ZONE_URL.format(kind='county', code=county_code),
            headers=_headers(), timeout=15)
        response.raise_for_status()
        zone = response.json().get('properties', {})
        return zone.get('name') or '', zone.get('state') or state
    except Exception as e:
        log.warning(f"Could not name county zone {county_code}: {e}")
        return '', state


def default_matrix_row(event):
    """Sensible defaults seeded from how serious an event usually is.

    Seeding only fills blanks -- an explicit choice is never overwritten (X-4).
    """
    warning = event.endswith('Warning')
    return {
        'enabled': True,
        'print_updates': warning,
        'print_cancels': warning,
        'quiet_hours': 'override' if warning else 'respect',
        'style': 'alert-large' if warning else 'alert-compact',
    }


class NWSListener(base.Listener):
    name = 'nws'
    title = 'NOAA Weather Alerts'
    description = ('Prints a receipt when the National Weather Service issues '
                   'an alert for your ZIP codes.')
    default_interval = 2          # alerts are time-critical
    max_prints_per_poll = 10

    CONFIG_SCHEMA = (
        base.field('enabled', 'Enabled', 'bool', default=False,
                   help='Poll the National Weather Service.'),
        base.field('zips', 'ZIP codes', 'multiselect', default=[],
                   group='Where',
                   help='One receipt per alert covering any of these.'),
        base.field('interval', 'Check every (minutes)', 'int', default=2,
                   min=1, max=60, group='Where',
                   help='Alerts are time-critical; 2 minutes is a sensible floor.'),
        base.field('min_severity', 'Minimum severity', 'select',
                   default='Moderate', options=SEVERITIES, group='Filtering',
                   help='Seeds the per-event defaults below. An event switched '
                        'on explicitly prints regardless of this.'),
        base.field('min_urgency', 'Minimum urgency', 'select',
                   default='Expected', options=URGENCIES, group='Filtering'),
        base.field('include_test', 'Include test alerts', 'bool', default=False,
                   group='Filtering',
                   help='Useful for checking the setup without waiting for weather.'),
        base.field('quiet_hours', 'Quiet hours', 'time_range',
                   default={'start': '22:00', 'end': '07:00'}, group='Quiet hours',
                   help='Non-urgent alerts wait until morning. Extreme severity '
                        'always prints immediately.'),
        base.field('events', 'Per-event settings', 'matrix', default={},
                   group='Alert types',
                   columns=[
                       {'key': 'enabled', 'label': 'Print', 'type': 'bool'},
                       {'key': 'print_updates', 'label': 'Updates', 'type': 'bool'},
                       {'key': 'print_cancels', 'label': 'Cancels', 'type': 'bool'},
                       {'key': 'quiet_hours', 'label': 'Quiet hours', 'type': 'select',
                        'options': ['respect', 'override', 'digest']},
                       {'key': 'style', 'label': 'Receipt style', 'type': 'select',
                        'options': [], 'dynamic': True},
                   ],
                   help='One row per alert type. Rows appear for anything seen '
                        'live, so the list grows to match your area.'),
    )

    PLACEHOLDERS = {
        'event': 'Severe Thunderstorm Warning',
        'severity': 'Severe',
        'urgency': 'Immediate',
        'headline': 'Severe Thunderstorm Warning issued July 27 at 10:41AM EDT',
        'area': 'Manchester (03102) - Hillsborough County, NH',
        'area_nws': 'Hillsborough, NH',
        'effective': '10:41 AM',
        'expires': '11:45 AM',
        'instruction': 'Move to an interior room on the lowest floor.',
        'description': 'At 1041 AM, a severe thunderstorm was located near Manchester.',
        'zip': '03101',
        'url': 'https://forecast.weather.gov/zipcity.php?inputstring=03101',
        'printed': '10:41 AM 7/27/26',
    }

    # --- polling -------------------------------------------------------------

    def matrix_rows(self, spec, config):
        """Known event types first, then anything seen live that is not listed.

        COMMON_EVENTS gives the table useful rows on a fresh install; the union
        means an alert type this area actually gets is never missing a row just
        because it was not anticipated.
        """
        configured = set((config.get(spec['key']) or {}).keys())
        return list(COMMON_EVENTS) + sorted(configured - set(COMMON_EVENTS))

    def matrix_row_default(self, spec, row):
        return default_matrix_row(row)

    def summary(self):
        config = self.config()
        zips = config.get('zips') or []
        if not zips:
            return 'No ZIP codes yet -- nothing will print.'
        places = []
        for code in zips[:3]:
            try:
                places.append(resolve_zip(code).get('place') or code)
            except Exception:
                places.append(code)
        more = f' +{len(zips) - 3} more' if len(zips) > 3 else ''
        return ', '.join(places) + more

    def zones(self, config):
        """Forecast and county zones for the configured ZIPs."""
        zones = {}
        for zipcode in config.get('zips') or []:
            try:
                resolved = resolve_zip(zipcode)
            except Exception as e:
                log.warning(f"Could not resolve ZIP {zipcode}: {e}")
                continue
            for key in ('zone', 'county'):
                if resolved.get(key):
                    zones[resolved[key]] = resolved
        return zones

    def poll(self, config, since):
        """Active alerts for the configured zones.

        Both forecast and county zones are queried: some products are issued
        against one and some the other, and subscribing to only one silently
        misses half of them.
        """
        zones = self.zones(config)
        if not zones:
            return []

        response = requests.get(
            ALERTS_URL, params={'zone': ','.join(sorted(zones))},
            headers=_headers(), timeout=25)
        response.raise_for_status()
        features = response.json().get('features') or []

        alerts = []
        for feature in features:
            props = feature.get('properties') or {}
            if not config.get('include_test') and props.get('status') != 'Actual':
                continue
            # Only the zones this alert actually covers, not every configured
            # one -- otherwise the receipt claims a tornado warning for a ZIP
            # three counties away that merely happens to be in the settings.
            affected = {url.rsplit('/', 1)[-1]
                        for url in (props.get('affectedZones') or [])}
            matched = {code: resolved for code, resolved in zones.items()
                       if code in affected}
            props['_zones'] = matched or zones
            props['_zones_exact'] = bool(matched)
            alerts.append(props)
        return alerts

    def dedup_key(self, item):
        return item.get('id')

    def sort_key(self, item):
        return item.get('effective') or item.get('sent') or ''

    def describe(self, item):
        return f"{item.get('event', 'Alert')} - {item.get('areaDesc', '')[:40]}"

    # --- filtering -----------------------------------------------------------

    def event_settings(self, config, event):
        """Settings for one event type, seeded from severity if unset."""
        matrix = config.get('events') or {}
        if event in matrix:
            row = dict(default_matrix_row(event))
            row.update(matrix[event])
            return row
        return default_matrix_row(event)

    def should_print(self, config, alert, now=None):
        """Whether this alert prints, and why not when it doesn't.

        Returns (bool, reason). The reason is logged, because "why didn't that
        print?" is the question this listener will most often be asked.
        """
        event = alert.get('event') or 'Unknown'
        row = self.event_settings(config, event)
        if not row.get('enabled'):
            return False, f'{event} is switched off'

        severity = alert.get('severity') or 'Unknown'
        message_type = alert.get('messageType') or 'Alert'
        if message_type == 'Update' and not row.get('print_updates'):
            return False, f'updates for {event} are off'
        if message_type == 'Cancel' and not row.get('print_cancels'):
            return False, f'cancellations for {event} are off'

        # Extreme always prints immediately; that behaviour is the product.
        if severity == 'Extreme':
            return True, ''

        if row.get('quiet_hours') == 'respect' and self.in_quiet_hours(config, now):
            return False, 'quiet hours'
        return True, ''

    def in_quiet_hours(self, config, now=None):
        window = config.get('quiet_hours') or {}
        start, end = window.get('start'), window.get('end')
        if not start or not end:
            return False
        now = now or datetime.now()
        try:
            start_h, start_m = (int(x) for x in start.split(':'))
            end_h, end_m = (int(x) for x in end.split(':'))
        except (ValueError, AttributeError):
            return False
        minutes = now.hour * 60 + now.minute
        start_min, end_min = start_h * 60 + start_m, end_h * 60 + end_m
        # A window that wraps midnight is the normal case here.
        if start_min <= end_min:
            return start_min <= minutes < end_min
        return minutes >= start_min or minutes < end_min

    # --- receipts ------------------------------------------------------------

    def alert_url(self, alert):
        """Where the QR points: the local forecast page for the first ZIP.

        The API gives no human-facing page for an alert. `@id` is the alert
        itself but serves raw JSON, and `web` is only www.weather.gov. The
        zipcity page redirects to the point forecast, which lists the active
        alerts in full at the top -- so scanning it answers both "what is this"
        and "is it still going", which the JSON does not.

        Empty when no ZIP resolved, and fill() then drops the QR block rather
        than printing a symbol that leads nowhere.
        """
        zips = sorted({z['zip'] for z in (alert.get('_zones') or {}).values()
                       if z.get('zip')})
        if not zips:
            return ''
        return f'{FORECAST_URL}?inputstring={zips[0]}'

    def context(self, alert):
        return {
            'event': alert.get('event', 'Weather Alert'),
            'severity': alert.get('severity', 'Unknown'),
            'urgency': alert.get('urgency', 'Unknown'),
            'headline': alert.get('headline', ''),
            'area': self.area_label(alert),
            'area_nws': alert.get('areaDesc', ''),
            'effective': _clock(alert.get('effective')),
            'expires': _clock(alert.get('expires')),
            'instruction': (alert.get('instruction') or '').strip(),
            'description': (alert.get('description') or '').strip(),
            'zip': ', '.join(sorted({z['zip'] for z in (alert.get('_zones') or {}).values()})),
            'url': self.alert_url(alert),
            'printed': layouts._stamp(),
        }

    def blocks_from_context(self, context, big_title=True, description=True, qr=True):
        """The layout, over already-resolved values.

        Split out from receipt_blocks so the Studio presets and the printed
        fallback are generated from one definition -- a preset that drifted
        from what actually prints would be worse than no preset at all, because
        it would look authoritative.

        `context` may hold real values or `{placeholder}` markers; the layout
        does not care, which is what makes one function serve both.

        Title size, description and QR are separate knobs. Size and description
        used to be one "loud" flag, which meant a wind advisory could not have a
        readable headline without also printing 600 characters of forecast
        discussion.
        """
        blocks = [
            receipt.text(context['event'], font='a',
                         width=2 if big_title else 1,
                         height=2 if big_title else 1, bold=True),
            receipt.gap(6),
            receipt.text(f"{context['severity']} - {context['urgency']}",
                         font='b', bold=True),
            receipt.text(context['area'], font='b'),
            receipt.text(f"Until {context['expires']}", font='b'),
        ]
        if context.get('instruction'):
            blocks.append(receipt.rule())
            blocks.append(receipt.text(context['instruction'], font='b', align='left'))
        if description and context.get('description'):
            blocks.append(receipt.rule())
            blocks.append(receipt.text(str(context['description'])[:600],
                                       font='b', align='left'))
        blocks.append(receipt.rule())
        if qr and context.get('url'):
            # At the foot rather than the head, unlike a task or an issue: the
            # event name has to be the first thing on the paper when this is
            # read from across the room. The QR is the follow-up, not the
            # identity of the receipt.
            #
            # Unlabelled, and guarded on the url: an alert whose ZIP did not
            # resolve has nowhere to point, and a "Scan for the forecast" line
            # over no QR would be worse than no line at all.
            blocks.append(receipt.qr(context['url'], size=4))
        blocks.append(receipt.text(f"NWS - Printed {context['printed']}", font='b'))
        return blocks

    def area_label(self, alert):
        """'Manchester (03102) - Hillsborough County, NH'.

        Built from the ZIPs that were configured rather than from the alert's
        own areaDesc, which is a bare county name like 'Hillsborough, NH'.
        That is ambiguous: Hillsborough is also a town in New Hampshire, and
        the same collision happens across most of the country.

        Several of your ZIPs can share a county, and one alert can cover ZIPs
        in different counties, so places are grouped under their county.
        """
        zones = alert.get('_zones') or {}
        if not zones:
            return alert.get('areaDesc', '')

        # Several zone codes (forecast and county) map to the same ZIP.
        by_county = {}
        for resolved in zones.values():
            label = resolved.get('county_label') or resolved.get('place') or ''
            place = resolved.get('city') or resolved.get('place') or ''
            code = resolved.get('zip') or ''
            entry = f'{place} ({code})' if place and code else place or code
            if entry and entry not in by_county.setdefault(label, []):
                by_county[label].append(entry)

        parts = []
        for label, places in by_county.items():
            joined = ', '.join(sorted(places))
            parts.append(f'{joined} - {label}' if label else joined)
        line = '; '.join(sorted(parts))

        # An alert whose zones we could not match exactly is described with the
        # NWS text as well, so the receipt never overstates how precisely it
        # knows where this applies.
        if not alert.get('_zones_exact') and alert.get('areaDesc'):
            return f"{alert['areaDesc']}"
        return line or alert.get('areaDesc', '')

    def receipt_blocks(self, alert):
        """Fallback layout when no template is configured.

        Severity drives the size: an Extreme alert should be readable from
        across the room, a routine advisory should not cost a hand of paper.
        """
        context = self.context(alert)
        return self.blocks_from_context(
            context, big_title=True,
            description=context['severity'] in ('Extreme', 'Severe'))

    def template_presets(self):
        """Two shipped layouts, because one size genuinely does not fit.

        A tornado warning and a wind advisory are not the same kind of event,
        and the per-event `style` column picks between these.
        """
        markers = {key: '{%s}' % key for key in self.PLACEHOLDERS}
        return [
            (f'{self.name}-default',
             self.blocks_from_context(markers, big_title=True, description=True,
                                      qr=True)),
            # No QR on the shorter two. They exist to keep a wind advisory from
            # costing a hand of paper, and a QR is another 25mm of it.
            (f'{self.name}-compact',
             self.blocks_from_context(markers, big_title=True, description=False,
                                      qr=False)),
            (f'{self.name}-minimal',
             self.blocks_from_context(markers, big_title=False, description=False,
                                      qr=False)),
        ]

    def template_name(self, config, alert):
        """The per-event style, so one listener covers both extremes."""
        event = alert.get('event', '')
        row = (config.get('events') or {}).get(event) or {}
        return row.get('style') or None

    def matrix_column_options(self, spec, column):
        """The style column offers whatever templates exist right now,
        including ones edited in the Studio -- a fixed list would go stale the
        moment someone saved a new layout."""
        if column['key'] != 'style':
            return column.get('options', [])
        from .. import styles
        return [''] + [t['name'] for t in styles.list_templates(self.name)]

    def history_record(self, alert):
        context = self.context(alert)
        return {
            'type': 'nws',
            'id': alert.get('id'),
            'category': context['event'],
            'severity': context['severity'],
            'address': context['area'],
            'description': context['description'][:500],
            'status': alert.get('messageType', 'Alert'),
            'reported_at': alert.get('effective', ''),
            # Carried so a reprint points at the same forecast page. Without it
            # the reprint falls back to the Studio's sample ZIP.
            'url': context['url'],
            'print_time': datetime.now().isoformat(),
        }


def _clock(value):
    if not value:
        return 'unknown'
    try:
        return datetime.fromisoformat(value).strftime('%-I:%M %p')
    except (ValueError, TypeError):
        return str(value)


listener = base.register(NWSListener())
