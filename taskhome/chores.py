"""Chore charts (MASTER_PLAN P5-2 #12).

A per-person checklist printed in the morning, with a QR code that marks the
day done from a phone. Doing so feeds a streak counter that appears on the next
morning's receipt, which is the entire behavioural trick: the paper is the
prompt, and the streak is the reason to keep it going.

Three decisions worth stating.

**The done-link needs no login.** It is a per-person token in a URL, scanned
from a QR on paper, on a home LAN. Demanding a password from a nine-year-old
holding a receipt would mean the feature is never used. The token is per person
so it can be rotated, and marking done is idempotent and non-destructive -- the
worst a stranger on your LAN can do is tick off a chore.

**A streak counts days the chart was completed, not chores.** Partial credit
turns the number into an average, which is not motivating and is harder to
explain than "you did it every day this week".

**A missed day breaks the streak, but only once the day is over.** Checking at
noon whether yesterday was completed is fair; checking whether *today* is
completed is not, because the day is still in progress.
"""
import os
import secrets
import uuid
from datetime import date, datetime, timedelta

from . import constants, layouts, receipt, storage

CHORES_FILENAME = 'chores.json'
MAX_PEOPLE = 12
MAX_CHORES = 30
WEEKDAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')


def chores_path():
    return os.path.join(constants.DATA_DIR, CHORES_FILENAME)


def load_people():
    value, ok = storage._load_json_file('chores', chores_path(), [])
    return value if ok and isinstance(value, list) else []


def save_people(people):
    return storage._save_json_file('chores', chores_path(), people)


def new_token():
    return secrets.token_urlsafe(16)


def get_person(person_id):
    return next((p for p in load_people() if p.get('id') == person_id), None)


def by_token(token):
    if not token:
        return None
    for person in load_people():
        # Constant-time, because this is the one value that authorises anything.
        if person.get('token') and secrets.compare_digest(person['token'], token):
            return person
    return None


def add_person(name):
    name = (name or '').strip()[:40]
    if not name:
        raise ValueError('Give them a name.')
    people = load_people()
    if len(people) >= MAX_PEOPLE:
        raise ValueError(f'That is the maximum of {MAX_PEOPLE} people.')
    person = {'id': str(uuid.uuid4()), 'name': name, 'token': new_token(),
              'chores': [], 'days': list(range(7)), 'completed': [],
              'created': datetime.now().isoformat()}
    people.append(person)
    save_people(people)
    return person


def update_person(person_id, name=None, days=None, chores=None, rotate=False):
    people = load_people()
    for person in people:
        if person.get('id') != person_id:
            continue
        if name is not None:
            cleaned = str(name).strip()[:40]
            if not cleaned:
                raise ValueError('Give them a name.')
            person['name'] = cleaned
        if days is not None:
            person['days'] = sorted({int(d) for d in days if 0 <= int(d) <= 6})
        if chores is not None:
            lines = [c.strip()[:80] for c in chores if str(c).strip()]
            person['chores'] = lines[:MAX_CHORES]
        if rotate:
            person['token'] = new_token()
        save_people(people)
        return person
    raise ValueError('No such person.')


def remove_person(person_id):
    people = load_people()
    remaining = [p for p in people if p.get('id') != person_id]
    if len(remaining) == len(people):
        raise ValueError('No such person.')
    save_people(remaining)
    return True


# --- streaks ------------------------------------------------------------------

def scheduled_days(person):
    return set(person.get('days') or range(7))


def is_scheduled(person, day):
    return day.weekday() in scheduled_days(person)


def mark_done(person_id, day=None):
    """Record a completed day. Idempotent."""
    day = day or date.today()
    people = load_people()
    for person in people:
        if person.get('id') != person_id:
            continue
        completed = set(person.get('completed') or [])
        completed.add(day.isoformat())
        # Bounded: a year is plenty to compute any streak worth showing.
        person['completed'] = sorted(completed)[-400:]
        save_people(people)
        return person
    raise ValueError('No such person.')


def undo_done(person_id, day=None):
    day = day or date.today()
    people = load_people()
    for person in people:
        if person.get('id') != person_id:
            continue
        person['completed'] = [d for d in (person.get('completed') or [])
                               if d != day.isoformat()]
        save_people(people)
        return person
    raise ValueError('No such person.')


def streak(person, today=None):
    """Consecutive *scheduled* days completed, ending today or yesterday.

    Counting only scheduled days is what makes a weekday-only chart survive the
    weekend: Saturday is not a missed day if nothing was due.

    Today counts if already done, but not having done it yet does not break the
    streak -- the day is still in progress. That is the difference between a
    counter that encourages and one that punishes you at breakfast.
    """
    today = today or date.today()
    completed = set(person.get('completed') or [])
    if not completed:
        return 0

    day = today
    if not (is_scheduled(person, day) and day.isoformat() in completed):
        day = today - timedelta(days=1)

    count = 0
    # A year back is the bound; nothing sensible needs more.
    for _ in range(400):
        if not is_scheduled(person, day):
            day -= timedelta(days=1)
            continue
        if day.isoformat() in completed:
            count += 1
            day -= timedelta(days=1)
            continue
        break
    return count


def best_streak(person):
    """The longest run ever, so a broken streak still shows something earned."""
    completed = sorted(person.get('completed') or [])
    if not completed:
        return 0
    best = run = 0
    previous = None
    for entry in completed:
        try:
            day = date.fromisoformat(entry)
        except ValueError:
            continue
        if previous is None:
            run = 1
        else:
            expected = previous + timedelta(days=1)
            while expected < day and not is_scheduled(person, expected):
                expected += timedelta(days=1)
            run = run + 1 if expected == day else 1
        previous = day
        best = max(best, run)
    return best


def done_today(person, today=None):
    today = today or date.today()
    return today.isoformat() in set(person.get('completed') or [])


# --- printing -----------------------------------------------------------------

def done_url(person):
    """Where the QR points. Resolved at print time so a changed hostname or
    port is picked up without reprinting anything."""
    from . import settings, state
    host = state.config.get('hostname', constants.DEFAULT_CONFIG['hostname'])
    return f"http://{host}:{settings.get_port()}/c/{person['token']}"


def chart_blocks(person, day=None):
    day = day or date.today()
    current = streak(person, day)
    best = best_streak(person)

    blocks = [
        receipt.text(person.get('name', ''), font='a', width=2, height=2, bold=True),
        receipt.text(day.strftime('%A %-d %B'), font='b'),
    ]

    if current:
        line = f'{current} day streak'
        if best > current:
            line += f'   (best {best})'
        blocks.append(receipt.gap(6))
        blocks.append(receipt.text(line, font='a', width=1, height=2, bold=True))

    blocks.append(receipt.rule())
    for chore in person.get('chores') or []:
        blocks.append(receipt.text(f'[ ] {chore}', font='a', width=1, height=2,
                                   align='left'))
    if not person.get('chores'):
        blocks.append(receipt.text('Nothing on the chart yet.', font='b'))

    blocks.append(receipt.rule())
    blocks.append(receipt.text('Scan when everything is done', font='b'))
    blocks.append(receipt.qr(done_url(person), size=5))
    blocks.append(receipt.text(f'Printed {layouts._stamp()}', font='b'))
    return blocks


def stats():
    people = load_people()
    return {
        'people': len(people),
        'done_today': sum(1 for p in people if done_today(p)),
        'best_streak': max((streak(p) for p in people), default=0),
    }
