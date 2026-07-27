"""Package tracking via 17TRACK (MASTER_PLAN P5-2 #7).

A receipt when a parcel changes status -- "Out for delivery" is the one worth
paper.

Carrier APIs are the reason this is last in the plan: each of the big four has
its own contract, auth model and approval process, and scraping them is both
fragile and rude. 17TRACK aggregates them behind one API with a free tier, so
this is one integration instead of four.

**Untested against the live API.** Written from the published v2.2 contract and
shipped with no key available to exercise it. What that means concretely: the
request shapes and the field names below come from documentation rather than
from a response I have seen, unlike every other listener here, where they were
verified by probing. The parsing is therefore written defensively -- every
field access tolerates absence, and an unrecognised payload logs what it got
rather than raising. Expect to adjust `_parse_track` on first real contact.

The alternative path needs no key at all and works today: forward shipping
emails to the webhook listener. That is not a worse answer for one or two
parcels a month.
"""
from datetime import datetime, timezone

import requests

from . import base
from .. import layouts, receipt
from ..logsetup import log

API = 'https://api.17track.net/track/v2.2'
TIMEOUT = 25
MAX_TRACKED = 40

#: 17TRACK's numeric package states. Documented values; the listener treats an
#: unknown one as "in transit" rather than dropping it.
STATUS_LABELS = {
    0: 'Not found',
    10: 'In transit',
    20: 'Expired',
    30: 'Ready for pickup',
    35: 'Undelivered',
    40: 'Delivered',
    50: 'Alert',
}

#: The states worth paper by default. "In transit" fires on every scan between
#: two cities, which is a receipt for nothing.
DEFAULT_PRINT_ON = ['30', '35', '40', '50']

#: Sub-status strings that mean "on the van today", which is the single most
#: useful moment to print.
OUT_FOR_DELIVERY = ('OutForDelivery', 'InTransit_PickedUp')


