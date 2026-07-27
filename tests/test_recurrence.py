"""calculate_next / advance_schedule — the recurrence primitives.

The hang bugs (P0-1, P0-2) both reduce to "the loop iterated without
advancing", so these tests pin that property directly rather than only
testing the two known symptoms.
"""
from datetime import datetime

import pytest

import taskhome


def dt(value):
    return datetime.fromisoformat(value)


# --- calculate_next -----------------------------------------------------------

@pytest.mark.parametrize('recurring,start,expected', [
    ('daily', '2026-03-10T09:00:00', '2026-03-11T09:00:00'),
    ('weekly', '2026-03-10T09:00:00', '2026-03-17T09:00:00'),
    ('monthly', '2026-03-10T09:00:00', '2026-04-10T09:00:00'),
    ('first_day_month', '2026-03-10T09:00:00', '2026-04-01T09:00:00'),
    # 2026-03-13 is a Friday, so the next weekday is Monday the 16th.
    ('every_weekday', '2026-03-13T09:00:00', '2026-03-16T09:00:00'),
])
def test_simple_recurrences(recurring, start, expected):
    assert taskhome.recurrence.calculate_next(start, recurring) == expected


def test_custom_selects_next_matching_weekday():
    # 2026-03-10 is a Tuesday; ask for Mondays (0) and Wednesdays (2).
    assert taskhome.recurrence.calculate_next('2026-03-10T09:00:00', 'custom', [0, 2]) == \
        '2026-03-11T09:00:00'


def test_none_returns_input_unchanged():
    """The P0-1 trigger: 'none' cannot advance, and says so by returning the
    input. Callers must not loop on this."""
    start = '2026-03-10T09:00:00'
    assert taskhome.recurrence.calculate_next(start, 'none') == start


def test_unknown_recurrence_returns_input_unchanged():
    start = '2026-03-10T09:00:00'
    assert taskhome.recurrence.calculate_next(start, 'fortnightly-ish') == start


@pytest.mark.parametrize('days', [[], None, [99], ['monday'], [7, -1]])
def test_custom_with_no_valid_days_terminates(days):
    """P0-2: this used to spin forever at 100% CPU."""
    start = '2026-03-10T09:00:00'
    assert taskhome.recurrence.calculate_next(start, 'custom', days) == start


def test_custom_ignores_invalid_days_but_uses_valid_ones():
    result = taskhome.recurrence.calculate_next('2026-03-10T09:00:00', 'custom', [99, 2])
    assert result == '2026-03-11T09:00:00'  # Wednesday


# --- advance_schedule ---------------------------------------------------------

def test_future_task_is_untouched(make_task):
    task = make_task('2026-03-10T09:00:00')
    next_time, missed = taskhome.recurrence.advance_schedule(task, dt('2026-03-09T00:00:00'))
    assert next_time == '2026-03-10T09:00:00'
    assert missed == []


def test_rolls_forward_past_now_and_reports_missed(make_task):
    task = make_task('2026-03-01T09:00:00', 'daily')
    next_time, missed = taskhome.recurrence.advance_schedule(task, dt('2026-03-05T12:00:00'))
    assert next_time == '2026-03-06T09:00:00'
    # 1st through 5th came due; the 6th is still ahead.
    assert missed == [f'2026-03-0{d}T09:00:00' for d in range(1, 6)]


def test_boundary_exactly_due_counts_as_missed(make_task):
    task = make_task('2026-03-05T09:00:00', 'daily')
    next_time, missed = taskhome.recurrence.advance_schedule(task, dt('2026-03-05T09:00:00'))
    assert missed == ['2026-03-05T09:00:00']
    assert next_time == '2026-03-06T09:00:00'


