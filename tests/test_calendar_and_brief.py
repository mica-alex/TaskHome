"""Calendar agenda (P5-2 #4) and morning brief (P5-2 #3)."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from taskhome import constants, state, storage
from taskhome.listeners import base, brief
from taskhome.listeners import calendar as cal

TZ = ZoneInfo('America/New_York')

ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:1
SUMMARY:Standup with a very long title that ICS folds across two separate lin
 es because it exceeds seventy-five octets
DTSTART;TZID=America/New_York:20260727T090000
RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR
END:VEVENT
BEGIN:VEVENT
UID:2
SUMMARY:Bin day\\, green bin
DTSTART;VALUE=DATE:20260727
END:VEVENT
BEGIN:VEVENT
UID:3
SUMMARY:Cancelled thing
STATUS:CANCELLED
DTSTART;TZID=America/New_York:20260727T140000
END:VEVENT
BEGIN:VEVENT
UID:4
SUMMARY:Lunch with Sam
LOCATION:Cafe Nero\\, Elm St
DTSTART;TZID=America/New_York:20260727T123000
END:VEVENT
END:VCALENDAR"""


# --- ICS parsing --------------------------------------------------------------

def test_folded_lines_are_rejoined():
    """A SUMMARY over 75 octets is split mid-word; parsing line by line
    silently truncates it."""
    events = cal.parse_ics(ICS, TZ)
    standup = next(e for e in events if e['uid'] == '1')
    assert 'seventy-five octets' in standup['summary']
    assert 'lin es' not in standup['summary'], 'unfolded at the wrong place'


def test_escaped_characters_are_decoded():
    events = cal.parse_ics(ICS, TZ)
    assert next(e for e in events if e['uid'] == '2')['summary'] == 'Bin day, green bin'
    assert next(e for e in events if e['uid'] == '4')['location'] == 'Cafe Nero, Elm St'


def test_all_day_events_are_recognised():
    """An all-day event is a calendar square, not an instant -- treating it as
    one shifts it a day for anyone west of Greenwich."""
    events = cal.parse_ics(ICS, TZ)
    assert next(e for e in events if e['uid'] == '2')['all_day'] is True
    assert next(e for e in events if e['uid'] == '4')['all_day'] is False


def test_a_recurring_event_expands_onto_the_right_day():
    events = cal.parse_ics(ICS, TZ)
    standup = next(e for e in events if e['uid'] == '1')
    monday = cal.occurrences_on(standup, date(2026, 7, 27), TZ)
    saturday = cal.occurrences_on(standup, date(2026, 8, 1), TZ)
    assert len(monday) == 1 and monday[0].strftime('%H:%M') == '09:00'
    assert saturday == [], 'a weekday rule fired at the weekend'


def test_exdates_are_honoured():
    ics = ICS.replace('RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR',
                      'RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR\n'
                      'EXDATE;TZID=America/New_York:20260727T090000')
    standup = next(e for e in cal.parse_ics(ics, TZ) if e['uid'] == '1')
    assert cal.occurrences_on(standup, date(2026, 7, 27), TZ) == []


def test_a_non_recurring_event_only_appears_on_its_own_day():
    lunch = next(e for e in cal.parse_ics(ICS, TZ) if e['uid'] == '4')
    assert len(cal.occurrences_on(lunch, date(2026, 7, 27), TZ)) == 1
    assert cal.occurrences_on(lunch, date(2026, 7, 28), TZ) == []


def test_an_unparseable_rrule_drops_the_event_not_the_agenda():
    event = {'start': datetime(2026, 7, 27, 9, tzinfo=TZ), 'rrule': 'NONSENSE'}
    assert cal.occurrences_on(event, date(2026, 7, 27), TZ) == []


def test_utc_timestamps_are_understood():
    ics = ICS.replace('DTSTART;TZID=America/New_York:20260727T123000',
                      'DTSTART:20260727T163000Z')
    lunch = next(e for e in cal.parse_ics(ics, TZ) if e['uid'] == '4')
    assert lunch['start'].hour == 16 and lunch['start'].tzinfo is not None


def test_webcal_urls_are_rewritten(monkeypatch):
    """webcal:// is what Apple hands you when you copy a calendar link."""
    captured = {}

    class Resp:
        def raise_for_status(self): pass
        def iter_content(self, n): yield b'BEGIN:VCALENDAR\nEND:VCALENDAR'

    def fake_get(url, **kwargs):
        captured['url'] = url
        return Resp()

    monkeypatch.setattr(cal.requests, 'get', fake_get)
    cal.fetch_calendar('webcal://example.com/cal.ics')
    assert captured['url'].startswith('https://')


# --- agenda -------------------------------------------------------------------

@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(state, 'listeners', {})
    monkeypatch.setattr(state, 'tasks', [])
    monkeypatch.setattr(storage, 'save_listeners', lambda: True)
    yield


def test_an_agenda_is_one_receipt():
    assert cal.CalendarListener.max_prints_per_poll == 1


def test_the_agenda_prints_once_a_day(store, monkeypatch):
    monkeypatch.setattr(cal, 'fetch_calendar', lambda url: ICS)
    config = dict(cal.listener.config(), urls=['https://x/cal.ics'], print_at='00:00')
    assert len(cal.listener.poll(config, None)) == 1
    assert cal.listener.poll(config, None) == [], 'printed twice in one day'


