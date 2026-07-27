"""HTTP routes, as a blueprint so the app factory can register them.

Write routes validate and return 400 with a readable page rather than a
traceback; the test-print routes signal the real outcome by status code so the
front end can trust resp.ok (P0-10).
"""
import uuid
from datetime import datetime, timezone

from flask import abort, Blueprint, redirect, render_template, request, url_for

from .. import constants, printing, queue, receipt, state, storage, styles
from ..listeners import base as listener_base, scf
from ..settings import get_port  # module name would clash with the route
from ..logsetup import log
from . import api, forms, pagination

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
    return render_template('index.html', status=status, config=state.config, tasks=[api.task_view(t) for t in state.tasks], history=recent_history)


@bp.route('/task_page')
def task_page():
    query = request.args.get('q', '')
    kind = request.args.get('type', '')
    matched = pagination.filter_history(state.history, query, kind)
    page = pagination.paginate(matched, request.args.get('page', 1),
                    request.args.get('per_page', pagination.DEFAULT_PAGE_SIZE))
    return render_template(
        'tasks.html', config=state.config, tasks=[api.task_view(t) for t in state.tasks],
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
    return render_template('tasks.html', config=state.config, tasks=[api.task_view(t) for t in state.tasks],
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


@bp.app_context_processor
def inject_history_helpers():
    """History rendering is registry-driven (P2-1), so the helpers have to be
    reachable from the shared row partial."""
    return {'history_kinds': pagination.history_kinds,
            'history_label': pagination.history_label,
            'history_title': pagination.history_title}


@bp.route('/listener')
def listener():
    """The listeners index.

    SeeClickFix keeps a bespoke page because its category picker needs a live
    lookup that no generic field type expresses; everything else renders from
    its CONFIG_SCHEMA (P2-11).
    """
    cards = []

    scf_config = state.listeners.get('scf') or {}
    cards.append({
        'name': 'scf',
        'title': 'SeeClickFix',
        'description': ('Prints a receipt for each new issue reported in the '
                        'categories you subscribe to.'),
        'url': url_for('main.listener_scf'),
        'enabled': bool(scf_config.get('enabled')),
        'interval': scf_config.get('interval') or 10,
        'last_check': scf_config.get('last_check'),
        'error': scf_config.get('last_error'),
        'summary': _scf_summary(scf_config),
    })

    for name, listener_obj in sorted(listener_base.registry().items()):
        runtime = state.listeners.get(name) or {}
        try:
            summary = listener_obj.summary()
        except Exception as e:      # a summary must never break the index
            log.warning(f"Could not summarise {name}: {e}")
            summary = ''
        cards.append({
            'name': name,
            'title': listener_obj.title,
            'description': listener_obj.description,
            'url': url_for('main.listener_settings', name=name),
            'enabled': listener_obj.enabled(),
            'interval': listener_obj.interval_minutes(),
            'last_check': runtime.get('last_check'),
            'error': runtime.get('last_error'),
            'summary': summary,
        })

    return render_template('listener.html', config=state.config, cards=cards)


def _scf_summary(scf_config):
    ids = [p for p in (scf_config.get('request_types') or '').split(',') if p.strip()]
    return f'{len(ids)} categor{"y" if len(ids) == 1 else "ies"}' if ids else \
        'No categories yet -- nothing will print.'


@bp.route('/listener/settings/<name>', methods=['GET', 'POST'])
def listener_settings(name):
    """Render and save any registered listener's settings from its schema.

    There is no per-listener code in this handler, and adding a listener must
    not require any. `parse_form` unpacks the structured field types and
    `save_config` validates against the schema, so a bad value produces a
    message naming the field rather than a 500 (P2-11).
    """
    listener_obj = listener_base.get(name)
    if listener_obj is None:
        abort(404)

    error = None
    if request.method == 'POST':
        try:
            listener_obj.save_config(listener_obj.parse_form(request.form))
        except ValueError as e:
            error = str(e)
        else:
            return redirect(url_for('main.listener_settings', name=name))

    return render_template(
        'listener_settings.html', config_obj=state.config, listener=listener_obj,
        config=listener_obj.config(), runtime=state.listeners.get(name) or {},
        error=error)


@bp.route('/listener/scf', methods=['GET', 'POST'])
def listener_scf():
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

        # Filters go through the schema coercer and then SCF's own rules --
        # notably that a keyword needs an area, because searching all of
        # SeeClickFix does not return before the timeout.
        try:
            # A field the submission does not carry keeps its stored value. An
            # older client, or a form saved before a filter existed, must not
            # silently clear it -- and for a multiselect "absent" and "all
            # unchecked" are the same on the wire, which is why the macro emits
            # a hidden marker for it.
            filters = scf.get_filters(existing)
            for spec in scf.FILTER_SCHEMA:
                key = spec['key']
                if key not in request.form:
                    continue
                filters[key] = listener_base.coerce_field(spec, request.form.get(key))
            scf.validate_filters(filters)
        except ValueError as e:
            return forms.reject(str(e))

        updated = dict(existing)
        updated.update({
            'enabled': 'enabled' in request.form,
            'request_types': request_types,
            'interval': interval,
            'last_check': existing.get('last_check'),  # Preserve existing last_check
            **filters,
        })
        state.listeners['scf'] = updated
        storage.save_listeners()
        return redirect(url_for('main.listener_scf'))

    scf_config = state.listeners.get('scf', {})
    # Names come from the cache; a lookup only happens for ids never seen
    # before, so opening this page does not depend on the network.
    try:
        described = scf.describe_request_types(scf_config.get('request_types', ''))
    except Exception as e:
        log.warning(f"Could not describe request types: {e}")
        described = []
    return render_template('listener_scf.html', config=state.config, scf=scf_config,
                           request_types=described,
                           filter_schema=scf.FILTER_SCHEMA,
                           filters=scf.get_filters(scf_config),
                           scf_listener=listener_base.Listener())


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
    if kind not in styles.kinds():
        kind = 'task'
    name = request.args.get('name') or styles.active_template_name(kind)
    template = styles.get_template(kind, name)
    return render_template(
        'receipt_studio.html',
        config=state.config,
        kind=kind,
        kinds=styles.kinds(),
        kind_label=styles.kind_label,
        template=template,
        templates=styles.list_templates(kind),
        active=styles.active_template_name(kind),
        placeholders=sorted(styles.placeholders(kind)),
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
    if kind not in styles.kinds():
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
    if kind not in styles.kinds():
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

@bp.route('/c/<token>')
def chore_done(token):
    """Mark a chore chart done. This is what the printed QR points at (P5-2 #12).

    Deliberately unauthenticated beyond the token in the URL: it is scanned
    from paper by a child on a home LAN, and demanding a password there means
    the feature is never used. Marking done is idempotent and non-destructive,
    so the worst outcome is a chore ticked off by someone else on your network.

    A short URL (/c/...) because it becomes a QR code, and every character adds
    modules to the symbol.
    """
    from .. import chores
    person = chores.by_token(token)
    if person is None:
        return render_template('chore_done.html', config=state.config,
                               person=None), 404

    already = chores.done_today(person)
    if not already:
        chores.mark_done(person['id'])
        person = chores.get_person(person['id'])
        log.info(f"Chore chart marked done: {person.get('name')}")
    return render_template('chore_done.html', config=state.config, person=person,
                           already=already, streak=chores.streak(person),
                           best=chores.best_streak(person))


@bp.route('/chores')
def chore_charts():
    from .. import chores
    people = [dict(p, streak=chores.streak(p), best=chores.best_streak(p),
                   done_today=chores.done_today(p),
                   done_url=chores.done_url(p))
              for p in chores.load_people()]
    return render_template('chores.html', config=state.config, people=people,
                           weekdays=chores.WEEKDAYS)


@bp.route('/lists')
def checklists():
    """Checklists (P5-2 #11). A mini-app rather than a listener: nothing polls
    and nothing fires on a schedule."""
    from .. import lists
    return render_template('lists.html', config=state.config,
                           lists=lists.all_lists())


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


@bp.route('/api/history/reprint/<uid>', methods=['POST'])
def api_history_reprint(uid):
    """Reprint a history record (P4-6).

    Addressed by `uid`, a handle assigned when the record is written and
    back-filled on load for older records. Not by list position, which stops
    being an identity the moment the table is filtered, searched, paged or
    trimmed by max_history; and not by the record's own id, because the three
    record types draw ids from different namespaces and can collide.

    Re-rendered from today's active template rather than replayed from stored
    blocks, because the reason to reprint is usually that the receipt was torn,
    smudged or lost -- you want the current layout, not a byte-perfect copy of
    a receipt printed under settings that have since changed. (The print queue
    is the opposite case and stores blocks for exactly that reason.)
    """
    with state.STATE_LOCK:
        match = next((r for r in state.history if r.get('uid') == uid), None)
    if match is None:
        return {'ok': False, 'error': 'That history entry no longer exists.'}, 404
    record = dict(match)

    try:
        blocks = reprint_blocks(record)
    except Exception as e:
        log.error(f"Could not rebuild receipt for reprint: {e}", exc_info=True)
        return {'ok': False, 'error': 'That receipt could not be rebuilt.'}, 500

    if printing.print_blocks(blocks):
        # Deliberately not recorded in history. History is the record of
        # scheduled and triggered prints; a reprint of an existing row would
        # make the list grow every time someone re-ran one, and the second copy
        # would look like a second occurrence.
        log.info(f"Reprinted history entry: {pagination.history_title(record)}")
        return {'ok': True}
    return {'ok': False, 'error': 'Printer not connected.'}, 503


def reprint_blocks(record):
    """Rebuild a receipt from a history record, whatever kind it is."""
    kind = record.get('type', 'task')
    if kind == 'task':
        return printing.task_blocks(record)

    listener = listener_base.get(kind)
    if listener is not None and kind in styles.kinds():
        template = styles.get_template(kind, styles.active_template_name(kind))
        # A history record is a projection of the item, not the item itself, so
        # it is filled directly rather than through listener.context().
        return styles.fill(template, styles.sample_context(kind, record))

    # SCF predates the plugin interface and keeps its own projection.
    #
    # History stores reported_at raw, as the API returned it, while the
    # receipt shows it formatted -- so it has to go back through the same
    # formatter or the reprint reads '2025-08-26T13:36:42Z' where the original
    # said '9:36 AM, 08/26/2025'.
    return printing.scf_blocks(
        {'id': record.get('id'), 'html_url': record.get('url', '')},
        category=record.get('category', ''), address=record.get('address', ''),
        status=record.get('status', ''),
        reported_at=printing.scf_reported_at({'created_at': record.get('reported_at')}),
        has_media=record.get('has_media', False),
        has_video=record.get('has_video', False),
        description=record.get('description', ''))


@bp.route('/api/listeners/<name>/poll', methods=['POST'])
def api_listener_poll(name):
    """Poll a listener right now instead of waiting for its interval (P4-6).

    Makes the whole pipeline debuggable: configure, press the button, see
    whether anything comes out and what the log says. Without this the only
    way to test a listener is to wait, which for NWS means waiting for weather.
    """
    listener = listener_base.get(name)
    if listener is None:
        return {'ok': False, 'error': 'Unknown listener.'}, 404
    if not listener.enabled():
        return {'ok': False, 'error': f'{listener.title} is switched off.'}, 400

    # Clear the interval gate for this one call, so "poll now" means now.
    runtime = listener.state()
    saved = runtime.pop('last_check', None), runtime.pop('backoff_until', None)
    try:
        printed = listener_base.run(listener, datetime.now(timezone.utc))
    except Exception as e:
        log.error(f"Manual poll of {name} failed: {e}", exc_info=True)
        runtime['last_check'], runtime['backoff_until'] = saved
        return {'ok': False, 'error': str(e)}, 502
    return {'ok': True, 'printed': printed,
            'last_error': runtime.get('last_error')}


@bp.route('/api/queue/<job_id>', methods=['DELETE'])
def api_queue_discard(job_id):
    removed = queue.discard(None if job_id == 'all' else job_id)
    return {'ok': True, 'removed': removed}
