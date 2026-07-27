"""The ESC/POS layer.

print_task and print_scf_issue return True only if paper actually came out.
That return value is load-bearing: the scheduler advances a schedule only on
success (P0-4), the listener marks an issue seen only on success, and the
test-print routes report the real outcome (P0-10).

Layouts live in layouts.py as block lists rendered by receipt.py, so the
preview and the printer share one definition (P3-2).
"""
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime

import usb.core
from escpos.printer import Usb

from . import constants, layouts, receipt, settings, state, storage, styles
from .logsetup import log


def is_printer_connected():
    try:
        dev = usb.core.find(idVendor=constants.VID, idProduct=constants.PID)
        return dev is not None
    except Exception as e:
        log.error(f"USB detection error: {e}")
        return False


@contextmanager
def open_printer():
    """Open the printer, guaranteeing the USB handle is released (P0-11).

    The previous code only called close() on the success path, so any
    exception mid-receipt leaked the claimed interface; enough of those and
    the device stops opening until it is physically replugged.
    """
    printer = Usb(constants.VID, constants.PID, profile='TM-T20II')
    try:
        yield printer
    finally:
        try:
            printer.close()
        except Exception as e:  # closing must never mask the original error
            log.warning(f"Error closing printer: {e}")


def record_history(record):
    """Prepend a print record and trim to the configured cap."""
    record.setdefault('uid', uuid.uuid4().hex)
    with state.STATE_LOCK:
        state.history.insert(0, record)
        max_history = state.config.get('max_history', constants.DEFAULT_CONFIG['max_history'])
        try:
            max_history = int(max_history)
        except (TypeError, ValueError):
            max_history = constants.DEFAULT_CONFIG['max_history']
        del state.history[max(max_history, 0):]
    storage.save_history()


def task_qr_url(task):
    """QR target for a task: its own url if set, else a deep link to the app."""
    hostname = state.config.get('hostname', constants.DEFAULT_CONFIG['hostname'])
    return task.get('url', '') or f"http://{hostname}:{settings.get_port()}/task_page#{task['id']}"


def task_blocks(task, when=None):
    """Blocks for a task receipt, from whichever template is active.

    Always goes through the template layer, even for the default: the built-in
    preset is generated from layouts.py, so this is the same output either way,
    and having one path means an edited template cannot behave differently from
    the shipped one in some subtle way.
    """
    context = {
        'title': task.get('title', ''),
        'extra': task.get('extra', ''),
        'recurrence': layouts.recurrence_label(task.get('recurring', 'none')),
        'printed': layouts._stamp(when),
        'id': layouts._short_id(task.get('id', '')),
        'qr_url': task_qr_url(task),
    }
    template = styles.get_template('task', styles.active_template_name('task'))
    return styles.fill(template, context)


def scf_image_url(issue):
    """The photo to print, or '' when there is none or photos are off.

    Resolved here rather than in the template so that "photos are switched
    off" and "this issue has no photo" produce the same empty placeholder --
    fill() then drops the block entirely and leaves no gap.
    """
    from .listeners import scf as scf_listener
    if not scf_listener.get_filters().get('print_photos'):
        return ''
    media = issue.get('media')
    if not isinstance(media, dict):
        return ''
    return media.get('image_full') or ''


def scf_blocks(issue, category, address, reported_at, status, has_media,
               has_video=False, description='', when=None):
    """Blocks for an SCF receipt, from whichever template is active."""
    context = {
        'media_url': scf_image_url(issue),
        'category': category,
        'address': address,
        'status': status,
        'reported': reported_at,
        'media': layouts.media_label(has_media, has_video),
        'description': description,
        'id': str(issue.get('id', '?')),
        'printed': layouts._stamp(when),
        'qr_url': issue.get('html_url') or '',
    }
    template = styles.get_template('scf', styles.active_template_name('scf'))
    return styles.fill(template, context)


#: One receipt at a time.
#:
#: There are now several threads that can print: the scheduler (due tasks and
#: the queue drain), a web request (test print, reprint, "print now", a list),
#: and a push listener's own network thread (MQTT delivers on paho's loop
#: thread). Two of them opening the same USB device at once produces
#: interleaved bytes -- half of one receipt inside another -- or a claim
#: failure that leaks the interface (P0-11).
#:
#: The lock is held for the whole receipt, not per write, because a receipt is
#: the atomic unit here. Waiting a few seconds behind another print is fine;
#: sharing paper with it is not.
PRINT_LOCK = threading.Lock()


