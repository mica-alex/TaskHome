"""HTTP routes, as a blueprint so the app factory can register them.

Write routes validate and return 400 with a readable page rather than a
traceback; the test-print routes signal the real outcome by status code so the
front end can trust resp.ok (P0-10).
"""
import uuid
from datetime import datetime, timezone

from flask import Blueprint, redirect, render_template, request, url_for

from .. import constants, printing, state, storage
from ..settings import get_port  # module name would clash with the route
from ..logsetup import log
from . import forms, pagination

bp = Blueprint('main', __name__)


@bp.app_context_processor
def inject_printer_status():
    """Make the printer state available to every template.

    The appbar shows a status dot on all pages, and probing per render is
    cheap -- one usb.core.find, which is what /settings already did.
    """
    return {'printer_online': printing.is_printer_connected()}


@bp.route('/')
def index():
    status = 'Connected' if printing.is_printer_connected() else 'Not connected'
    recent_history = state.history[:5]
    # All state.tasks, not just enabled ones: a task disabled by a schedule error or
    # a missed one-off must stay visible (P0-13, and P1-10's promise that
    # skipping isn't vanishing). The template renders the status.
    return render_template('index.html', status=status, config=state.config, tasks=state.tasks, history=recent_history)


@bp.route('/task_page')
def task_page():
    query = request.args.get('q', '')
    kind = request.args.get('type', '')
    matched = pagination.filter_history(state.history, query, kind)
    page = pagination.paginate(matched, request.args.get('page', 1),
                    request.args.get('per_page', pagination.DEFAULT_PAGE_SIZE))
    return render_template(
        'tasks.html', config=state.config, tasks=state.tasks,
        history=page['items'], page=page,
        page_numbers=pagination.page_numbers(page['page'], page['pages']),
        query=query, kind=kind, total_history=len(state.history))


@bp.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        if 'clear_history' in request.form:
            # Mutate in place rather than rebinding the global: rebinding
            # detaches the module-level name from the list other code is
            # already holding, so their writes would go to an orphan.
            with state.STATE_LOCK:
                del state.history[:]
            storage.save_history()
            return redirect(url_for('main.settings'))
        raw_max = request.form.get('max_history', '')
        try:
            max_history = int(raw_max)
        except (TypeError, ValueError):
            return forms.reject(f"'{raw_max}' is not a valid state.history size.")
        if not 0 <= max_history <= 100000:
            return forms.reject('History size must be between 0 and 100000.')

        theme = request.form.get('theme', 'system')
        if theme not in constants.THEMES:
            return forms.reject(f"'{theme}' is not a valid theme.")

        state.config['max_history'] = max_history
        state.config['hostname'] = (request.form.get('hostname') or '').strip() or \
            constants.DEFAULT_CONFIG['hostname']
        state.config['theme'] = theme
        storage.save_config()
        del state.history[max_history:]
        storage.save_history()
        return redirect(url_for('main.settings'))
    printer_info = {
        'manufacturer': constants.PRINTER_MANUFACTURER,
        'model': constants.PRINTER_MODEL,
        'connection': constants.PRINTER_CONNECTION,
        'status': 'Connected' if printing.is_printer_connected() else 'Not connected'
    }
    return render_template('settings.html', config=state.config, printer_info=printer_info)


@bp.route('/test_print', methods=['POST'])
def test_print():
    if not printing.is_printer_connected():
        # 503, not 200: the front end trusts the status code, so a "not
        # connected" reply must not read as success (P0-10).
        return 'Printer not connected. <a href="/settings">Back</a>', 503
    try:
        # Create a test task with example data
        test_task = {
            'id': str(uuid.uuid4()),
            'title': 'Test Task Print',
            'extra': 'This is a test print from TaskHome',
            'url': f"http://{state.config.get('hostname', constants.DEFAULT_CONFIG['hostname'])}:{get_port()}/task_page#test",
            'next_time': datetime.now().isoformat(),
            'recurring': 'none',
            'enabled': True
        }
        # printing.print_task swallows its own errors, so the return value is the only
        # honest signal of whether paper came out (P0-10).
        if printing.print_task(test_task):
            return 'Test print successful! <a href="/settings">Back</a>'
        return ('Test print failed - see the log for details. '
                '<a href="/settings">Back</a>'), 500
    except Exception as e:
        log.error(f"Test print error: {e}")
        return f'Test print failed: {e}. <a href="/settings">Back</a>', 500