class PackageListener(base.Listener):
    name = 'packages'
    title = 'Package tracking'
    description = ('A receipt when a parcel changes status. Uses 17TRACK, '
                   'which covers most carriers behind one API key.')
    default_interval = 60
    max_prints_per_poll = 5

    CONFIG_SCHEMA = (
        base.field('enabled', 'Enabled', 'bool', default=False),
        base.field('api_key', '17TRACK API key', 'secret', default='',
                   group='Account',
                   help='From api.17track.net. Required -- unlike the other '
                        'listeners here, there is no useful unauthenticated '
                        'tier. The free plan covers a household.'),
        base.field('interval', 'Check every (minutes)', 'int', default=60,
                   min=15, max=1440, group='Account',
                   help='Parcels do not move minute to minute, and the free '
                        'tier has a monthly quota worth spending slowly.'),
        base.field('numbers', 'Tracking numbers', 'multiselect', default=[],
                   group='Parcels',
                   help='Paste tracking numbers. The carrier is detected '
                        'automatically; add "number:carrier_code" to force one.'),
        base.field('print_on', 'Print when a parcel becomes', 'multiselect',
                   default=DEFAULT_PRINT_ON,
                   options=[str(k) for k in sorted(STATUS_LABELS)],
                   group='Parcels',
                   help='10 is In transit, which fires on every scan between '
                        'two cities. 40 is Delivered, 30 is Ready for pickup, '
                        '50 is Alert.'),
        base.field('print_out_for_delivery', 'Print when out for delivery',
                   'bool', default=True, group='Parcels',
                   help='The single most useful moment: it is on the van today.'),
        base.field('forget_after_days', 'Forget delivered parcels after (days)',
                   'int', default=3, min=1, max=60, group='Parcels',
                   help='A delivered parcel stops being tracked, so the quota '
                        'is not spent on parcels already in the hallway.'),
    )

    PLACEHOLDERS = {
        'number': '1Z999AA10123456784',
        'carrier': 'UPS',
        'status': 'Out for delivery',
        'location': 'Manchester, NH',
        'detail': 'On vehicle for delivery',
        'printed': '8:30 AM 7/27/26',
    }

    # --- API ------------------------------------------------------------------

    def _post(self, config, path, payload):
        key = (config.get('api_key') or '').strip()
        if not key:
            raise RuntimeError('No 17TRACK API key configured.')
        response = requests.post(
            f'{API}{path}',
            headers={'17token': key, 'Content-Type': 'application/json'},
            json=payload, timeout=TIMEOUT)
        if response.status_code == 401:
            raise RuntimeError('17TRACK rejected the API key.')
        response.raise_for_status()
        body = response.json()
        # The documented envelope is {code, data:{accepted, rejected}}. Anything
        # else is logged whole, because this listener has never seen a real
        # response and the log is the only way to find out what arrived.
        if not isinstance(body, dict):
            raise RuntimeError(f'Unexpected 17TRACK response: {body!r:.200}')
        return body

    def _register(self, config, numbers):
        """Numbers must be registered before they can be queried."""
        payload = []
        for entry in numbers:
            number, _, carrier = entry.partition(':')
            item = {'number': number.strip()}
            if carrier.strip():
                try:
                    item['carrier'] = int(carrier.strip())
                except ValueError:
                    item['carrier'] = carrier.strip()
            payload.append(item)
        if not payload:
            return
        try:
            body = self._post(config, '/register', payload)
            rejected = ((body.get('data') or {}).get('rejected')) or []
            for entry in rejected:
                # Already-registered is the common, harmless rejection.
                log.info(f'17TRACK did not register {entry}')
        except Exception as e:
            log.warning(f'17TRACK register failed: {e}')

    def poll(self, config, since):
        numbers = [n.strip() for n in (config.get('numbers') or []) if n.strip()]
        if not numbers:
            return []
        numbers = numbers[:MAX_TRACKED]

        runtime = self.state()
        if not runtime.get('registered'):
            self._register(config, numbers)
            runtime['registered'] = True
        else:
            # Register anything added since last time.
            known = set(runtime.get('known_numbers') or [])
            fresh = [n for n in numbers if n.split(':')[0] not in known]
            if fresh:
                self._register(config, fresh)
        runtime['known_numbers'] = [n.split(':')[0] for n in numbers]

        payload = [{'number': n.split(':')[0]} for n in numbers]
        try:
            body = self._post(config, '/gettrackinfo', payload)
        except Exception as e:
            log.warning(f'17TRACK lookup failed: {e}')
            raise

        accepted = ((body.get('data') or {}).get('accepted')) or []
        seen_states = runtime.get('states') or {}
        items = []

        for entry in accepted:
            parsed = self._parse_track(entry)
            if parsed is None:
                continue
            number = parsed['number']
            # The state key includes the sub-status, so "in transit" to "out
            # for delivery" is a change even though the numeric status is the
            # same.
            state_key = f"{parsed['status_code']}:{parsed['substatus']}:{parsed['detail'][:60]}"
            if seen_states.get(number) == state_key:
                continue
            first_sight = number not in seen_states
            seen_states[number] = state_key

            # The first poll after adding a parcel would otherwise print its
            # current state, which is usually "in transit" and not news.
            if first_sight:
                continue
            if self._wanted(config, parsed):
                items.append(parsed)

        runtime['states'] = seen_states
        self._forget_delivered(config, runtime, seen_states)
        return items

    def _wanted(self, config, parsed):
        if (config.get('print_out_for_delivery', True)
                and parsed['substatus'] in OUT_FOR_DELIVERY):
            return True
        return str(parsed['status_code']) in (config.get('print_on') or DEFAULT_PRINT_ON)

    def _forget_delivered(self, config, runtime, seen_states):
        """Stop tracking a parcel that arrived, so quota is not spent on
        parcels already in the hallway."""
        days = max(int(config.get('forget_after_days', 3) or 3), 1)
        delivered = runtime.get('delivered_at') or {}
        now = datetime.now(timezone.utc)
        for number, state_key in list(seen_states.items()):
            if state_key.startswith('40:'):
                delivered.setdefault(number, now.isoformat())
        for number, stamp in list(delivered.items()):
            try:
                when = datetime.fromisoformat(stamp)
            except (TypeError, ValueError):
                continue
            if (now - when).days >= days:
                seen_states.pop(number, None)
                delivered.pop(number, None)
                log.info(f'17TRACK: no longer tracking delivered parcel {number}')
        runtime['delivered_at'] = delivered

    def _parse_track(self, entry):
        """One accepted tracking result -> an item.

        Written from the documented shape and never run against a real
        response, so every access tolerates absence and an unrecognised entry
        is logged rather than raised.
        """
        try:
            number = entry.get('number') or ''
            track = entry.get('track_info') or entry.get('track') or {}
            latest = (track.get('latest_status') or {})
            event = (track.get('latest_event') or {})
            carrier = ((track.get('tracking') or {}).get('providers') or [{}])
            carrier_name = ''
            if carrier and isinstance(carrier, list):
                carrier_name = ((carrier[0].get('provider') or {}).get('name')) or ''

            status_code = latest.get('status')
            if isinstance(status_code, str) and status_code.isdigit():
                status_code = int(status_code)
            if not isinstance(status_code, int):
                status_code = 10

            location = ''
            for key in ('location', 'address'):
                value = event.get(key)
                if isinstance(value, str) and value:
                    location = value
                    break
                if isinstance(value, dict):
                    location = ', '.join(
                        str(v) for v in (value.get('city'), value.get('state'))
                        if v)
                    break

            if not number:
                log.warning(f'17TRACK entry with no number: {str(entry)[:200]}')
                return None

            return {
                'id': f'pkg:{number}',
                'number': number,
                'carrier': carrier_name or 'Carrier',
                'status_code': status_code,
                'status': STATUS_LABELS.get(status_code, 'In transit'),
                'substatus': latest.get('sub_status') or '',
                'detail': (event.get('description')
                           or event.get('stage') or ''),
                'location': location,
                'time': event.get('time_iso') or event.get('time_utc') or '',
            }
        except Exception as e:
            log.warning(f'17TRACK entry not understood ({e}): {str(entry)[:200]}')
            return None

    # --- receipt -------------------------------------------------------------

    def dedup_key(self, item):
        return f"{item['id']}:{item['status_code']}:{item['substatus']}"

    def describe(self, item):
        return f"{item['carrier']} {item['number'][-6:]}: {item['status']}"

    def context(self, item):
        return {
            'number': item.get('number', ''),
            'carrier': item.get('carrier', ''),
            'status': item.get('status', ''),
            'location': item.get('location', ''),
            'detail': item.get('detail', ''),
            'printed': layouts._stamp(),
        }

    def blocks_from_context(self, context):
        blocks = [
            receipt.text(context['status'], font='a', width=2, height=2, bold=True),
            receipt.gap(6),
            receipt.text(context['carrier'], font='b', bold=True),
        ]
        if context.get('detail'):
            blocks.append(receipt.text(context['detail'], font='b', align='left'))
        if context.get('location'):
            blocks.append(receipt.text(context['location'], font='b'))
        blocks.append(receipt.rule())
        blocks.append(receipt.text(context['number'], font='b'))
        blocks.append(receipt.text(f"Printed {context['printed']}", font='b'))
        return blocks

    def receipt_blocks(self, item):
        return self.blocks_from_context(self.context(item))

    def template_presets(self):
        markers = {key: '{%s}' % key for key in self.PLACEHOLDERS}
        return [(f'{self.name}-default', self.blocks_from_context(markers))]

    def history_record(self, item):
        return {
            'type': 'packages',
            'id': item.get('number'),
            'category': item.get('carrier', 'Parcel'),
            'title': f"{item.get('status')} - {item.get('number')}",
            'description': item.get('detail', '')[:500],
            'print_time': datetime.now().isoformat(),
        }

    def notice(self):
        if not (self.config().get('api_key') or '').strip():
            return {
                'title': 'This one needs a key',
                'body': 'Unlike the other listeners here, 17TRACK has no useful '
                        'unauthenticated tier. Sign up at api.17track.net; the '
                        'free plan covers a household. '
                        'If you would rather not, forwarding shipping emails to '
                        'the Webhook listener works today with no key at all.',
            }
        return {
            'title': 'Untested against the live API',
            'body': 'This listener was written from the published contract and '
                    'has never been run against a real 17TRACK response. If a '
                    'parcel updates and nothing prints, the log records exactly '
                    'what arrived -- that is the first place to look.',
        }

    def summary(self):
        config = self.config()
        numbers = [n for n in (config.get('numbers') or []) if n.strip()]
        if not config.get('api_key'):
            return 'No API key yet -- nothing will print.'
        if not numbers:
            return 'No tracking numbers yet.'
        return f'{len(numbers)} parcel(s)'


listener = base.register(PackageListener())
