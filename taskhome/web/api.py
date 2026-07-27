"""The JSON API (MASTER_PLAN P2-3).

Every response is `{"ok": true, "data": ...}` or `{"ok": false, "error": "..."}`.
A uniform envelope means the client never has to know which endpoint it called
to find out whether it worked, which is what killed the previous contract --
`/test_print` returned raw HTML and the page sniffed it for the word
"successful" (P0-10's UI half).

Validation is **shared with the HTML forms**, not reimplemented. Two validators
for one datastore is how a rule gets fixed in one place and not the other; the
JSON body is adapted to the form interface (`forms.JsonForm`) instead.

The HTML form routes keep working. This is an addition, not a migration --
a LAN appliance whose UI depends on JavaScript to add a task is a downgrade.
"""
from datetime import datetime

from flask import Blueprint, jsonify, request

from .. import constants, printing, recurrence, state, storage, styles
from ..listeners import base as listener_base
from ..logsetup import log
from . import forms, pagination

bp = Blueprint('api', __name__, url_prefix='/api')

#: Config keys the API will write. An allow-list rather than a merge: config
#: also holds `styles`, which has its own endpoints and its own validation.
WRITABLE_CONFIG = ('max_history', 'hostname', 'theme', 'app_name',
                   'catchup', 'log_level', 'host', 'port')


def ok(data=None, status=200):
    return jsonify({'ok': True, 'data': data}), status


def fail(message, status=400):
    return jsonify({'ok': False, 'error': message}), status


def body():
    """The request body as a dict, whether sent as JSON or as a form."""
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form.to_dict(flat=False) if request.form else {}


def as_form(data):
    """Adapt a payload to the form interface the shared validator expects."""
    if request.is_json:
        return forms.JsonForm(data)
    return request.form


def find_task(task_id):
    return next((t for t in state.tasks if t.get('id') == task_id), None)


def task_view(task):
    """A task plus the derived fields a client would otherwise recompute.

    `last_printed` comes from history rather than being stored on the task:
    duplicating it would create a second source of truth that the print path
    would have to keep in step, and it is cheap to derive.
    """
    last = next((h.get('print_time') for h in state.history
                 if h.get('type', 'task') == 'task' and h.get('id') == task.get('id')), None)
    return {**task, 'last_printed': last,
            'recurrence_label': _recurrence_label(task)}


def _recurrence_label(task):
    from .. import layouts
    label = layouts.recurrence_label(task.get('recurring', 'none'))
    days = task.get('days')
    if task.get('recurring') == 'custom' and days:
        names = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
        return '/'.join(names[d] for d in days if 0 <= d < 7)
    return label


# --- tasks --------------------------------------------------------------------

@bp.route('/tasks', methods=['GET'])
def list_tasks():
    return ok([task_view(t) for t in state.tasks])


@bp.route('/tasks', methods=['POST'])
def create_task():
    try:
        task = forms.task_from_form(as_form(body()))
    except forms.ValidationError as e:
        return fail(str(e))
    with state.STATE_LOCK:
        state.tasks.append(task)
    if not storage.save_tasks():
        return fail('Tasks could not be saved.', 500)
    return ok(task_view(task), 201)


@bp.route('/tasks/<task_id>', methods=['GET'])
def get_task(task_id):
    task = find_task(task_id)
    return ok(task_view(task)) if task else fail('No such task.', 404)


@bp.route('/tasks/<task_id>', methods=['PUT', 'PATCH'])
def update_task(task_id):
    task = find_task(task_id)
    if task is None:
        return fail('No such task.', 404)

    payload = body()
    if request.method == 'PATCH':
        # PATCH means "change these fields". The validator needs a whole task,
        # so the existing values are the defaults -- otherwise a PATCH that
        # only sets `enabled` would blank the title and fail validation.
        merged = {k: v for k, v in task.items()}
        merged.update(payload)
        payload = merged

    try:
        # Validated against a copy: a rejected edit must not leave the stored
        # task half-updated, which is the whole reason task_from_form mutates
        # only after every field has validated.
        candidate = forms.task_from_form(as_form(payload), existing=dict(task))
    except forms.ValidationError as e:
        return fail(str(e))

    with state.STATE_LOCK:
        task.clear()
        task.update(candidate)
    if not storage.save_tasks():
        return fail('Tasks could not be saved.', 500)
    return ok(task_view(task))


@bp.route('/tasks/<task_id>', methods=['DELETE'])
def delete_task(task_id):
    with state.STATE_LOCK:
        remaining = [t for t in state.tasks if t.get('id') != task_id]
        removed = len(state.tasks) - len(remaining)
        state.tasks[:] = remaining
    if not removed:
        return fail('No such task.', 404)
    if not storage.save_tasks():
        return fail('Tasks could not be saved.', 500)
    return ok({'deleted': task_id})