def test_overdue_oneoff_returns_no_future_occurrence(make_task):
    """P0-1: previously an infinite loop; now reports 'no next occurrence'."""
    task = make_task('2026-03-01T09:00:00', 'none')
    next_time, missed = taskhome.recurrence.advance_schedule(task, dt('2026-03-05T00:00:00'))
    assert next_time is None
    assert missed == ['2026-03-01T09:00:00']


def test_overdue_custom_with_empty_days_raises_not_hangs(make_task):
    """P0-2 at the advance_schedule level: refuses rather than spinning."""
    task = make_task('2026-03-01T09:00:00', 'custom', days=[])
    with pytest.raises(taskhome.recurrence.ScheduleError, match='did not advance'):
        taskhome.recurrence.advance_schedule(task, dt('2026-03-05T00:00:00'))


def test_unknown_recurrence_raises_not_hangs(make_task):
    task = make_task('2026-03-01T09:00:00', 'every_blue_moon')
    with pytest.raises(taskhome.recurrence.ScheduleError, match='did not advance'):
        taskhome.recurrence.advance_schedule(task, dt('2026-03-05T00:00:00'))


def test_iteration_ceiling_is_enforced(make_task, monkeypatch):
    """Final backstop: even a recurrence that *does* advance can't loop
    unboundedly."""
    task = make_task('2020-01-01T09:00:00', 'daily')
    with pytest.raises(taskhome.recurrence.ScheduleError, match='exceeded'):
        taskhome.recurrence.advance_schedule(task, dt('2026-03-05T00:00:00'), max_iterations=10)


def test_backwards_recurrence_raises(make_task, monkeypatch):
    """A recurrence that moves backwards would loop forever; it must raise."""
    monkeypatch.setattr(taskhome.recurrence, 'calculate_next',
                        lambda s, r, d=None: '2019-01-01T09:00:00')
    task = make_task('2020-01-01T09:00:00', 'daily')
    with pytest.raises(taskhome.recurrence.ScheduleError, match='backwards'):
        taskhome.recurrence.advance_schedule(task, dt('2026-03-05T00:00:00'))


def test_long_outage_terminates_quickly(make_task):
    """A year-long gap on a daily task: correctness plus no runaway."""
    task = make_task('2025-03-05T09:00:00', 'daily')
    next_time, missed = taskhome.recurrence.advance_schedule(task, dt('2026-03-05T12:00:00'))
    assert next_time == '2026-03-06T09:00:00'
    assert len(missed) == 366  # 2025-03-05 .. 2026-03-05 inclusive


# --- parse_task_time ----------------------------------------------------------

def test_parse_naive_time_roundtrips():
    assert taskhome.recurrence.parse_task_time('2026-03-10T09:00:00') == dt('2026-03-10T09:00:00')


def test_parse_strips_timezone_to_local():
    """Aware values are normalised so comparisons stay in one frame (P0-3)."""
    parsed = taskhome.recurrence.parse_task_time('2026-03-10T09:00:00+00:00')
    assert parsed.tzinfo is None


@pytest.mark.parametrize('value', ['not-a-date', '', '2026-13-45T99:00:00', 'null'])
def test_parse_rejects_garbage(value):
    with pytest.raises(ValueError):
        taskhome.recurrence.parse_task_time(value)


def test_double_seconds_suffix_is_tolerated_on_modern_python():
    """Pins a version-dependent behaviour change.

    `next_time` is built as `<datetime-local value> + ':00'`, which yields
    '...T21:00:00:00' when the browser already supplied seconds. On Python 3.9
    that raised ValueError and poisoned every scheduler tick (MASTER_PLAN
    P0-6). Python 3.11+ parses it, silently ignoring the trailing ':00'.

    So on 3.13 this specific input is no longer a denial-of-service — it is a
    silent-acceptance bug instead. The real fix is validating at ingress
    (P0-9); this test exists so the change is noticed if the floor ever moves.
    """
    assert taskhome.recurrence.parse_task_time('2026-03-10T21:00:00:00') == dt('2026-03-10T21:00:00')
