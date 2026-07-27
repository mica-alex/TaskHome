"""The ESC/POS layer.

print_task and print_scf_issue return True only if paper actually came out.
That return value is load-bearing: the scheduler advances a schedule only on
success (P0-4), the listener marks an issue seen only on success, and the
test-print routes report the real outcome (P0-10).

Layouts live in layouts.py as block lists rendered by receipt.py, so the
preview and the printer share one definition (P3-2).
"""
from contextlib import contextmanager
from datetime import datetime

import usb.core
from escpos.printer import Usb

from . import constants, layouts, receipt, settings, state, storage
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


def print_task(task):
    """Print a task receipt. Returns True only if paper actually came out.

    The layout lives in layouts.task_receipt as a list of blocks, rendered by
    receipt.render_escpos -- the same definition the preview uses, so what is
    previewed is what prints (P3-2).

    The return value matters: the scheduler must not advance a task's schedule
    for a print that never happened (P0-4), and the test-print routes must not
    claim success when the print failed (P0-10).
    """
    if not is_printer_connected():
        log.warning("Printer not connected, skipping print")
        return False
    try:
        blocks = layouts.task_receipt(task, task_qr_url(task))
        with open_printer() as p:
            receipt.render_escpos(blocks, p)
            p.cut()

        # Only recorded once the receipt is out and the handle is closed.
        record_history({**task, 'print_time': datetime.now().isoformat(), 'type': 'task'})
        return True
    except Exception as e:
        log.error(f"Print error: {e}", exc_info=True)
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
    if not is_printer_connected():
        log.warning("Printer not connected, skipping SCF issue print")
        return False

    # Resolve every field BEFORE opening the printer, so a malformed payload
    # fails without wasting paper on a partial receipt (P0-8).
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
        blocks = layouts.scf_receipt(
            issue, category=category, address=address, reported_at=reported_at,
            status=status, has_media=has_media, description=description,
            has_video=has_video)
        with open_printer() as p:
            receipt.render_escpos(blocks, p)
            p.cut()

        # Add to state.history
        record_history({
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
            'print_time': datetime.now().isoformat()
        })
        return True
    except Exception as e:
        log.error(f"SCF issue print error: {e}", exc_info=True)
        return False
