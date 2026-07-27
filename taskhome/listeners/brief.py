"""Morning brief (MASTER_PLAN P5-2 #3).

One composite receipt at a set time: the date, today's weather, today's tasks,
today's calendar, and the latest headlines. The plan calls this the daily-driver
feature, and it is the only listener that **composes other listeners** rather
than fetching anything of its own.

That composition is the whole design. Each section asks an existing listener
for its data, which means:

* configuration is not duplicated -- the brief uses the ZIP codes, calendar
  URLs and feeds already set up elsewhere;
* a section whose listener is switched off is simply absent, with no separate
  enable/disable to keep in step;
* a section that fails leaves the rest of the brief intact. A brief that
  refuses to print because a news feed 502'd would be worse than one missing
  its headlines.

The brief does **not** require those listeners to be enabled for printing.
Someone may want weather in their brief without a receipt for every advisory,
so each section has its own toggle and reads the other listener's config
directly.
"""
from datetime import datetime, timedelta

from . import base
from .. import layouts, receipt, recurrence, state
from ..logsetup import log


class BriefListener(base.Listener):
    name = 'brief'
    title = 'Morning brief'
    description = ('One receipt each morning: weather, your tasks, your '
                   'calendar and the headlines.')
    default_interval = 15
    max_prints_per_poll = 1

    CONFIG_SCHEMA = (
        base.field('enabled', 'Enabled', 'bool', default=False),
        base.field('print_at', 'Print at', 'time', default='07:00', group='When',
                   help='Once a day. Checked every 15 minutes, so it prints at '
                        'the first check after this time.'),
        base.field('interval', 'Check every (minutes)', 'int', default=15,
                   min=5, max=120, group='When'),
        base.field('sections', 'Include', 'multiselect',
                   default=['weather', 'tasks', 'calendar', 'news'],
                   options=['weather', 'tasks', 'calendar', 'news'],
                   group='Contents',
                   help='Each section uses the settings of the listener it '
                        'comes from. A listener with nothing configured is '
                        'left out rather than printing an empty heading.'),
        base.field('news_items', 'Headlines to include', 'int', default=5,
                   min=1, max=15, group='Contents'),
        base.field('task_window_hours', 'Task window (hours)', 'int', default=24,
                   min=1, max=168, group='Contents',
                   help='How far ahead to list scheduled tasks.'),
    )

    PLACEHOLDERS = {
        'date': 'Monday 27 July',
        'weather': 'Wind Advisory until 6:00 PM',
        'tasks': '09:00  Play with Sara\n21:00  Brush teeth',
        'calendar': '12:30  Lunch with Sam',
        'news': '1. Something happened today\n   - BBC News',
        'printed': '7:00 AM 7/27/26',
    }

    # --- sections -------------------------------------------------------------
    #
    # Each returns a list of lines, or [] when there is nothing to say. Each is
    # wrapped by the caller so one failing source cannot take the brief down.

    def section_weather(self, config, now):
        from . import nws
        weather_config = nws.listener.config()
        if not weather_config.get('zips'):
            return []
        try:
            alerts = nws.listener.poll(weather_config, None)
        except Exception as e:
            log.warning(f'Brief: weather section failed: {e}')
            return ['(weather unavailable)']
        if not alerts:
            return ['No active alerts.']
        return [f"{a.get('event', 'Alert')} - {a.get('severity', '')}"
                for a in alerts[:4]]

    def section_tasks(self, config, now):
        window = timedelta(hours=max(int(config.get('task_window_hours', 24) or 24), 1))
        cutoff = now + window
        upcoming = []
        for task in state.tasks:
            if not task.get('enabled', True):
                continue
            try:
                when = recurrence.parse_task_time(task['next_time'])
            except Exception:
                continue
            if now - timedelta(hours=1) <= when <= cutoff:
                upcoming.append((when, task.get('title', '')))
        upcoming.sort()
        # A window longer than today needs the day, or tomorrow's 09:00 reads
        # as earlier than tonight's 21:00.
        today = now.date()
        return [
            f"{when.strftime('%H:%M') if when.date() == today else when.strftime('%a %H:%M')}"
            f"  {title}"
            for when, title in upcoming
        ]

    def section_calendar(self, config, now):
        from . import calendar as calendar_listener
        calendar_config = calendar_listener.listener.config()
        urls = [u for u in (calendar_config.get('urls') or []) if u.strip()]
        if not urls:
            return []

        tzinfo = now.astimezone().tzinfo
        today = now.date()
        lines = []
        for url in urls:
            try:
                events = calendar_listener.parse_ics(
                    calendar_listener.fetch_calendar(url), tzinfo)
            except Exception as e:
                log.warning(f'Brief: calendar {url} failed: {e}')
                continue
            for event in events:
                if event.get('status') == 'CANCELLED':
                    continue
                for start in calendar_listener.occurrences_on(event, today, tzinfo):
                    when = 'All day' if event.get('all_day') else start.strftime('%H:%M')
                    lines.append((event.get('all_day', False), start,
                                  f"{when}  {event.get('summary') or '(no title)'}"))
        lines.sort(key=lambda row: (not row[0], row[1]))
        return [row[2] for row in lines]

    def section_news(self, config, now):
        from . import feeds
        feed_config = feeds.listener.config()
        urls = [u for u in (feed_config.get('urls') or []) if u.strip()]
        if not urls:
            return []

        limit = max(int(config.get('news_items', 5) or 5), 1)
        lines = []
        for url in urls:
            if len(lines) >= limit * 2:
                break
            try:
                entries, feed_title, _ = feeds.fetch_feed(url)
            except Exception as e:
                log.warning(f'Brief: feed {url} failed: {e}')
                continue
            for entry in entries[:2]:
                lines.append(f"{len(lines) // 2 + 1}. {entry['title']}")
                source = feed_title or feeds._domain(url)
                if source:
                    lines.append(f'   - {source}')
        return lines[:limit * 2]

    SECTIONS = {
        'weather': ('Weather', 'section_weather'),
        'tasks': ('Today', 'section_tasks'),
        'calendar': ('Calendar', 'section_calendar'),
        'news': ('Headlines', 'section_news'),
    }

    # --- polling -------------------------------------------------------------

    def poll(self, config, since):
        now = datetime.now()
        today = now.date()
        runtime = self.state()

        if runtime.get('last_brief') == today.isoformat():
            return []
        if not self._due_now(config, now):
            return []

        wanted = config.get('sections') or list(self.SECTIONS)
        sections = []
        for key in self.SECTIONS:
            if key not in wanted:
                continue
            heading, method = self.SECTIONS[key]
            try:
                lines = getattr(self, method)(config, now)
            except Exception as e:
                # One broken source must not cost the whole brief. A brief
                # missing its headlines is useful; one that never printed is
                # not.
                log.error(f'Brief: {key} section failed: {e}', exc_info=True)
                lines = ['(unavailable)']
            if lines:
                sections.append((heading, lines))

        runtime['last_brief'] = today.isoformat()
        if not sections:
            log.info('Brief: nothing to report, not printing')
            return []

        return [{'id': f'brief-{today.isoformat()}', 'day': today,
                 'sections': sections}]

    def _due_now(self, config, now):
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
        return f"Morning brief for {item['day'].isoformat()}"

    def context(self, item):
        sections = dict(item.get('sections', []))
        return {
            'date': item['day'].strftime('%A %-d %B'),
            'weather': '\n'.join(sections.get('Weather', [])),
            'tasks': '\n'.join(sections.get('Today', [])),
            'calendar': '\n'.join(sections.get('Calendar', [])),
            'news': '\n'.join(sections.get('Headlines', [])),
            'printed': layouts._stamp(),
        }

    def receipt_blocks(self, item):
        """Built from the sections that actually have content.

        Not from the template, because a template is a fixed block list and a
        brief's shape changes daily -- an empty heading with nothing under it
        is worse than no heading.
        """
        blocks = [
            receipt.text(item['day'].strftime('%A'), font='a', width=2, height=2,
                         bold=True),
            receipt.text(item['day'].strftime('%-d %B %Y'), font='b'),
        ]
        for heading, lines in item.get('sections', []):
            blocks.append(receipt.rule())
            blocks.append(receipt.text(heading.upper(), font='b', bold=True))
            blocks.append(receipt.gap(4))
            blocks.append(receipt.text('\n'.join(lines), font='b', align='left'))
        blocks.append(receipt.rule())
        blocks.append(receipt.text(f'Printed {layouts._stamp()}', font='b'))
        return blocks

    def blocks_from_context(self, context):
        """The editable template: every section, with empty ones dropped by
        fill() rather than by this code."""
        blocks = [
            receipt.text(context['date'], font='a', width=2, height=2, bold=True),
        ]
        for key, heading in (('weather', 'WEATHER'), ('tasks', 'TODAY'),
                             ('calendar', 'CALENDAR'), ('news', 'HEADLINES')):
            blocks.append(receipt.rule())
            blocks.append(receipt.text(heading, font='b', bold=True))
            blocks.append(receipt.text(context[key], font='b', align='left'))
        blocks.append(receipt.rule())
        blocks.append(receipt.text(f"Printed {context['printed']}", font='b'))
        return blocks

    def template_presets(self):
        markers = {key: '{%s}' % key for key in self.PLACEHOLDERS}
        return [(f'{self.name}-default', self.blocks_from_context(markers))]

    def history_record(self, item):
        return {
            'type': 'brief',
            'id': item.get('id'),
            'category': 'Morning brief',
            'title': f"Brief for {item['day'].strftime('%a %d %b')}",
            'description': ', '.join(h for h, _ in item.get('sections', []))[:500],
            'print_time': datetime.now().isoformat(),
        }

    def summary(self):
        config = self.config()
        sections = config.get('sections') or []
        if not sections:
            return 'No sections selected -- nothing will print.'
        return f"{', '.join(sections)} at {config.get('print_at', '07:00')}"


listener = base.register(BriefListener())
