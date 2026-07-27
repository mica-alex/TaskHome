"""Chore chart printing (MASTER_PLAN P5-2 #12).

One receipt per person, each morning they are scheduled. The storage, streak
logic and done-link live in `taskhome/chores.py`; this is only the part that
decides when to print.

Separate receipts per person, deliberately. A shared sheet has to be passed
around and argued over; a receipt with one child's name at the top is theirs.
"""
from datetime import date, datetime

from . import base
from .. import chores, layouts, receipt


class ChoreListener(base.Listener):
    name = 'chores'
    title = 'Chore charts'
    description = ('A morning checklist per person, with a QR code to mark the '
                   'day done and a streak counter.')
    default_interval = 15
    max_prints_per_poll = chores.MAX_PEOPLE

    CONFIG_SCHEMA = (
        base.field('enabled', 'Enabled', 'bool', default=False),
        base.field('print_at', 'Print at', 'time', default='07:00', group='When',
                   help='Once a day, per person, on the days they are scheduled.'),
        base.field('interval', 'Check every (minutes)', 'int', default=15,
                   min=5, max=120, group='When'),
        base.field('skip_if_done', 'Skip if already marked done', 'bool',
                   default=True, group='When',
                   help='Someone who marked the day done before the print time '
                        'does not need the reminder.'),
    )

    PLACEHOLDERS = {
        'name': 'Sara',
        'date': 'Monday 27 July',
        'streak': '4 day streak',
        'chores': '[ ] Feed the cat\n[ ] Tidy your room',
        'qr_url': 'http://taskhome.local:5000/c/abc123',
        'printed': '7:00 AM 7/27/26',
    }

    def poll(self, config, since):
        now = datetime.now()
        today = date.today()
        runtime = self.state()

        if not self._due_now(config, now):
            return []

        printed_today = set(runtime.get('printed') or [])
        items = []
        for person in chores.load_people():
            if not chores.is_scheduled(person, today):
                continue
            key = f"{person['id']}:{today.isoformat()}"
            if key in printed_today:
                continue
            if config.get('skip_if_done', True) and chores.done_today(person, today):
                # Already done before the print time; the reminder is pointless.
                printed_today.add(key)
                continue
            items.append({'id': key, 'person': person, 'day': today})
            printed_today.add(key)

        # Bounded: enough to cover any plausible number of people over a month.
        runtime['printed'] = sorted(printed_today)[-400:]
        return items

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
        return f"Chore chart for {item['person'].get('name', '')}"

    def context(self, item):
        person, day = item['person'], item['day']
        current = chores.streak(person, day)
        best = chores.best_streak(person)
        streak_line = ''
        if current:
            streak_line = f'{current} day streak'
            if best > current:
                streak_line += f'   (best {best})'
        return {
            'name': person.get('name', ''),
            'date': day.strftime('%A %-d %B'),
            'streak': streak_line,
            'chores': '\n'.join(f'[ ] {c}' for c in person.get('chores') or []),
            'qr_url': chores.done_url(person),
            'printed': layouts._stamp(),
        }

    def receipt_blocks(self, item):
        """Built by chores.chart_blocks so the printed chart and the one the
        management page previews cannot drift apart."""
        return chores.chart_blocks(item['person'], item['day'])

    def blocks_from_context(self, context):
        return [
            receipt.text(context['name'], font='a', width=2, height=2, bold=True),
            receipt.text(context['date'], font='b'),
            receipt.gap(6),
            receipt.text(context['streak'], font='a', width=1, height=2, bold=True),
            receipt.rule(),
            receipt.text(context['chores'], font='a', width=1, height=2, align='left'),
            receipt.rule(),
            receipt.text('Scan when everything is done', font='b'),
            receipt.qr(context['qr_url'], size=5),
            receipt.text(f"Printed {context['printed']}", font='b'),
        ]

    def template_presets(self):
        markers = {key: '{%s}' % key for key in self.PLACEHOLDERS}
        return [(f'{self.name}-default', self.blocks_from_context(markers))]

    def history_record(self, item):
        return {
            'type': 'chores',
            'id': item.get('id'),
            'category': 'Chore chart',
            'title': item['person'].get('name', ''),
            'description': ', '.join(item['person'].get('chores') or [])[:500],
            'print_time': datetime.now().isoformat(),
        }

    def summary(self):
        people = chores.load_people()
        if not people:
            return 'Nobody on the chart yet -- nothing will print.'
        done = sum(1 for p in people if chores.done_today(p))
        return (f"{len(people)} person(s), {done} done today, at "
                f"{self.config().get('print_at', '07:00')}")


listener = base.register(ChoreListener())