def test_nothing_prints_before_the_configured_time(store, monkeypatch):
    monkeypatch.setattr(cal, 'fetch_calendar', lambda url: ICS)
    config = dict(cal.listener.config(), urls=['https://x/cal.ics'], print_at='23:59')
    assert cal.listener.poll(config, None) == []


def test_an_empty_day_marks_itself_done(store, monkeypatch):
    """Otherwise it retries every interval until midnight looking for events
    that are not there."""
    monkeypatch.setattr(cal, 'fetch_calendar',
                        lambda url: 'BEGIN:VCALENDAR\nEND:VCALENDAR')
    config = dict(cal.listener.config(), urls=['https://x/cal.ics'],
                  print_at='00:00', skip_empty=True)
    assert cal.listener.poll(config, None) == []
    assert cal.listener.state()['last_agenda']


def test_one_broken_calendar_does_not_stop_the_agenda(store, monkeypatch):
    def fetch(url):
        if 'broken' in url:
            raise RuntimeError('404')
        return ICS

    monkeypatch.setattr(cal, 'fetch_calendar', fetch)
    config = dict(cal.listener.config(),
                  urls=['https://broken/c.ics', 'https://good/c.ics'], print_at='00:00')
    items = cal.listener.poll(config, None)
    assert items, 'a dead calendar took the agenda with it'
    assert cal.listener.state()['last_failures'] == ['https://broken/c.ics']


def test_cancelled_events_are_left_out(store, monkeypatch):
    monkeypatch.setattr(cal, 'fetch_calendar', lambda url: ICS)
    monkeypatch.setattr(cal, 'occurrences_on',
                        lambda e, d, tz: [e['start']])
    config = dict(cal.listener.config(), urls=['https://x/c.ics'], print_at='00:00')
    items = cal.listener.poll(config, None)
    titles = [e['summary'] for e in items[0]['events']]
    assert 'Cancelled thing' not in titles


def test_the_location_line_survives_wrapping():
    """wrap() strips leading whitespace, so an indented location would read as
    another event."""
    item = {'day': date(2026, 7, 27), 'events': [
        {'summary': 'Lunch', 'location': 'Cafe Nero', 'all_day': False,
         'start': datetime(2026, 7, 27, 12, 30, tzinfo=TZ)}]}
    assert '- Cafe Nero' in cal.listener.context(item)['events']


# --- morning brief ------------------------------------------------------------

def test_the_brief_composes_other_listeners_configs(store):
    """It must not duplicate configuration -- the ZIPs, calendars and feeds are
    already set up elsewhere."""
    keys = {spec['key'] for spec in brief.BriefListener.CONFIG_SCHEMA}
    for owned_elsewhere in ('zips', 'urls', 'events'):
        assert owned_elsewhere not in keys, f'brief redefines {owned_elsewhere}'


def test_a_section_with_nothing_configured_is_absent(store):
    """Rather than printing an empty heading."""
    config = dict(brief.listener.config(), print_at='00:00',
                  sections=['calendar', 'news'])
    assert brief.listener.poll(config, None) == []


def test_a_failing_section_does_not_lose_the_brief(store, monkeypatch):
    """A brief missing its headlines is useful; one that never printed is not."""
    monkeypatch.setattr(brief.BriefListener, 'section_news',
                        lambda self, c, n: 1 / 0)
    state.tasks.append({'id': '1', 'title': 'Bins', 'enabled': True,
                        'next_time': datetime.now().replace(microsecond=0).isoformat()})
    config = dict(brief.listener.config(), print_at='00:00',
                  sections=['tasks', 'news'])
    items = brief.listener.poll(config, None)
    assert items, 'a broken section took the whole brief down'
    headings = [h for h, _ in items[0]['sections']]
    assert 'Today' in headings


def test_the_brief_prints_once_a_day(store):
    state.tasks.append({'id': '1', 'title': 'Bins', 'enabled': True,
                        'next_time': datetime.now().replace(microsecond=0).isoformat()})
    config = dict(brief.listener.config(), print_at='00:00', sections=['tasks'])
    assert len(brief.listener.poll(config, None)) == 1
    assert brief.listener.poll(config, None) == []


def test_tomorrows_tasks_carry_their_day(store):
    """Otherwise tomorrow's 09:00 reads as earlier than tonight's 21:00."""
    from datetime import timedelta
    now = datetime(2026, 7, 27, 14, 0)
    state.tasks.extend([
        {'id': '1', 'title': 'Tonight', 'enabled': True,
         'next_time': (now.replace(hour=21)).isoformat()},
        {'id': '2', 'title': 'Tomorrow', 'enabled': True,
         'next_time': (now + timedelta(days=1)).replace(hour=9).isoformat()},
    ])
    lines = brief.listener.section_tasks({'task_window_hours': 48}, now)
    assert lines[0].startswith('21:00')
    assert lines[1].startswith('Tue '), lines[1]


def test_disabled_tasks_are_left_out(store):
    now = datetime(2026, 7, 27, 8, 0)
    state.tasks.append({'id': '1', 'title': 'Paused', 'enabled': False,
                        'next_time': now.replace(hour=9).isoformat()})
    assert brief.listener.section_tasks({'task_window_hours': 24}, now) == []


# --- both are first-class listeners -------------------------------------------

@pytest.mark.parametrize('name', ['calendar', 'brief'])
def test_registered_and_editable(name):
    from taskhome import styles
    assert name in base.registry()
    assert name in styles.kinds()
    assert styles.builtin_templates(name)
    listener = base.get(name)
    assert listener.PLACEHOLDERS and listener.CONFIG_SCHEMA