@bp.route('/tasks/<task_id>/print', methods=['POST'])
def print_task_now(task_id):
    """Print immediately **without touching the schedule** (P2-2).

    Deliberately not `fire_due_task`: printing one now is not the same as the
    occurrence coming due, and advancing `next_time` would silently skip the
    real reminder. This is the same distinction the reprint endpoint makes.
    """
    task = find_task(task_id)
    if task is None:
        return fail('No such task.', 404)
    if printing.print_task(dict(task)):
        return ok({'printed': True})
    return fail('Printer not connected.', 503)


@bp.route('/tasks/<task_id>/duplicate', methods=['POST'])
def duplicate_task(task_id):
    import uuid
    task = find_task(task_id)
    if task is None:
        return fail('No such task.', 404)
    copy = {**task, 'id': str(uuid.uuid4()),
            'title': f"{task.get('title', 'Task')} (copy)"}
    # A duplicate starts disabled. Copying a task usually means editing it
    # next, and a half-edited chore printing at 6am is a bad surprise.
    copy['enabled'] = False
    copy.pop('schedule_error', None)
    copy.pop('missed', None)
    with state.STATE_LOCK:
        state.tasks.append(copy)
    if not storage.save_tasks():
        return fail('Tasks could not be saved.', 500)
    return ok(task_view(copy), 201)


# --- history ------------------------------------------------------------------

@bp.route('/history', methods=['GET'])
def list_history():
    """Same filter/page contract as the HTML table, so a client cannot get a
    different answer from the same query."""
    query = request.args.get('q', '')
    kind = request.args.get('kind', '')
    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', pagination.DEFAULT_PAGE_SIZE))
    except (TypeError, ValueError):
        return fail('page and per_page must be numbers.')

    filtered = pagination.filter_history(state.history, query, kind)
    view = pagination.paginate(filtered, page, per_page)
    return ok({
        'records': view['items'],
        'page': view['page'],
        'pages': view['pages'],
        'total': view.get('total', len(filtered)),
        'kinds': [{'key': k, 'label': label} for k, label in pagination.history_kinds()],
    })


# --- config -------------------------------------------------------------------

@bp.route('/config', methods=['GET'])
def get_config():
    return ok(dict(state.config))


@bp.route('/config', methods=['PUT', 'PATCH'])
def put_config():
    payload = body()
    if not isinstance(payload, dict):
        return fail('Expected an object.')

    unknown = set(payload) - set(WRITABLE_CONFIG)
    if unknown:
        return fail(f"Not writable here: {', '.join(sorted(unknown))}.")

    updates = {}
    for key, value in payload.items():
        if key in ('max_history', 'port'):
            try:
                value = int(value)
            except (TypeError, ValueError):
                return fail(f'{key} must be a number.')
            if value < 0:
                return fail(f'{key} cannot be negative.')
        elif key == 'theme' and value not in ('system', 'light', 'dark'):
            return fail(f"'{value}' is not a valid theme.")
        elif key == 'catchup':
            # The only writable key that is a nested dict, and the only one
            # validated here rather than by a shared helper.
            #
            # recurrence.get_catchup_config() deliberately never raises -- it
            # degrades a malformed config to defaults so a bad value cannot
            # take down the scheduler at 3am. That is right for the scheduler
            # and wrong for an API: silently storing something different from
            # what was sent is worse than refusing it.
            problem = _validate_catchup(value)
            if problem:
                return fail(problem)
            value = {**(state.config.get('catchup') or {}), **value}
        updates[key] = value

    with state.STATE_LOCK:
        state.config.update(updates)
    if not storage.save_config():
        return fail('Config could not be saved.', 500)
    return ok(dict(state.config))


def _validate_catchup(value):
    """None if the catch-up patch is valid, else a message for the caller."""
    if not isinstance(value, dict):
        return 'catchup must be an object.'
    unknown = set(value) - set(constants.DEFAULT_CATCHUP)
    if unknown:
        return f"Unknown catch-up settings: {', '.join(sorted(unknown))}."
    for key in ('policy', 'oneoff_policy'):
        if key in value and value[key] not in constants.CATCHUP_POLICIES:
            return (f"'{value[key]}' is not a valid {key}. "
                    f"Choose one of: {', '.join(constants.CATCHUP_POLICIES)}.")
    for key in ('recent_window_hours', 'max_prints'):
        if key not in value:
            continue
        try:
            number = int(value[key])
        except (TypeError, ValueError):
            return f'{key} must be a number.'
        if number < 0:
            return f'{key} cannot be negative.'
        value[key] = number
    return None


