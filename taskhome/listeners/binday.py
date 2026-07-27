"""Bin day reminder (MASTER_PLAN P5-2 #6).

"Trash and recycling tonight" the evening before pickup.

Two ways to configure it, because municipalities are split down the middle:

* **A rule** -- collection is on a weekday, optionally alternating weeks for
  recycling. Covers most places and needs no network at all.
* **An ICS calendar** -- many councils publish one, and the calendar listener
  already knows how to read them. Reused rather than reimplemented.

The reminder fires the *evening before* by default, which is the only time it
is useful: a receipt on collection morning arrives after the lorry.

Alternating-week recycling is the part worth getting right. It is anchored to a
date the user supplies -- "recycling went out on this day" -- and counted in
whole weeks from there, rather than derived from ISO week numbers. Week
parity flips at new year and would silently invert the schedule every few
years.
"""
from datetime import date, datetime, timedelta

from . import base
from .. import layouts, receipt
from ..logsetup import log

WEEKDAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday',
            'Saturday', 'Sunday')


def weeks_between(anchor, day):
    """Whole weeks from `anchor` to `day`, which may be negative."""
    return (day - anchor).days // 7


class BinDayListener(base.Listener):
    name = 'binday'
    title = 'Bin day'
    description = 'A reminder the evening before collection.'
    default_interval = 30
    max_prints_per_poll = 1

    CONFIG_SCHEMA = (
        base.field('enabled', 'Enabled', 'bool', default=False),
        base.field('source', 'Where the schedule comes from', 'select',
                   default='rule', options=['rule', 'calendar'], group='Schedule',
                   help="'rule' is a weekday you set here. 'calendar' reads an "
                        'ICS feed, which many councils publish.'),
        base.field('collection_day', 'Collection day', 'select',
                   default='Tuesday', options=WEEKDAYS, group='Schedule',
                   depends_on=None,
                   help='Used when the source is a rule.'),
        base.field('remind_at', 'Remind at', 'time', default='18:00',
                   group='Schedule',
                   help='The evening before. A receipt on collection morning '
                        'arrives after the lorry.'),
        base.field('remind_days_before', 'Days before collection', 'int',
                   default=1, min=0, max=3, group='Schedule',
                   help='1 means the evening before. 0 means the same morning.'),

        base.field('bins', 'What goes out every time', 'multiselect',
                   default=['Trash'], group='Bins',
                   help='Listed on every reminder.'),
        base.field('alternating_bins', 'What alternates', 'multiselect',
                   default=[], group='Bins',
                   help='Listed only on weeks that match the anchor date below '
                        '-- for fortnightly recycling.'),
        base.field('alternating_anchor', 'A date one of those went out', 'text',
                   default='', group='Bins',
                   help='YYYY-MM-DD. Counted in whole weeks from here, so the '
                        'schedule cannot drift at new year the way week '
                        'numbers do.'),

        base.field('calendar_url', 'Calendar URL', 'text', default='',
                   group='Calendar', depends_on='source',
                   help='An ICS feed. Events whose title matches the keyword '
                        'below count as a collection.'),
        base.field('calendar_keyword', 'Match events containing', 'text',
                   default='', group='Calendar', depends_on='source',
                   help='Leave blank to treat every event in that calendar as '
                        'a collection.'),
    )

    PLACEHOLDERS = {
        'when': 'Tomorrow, Tuesday 28 July',
        'bins': 'Trash\nRecycling',
        'printed': '6:00 PM 7/27/26',
    }

    # --- schedule ------------------------------------------------------------

    def next_collection(self, config, today):
        """The next collection date at or after `today`, or None."""
        if config.get('source') == 'calendar':
            return self._next_from_calendar(config, today)

        try:
            target = WEEKDAYS.index(config.get('collection_day') or 'Tuesday')
        except ValueError:
            target = 1
        ahead = (target - today.weekday()) % 7
        return today + timedelta(days=ahead)

    def _next_from_calendar(self, config, today):
        from . import calendar as calendar_listener
        url = (config.get('calendar_url') or '').strip()
        if not url:
            return None
        keyword = (config.get('calendar_keyword') or '').strip().lower()
        try:
            events = calendar_listener.parse_ics(
                calendar_listener.fetch_calendar(url))
        except Exception as e:
            log.warning(f'Bin day calendar failed: {e}')
            return None

        tzinfo = datetime.now().astimezone().tzinfo
        for offset in range(0, 21):
            day = today + timedelta(days=offset)
            for event in events:
                if event.get('status') == 'CANCELLED':
                    continue
                if keyword and keyword not in (event.get('summary') or '').lower():
                    continue
                if calendar_listener.occurrences_on(event, day, tzinfo):
                    return day
        return None

    def bins_for(self, config, collection_day):
        """Which bins go out on that date."""
        bins = list(config.get('bins') or [])
        alternating = config.get('alternating_bins') or []
        if not alternating:
            return bins

        anchor_raw = (config.get('alternating_anchor') or '').strip()
        if not anchor_raw:
            # Without an anchor there is no way to know which week it is.
            # Listing them every time is wrong half the time; leaving them off
            # is wrong half the time and obviously wrong, which is better.
            log.warning('Bin day: alternating bins configured with no anchor date')
            return bins
        try:
            anchor = date.fromisoformat(anchor_raw)
        except ValueError:
            log.warning(f'Bin day: unparseable anchor date {anchor_raw!r}')
            return bins

        if weeks_between(anchor, collection_day) % 2 == 0:
            bins.extend(alternating)
        return bins

    # --- polling -------------------------------------------------------------

    def poll(self, config, since):
        now = datetime.now()
        today = now.date()
        runtime = self.state()

        collection = self.next_collection(config, today)
        if collection is None:
            return []

        days_before = max(int(config.get('remind_days_before', 1) or 0), 0)
        remind_on = collection - timedelta(days=days_before)
        if today != remind_on:
            return []

        if runtime.get('last_reminder') == collection.isoformat():
            return []
        if not self._time_reached(config, now):
            return []

        bins = self.bins_for(config, collection)
        if not bins:
            log.info('Bin day: nothing configured to put out')
            runtime['last_reminder'] = collection.isoformat()
            return []

        runtime['last_reminder'] = collection.isoformat()
        return [{
            'id': f'bins-{collection.isoformat()}',
            'collection': collection,
            'days_before': days_before,
            'bins': bins,
        }]

    def _time_reached(self, config, now):
        raw = str(config.get('remind_at') or '18:00')
        try:
            hour, minute = (int(x) for x in raw.split(':'))
        except (ValueError, AttributeError):
            hour, minute = 18, 0
        return (now.hour, now.minute) >= (hour, minute)

    # --- receipt -------------------------------------------------------------

    def dedup_key(self, item):
        return item.get('id')

    def describe(self, item):
        return f"Bin day {item['collection'].isoformat()}"

    def context(self, item):
        collection = item['collection']
        if item.get('days_before') == 0:
            when = f"Today, {collection.strftime('%A %-d %B')}"
        elif item.get('days_before') == 1:
            when = f"Tomorrow, {collection.strftime('%A %-d %B')}"
        else:
            when = collection.strftime('%A %-d %B')
        return {
            'when': when,
            'bins': '\n'.join(item['bins']),
            'printed': layouts._stamp(),
        }

    def blocks_from_context(self, context):
        return [
            receipt.text('BIN DAY', font='a', width=2, height=2, bold=True),
            receipt.gap(6),
            receipt.text(context['when'], font='b', bold=True),
            receipt.rule(),
            receipt.text(context['bins'], font='a', width=1, height=2, align='left'),
            receipt.rule(),
            receipt.text(f"Printed {context['printed']}", font='b'),
        ]

    def receipt_blocks(self, item):
        return self.blocks_from_context(self.context(item))

    def template_presets(self):
        markers = {key: '{%s}' % key for key in self.PLACEHOLDERS}
        return [(f'{self.name}-default', self.blocks_from_context(markers))]

    def history_record(self, item):
        return {
            'type': 'binday',
            'id': item.get('id'),
            'category': 'Bin day',
            'title': f"Bin day {item['collection'].strftime('%a %d %b')}",
            'description': ', '.join(item['bins']),
            'print_time': datetime.now().isoformat(),
        }

    def summary(self):
        config = self.config()
        if config.get('source') == 'calendar':
            url = config.get('calendar_url')
            return f'From a calendar at {config.get("remind_at", "18:00")}' if url \
                else 'No calendar URL yet -- nothing will print.'
        bins = config.get('bins') or []
        if not bins:
            return 'No bins configured -- nothing will print.'
        return (f"{config.get('collection_day', 'Tuesday')}s, "
                f"reminder at {config.get('remind_at', '18:00')}")


listener = base.register(BinDayListener())