def print_blocks(blocks):
    """Put rendered blocks on paper. Returns True only if they came out.

    The lowest level of the print path: no history, no queueing, no layout --
    just the device. The queue drains through this, which is what lets a
    queued job be retried without re-rendering it against settings that may
    have changed since (P6-3).

    Serialised across threads -- see PRINT_LOCK.
    """
    with PRINT_LOCK:
        if not is_printer_connected():
            return False
        try:
            with open_printer() as p:
                receipt.render_escpos(blocks, p)
                p.cut()
            return True
        except Exception as e:
            log.error(f"Print error: {e}", exc_info=True)
            return False


def print_task(task):
    """Print a task receipt. Returns True only if paper actually came out.

    The layout lives in layouts.task_receipt as a list of blocks, rendered by
    receipt.render_escpos -- the same definition the preview uses, so what is
    previewed is what prints (P3-2).

    The return value matters: the scheduler must not advance a task's schedule
    for a print that never happened (P0-4), and the test-print routes must not
    claim success when the print failed (P0-10).
    """
    try:
        blocks = task_blocks(task)
    except Exception as e:
        log.error(f"Could not build the task receipt: {e}", exc_info=True)
        return False

    history = {**task, 'print_time': datetime.now().isoformat(), 'type': 'task'}

    if print_blocks(blocks):
        # Only recorded once the receipt is out and the handle is closed.
        record_history(history)
        return True

    # Deliberately NOT queued, unlike an SCF or NWS receipt.
    #
    # A task already has a durable retry: the scheduler leaves next_time alone
    # on failure (P0-4) and save_tasks() persists that, so the occurrence is
    # still due after a restart. Queueing as well would give it two retry
    # mechanisms, and when the printer came back both would fire -- the queue
    # would drain the receipt and the still-due task would print it again.
    #
    # The queue exists for items with nowhere else to live: a listener's
    # polling window has already moved past an issue by the time the print
    # fails, so without the queue it is simply gone.
    return False


def scf_has_video(issue):
    """Whether an issue carries a video. The API exposes video_url alongside
    the image fields; it was previously ignored entirely."""
    media = issue.get('media')
    return bool(isinstance(media, dict) and media.get('video_url'))


def scf_has_media(issue):
    """Whether an issue carries a full-size image.

    `media` may be absent, null, or present-without-image_full depending on
    the issue; indexing it blindly raised mid-receipt, wasting paper on a
    half-printed job (P0-8).
    """
    media = issue.get('media')
    if not isinstance(media, dict):
        return False
    return bool(media.get('image_full'))


def scf_category(issue):
    request_type = issue.get('request_type')
    if isinstance(request_type, dict) and request_type.get('title'):
        return request_type['title']
    return 'Unknown Category'


def scf_reported_at(issue):
    created = issue.get('created_at')
    if not created:
        return 'Unknown'
    try:
        return datetime.fromisoformat(created.replace('Z', '+00:00')).strftime('%I:%M %p, %m/%d/%Y')
    except (ValueError, AttributeError):
        log.warning(f"Unparseable SCF created_at {created!r}")
        return str(created)


def print_scf_issue(issue):  # New: Custom print for SCF issues
    """Print an SCF issue receipt. Returns True only if it actually printed.

    The layout lives in layouts.scf_receipt; see print_task for why.
    """
    from . import queue

    # Resolve every field BEFORE rendering, so a malformed payload fails
    # without wasting paper on a partial receipt (P0-8).
    category = scf_category(issue)
    address = issue.get('address', 'Unknown Location')
    reported_at = scf_reported_at(issue)
    status = issue.get('status', 'Unknown')
    has_media = scf_has_media(issue)
    has_video = scf_has_video(issue)
    html_url = issue.get('html_url', '')
    issue_id = issue.get('id', 'unknown')
    description = issue.get('description') or ''

    try:
        blocks = scf_blocks(
            issue, category=category, address=address, reported_at=reported_at,
            status=status, has_media=has_media, has_video=has_video,
            description=description)
    except Exception as e:
        log.error(f"Could not build the SCF receipt: {e}", exc_info=True)
        return False

    history = {
        'type': 'scf',
        'id': issue_id,
        'category': category,
        'summary': issue.get('summary', ''),
        'address': address,
        'reported_at': issue.get('created_at', ''),
        'status': status,
        'description': description,
        'url': html_url,
        'has_media': has_media,
        'has_video': has_video,
        'print_time': datetime.now().isoformat(),
    }

    if print_blocks(blocks):
        record_history(history)
        return True

    # Queued rather than lost (P6-3). This matters more here than for tasks:
    # a task stays due and retries next tick, but the listener's window has
    # already moved past this issue.
    queue.enqueue('scf', blocks, description=f'{category} #{issue_id}',
                  history=history)
    return False
