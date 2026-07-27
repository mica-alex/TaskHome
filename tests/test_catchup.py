"""Catch-up policy (MASTER_PLAN P1-10) and the scheduler loop's use of it.

Covers policy resolution, what each policy prints, and the invariant that
matters most: after catch-up, the steady-state loop must not immediately fire
what catch-up just decided to skip.
"""
from datetime import datetime

import pytest

import app as taskhome


def dt(value):
    return datetime.fromisoformat(value)


# --- policy resolution --------------------------------------------------------

def test_defaults(clean_state):
    cfg = taskhome.get_catchup_config()
    assert cfg['policy'] == 'skip'
    assert cfg['oneoff_policy'] == 'print_once'
    assert cfg['recent_window_hours'] == 6
    assert cfg['max_prints'] == 20


def test_recurring_uses_global_policy(clean_state, make_task):
    task = make_task('2026-03-01T09:00:00', 'daily')
    assert taskhome.resolve_catchup_policy(task) == 'skip'


def test_oneoff_uses_oneoff_policy(clean_state, make_task):
    """A skipped one-off never prints at all, so it defaults differently."""
    task = make_task('2026-03-01T09:00:00', 'none')
    assert taskhome.resolve_catchup_policy(task) == 'print_once'


def test_per_task_override_wins(clean_state, make_task):
    task = make_task('2026-03-01T09:00:00', 'daily', catchup='print_all')
    assert taskhome.resolve_catchup_policy(task) == 'print_all'


def test_inherit_falls_through_to_global(clean_state, make_task):
    task = make_task('2026-03-01T09:00:00', 'daily', catchup='inherit')
    assert taskhome.resolve_catchup_policy(task) == 'skip'


def test_invalid_per_task_value_falls_back(clean_state, make_task):
    task = make_task('2026-03-01T09:00:00', 'daily', catchup='print_everything')
    assert taskhome.resolve_catchup_policy(task) == 'skip'


@pytest.mark.parametrize('stored,expected', [
    ({'policy': 'print_all'}, 'print_all'),
    ({'policy': 'nonsense'}, 'skip'),
    ({'policy': None}, 'skip'),
    ('not-a-dict', 'skip'),
])
def test_config_validation(clean_state, stored, expected):
    taskhome.config['catchup'] = stored
    assert taskhome.get_catchup_config()['policy'] == expected


@pytest.mark.parametrize('value', ['abc', -1, None, [1]])
def test_numeric_config_validation(clean_state, value):
    taskhome.config['catchup'] = {'max_prints': value}
    assert taskhome.get_catchup_config()['max_prints'] == 20


def test_partial_config_merges_over_defaults(clean_state):
    """X-4: a config specifying one key must not blank the others."""
    taskhome.config['catchup'] = {'policy': 'print_all'}
    cfg = taskhome.get_catchup_config()
    assert cfg['policy'] == 'print_all'
    assert cfg['oneoff_policy'] == 'print_once'
    assert cfg['max_prints'] == 20


# --- what each policy prints --------------------------------------------------

def missed_range(n):
    return [f'2026-03-{d:02d}T09:00:00' for d in range(1, n + 1)]


def test_skip_prints_nothing(clean_state):
    cfg = taskhome.get_catchup_config()
    chosen, dropped = taskhome.select_catchup_prints(
        missed_range(5), 'skip', cfg, dt('2026-03-06T00:00:00'))
    assert chosen == [] and dropped == 0


def test_print_once_selects_only_most_recent(clean_state):
    cfg = taskhome.get_catchup_config()
    chosen, dropped = taskhome.select_catchup_prints(
        missed_range(5), 'print_once', cfg, dt('2026-03-06T00:00:00'))
    assert chosen == ['2026-03-05T09:00:00'] and dropped == 0


def test_print_all_selects_everything(clean_state):
    cfg = taskhome.get_catchup_config()
    chosen, dropped = taskhome.select_catchup_prints(
        missed_range(5), 'print_all', cfg, dt('2026-03-06T00:00:00'))
    assert chosen == missed_range(5) and dropped == 0


