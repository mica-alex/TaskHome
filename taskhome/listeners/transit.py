"""Transit departures and service alerts (MASTER_PLAN P5-2 #10).

"Next three trains from your stop", and a receipt when your line is disrupted.

Agencies differ enough that this is built around **providers** rather than one
API client:

* `mbta` -- a first-class native implementation. The MBTA V3 API is modern
  JSON, needs no key, and exposes predictions and alerts directly, so it gets
  proper stop search, route names and headsigns.
* `gtfsrt` -- any agency publishing a GTFS-Realtime feed, read by
  `taskhome/gtfsrt.py` with no protobuf dependency. Verified against NYC MTA.

A provider is a small class: find stops, fetch departures, fetch alerts. Adding
an agency means adding one, not touching the listener.

Two distinct things get printed, with separate toggles, because they are wanted
at different times: a **departure board** on a schedule ("print my commute at
08:00"), and an **alert** whenever the agency issues one for a line you watch.
A departure board printed at random is useless -- the times are stale by the
time you reach the door.
"""
import re
from datetime import datetime, timedelta, timezone

import requests

from . import base
from .. import gtfsrt, layouts, receipt
from ..logsetup import log

TIMEOUT = 20
USER_AGENT = 'TaskHome/2.0 (+https://github.com/mica-alex/TaskHome)'

MBTA_API = 'https://api-v3.mbta.com'

#: Feeds confirmed to work without a key. Anything else can be pasted in.
KNOWN_FEEDS = {
    'nyc-ace': ('NYC Subway A/C/E',
                'https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-ace'),
    'nyc-bdfm': ('NYC Subway B/D/F/M',
                 'https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-bdfm'),
    'nyc-g': ('NYC Subway G',
              'https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-g'),
    'nyc-jz': ('NYC Subway J/Z',
               'https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-jz'),
    'nyc-nqrw': ('NYC Subway N/Q/R/W',
                 'https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-nqrw'),
    'nyc-l': ('NYC Subway L',
              'https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-l'),
    'nyc-numbered': ('NYC Subway 1-7',
                     'https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs'),
}


def _headers():
    return {'User-Agent': USER_AGENT}


# --- providers ----------------------------------------------------------------

class Provider:
    key = ''
    label = ''

    def departures(self, config, limit):
        """[{route, headsign, when (aware datetime), stop}]"""
        raise NotImplementedError

    def alerts(self, config):
        """[{id, header, description, severity, routes}]"""
        return []

    def find_stops(self, config, query):
        """[{id, name}] -- for the settings page picker."""
        return []