# --- listeners ----------------------------------------------------------------

@bp.route('/listeners', methods=['GET'])
def list_listeners():
    data = []
    for name, listener in sorted(listener_base.registry().items()):
        data.append({
            'name': name, 'title': listener.title,
            'enabled': listener.enabled(),
            'interval': listener.interval_minutes(),
            'config': listener.config(),
            'schema': [dict(spec) for spec in listener.CONFIG_SCHEMA],
        })
    return ok(data)


@bp.route('/listeners/<name>', methods=['GET'])
def get_listener(name):
    listener = listener_base.get(name)
    if listener is None:
        return fail('Unknown listener.', 404)
    return ok({'name': name, 'title': listener.title,
               'config': listener.config(),
               'schema': [dict(spec) for spec in listener.CONFIG_SCHEMA]})


@bp.route('/listeners/<name>', methods=['PUT', 'PATCH'])
def put_listener(name):
    listener = listener_base.get(name)
    if listener is None:
        return fail('Unknown listener.', 404)
    payload = body()
    if not isinstance(payload, dict):
        return fail('Expected an object.')
    try:
        # The schema validator, not a second copy of the rules.
        listener.save_config({k: v for k, v in payload.items()
                              if k in {s['key'] for s in listener.CONFIG_SCHEMA}})
    except ValueError as e:
        return fail(str(e))
    return ok(listener.config())


# --- printing -----------------------------------------------------------------

@bp.route('/test_print', methods=['POST'])
def test_print():
    """Replaces the contract where the page sniffed HTML for the word
    'successful' (P0-10)."""
    payload = body()
    kind = payload.get('kind', 'task') if isinstance(payload, dict) else 'task'
    if kind not in styles.kinds():
        return fail('Unknown receipt kind.')
    if not printing.is_printer_connected():
        return fail('Printer not connected.', 503)

    blocks = styles.fill(
        styles.get_template(kind, styles.active_template_name(kind)),
        styles.sample_context(kind))
    if printing.print_blocks(blocks):
        log.info(f"Test print ({kind}) succeeded")
        return ok({'printed': True, 'kind': kind})
    return fail('The print failed. Check the printer and the log.', 500)


@bp.route('/inbound/<token>', methods=['POST'])
def inbound(token):
    """Print whatever was POSTed here (P5-2 #1).

    Deliberately permissive about its input -- a JSON object, or a bare string,
    because half the things that will call this are a shell script with
    `-d "Bins tonight"`. Deliberately strict about everything else.

    Responses say as little as possible to an unauthenticated caller: a wrong
    token gets 404, not 403, so scanning cannot distinguish "no such endpoint"
    from "right endpoint, wrong secret".
    """
    from ..listeners import webhook

    listener = webhook.listener
    config = listener.config()

    if not config.get('enabled') or not listener.check_token(config, token):
        log.warning('Rejected inbound webhook (disabled or bad token)')
        return fail('Not found.', 404)

    allowed, used, limit = listener.within_rate_limit(config)
    if not allowed:
        # 429 with a Retry-After, so a well-behaved client backs off rather
        # than hammering. The limit exists for stuck loops, not attackers.
        log.warning(f'Webhook rate limit reached ({used}/{limit} this hour)')
        response = fail(f'Rate limit reached ({limit} per hour).', 429)
        response[0].headers['Retry-After'] = '3600'
        return response

    payload = request.get_json(silent=True)
    if payload is None:
        raw = request.get_data(as_text=True).strip()
        payload = raw or (request.form.to_dict() or None)
    if payload is None:
        return fail('Send a JSON object, a form, or plain text.')

    try:
        item = listener.parse(payload)
    except ValueError as e:
        return fail(str(e))

    listener.note_delivery()
    printed = listener_base.deliver(listener, [item])
    if not printed:
        # Filtered out by allow_sources. Reported honestly rather than as
        # success, or a misconfigured filter looks like a working webhook.
        return ok({'printed': 0, 'reason': 'filtered'})
    return ok({'printed': printed, 'title': item['title']})


@bp.route('/webhook/token', methods=['POST'])
def rotate_webhook_token():
    """Generate a new token, invalidating the old one."""
    from ..listeners import webhook
    token = webhook.new_token()
    webhook.listener.save_config({'token': token})
    return ok({'token': token})


# --- checklists (P5-2 #11) ----------------------------------------------------

@bp.route('/lists', methods=['GET'])
def get_lists():
    from .. import lists
    return ok(lists.all_lists())


@bp.route('/lists', methods=['POST'])
def create_list():
    from .. import lists
    try:
        return ok(lists.create_list((body() or {}).get('name')), 201)
    except ValueError as e:
        return fail(str(e))


