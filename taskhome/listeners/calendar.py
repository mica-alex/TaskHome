"""Calendar agenda from ICS feeds (MASTER_PLAN P5-2 #4).

One receipt with today's events, from any calendar that publishes an ICS URL --
Google, iCloud and Outlook all do, as do most municipal "bin day" calendars.

Parsed here rather than with `icalendar`, but recurrence is expanded with
`dateutil.rrule`, which is already a dependency. That split is deliberate:
unfolding lines and splitting properties is thirty lines of obvious code, while
RRULE is a genuinely hard specification -- BYSETPOS, BYDAY with ordinals,
interval arithmetic across DST -- and hand-rolling it would be a slow-burning
source of "why did my Tuesday meeting print on Wednesday".

The agenda is a **digest**, like the news feed: one receipt listing today, not
one receipt per event. A day with six meetings should cost one piece of paper.
"""
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from dateutil import rrule as dateutil_rrule

from . import base
from .. import layouts, receipt
from ..logsetup import log

USER_AGENT = 'TaskHome/2.0 (+https://github.com/mica-alex/TaskHome)'
MAX_BYTES = 8 * 1024 * 1024
TIMEOUT = 25

#: How far back to expand recurrences. A rule starting years ago still has to
#: be walked forward, but there is no need to materialise the whole history.
EXPANSION_WINDOW_DAYS = 40


def unfold(text):
    """ICS folds long lines with a leading space or tab on the continuation.

    Unfolding before parsing is not optional: a SUMMARY over 75 octets is
    split mid-word, and parsing line-by-line silently truncates it.
    """
    return re.sub(r'\r?\n[ \t]', '', text)


def unescape(value):
    """ICS escapes commas, semicolons and newlines inside TEXT values."""
    return (value.replace('\\n', '\n').replace('\\N', '\n')
                 .replace('\\,', ',').replace('\\;', ';')
                 .replace('\\\\', '\\'))


def parse_line(line):
    """'DTSTART;TZID=America/New_York:20260727T090000' -> (name, params, value)."""
    if ':' not in line:
        return None, {}, ''
    head, value = line.split(':', 1)
    parts = head.split(';')
    name = parts[0].upper()
    params = {}
    for part in parts[1:]:
        if '=' in part:
            key, val = part.split('=', 1)
            params[key.upper()] = val.strip('"')
    return name, params, value


def parse_dt(value, params, default_tz):
    """An ICS date or date-time -> (datetime, is_all_day).

    All-day events carry VALUE=DATE and no time. They are kept as midnight
    local rather than converted from UTC, because an all-day event is a
    calendar square, not an instant -- treating it as one shifts it a day for
    anyone west of Greenwich.
    """
    value = value.strip()
    if params.get('VALUE') == 'DATE' or re.fullmatch(r'\d{8}', value):
        parsed = datetime.strptime(value[:8], '%Y%m%d')
        return parsed.replace(tzinfo=default_tz), True

    naive = value.endswith('Z')
    stamp = value[:-1] if naive else value
    try:
        parsed = datetime.strptime(stamp, '%Y%m%dT%H%M%S')
    except ValueError:
        try:
            parsed = datetime.strptime(stamp, '%Y%m%dT%H%M')
        except ValueError:
            raise ValueError(f'Unparseable date-time {value!r}')

    if naive:
        return parsed.replace(tzinfo=timezone.utc), False
    tzid = params.get('TZID')
    if tzid:
        try:
            return parsed.replace(tzinfo=ZoneInfo(tzid)), False
        except (ZoneInfoNotFoundError, ValueError):
            log.warning(f'Unknown TZID {tzid!r}; treating as local')
    return parsed.replace(tzinfo=default_tz), False