class MBTAProvider(Provider):
    """The MBTA V3 API. JSON, no key required.

    A key raises the rate limit substantially but nothing here needs it, and
    demanding one before the listener does anything would stop most people
    using it.
    """
    key = 'mbta'
    label = 'MBTA (Boston)'

    def _get(self, config, path, params):
        headers = _headers()
        token = (config.get('api_key') or '').strip()
        if token:
            headers['x-api-key'] = token
        response = requests.get(f'{MBTA_API}{path}', headers=headers,
                                params=params, timeout=TIMEOUT)
        response.raise_for_status()
        return response.json()

    def find_stops(self, config, query):
        query = (query or '').strip()
        if not query:
            return []
        # Coordinates search by radius; anything else is a name match, which
        # the API has no endpoint for -- so names are filtered client-side
        # over the route's stop list.
        coords = re.match(r'^\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*$', query)
        if coords:
            payload = self._get(config, '/stops', {
                'filter[latitude]': coords.group(1),
                'filter[longitude]': coords.group(2),
                'filter[radius]': 0.02, 'page[limit]': 15})
        else:
            # There is no name-search endpoint, and the full stop list is in
            # the thousands. Parent stations are ~276 and are what someone
            # naming a stop actually means -- "North Station", not each of its
            # platforms.
            payload = self._get(config, '/stops', {
                'page[limit]': 400, 'filter[location_type]': 1})
        found = []
        for stop in payload.get('data', []):
            name = stop.get('attributes', {}).get('name', '')
            if coords or query.lower() in name.lower():
                found.append({'id': stop['id'], 'name': name})
        return found[:15]

    def departures(self, config, limit):
        stops = [s for s in (config.get('stops') or []) if s.strip()]
        if not stops:
            return []

        payload = self._get(config, '/predictions', {
            'filter[stop]': ','.join(stops),
            'sort': 'departure_time',
            'page[limit]': 60,
            'include': 'route,trip,stop',
        })
        included = {(i['type'], i['id']): i for i in payload.get('included', [])}

        now = datetime.now(timezone.utc)
        found = []
        for prediction in payload.get('data', []):
            attributes = prediction.get('attributes', {})

            # Sorting ascending by departure_time puts NULLs first, and a
            # cancelled trip has no times at all -- so an unfiltered query
            # returns a board full of blanks during a disruption, which is
            # exactly when someone is looking at it.
            if attributes.get('schedule_relationship') in ('CANCELLED', 'SKIPPED'):
                continue
            raw = attributes.get('departure_time') or attributes.get('arrival_time')
            if not raw:
                continue
            try:
                when = datetime.fromisoformat(raw)
            except ValueError:
                continue
            if when < now - timedelta(minutes=1):
                continue

            relationships = prediction.get('relationships', {})
            route_id = ((relationships.get('route') or {}).get('data') or {}).get('id')
            trip_id = ((relationships.get('trip') or {}).get('data') or {}).get('id')
            stop_id = ((relationships.get('stop') or {}).get('data') or {}).get('id')

            route = included.get(('route', route_id), {}).get('attributes', {})
            trip = included.get(('trip', trip_id), {}).get('attributes', {})
            stop = included.get(('stop', stop_id), {}).get('attributes', {})

            found.append({
                'route': route.get('short_name') or route.get('long_name') or route_id,
                'headsign': trip.get('headsign') or '',
                'when': when,
                'stop': stop.get('name') or stop_id or '',
            })

        found.sort(key=lambda d: d['when'])
        return found[:limit]

    def alerts(self, config):
        # Narrowed by the caller to the routes and stops that asked for alerts.
        routes = config.get('_alert_routes') or []
        stops = config.get('_alert_stops') or []
        params = {'page[limit]': 20, 'filter[datetime]': 'NOW'}
        if routes:
            params['filter[route]'] = ','.join(routes)
        elif stops:
            params['filter[stop]'] = ','.join(stops)
        else:
            return []

        payload = self._get(config, '/alerts', params)
        found = []
        for alert in payload.get('data', []):
            attributes = alert.get('attributes', {})
            # Which of the watched routes this alert touches, so the per-route
            # severity threshold can be applied to the right one.
            informed = set()
            for entity in attributes.get('informed_entity') or []:
                if entity.get('route'):
                    informed.add(entity['route'])
            found.append({
                'id': f"mbta:{alert.get('id')}",
                'header': attributes.get('header') or '',
                'description': (attributes.get('description') or '')[:600],
                'severity': attributes.get('severity'),
                'effect': (attributes.get('effect') or '').replace('_', ' ').title(),
                'routes': sorted(informed) or routes,
            })
        return found


class GTFSRTProvider(Provider):
    """Any agency publishing GTFS-Realtime.

    Confirmed keyless against NYC MTA. LA Metro and others can be used by
    pasting their feed URL; there is no universal directory of these, which is
    why the field is free text with a few known ones offered.

    A GTFS-RT feed carries ids, not names -- it is the realtime half of a
    dataset whose static half has the names. Stop and route ids are therefore
    shown as-is rather than invented.
    """
    key = 'gtfsrt'
    label = 'GTFS-Realtime feed'

    def _feed_urls(self, config):
        urls = []
        for entry in (config.get('feeds') or []):
            entry = entry.strip()
            if not entry:
                continue
            urls.append(KNOWN_FEEDS[entry][1] if entry in KNOWN_FEEDS else entry)
        return urls

    def _fetch(self, url):
        response = requests.get(url, headers=_headers(), timeout=TIMEOUT)
        response.raise_for_status()
        return gtfsrt.parse_feed(response.content)

    def departures(self, config, limit):
        stops = {s.strip() for s in (config.get('stops') or []) if s.strip()}
        if not stops:
            return []

        now = datetime.now(timezone.utc).timestamp()
        found = []
        for url in self._feed_urls(config):
            try:
                feed = self._fetch(url)
            except Exception as e:
                log.warning(f'Transit feed failed ({url[:60]}): {e}')
                continue
            for update in feed['trip_updates']:
                for stop in update['stops']:
                    if stop['stop_id'] not in stops:
                        continue
                    stamp = stop.get('departure') or stop.get('arrival')
                    if not stamp or stamp < now - 60:
                        continue
                    found.append({
                        'route': update.get('route_id') or '?',
                        'headsign': '',
                        'when': datetime.fromtimestamp(stamp, timezone.utc),
                        'stop': stop['stop_id'],
                    })
        found.sort(key=lambda d: d['when'])
        return found[:limit]

    def alerts(self, config):
        watched = set(config.get('_alert_routes') or [])
        found = []
        for url in self._feed_urls(config):
            try:
                feed = self._fetch(url)
            except Exception as e:
                log.warning(f'Transit alerts failed ({url[:60]}): {e}')
                continue
            for alert in feed['alerts']:
                if watched and not (set(alert['routes']) & watched):
                    continue
                found.append({
                    'id': f"gtfsrt:{hash(alert['header']) & 0xFFFFFFFF}",
                    'header': alert['header'],
                    'description': alert['description'][:600],
                    'severity': None,
                    'effect': '',
                    'routes': alert['routes'],
                })
        return found


