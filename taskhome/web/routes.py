"""HTTP routes, as a blueprint so the app factory can register them.

Write routes validate and return 400 with a readable page rather than a
traceback; the test-print routes signal the real outcome by status code so the
front end can trust resp.ok (P0-10).
"""
import uuid
from datetime import datetime, timezone

from flask import Blueprint, redirect, render_template, request, url_for

from .. import constants, printing, queue, receipt, state, storage, styles
from ..listeners import scf
from ..settings import get_port  # module name would clash with the route
from ..logsetup import log
from . import forms, pagination

bp = Blueprint('main', __name__)


@bp.app_context_processor
def inject_queue_badge():
    """Backlog size for the appbar, so a stuck queue is visible everywhere
    rather than only on its own page."""
    try:
        return {'queue_waiting': queue.stats()['total']}
    except Exception:
        return {'queue_waiting': 0}


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
        # listeners['scf'] may not exist yet; the old code indexed it directly
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

        updated = dict(existing)
        updated.update({
            'enabled': 'enabled' in request.form,
            'request_types': request_types,
            'interval': interval,
            'last_check': existing.get('last_check'),  # Preserve existing last_check
        })
        state.listeners['scf'] = updated
        storage.save_listeners()
        return redirect(url_for('main.listener'))

    scf_config = state.listeners.get('scf', {})
    # Names come from the cache; a lookup only happens for ids never seen
    # before, so opening this page does not depend on the network.
    try:
        described = scf.describe_request_types(scf_config.get('request_types', ''))
    except Exception as e:
        log.warning(f"Could not describe request types: {e}")
        described = []
    return render_template('listener.html', config=state.config, scf=scf_config,
                           request_types=described)


@bp.route('/api/scf/browse')
def api_scf_browse():
    """Request types offered at a location, for the picker (P4-3).

    The API has no search-by-name endpoint, so discovery goes through the
    report-a-problem form, which lists everything available for a place.
    """
    try:
        lat = float(request.args.get('lat', ''))
        lng = float(request.args.get('lng', ''))
    except (TypeError, ValueError):
        return {'ok': False, 'error': 'Give a latitude and longitude.'}, 400
    try:
        return {'ok': True, 'request_types': scf.browse_request_types(lat, lng)}
    except Exception as e:
        log.warning(f"SCF browse failed: {e}")
        return {'ok': False, 'error': f'Could not reach SeeClickFix: {e}'}, 502


@bp.route('/api/scf/names', methods=['POST'])
def api_scf_names():
    """Look up names for ids, so the picker can label a pasted list."""
    payload = request.get_json(silent=True) or {}
    ids = payload.get('ids') or []
    if not isinstance(ids, list) or len(ids) > 100:
        return {'ok': False, 'error': 'Give a list of at most 100 ids.'}, 400
    try:
        return {'ok': True,
                'request_types': scf.describe_request_types(','.join(str(i) for i in ids))}
    except Exception as e:
        return {'ok': False, 'error': str(e)}, 502


# --- Receipt Style Studio (P3-4) ----------------------------------------------

@bp.route('/settings/receipts')
def receipt_studio():
    kind = request.args.get('kind', 'task')
    if kind not in styles.KINDS:
        kind = 'task'
    name = request.args.get('name') or styles.active_template_name(kind)
    template = styles.get_template(kind, name)
    return render_template(
        'receipt_studio.html',
        config=state.config,
        kind=kind,
        kinds=styles.KINDS,
        template=template,
        templates=styles.list_templates(kind),
        active=styles.active_template_name(kind),
        placeholders=sorted(styles.PLACEHOLDERS[kind]),
        preview=styles.preview(template),
    )


@bp.route('/api/receipt/preview', methods=['POST'])
def api_receipt_preview():
    """Render a template to preview lines.

    Server-side on purpose. A JavaScript re-implementation would be a second
    renderer that can disagree with the printer, which is the exact failure the
    shared renderer exists to prevent -- and it has already happened once, when
    the printer hard-wrapped mid-word while the preview wrapped on words.
    """
    payload = request.get_json(silent=True) or {}
    try:
        return {'ok': True, **styles.preview(payload.get('template') or {},
                                             payload.get('context'))}
    except styles.TemplateError as e:
        return {'ok': False, 'error': str(e)}, 400


@bp.route('/api/receipt/templates/<kind>', methods=['POST'])
def api_save_template(kind):
    payload = request.get_json(silent=True) or {}
    template = payload.get('template') or {}
    template['kind'] = kind
    try:
        saved = styles.save_template(template)
    except styles.TemplateError as e:
        return {'ok': False, 'error': str(e)}, 400
    if payload.get('activate'):
        styles.set_active_template(kind, saved['name'])
    return {'ok': True, 'name': saved['name'],
            'active': styles.active_template_name(kind)}


@bp.route('/api/receipt/activate/<kind>/<name>', methods=['POST'])
def api_activate_template(kind, name):
    if kind not in styles.KINDS:
        return {'ok': False, 'error': 'Unknown receipt kind.'}, 400
    styles.set_active_template(kind, name)
    return {'ok': True, 'active': styles.active_template_name(kind)}


@bp.route('/api/receipt/templates/<kind>/<name>', methods=['DELETE'])
def api_delete_template(kind, name):
    try:
        styles.delete_template(kind, name)
    except styles.TemplateError as e:
        return {'ok': False, 'error': str(e)}, 400
    if styles.active_template_name(kind) == name:
        styles.set_active_template(kind, f'{kind}-default')
    return {'ok': True}


@bp.route('/api/receipt/test_print/<kind>', methods=['POST'])
def api_template_test_print(kind):
    """Print the template being edited. Emits real paper."""
    if kind not in styles.KINDS:
        return {'ok': False, 'error': 'Unknown receipt kind.'}, 400
    if not printing.is_printer_connected():
        return {'ok': False, 'error': 'Printer not connected.'}, 503
    payload = request.get_json(silent=True) or {}
    try:
        template = styles.validate_template(payload.get('template') or {})
    except styles.TemplateError as e:
        return {'ok': False, 'error': str(e)}, 400

    blocks = styles.fill(template, styles.sample_context(kind))
    try:
        with printing.open_printer() as p:
            receipt.render_escpos(blocks, p)
            p.cut()
    except Exception as e:
        log.error(f"Template test print failed: {e}", exc_info=True)
        return {'ok': False, 'error': str(e)}, 500
    return {'ok': True}


# --- print queue (P6-3) -------------------------------------------------------

@bp.route('/queue')
def print_queue():
    jobs = queue.load_queue()
    return render_template(
        'queue.html', config=state.config,
        jobs=[{**job, 'summary': queue.describe(job),
               'paper_mm': round(receipt.height_mm(job.get('blocks') or []))}
              for job in jobs],
        stats=queue.stats(),
        paper_mm=queue.estimated_paper_mm(jobs))


@bp.route('/api/queue/retry', methods=['POST'])
def api_queue_retry():
    """Release parked jobs and attempt a drain now."""
    released = queue.release_parked()
    printed, remaining = queue.drain()
    return {'ok': True, 'released': released, 'printed': printed,
            'remaining': remaining}


@bp.route('/api/queue/<job_id>', methods=['DELETE'])
def api_queue_discard(job_id):
    removed = queue.discard(None if job_id == 'all' else job_id)
    return {'ok': True, 'removed': removed}