def parse_ics(text, default_tz=None):
    """ICS text -> [event, ...] with recurrence rules left unexpanded."""
    default_tz = default_tz or datetime.now().astimezone().tzinfo
    events, current = [], None

    for line in unfold(text).splitlines():
        line = line.strip()
        if line == 'BEGIN:VEVENT':
            current = {'exdates': []}
            continue
        if line == 'END:VEVENT':
            if current is not None and current.get('start'):
                events.append(current)
            current = None
            continue
        if current is None:
            continue

        name, params, value = parse_line(line)
        if name == 'SUMMARY':
            current['summary'] = unescape(value).strip()
        elif name == 'LOCATION':
            current['location'] = unescape(value).strip()
        elif name == 'UID':
            current['uid'] = value.strip()
        elif name == 'STATUS':
            current['status'] = value.strip().upper()
        elif name == 'RRULE':
            current['rrule'] = value.strip()
        elif name == 'DTSTART':
            try:
                current['start'], current['all_day'] = parse_dt(value, params, default_tz)
            except ValueError as e:
                log.warning(f'Skipping event with bad DTSTART: {e}')
        elif name == 'DTEND':
            try:
                current['end'], _ = parse_dt(value, params, default_tz)
            except ValueError:
                pass
        elif name == 'EXDATE':
            for piece in value.split(','):
                try:
                    excluded, _ = parse_dt(piece, params, default_tz)
                    current['exdates'].append(excluded)
                except ValueError:
                    continue

    return events


def occurrences_on(event, day, tzinfo):
    """Every start time this event has on `day`, expanding RRULE if present."""
    start = event['start']
    if start.tzinfo is None:
        start = start.replace(tzinfo=tzinfo)

    window_start = datetime.combine(day, time.min, tzinfo=tzinfo)
    window_end = window_start + timedelta(days=1)

    if not event.get('rrule'):
        return [start] if window_start <= start < window_end else []

    try:
        # dateutil needs a naive dtstart to match a naive UNTIL, and mixing the
        # two raises. Expanding naively in the calendar's own zone and
        # re-attaching it afterwards keeps DST handling correct.
        rule = dateutil_rrule.rrulestr(
            event['rrule'], dtstart=start.replace(tzinfo=None))
    except Exception as e:
        log.warning(f"Unparseable RRULE {event.get('rrule')!r}: {e}")
        return []

    excluded = {d.replace(tzinfo=None) for d in event.get('exdates', [])}
    found = []
    for moment in rule.between(window_start.replace(tzinfo=None) - timedelta(seconds=1),
                               window_end.replace(tzinfo=None), inc=True):
        if moment in excluded:
            continue
        found.append(moment.replace(tzinfo=tzinfo))
    return found


def fetch_calendar(url):
    """Download one ICS feed, capped."""
    headers = {'User-Agent': USER_AGENT, 'Accept': 'text/calendar, text/plain'}
    # webcal:// is what Apple hands you when you "copy calendar link".
    if url.startswith('webcal://'):
        url = 'https://' + url[len('webcal://'):]

    response = requests.get(url, headers=headers, timeout=TIMEOUT, stream=True)
    response.raise_for_status()

    chunks, total = [], 0
    for chunk in response.iter_content(8192):
        total += len(chunk)
        if total > MAX_BYTES:
            raise ValueError(f'Calendar exceeds {MAX_BYTES} bytes')
        chunks.append(chunk)
    return b''.join(chunks).decode('utf-8', errors='replace')