@bp.route('/test_scf_print', methods=['POST'])
def test_scf_print():
    if not printing.is_printer_connected():
        return 'Printer not connected. <a href="/settings">Back</a>', 503
    try:
        # Example SCF issue data
        test_issue = {
            'id': 12345678,
            'html_url': 'https://seeclickfix.com/issues/12345678',
            'request_type': {'title': 'Streetlight Out'},
            'address': '123 Main St, Springfield',
            'created_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'status': 'Open',
            'description': 'The streetlight in front of my house is not working.',
            'summary': 'Streetlight outage reported',
            'media': {
                # 'image_full': 'https://seeclickfix.com/media/issues/12345678/full.jpg',
                'image_full': None,
                'image_square_100x100': 'https://seeclickfix.com/media/issues/12345678/thumb.jpg'
            }
        }
        if printing.print_scf_issue(test_issue):
            return 'Test SCF issue print successful! <a href="/settings">Back</a>'
        return ('Test SCF issue print failed - see the log for details. '
                '<a href="/settings">Back</a>'), 500
    except Exception as e:
        log.error(f"Test SCF print error: {e}")
        return f'Test SCF print failed: {e}. <a href="/settings">Back</a>', 500


@bp.route('/add_task', methods=['POST'])
def add_task():
    try:
        task = forms.task_from_form(request.form)
    except forms.ValidationError as e:
        return forms.reject(str(e))
    with state.STATE_LOCK:
        state.tasks.append(task)
    storage.save_tasks()
    return redirect(url_for('main.task_page'))


@bp.route('/edit_task/<task_id>', methods=['GET', 'POST'])
def edit_task(task_id):
    task = next((t for t in state.tasks if t['id'] == task_id), None)
    if not task:
        return 'Task not found', 404
    if request.method == 'POST':
        try:
            # Validate against a copy so a rejected edit leaves the live task
            # untouched rather than partially applied.
            candidate = forms.task_from_form(request.form, existing=dict(task))
        except forms.ValidationError as e:
            return forms.reject(str(e))
        with state.STATE_LOCK:
            task.clear()
            task.update(candidate)
        storage.save_tasks()
        return redirect(url_for('main.task_page'))
    return render_template('tasks.html', config=state.config, tasks=state.tasks,
                           history=state.history, edit_task=task)


@bp.route('/delete_task', methods=['POST'])
def delete_task():
    task_id = request.form.get('id')
    if not task_id:
        return forms.reject('No task specified.')
    with state.STATE_LOCK:
        remaining = [t for t in state.tasks if t.get('id') != task_id]
        if len(remaining) == len(state.tasks):
            return forms.reject('That task no longer exists.', status=404)
        state.tasks[:] = remaining
    storage.save_tasks()
    return redirect(url_for('main.task_page'))


# New route for state.listeners page
@bp.route('/listener', methods=['GET', 'POST'])  # Note: singular as per your request
def listener():
    if request.method == 'POST':
        # state.listeners['scf'] may not exist yet; the old code indexed it directly
        # to preserve last_check and raised KeyError on a fresh install (P0-9).
        existing = state.listeners.get('scf') or {}

        raw_interval = request.form.get('interval', '')
        try:
            interval = int(raw_interval)
        except (TypeError, ValueError):
            return forms.reject(f"'{raw_interval}' is not a valid interval.")
        if not 1 <= interval <= 1440:
            return forms.reject('Interval must be between 1 and 1440 minutes.')

        request_types = ','.join(
            part.strip() for part in (request.form.get('request_types') or '').split(',')
            if part.strip())

        state.listeners['scf'] = {
            'enabled': 'enabled' in request.form,
            'request_types': request_types,
            'interval': interval,
            'last_check': existing.get('last_check'),  # Preserve existing last_check
        }
        storage.save_listeners()
        return redirect(url_for('main.listener'))
    return render_template('listener.html', config=state.config, scf=state.listeners.get('scf', {}))


scheduler_thread = None