def test_print_all_caps_and_reports_the_drop(clean_state):
    """No silent truncation (X-4): the overflow count must surface."""
    taskhome.config['catchup'] = {'max_prints': 3}
    cfg = taskhome.get_catchup_config()
    chosen, dropped = taskhome.select_catchup_prints(
        missed_range(10), 'print_all', cfg, dt('2026-03-11T00:00:00'))
    assert len(chosen) == 3
    assert dropped == 7
    assert chosen == missed_range(10)[-3:]  # keeps the most recent


def test_print_if_recent_filters_by_window(clean_state):
    taskhome.config['catchup'] = {'recent_window_hours': 6}
    cfg = taskhome.get_catchup_config()
    missed = ['2026-03-05T00:00:00', '2026-03-05T09:00:00', '2026-03-05T11:00:00']
    chosen, _ = taskhome.select_catchup_prints(
        missed, 'print_if_recent', cfg, dt('2026-03-05T12:00:00'))
    assert chosen == ['2026-03-05T09:00:00', '2026-03-05T11:00:00']


# --- apply_catchup end to end -------------------------------------------------

def test_skip_rolls_forward_without_printing(clean_state, make_task):
    task = make_task('2026-03-01T09:00:00', 'daily')
    taskhome.apply_catchup(task, dt('2026-03-05T12:00:00'))
    assert clean_state == []
    assert task['next_time'] == '2026-03-06T09:00:00'
    assert task['missed_count'] == 5
    assert task['last_missed_at'] == '2026-03-05T09:00:00'


def test_missed_is_recorded_even_when_skipped(clean_state, make_task):
    """Skipping must not mean vanishing."""
    task = make_task('2026-03-01T09:00:00', 'daily')
    taskhome.apply_catchup(task, dt('2026-03-05T12:00:00'))
    assert task['missed_count'] == 5
    assert 'last_missed_at' in task


def test_missed_count_accumulates_across_outages(clean_state, make_task):
    task = make_task('2026-03-01T09:00:00', 'daily')
    taskhome.apply_catchup(task, dt('2026-03-03T12:00:00'))
    first = task['missed_count']
    taskhome.apply_catchup(task, dt('2026-03-06T12:00:00'))
    assert task['missed_count'] > first


def test_oneoff_prints_once_and_is_marked_missed(clean_state, make_task):
    task = make_task('2026-03-01T09:00:00', 'none')
    taskhome.apply_catchup(task, dt('2026-03-05T12:00:00'))
    assert len(clean_state) == 1
    assert task['enabled'] is False
    assert task['missed'] is True


def test_oneoff_receipt_is_marked_as_catchup(clean_state, make_task):
    task = make_task('2026-03-01T09:00:00', 'none')
    taskhome.apply_catchup(task, dt('2026-03-05T12:00:00'))
    receipt = clean_state[0]
    assert receipt['catchup'] is True
    assert 'MISSED' in receipt['extra']


def test_catchup_receipt_preserves_existing_extra(clean_state, make_task):
    task = make_task('2026-03-01T09:00:00', 'none', extra='Take with food')
    taskhome.apply_catchup(task, dt('2026-03-05T12:00:00'))
    assert 'Take with food' in clean_state[0]['extra']


def test_oneoff_skip_policy_prints_nothing_but_still_disables(clean_state, make_task):
    """The invariant: a skipped one-off must not stay enabled and overdue, or
    the steady-state loop fires it a minute later, contradicting the policy."""
    task = make_task('2026-03-01T09:00:00', 'none', catchup='skip')
    taskhome.apply_catchup(task, dt('2026-03-05T12:00:00'))
    assert clean_state == []
    assert task['enabled'] is False


def test_print_all_emits_one_per_occurrence_plus_overflow(clean_state, make_task):
    taskhome.config['catchup'] = {'policy': 'print_all', 'max_prints': 2}
    task = make_task('2026-03-01T09:00:00', 'daily')
    taskhome.apply_catchup(task, dt('2026-03-05T12:00:00'))
    assert len(clean_state) == 3  # 2 occurrences + 1 overflow notice
    assert 'not printed' in clean_state[-1]['extra']