class CalendarListener(base.Listener):
    name = 'calendar'
    title = 'Calendar agenda'
    description = ("Today's events from any calendar that publishes an ICS "
                   'link -- Google, iCloud, Outlook.')
    default_interval = 30
    max_prints_per_poll = 1          # an agenda is one receipt

    CONFIG_SCHEMA = (
        base.field('enabled', 'Enabled', 'bool', default=False),
        base.field('urls', 'Calendar URLs', 'multiselect', default=[], group='Calendars',
                   help='ICS links. In Google Calendar: Settings for my '
                        'calendars, Secret address in iCal format. webcal:// works too.'),
        base.field('interval', 'Check every (minutes)', 'int', default=30,
                   min=5, max=1440, group='Calendars'),
        base.field('print_at', 'Print the agenda at', 'time', default='07:00',
                   group='Agenda',
                   help='Once a day, at this time. An agenda that arrives at '
                        'random is not an agenda.'),
        base.field('include_all_day', 'Include all-day events', 'bool', default=True,
                   group='Agenda'),
        base.field('include_location', 'Include locations', 'bool', default=True,
                   group='Agenda'),
        base.field('skip_empty', 'Skip days with no events', 'bool', default=True,
                   group='Agenda',
                   help='Off prints "Nothing scheduled", which some people '
                        'find reassuring and others find a waste of paper.'),
        base.field('max_events', 'Maximum events listed', 'int', default=20,
                   min=1, max=60, group='Agenda'),
    )

    PLACEHOLDERS = {
        'date': 'Monday 27 July',
        'count': '3',
        'events': '09:00  Standup\n12:30  Lunch with Sam\n   Cafe Nero\nAll day  Bin day',
        'printed': '7:00 AM 7/27/26',
    }

    # --- polling -------------------------------------------------------------

    def poll(self, config, since):
        """One agenda for today, at most once per day.

        The interval gate makes this run every half hour; the print_at check is
        what makes it a morning agenda rather than 48 identical receipts.
        """
        urls = [u for u in (config.get('urls') or []) if u.strip()]
        if not urls:
            return []

        now = datetime.now()
        today = now.date()
        runtime = self.state()

        if runtime.get('last_agenda') == today.isoformat():
            return []
        if not self._due_now(config, now):
            return []

        tzinfo = now.astimezone().tzinfo
        events, failures = [], []
        for url in urls:
            try:
                parsed = parse_ics(fetch_calendar(url), tzinfo)
            except Exception as e:
                log.warning(f'Calendar failed ({url}): {e}')
                failures.append(url)
                continue
            for event in parsed:
                if event.get('status') == 'CANCELLED':
                    continue
                for start in occurrences_on(event, today, tzinfo):
                    if event.get('all_day') and not config.get('include_all_day', True):
                        continue
                    events.append({
                        'summary': event.get('summary') or '(no title)',
                        'location': event.get('location', ''),
                        'start': start,
                        'all_day': event.get('all_day', False),
                    })

        runtime['last_failures'] = failures
        if not events and config.get('skip_empty', True):
            # Still mark the day done, or it retries every interval until
            # midnight looking for events that are not there.
            runtime['last_agenda'] = today.isoformat()
            return []

        events.sort(key=lambda e: (not e['all_day'], e['start']))
        limit = max(int(config.get('max_events', 20) or 20), 1)
        runtime['last_agenda'] = today.isoformat()

        return [{
            'id': f'agenda-{today.isoformat()}',
            'day': today,
            'events': events[:limit],
            'truncated': max(0, len(events) - limit),
        }]

    def _due_now(self, config, now):
        """True once the configured time has passed today."""
        raw = str(config.get('print_at') or '07:00')
        try:
            hour, minute = (int(x) for x in raw.split(':'))
        except (ValueError, AttributeError):
            hour, minute = 7, 0
        return (now.hour, now.minute) >= (hour, minute)

    # --- receipt -------------------------------------------------------------

    def dedup_key(self, item):
        return item.get('id')

    def describe(self, item):
        return f"Agenda for {item['day'].isoformat()} ({len(item['events'])} events)"

    def context(self, item):
        config = self.config()
        lines = []
        for event in item['events']:
            when = 'All day' if event['all_day'] else event['start'].strftime('%H:%M')
            lines.append(f"{when}  {event['summary']}")
            if event.get('location') and config.get('include_location', True):
                # A `-` prefix, not indentation: wrap() strips leading spaces.
                lines.append(f"   - {event['location']}")
        if item.get('truncated'):
            lines.append(f"... and {item['truncated']} more")
        return {
            'date': item['day'].strftime('%A %-d %B'),
            'count': str(len(item['events'])),
            'events': '\n'.join(lines) or 'Nothing scheduled.',
            'printed': layouts._stamp(),
        }

    def blocks_from_context(self, context):
        return [
            receipt.text(context['date'], font='a', width=2, height=2, bold=True),
            receipt.gap(6),
            receipt.text(f"{context['count']} event(s)", font='b'),
            receipt.rule(),
            receipt.text(context['events'], font='b', align='left'),
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
            'type': 'calendar',
            'id': item.get('id'),
            'category': 'Agenda',
            'title': f"Agenda for {item['day'].strftime('%a %d %b')}",
            'description': '; '.join(e['summary'] for e in item['events'])[:500],
            'print_time': datetime.now().isoformat(),
        }

    def summary(self):
        config = self.config()
        urls = config.get('urls') or []
        if not urls:
            return 'No calendars yet -- nothing will print.'
        failures = self.state().get('last_failures') or []
        note = f' ({len(failures)} failing)' if failures else ''
        return f"{len(urls)} calendar(s) at {config.get('print_at', '07:00')}{note}"


listener = base.register(CalendarListener())