PROVIDERS = {p.key: p() for p in (MBTAProvider, GTFSRTProvider)}


# --- the listener -------------------------------------------------------------

class TransitListener(base.Listener):
    name = 'transit'
    title = 'Transit'
    description = ('Next departures from your stops, and a receipt when your '
                   'line is disrupted.')
    default_interval = 5
    max_prints_per_poll = 5

    CONFIG_SCHEMA = (
        base.field('enabled', 'Enabled', 'bool', default=False),
        base.field('provider', 'Agency', 'select', default='mbta',
                   options=sorted(PROVIDERS), group='Agency',
                   help='MBTA has a proper API. Anything else uses a '
                        'GTFS-Realtime feed.'),
        base.field('api_key', 'MBTA API key', 'secret', default='', group='Agency',
                   help='Optional. Everything works without one; a key just '
                        'raises the rate limit.'),
        base.field('feeds', 'GTFS-RT feeds', 'multiselect', default=[],
                   group='Agency',
                   help='A feed URL, or one of: ' + ', '.join(sorted(KNOWN_FEEDS))),

        base.field('stops', 'Stops', 'multiselect', default=[], group='Stops',
                   help='MBTA stop ids such as place-north, or GTFS stop ids '
                        'such as H11S. Each gets its own row below.'),
        base.field('stop_settings', 'Per-stop', 'matrix', default={},
                   group='Stops',
                   columns=[
                       {'key': 'departures', 'label': 'Departure board', 'type': 'bool'},
                       {'key': 'alerts', 'label': 'Alerts here', 'type': 'bool'},
                   ],
                   help='A departure board for the stop you leave from, and '
                        'alerts only where they would change your plans.'),

        base.field('routes', 'Routes', 'multiselect', default=[], group='Routes',
                   help='Red, Orange, Green-B, 1, A ... Each gets its own row.'),
        base.field('route_settings', 'Per-route', 'matrix', default={},
                   group='Routes',
                   columns=[
                       {'key': 'alerts', 'label': 'Service alerts', 'type': 'bool'},
                       {'key': 'min_severity', 'label': 'From severity',
                        'type': 'select',
                        'options': ['any', '3', '5', '7', '9']},
                   ],
                   help='Alerts per line, at whatever severity you care about. '
                        'MBTA scores 1-10; 7 is shuttle-bus level. GTFS-RT '
                        'feeds carry no severity, so "any" applies there.'),

        base.field('print_departures', 'Print departure boards', 'bool',
                   default=True, group='Departure board',
                   help='Master switch. Individual stops are set above.'),
        base.field('board_times', 'At these times', 'multiselect',
                   default=['08:00'], group='Departure board',
                   help='24-hour, e.g. 08:00. A board printed at a random '
                        'moment is stale before you reach the door.'),
        base.field('board_size', 'Departures to list', 'int', default=5,
                   min=1, max=15, group='Departure board'),

        base.field('print_alerts', 'Print service alerts', 'bool', default=True,
                   group='Alerts', help='Master switch. Lines are set above.'),
    )

    PLACEHOLDERS = {
        'stop': 'North Station',
        'departures': 'Orange   Forest Hills      4 min\n'
                      'Green-E  Heath Street      7 min',
        'header': 'Red Line: Shuttle buses replacing service',
        'description': 'Shuttles run between Alewife and Harvard.',
        'printed': '8:00 AM 7/27/26',
    }

    def matrix_rows(self, spec, config):
        """Rows are whatever the user has added above, so the two lists and
        their settings cannot drift apart."""
        source = 'stops' if spec['key'] == 'stop_settings' else 'routes'
        return [v for v in (config.get(source) or []) if v.strip()]

    def matrix_row_default(self, spec, row):
        if spec['key'] == 'stop_settings':
            # A stop you added is one you travel from; alerts there are
            # noisier and are opt-in.
            return {'departures': True, 'alerts': False}
        return {'alerts': True, 'min_severity': '3'}

    def stop_setting(self, config, stop, key):
        row = (config.get('stop_settings') or {}).get(stop)
        if row is None:
            return self.matrix_row_default({'key': 'stop_settings'}, stop).get(key)
        return row.get(key, False)

    def route_setting(self, config, route, key):
        row = (config.get('route_settings') or {}).get(route)
        if row is None:
            return self.matrix_row_default({'key': 'route_settings'}, route).get(key)
        return row.get(key)

    def wanted_stops(self, config, purpose):
        """Stops with `purpose` switched on."""
        return [s for s in (config.get('stops') or []) if s.strip()
                and self.stop_setting(config, s, purpose)]

    def wanted_routes(self, config):
        return [r for r in (config.get('routes') or []) if r.strip()
                and self.route_setting(config, r, 'alerts')]

    def alert_passes(self, config, alert):
        """Per-route subscription and severity, rather than one global number.

        An alert usually names several routes. Only the ones actually
        subscribed to are consulted -- otherwise a minor notice on a line that
        was switched off prints anyway, just because it happened to share an
        alert with a line that is on.
        """
        subscribed = set(self.wanted_routes(config))
        touched = set(alert.get('routes') or [])

        if subscribed:
            relevant = touched & subscribed
            if touched and not relevant:
                return False
            # An alert that names no route at all is a system-wide notice.
            candidates = relevant or subscribed
        else:
            # No route subscriptions: this came from a stop's alert setting.
            candidates = touched

        severity = alert.get('severity')
        if severity is None:
            return True                    # GTFS-RT carries no severity

        thresholds = []
        for route in candidates:
            raw = self.route_setting(config, route, 'min_severity')
            if raw in (None, 'any', ''):
                thresholds.append(0)
            else:
                try:
                    thresholds.append(int(raw))
                except (TypeError, ValueError):
                    thresholds.append(0)
        if not thresholds:
            return True
        # The most permissive subscribed line wins: if any of them wants this
        # severity, it prints.
        return severity >= min(thresholds)

    def provider(self, config):
        return PROVIDERS.get(config.get('provider') or 'mbta', PROVIDERS['mbta'])

    # --- polling -------------------------------------------------------------

    def poll(self, config, since):
        items = []
        now = datetime.now()
        provider = self.provider(config)

        if config.get('print_alerts', True):
            alert_config = dict(config,
                                _alert_routes=self.wanted_routes(config),
                                _alert_stops=self.wanted_stops(config, 'alerts'))
            try:
                for alert in provider.alerts(alert_config):
                    if self.alert_passes(config, alert):
                        items.append({'kind': 'alert', **alert})
            except Exception as e:
                log.warning(f'Transit alerts failed: {e}')

        if config.get('print_departures', True) and self._board_due(config, now):
            # Only the stops that asked for a board.
            board_config = dict(config, stops=self.wanted_stops(config, 'departures'))
            try:
                departures = provider.departures(
                    board_config, max(int(config.get('board_size', 5) or 5), 1))
            except Exception as e:
                log.warning(f'Transit departures failed: {e}')
                departures = []
            if departures:
                self.state()['last_board'] = f"{now.date()}T{now.strftime('%H:%M')}"
                items.append({
                    'kind': 'board',
                    'id': f"board-{now.strftime('%Y%m%dT%H%M')}",
                    'departures': departures,
                })
        return items

    def _board_due(self, config, now):
        """True when one of the configured times has just passed.

        Matched to the minute and remembered, so a five-minute poll interval
        prints one board rather than one per tick for the rest of the day.
        """
        wanted = [t.strip() for t in (config.get('board_times') or []) if t.strip()]
        if not wanted:
            return False
        current = now.strftime('%H:%M')
        runtime = self.state()
        for target in wanted:
            if current < target:
                continue
            stamp = f'{now.date()}T{target}'
            if runtime.get('last_board') == stamp:
                continue
            # Only fire within a sensible window of the target, or restarting
            # at 23:00 would print every board configured for that day.
            try:
                hour, minute = (int(x) for x in target.split(':'))
            except ValueError:
                continue
            target_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if timedelta(0) <= (now - target_dt) <= timedelta(minutes=30):
                runtime['last_board'] = stamp
                return True
        return False

    # --- receipt -------------------------------------------------------------

    def dedup_key(self, item):
        return item.get('id')

    def describe(self, item):
        if item.get('kind') == 'board':
            return f"Departure board ({len(item.get('departures', []))})"
        return f"Transit alert: {item.get('header', '')[:40]}"

    def sort_key(self, item):
        return item.get('kind', '')

    def context(self, item):
        if item.get('kind') == 'board':
            lines = []
            now = datetime.now(timezone.utc)
            for departure in item['departures']:
                minutes = max(0, round((departure['when'] - now).total_seconds() / 60))
                when = 'now' if minutes < 1 else f'{minutes} min'
                headsign = departure.get('headsign') or departure.get('stop') or ''
                lines.append(f"{departure['route'][:8]:<9}{headsign[:20]:<21}{when}")
            return {
                'stop': item['departures'][0].get('stop', '') if item['departures'] else '',
                'departures': '\n'.join(lines),
                'header': '', 'description': '',
                'printed': layouts._stamp(),
            }
        return {
            'stop': '', 'departures': '',
            'header': item.get('header', ''),
            'description': item.get('description', ''),
            'printed': layouts._stamp(),
        }

    def receipt_blocks(self, item):
        context = self.context(item)
        if item.get('kind') == 'board':
            return [
                receipt.text('DEPARTURES', font='a', width=2, height=2, bold=True),
                receipt.gap(6),
                receipt.text(context['stop'], font='b', bold=True),
                receipt.rule(),
                receipt.text(context['departures'], font='b', align='left'),
                receipt.rule(),
                receipt.text(f"Printed {context['printed']}", font='b'),
            ]
        blocks = [
            receipt.text(context['header'], font='a', width=2, height=2, bold=True),
        ]
        if context['description']:
            blocks.append(receipt.gap(6))
            blocks.append(receipt.text(context['description'], font='b', align='left'))
        blocks.append(receipt.rule())
        blocks.append(receipt.text(f"Printed {context['printed']}", font='b'))
        return blocks

    def blocks_from_context(self, context):
        return [
            receipt.text('DEPARTURES', font='a', width=2, height=2, bold=True),
            receipt.gap(6),
            receipt.text(context['stop'], font='b', bold=True),
            receipt.rule(),
            receipt.text(context['departures'], font='b', align='left'),
            receipt.rule(),
            receipt.text(f"Printed {context['printed']}", font='b'),
        ]

    def template_presets(self):
        markers = {key: '{%s}' % key for key in self.PLACEHOLDERS}
        return [(f'{self.name}-default', self.blocks_from_context(markers))]

    def history_record(self, item):
        return {
            'type': 'transit',
            'id': item.get('id'),
            'category': 'Departures' if item.get('kind') == 'board' else 'Alert',
            'title': (self.describe(item))[:120],
            'description': (item.get('description')
                            or ', '.join(d['route'] for d in item.get('departures', [])))[:500],
            'print_time': datetime.now().isoformat(),
        }

    def summary(self):
        config = self.config()
        stops = config.get('stops') or []
        provider = self.provider(config)
        if not stops and not (config.get('routes') or []):
            return 'No stops or routes yet -- nothing will print.'
        return f"{provider.label}: {len(stops)} stop(s)"


listener = base.register(TransitListener())