def test_future_task_is_left_alone(clean_state, make_task):
    task = make_task('2026-04-01T09:00:00', 'daily')
    assert taskhome.apply_catchup(task, dt('2026-03-05T12:00:00')) is False
    assert clean_state == []
    assert 'missed_count' not in task


# --- run_catchup isolation ----------------------------------------------------

def test_one_broken_task_does_not_stop_the_others(clean_state, make_task):
    """P0-6: a task that can't advance must not starve the rest."""
    broken = make_task('2026-03-01T09:00:00', 'custom', days=[])
    healthy = make_task('2026-03-01T09:00:00', 'daily')
    taskhome.tasks.extend([broken, healthy])

    taskhome.run_catchup(dt('2026-03-05T12:00:00'))

    assert broken['enabled'] is False
    assert 'schedule_error' in broken
    assert healthy['next_time'] == '2026-03-06T09:00:00'


def test_overdue_oneoff_does_not_hang_startup(clean_state, make_task):
    """P0-1 regression guard: this used to spin forever at 100% CPU."""
    taskhome.tasks.append(make_task('2026-03-01T09:00:00', 'none'))
    taskhome.run_catchup(dt('2026-03-05T12:00:00'))
    assert taskhome.tasks[0]['enabled'] is False


def test_disabled_tasks_are_not_caught_up(clean_state, make_task):
    task = make_task('2026-03-01T09:00:00', 'daily', enabled=False)
    taskhome.tasks.append(task)
    taskhome.run_catchup(dt('2026-03-05T12:00:00'))
    assert task['next_time'] == '2026-03-01T09:00:00'
    assert clean_state == []


def test_unparseable_next_time_is_isolated_and_disabled(clean_state, make_task):
    """P0-6. Isolation alone isn't enough: an unparseable task must also stop
    being retried, or it errors on every tick forever."""
    bad = make_task('total-garbage', 'daily')
    healthy = make_task('2026-03-01T09:00:00', 'daily')
    taskhome.tasks.extend([bad, healthy])

    taskhome.run_catchup(dt('2026-03-05T12:00:00'))

    assert healthy['next_time'] == '2026-03-06T09:00:00'
    assert bad['enabled'] is False
    assert 'schedule_error' in bad


def test_unparseable_next_time_in_steady_state_is_disabled(clean_state, make_task):
    bad = make_task('total-garbage', 'daily')
    taskhome.tasks.append(bad)
    taskhome.run_due_tasks(dt('2026-03-05T12:00:00'))
    assert bad['enabled'] is False
    assert clean_state == []


# --- steady-state loop --------------------------------------------------------

def test_due_task_prints_and_reschedules(clean_state, make_task):
    task = make_task('2026-03-05T09:00:00', 'daily')
    taskhome.tasks.append(task)
    assert taskhome.run_due_tasks(dt('2026-03-05T09:30:00')) == 1
    assert len(clean_state) == 1
    assert task['next_time'] == '2026-03-06T09:00:00'


def test_future_task_does_not_fire(clean_state, make_task):
    task = make_task('2026-03-05T09:00:00', 'daily')
    taskhome.tasks.append(task)
    assert taskhome.run_due_tasks(dt('2026-03-05T08:00:00')) == 0
    assert clean_state == []


def test_due_oneoff_prints_then_is_removed(clean_state, make_task):
    task = make_task('2026-03-05T09:00:00', 'none')
    taskhome.tasks.append(task)
    taskhome.run_due_tasks(dt('2026-03-05T09:30:00'))
    assert len(clean_state) == 1
    assert taskhome.tasks == []


def test_broken_task_is_disabled_not_retried_forever(clean_state, make_task):
    task = make_task('2026-03-05T09:00:00', 'custom', days=[])
    taskhome.tasks.append(task)
    taskhome.run_due_tasks(dt('2026-03-05T09:30:00'))
    assert task['enabled'] is False
    # Second pass must be a no-op rather than printing again.
    before = len(clean_state)
    taskhome.run_due_tasks(dt('2026-03-05T09:31:00'))
    assert len(clean_state) == before


# --- printer offline (P0-4) ---------------------------------------------------

