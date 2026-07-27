"""Default receipt layouts, as data.

Each function returns a list of blocks (see receipt.py) — no ESC/POS calls, so
the same definition drives the printer and the preview. When MASTER_PLAN `P3-1`
makes layouts user-editable, these become the shipped presets rather than being
rewritten.

The `legacy_*` variants are kept deliberately: they are what the receipts used
to look like, and having both means a layout change can be justified with a
measured height difference instead of an assertion.
"""
from datetime import datetime

from . import receipt as r


def _stamp(when=None):
    return (when or datetime.now()).strftime('%-I:%M %p %-m/%-d/%y')


# Recurrence labels. `recurring.capitalize()` produced "Every_weekday" and
# "First_day_month"; these read as English (MASTER_PLAN P2-10).
RECURRENCE_LABELS = {
    'none': 'One-off',
    'daily': 'Daily',
    'weekly': 'Weekly',
    'monthly': 'Monthly',
    'every_weekday': 'Every weekday',
    'first_day_month': 'First of month',
    'custom': 'Custom days',
}


def recurrence_label(recurring):
    return RECURRENCE_LABELS.get(recurring) or str(recurring).replace('_', ' ').capitalize()


def media_label(has_photo, has_video=False):
    """Describe attached media without inventing a count.

    The SCF v2 API exposes a single representative image and an optional
    video URL -- there is no list and no count -- so "1 Photo" would be a
    guess. An issue may well have several photos and we would only ever see
    one of them. Naming the kind is the most we can honestly say.
    """
    if has_photo and has_video:
        return 'Photo & Video'
    if has_photo:
        return 'Photo'
    if has_video:
        return 'Video'
    return ''


def _short_id(value):
    """First segment of a UUID. The full value is in the QR; a 36-character
    line of hex on a 48-column receipt is a whole wasted line for something
    nobody reads off paper."""
    return str(value).split('-')[0]


# --- task reminders -----------------------------------------------------------

def task_receipt(task, qr_url, when=None):
    """Compact task reminder.

    Design notes, since the changes are deliberate rather than cosmetic:

    * QR at size 4 rather than 5. It still scans comfortably at ~15 mm; size 5
      spent about a quarter of the receipt on the code alone.
    * Title at 2x rather than 3x. On a 48-column head, 3x leaves only 16
      usable columns, so anything but a very short title wrapped onto a second
      triple-height line — the single biggest source of wasted paper.
    * The metadata that used to occupy four lines (blank, printed-at, blank,
      task-type, task-id) is now one line, because font b fits 64 columns and
      none of it needs to be large.
    * The task id is shortened. It exists for cross-reference; the full value
      is already encoded in the QR.
    * Line spacing is computed per block rather than left at the printer
      default, which was shorter than a double-height character cell and so
      printed the following line into the title's descenders. A small gap
      under the title supplies the visual separation on top of that.
    """
    blocks = [r.qr(qr_url, size=4)]
    blocks.append(r.text(task.get('title', ''), font='a', width=2, height=2,
                         bold=True))
    # A partial line under the title. The per-block leading already stops the
    # next line printing into the descenders; this is the visual separation
    # that makes the title read as a heading rather than as run-on text.
    blocks.append(r.gap(6))
    extra = task.get('extra')
    if extra:
        blocks.append(r.text(extra, font='a', width=1, height=1))

    kind = recurrence_label(task.get('recurring', 'none'))
    blocks.append(r.text(
        f"{kind}  -  Printed {_stamp(when)}  -  {_short_id(task.get('id', ''))}",
        font='b', width=1, height=1))
    return blocks


def legacy_task_receipt(task, qr_url, when=None):
    """The previous layout, for height comparison."""
    recurring = task.get('recurring', 'none')
    task_type = ('Non-recurring' if recurring == 'none'
                 else f'Recurring ({recurring.capitalize()})')
    blocks = [
        r.qr(qr_url, size=5),
        r.text(task.get('title', ''), font='a', width=3, height=3, bold=True),
    ]
    if task.get('extra'):
        blocks.append(r.blank())
        blocks.append(r.text(task['extra'], font='b', width=2, height=2))
    blocks.append(r.blank())
    blocks.append(r.text(f'Printed at {_stamp(when)}', font='b'))
    blocks.append(r.blank())
    blocks.append(r.text(f'Task Type: {task_type}', font='b'))
    blocks.append(r.text(f"Task ID: {task.get('id', '')}", font='b'))
    return blocks


# --- SeeClickFix issues -------------------------------------------------------

def scf_receipt(issue, category, address, reported_at, status, has_media,
                description, when=None, has_video=False):
    """Compact SCF issue.

    * The CODE39 barcode is gone. It cost roughly 10 mm — symbol plus its text
      label — and the issue id is both printed below and encoded in the QR, so
      nothing is lost but height.
    * Four labelled lines (Location/Reported/Status/Has Media) collapse into
      two: the address reads fine without a "Location:" prefix, and status,
      time and media fit together on one 64-column line.
    * Category at 2x for the same reason as the task title.
    """
    blocks = [r.qr(issue.get('html_url') or '', size=4)]
    blocks.append(r.text(category, font='a', width=2, height=2, bold=True))
    blocks.append(r.gap(6))
    blocks.append(r.text(address, font='b', width=1, height=1))

    facts = f'{status}  -  {reported_at}'
    media = media_label(has_media, has_video)
    if media:
        facts += f'  -  {media}'
    blocks.append(r.text(facts, font='b'))

    if description:
        blocks.append(r.rule())
        blocks.append(r.text(description, font='b', align='left'))

    blocks.append(r.rule())
    blocks.append(r.text(f"#{issue.get('id', '?')}  -  Printed {_stamp(when)}",
                         font='b'))
    return blocks


def legacy_scf_receipt(issue, category, address, reported_at, status, has_media,
                       description, when=None):
    """The previous layout, for height comparison."""
    blocks = [
        r.qr(issue.get('html_url') or '', size=5),
        r.text(category, font='a', width=3, height=3, bold=True),
        r.blank(),
        r.text(f'Location: {address}', font='b'),
        r.text(f'Reported: {reported_at}', font='b'),
        r.text(f'Status: {status}', font='b'),
        r.text(f"Has Media: {'Yes' if has_media else 'No'}", font='b'),
    ]
    if description:
        blocks.append(r.text('\nDescription:', font='b'))
        blocks.append(r.text(description, font='b'))
    blocks.append(r.blank())
    blocks.append(r.text(f'Printed at {_stamp(when)}', font='b'))
    blocks.append(r.barcode(issue.get('id', ''), height=60))
    return blocks