@bp.route('/lists/<list_id>', methods=['PATCH', 'DELETE'])
def modify_list(list_id):
    from .. import lists
    try:
        if request.method == 'DELETE':
            lists.delete_list(list_id)
            return ok({'deleted': list_id})
        return ok(lists.rename_list(list_id, (body() or {}).get('name')))
    except ValueError as e:
        return fail(str(e), 404 if 'No such' in str(e) else 400)


@bp.route('/lists/<list_id>/items', methods=['POST'])
def add_list_item(list_id):
    from .. import lists
    try:
        return ok(lists.add_item(list_id, (body() or {}).get('text')), 201)
    except ValueError as e:
        return fail(str(e), 404 if 'No such' in str(e) else 400)


@bp.route('/lists/<list_id>/items/<item_id>', methods=['PATCH', 'DELETE'])
def modify_list_item(list_id, item_id):
    from .. import lists
    try:
        if request.method == 'DELETE':
            lists.remove_item(list_id, item_id)
            return ok({'deleted': item_id})
        payload = body() or {}
        return ok(lists.set_item(list_id, item_id,
                                 done=payload.get('done'), text=payload.get('text')))
    except ValueError as e:
        return fail(str(e), 404 if 'No such' in str(e) else 400)


@bp.route('/lists/<list_id>/clear', methods=['POST'])
def clear_list_done(list_id):
    from .. import lists
    try:
        return ok({'removed': lists.clear_done(list_id)})
    except ValueError as e:
        return fail(str(e), 404)


@bp.route('/lists/<list_id>/print', methods=['POST'])
def print_list(list_id):
    from .. import lists
    include_done = bool((body() or {}).get('include_done'))
    try:
        printed = lists.print_list(list_id, include_done)
    except ValueError as e:
        return fail(str(e), 404)
    if printed:
        return ok({'printed': True})
    return fail('Printer not connected -- the list was queued.', 503)


# --- chore charts (P5-2 #12) --------------------------------------------------

@bp.route('/chores', methods=['POST'])
def create_person():
    from .. import chores
    try:
        return ok(chores.add_person((body() or {}).get('name')), 201)
    except ValueError as e:
        return fail(str(e))


@bp.route('/chores/<person_id>', methods=['PATCH', 'DELETE'])
def modify_person(person_id):
    from .. import chores
    try:
        if request.method == 'DELETE':
            chores.remove_person(person_id)
            return ok({'deleted': person_id})
        payload = body() or {}
        return ok(chores.update_person(
            person_id, name=payload.get('name'), days=payload.get('days'),
            chores=payload.get('chores'), rotate=bool(payload.get('rotate'))))
    except ValueError as e:
        return fail(str(e), 404 if 'No such' in str(e) else 400)


@bp.route('/chores/<person_id>/done', methods=['POST', 'DELETE'])
def set_person_done(person_id):
    from .. import chores
    try:
        person = (chores.undo_done(person_id) if request.method == 'DELETE'
                  else chores.mark_done(person_id))
    except ValueError as e:
        return fail(str(e), 404)
    return ok({'streak': chores.streak(person),
               'done_today': chores.done_today(person)})


@bp.route('/chores/<person_id>/print', methods=['POST'])
def print_chore_chart(person_id):
    from .. import chores, printing, queue
    person = chores.get_person(person_id)
    if person is None:
        return fail('No such person.', 404)
    blocks = chores.chart_blocks(person)
    history = {'type': 'chores', 'id': person['id'], 'category': 'Chore chart',
               'title': person.get('name', ''),
               'print_time': __import__('datetime').datetime.now().isoformat()}
    if printing.print_blocks(blocks):
        printing.record_history(history)
        return ok({'printed': True})
    queue.enqueue('chores', blocks,
                  description=f"Chore chart: {person.get('name', '')}",
                  history=history)
    return fail('Printer not connected -- the chart was queued.', 503)


@bp.route('/scheduler', methods=['GET'])
def scheduler_info():
    """What the scheduler is about to do, which is otherwise only visible by
    waiting for it to happen."""
    from .. import scheduler
    last_tick, ticks = scheduler.heartbeat()
    due = []
    now = datetime.now()
    for task in state.tasks:
        if not task.get('enabled', True):
            continue
        try:
            when = recurrence.parse_task_time(task['next_time'])
        except Exception:
            continue
        due.append({'id': task.get('id'), 'title': task.get('title'),
                    'next_time': task['next_time'],
                    'overdue': when <= now})
    due.sort(key=lambda t: t['next_time'])
    return ok({
        'running': scheduler.is_alive(),
        'last_tick': last_tick.isoformat() if last_tick else None,
        'ticks': ticks,
        'upcoming': due[:10],
    })