def test_offline_printer_does_not_advance_the_schedule(clean_state, make_task):
    """The occurrence must survive an offline printer, not be silently lost."""
    clean_state.online = False
    task = make_task('2026-03-05T09:00:00', 'daily')
    taskhome.tasks.append(task)

    taskhome.run_due_tasks(dt('2026-03-05T09:30:00'))

    assert clean_state == []
    assert task['next_time'] == '2026-03-05T09:00:00'  # unchanged: still due
    assert task['print_failures'] == 1


def test_offline_printer_retries_until_it_succeeds(clean_state, make_task):
    clean_state.online = False
    task = make_task('2026-03-05T09:00:00', 'daily')
    taskhome.tasks.append(task)

    taskhome.run_due_tasks(dt('2026-03-05T09:30:00'))
    taskhome.run_due_tasks(dt('2026-03-05T09:31:00'))
    assert task['print_failures'] == 2
    assert clean_state == []

    clean_state.online = True
    taskhome.run_due_tasks(dt('2026-03-05T09:32:00'))

    assert len(clean_state) == 1
    assert task['next_time'] == '2026-03-06T09:00:00'
    assert 'print_failures' not in task  # cleared on success


def test_offline_oneoff_is_not_dropped(clean_state, make_task):
    """A one-off is removed after firing, so losing it to an offline printer
    would be unrecoverable."""
    clean_state.online = False
    task = make_task('2026-03-05T09:00:00', 'none')
    taskhome.tasks.append(task)

    taskhome.run_due_tasks(dt('2026-03-05T09:30:00'))
    assert taskhome.tasks == [task]  # still there

    clean_state.online = True
    taskhome.run_due_tasks(dt('2026-03-05T09:31:00'))
    assert len(clean_state) == 1
    assert taskhome.tasks == []


def test_recovery_after_long_outage_applies_catchup_policy(clean_state, make_task):
    """Printer offline for days: print the current occurrence once, and let
    the catch-up policy govern the intervening ones (default: skip)."""
    clean_state.online = False
    task = make_task('2026-03-01T09:00:00', 'daily')
    taskhome.tasks.append(task)
    taskhome.run_due_tasks(dt('2026-03-01T09:30:00'))

    clean_state.online = True
    taskhome.run_due_tasks(dt('2026-03-05T12:00:00'))

    assert len(clean_state) == 1              # one receipt, not four
    assert task['next_time'] == '2026-03-06T09:00:00'
    assert task['missed_count'] == 4          # but the gap is recorded


def test_recovery_honours_print_all_policy(clean_state, make_task):
    taskhome.config['catchup'] = {'policy': 'print_all'}
    clean_state.online = False
    task = make_task('2026-03-01T09:00:00', 'daily')
    taskhome.tasks.append(task)
    taskhome.run_due_tasks(dt('2026-03-01T09:30:00'))

    clean_state.online = True
    taskhome.run_due_tasks(dt('2026-03-05T12:00:00'))

    # The due occurrence plus the four that elapsed during the outage.
    assert len(clean_state) == 5


def test_failed_print_is_persisted(clean_state, make_task, monkeypatch):
    """A failed print mutates the task, so it must trigger a save even though
    nothing 'fired'."""
    saves = []
    monkeypatch.setattr(taskhome, 'save_tasks', lambda: saves.append(1) or True)
    clean_state.online = False
    taskhome.tasks.append(make_task('2026-03-05T09:00:00', 'daily'))

    taskhome.run_due_tasks(dt('2026-03-05T09:30:00'))
    assert saves, 'failed print was not persisted'


def test_catchup_then_steady_state_does_not_double_fire(clean_state, make_task):
    """The integration invariant: catch-up decides to skip, and the loop that
    runs a moment later must honour that rather than firing immediately."""
    task = make_task('2026-03-01T09:00:00', 'daily')
    taskhome.tasks.append(task)

    now = dt('2026-03-05T12:00:00')
    taskhome.run_catchup(now)
    taskhome.run_due_tasks(now)

    assert clean_state == []
    assert task['next_time'] == '2026-03-06T09:00:00'
