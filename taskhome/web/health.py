"""Health, status and diagnostics (MASTER_PLAN P6-4).

Two consumers, two shapes:

* `/api/health` — for an uptime monitor or `deploy/healthcheck.sh`. Returns a
  non-200 when something is actually wrong, because a monitor that has to parse
  the body to find out is a monitor nobody wires up.
* `/api/status` — for the running page (P2-5). Always 200; the page renders
  whatever it says.

The interesting field is the **scheduler heartbeat**. If that thread dies or
wedges, the web UI keeps serving perfectly and nothing else in the system
notices — receipts simply stop. That is the failure mode P0-1 and P0-2 both
had, and it went unnoticed until someone wondered why the bins reminder never
came. An age that stops advancing is the only external symptom.

A printer that is unplugged is deliberately **not** unhealthy. That is a normal
state for this appliance — the queue exists precisely so it is survivable — and
a monitor that pages at 2am because someone moved the printer is a monitor that
gets muted.
"""
from datetime import datetime, timezone

from flask import Blueprint, jsonify

from .. import constants, printing, queue, scheduler, state
from ..listeners import base as listener_base

bp = Blueprint('health', __name__)

#: Three missed ticks. One missed tick is a slow print or a slow poll; three in
#: a row means the loop is not coming back.
STALE_HEARTBEAT_SECONDS = 195


def _age_seconds(when):
    if when is None:
        return None
    return round((datetime.now(timezone.utc) - when).total_seconds(), 1)


def scheduler_health():
    last_tick, ticks = scheduler.heartbeat()
    age = _age_seconds(last_tick)
    running = scheduler.is_alive()
    if not running:
        # Not an error on its own: the web UI is routinely run without a
        # scheduler for development, and saying "unhealthy" for a deliberate
        # configuration would train people to ignore this endpoint.
        status = 'not running'
    elif age is None:
        status = 'starting'
    elif age > STALE_HEARTBEAT_SECONDS:
        status = 'stalled'
    else:
        status = 'ok'
    return {
        'status': status,
        'running': running,
        'last_tick': last_tick.isoformat() if last_tick else None,
        'age_seconds': age,
        'ticks': ticks,
    }


def listener_health():
    listeners = {}

    scf_config = state.listeners.get('scf') or {}
    listeners['scf'] = {
        'title': 'SeeClickFix',
        'enabled': bool(scf_config.get('enabled')),
        'last_check': scf_config.get('last_check'),
        'last_error': scf_config.get('last_error'),
        'backoff_until': scf_config.get('backoff_until'),
    }

    for name, listener in listener_base.registry().items():
        runtime = state.listeners.get(name) or {}
        listeners[name] = {
            'title': listener.title,
            'enabled': listener.enabled(),
            'last_check': runtime.get('last_check'),
            'last_error': runtime.get('last_error'),
            'backoff_until': runtime.get('backoff_until'),
        }
    return listeners


def snapshot():
    """Everything both endpoints report."""
    queue_stats = queue.stats()
    return {
        'version': constants.VERSION,
        'printer': {
            'connected': printing.is_printer_connected(),
            'vendor_id': hex(constants.VID),
            'product_id': hex(constants.PID),
        },
        'scheduler': scheduler_health(),
        'queue': {
            **queue_stats,
            'paper_mm': queue.estimated_paper_mm(),
        },
        'listeners': listener_health(),
        'stores': {
            # A store that failed to load is write-blocked, so it is silently
            # read-only until someone looks. Surfacing it is the whole point.
            'failed': sorted(state.load_failed),
            'tasks': len(state.tasks),
            'history': len(state.history),
        },
    }


def problems(data):
    """What is actually wrong, as a list of human-readable strings.

    Deliberately narrow. Everything listed here is something a person needs to
    go and fix; anything merely worth knowing belongs in the body, not here.
    """
    found = []
    if data['scheduler']['status'] == 'stalled':
        found.append(
            f"Scheduler has not ticked for {data['scheduler']['age_seconds']}s "
            f"-- the thread is alive but not completing a loop.")
    if data['stores']['failed']:
        found.append(
            f"These stores failed to load and are write-blocked: "
            f"{', '.join(data['stores']['failed'])}.")
    if data['queue']['parked']:
        found.append(
            f"{data['queue']['parked']} print job(s) parked after repeated "
            f"failures; they will not retry until released.")
    for name, listener in data['listeners'].items():
        if listener['enabled'] and listener['last_error']:
            found.append(f"{listener['title']}: {listener['last_error']}")
    return found


def print_stats(days=14, history=None):
    """Prints per day and per kind over the last `days` days (P6-4).

    Derived from history rather than kept as counters. A counter would be a
    second source of truth that every print path has to remember to increment,
    and one that silently drifts the first time somebody forgets. History is
    already the record of paper that exists.

    The consequence, stated rather than hidden: this only sees as far back as
    `max_history` allows, so a busy install with a small cap has a short
    window. The counts are honest about what they cover.
    """
    from datetime import date, timedelta

    records = state.history if history is None else history
    today = date.today()
    window = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    by_day = {day.isoformat(): 0 for day in window}
    by_kind = {}
    oldest = None

    for record in records:
        stamp = record.get('print_time')
        if not stamp:
            continue
        try:
            when = datetime.fromisoformat(stamp).date()
        except (TypeError, ValueError):
            continue
        if oldest is None or when < oldest:
            oldest = when
        kind = record.get('type', 'task')
        by_kind[kind] = by_kind.get(kind, 0) + 1
        key = when.isoformat()
        if key in by_day:
            by_day[key] += 1

    series = [by_day[day.isoformat()] for day in window]
    return {
        'days': [day.isoformat() for day in window],
        'series': series,
        'total_window': sum(series),
        'total_recorded': len([r for r in records if r.get('print_time')]),
        'by_kind': dict(sorted(by_kind.items(), key=lambda kv: -kv[1])),
        'busiest': max(series) if series else 0,
        'oldest_record': oldest.isoformat() if oldest else None,
        # History is capped, so "all time" is not all time.
        'window_limited': len(records) >= state.config.get('max_history', 500),
    }


@bp.route('/api/stats')
def api_stats():
    return jsonify({'ok': True, 'data': print_stats()})


@bp.route('/api/health')
def api_health():
    """For uptime monitors. Non-200 means go and look at it."""
    data = snapshot()
    found = problems(data)
    data['ok'] = not found
    data['problems'] = found
    return jsonify(data), (200 if data['ok'] else 503)


@bp.route('/api/status')
def api_status():
    """For the page. Always 200 -- a status widget that vanishes when
    something is wrong is worse than useless."""
    data = snapshot()
    data['problems'] = problems(data)
    return jsonify(data)
