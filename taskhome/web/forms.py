"""Form parsing and validation (P0-9).

Unvalidated form data used to reach the datastore directly: int() on arbitrary
strings returned a 500 and a missing field raised KeyError. Everything here
raises ValidationError with a message fit to show a user.
"""
import uuid
from datetime import datetime

from flask import render_template

from .. import constants, recurrence
from ..logsetup import log


class ValidationError(Exception):
    """A form submission was rejected. Carries a user-facing message."""


def normalize_next_time(raw, fallback=None):
    """Turn a datetime-local form value into a stored naive-local ISO string.

    The form yields 'YYYY-MM-DDTHH:MM'; seconds were previously appended
    blindly, producing '...T21:00:00:00' when the browser already included
    them. Parse first, then re-serialise from the parsed value, so the stored
    form is always canonical regardless of what the browser sent (P0-9).
    """
    raw = (raw or '').strip()
    if not raw:
        if fallback is not None:
            return fallback
        return datetime.now().replace(microsecond=0).isoformat()
    try:
        return recurrence.parse_task_time(raw).isoformat()
    except (ValueError, TypeError):
        raise ValidationError(f"'{raw}' is not a valid date and time.")


def parse_days(raw_days):
    """Weekday indices from the form, deduped and ordered. 0=Mon .. 6=Sun."""
    days = set()
    for value in raw_days:
        try:
            day = int(value)
        except (TypeError, ValueError):
            raise ValidationError(f"'{value}' is not a valid weekday.")
        if not 0 <= day <= 6:
            raise ValidationError(f"Weekday {day} is out of range.")
        days.add(day)
    return sorted(days)


def task_from_form(form, existing=None):
    """Build or update a task from form data, validating as we go.

    Raises ValidationError with a message suitable for showing the user. The
    task dict is only mutated once every field has validated, so a rejected
    edit can't leave a task half-updated.
    """
    title = (form.get('title') or '').strip()
    if not title:
        raise ValidationError('Title is required.')

    recurring = form.get('recurring') or 'none'
    if recurring not in constants.RECURRENCE_MODES:
        raise ValidationError(f"'{recurring}' is not a valid recurrence.")

    next_time = normalize_next_time(
        form.get('next_time'), fallback=existing['next_time'] if existing else None)

    days = parse_days(form.getlist('days')) if recurring == 'custom' else None
    if recurring == 'custom' and not days:
        # Without this the schedule can never advance (P0-2).
        raise ValidationError('Pick at least one weekday for a custom recurrence.')

    task = existing if existing is not None else {'id': str(uuid.uuid4())}
    task['title'] = title
    task['next_time'] = next_time
    task['recurring'] = recurring
    task['enabled'] = 'enabled' in form

    for field in ('extra', 'url'):
        value = (form.get(field) or '').strip()
        if value:
            task[field] = value
        else:
            task.pop(field, None)

    if days:
        task['days'] = days
    else:
        task.pop('days', None)

    # A successful edit clears any prior failure state, since the user has
    # just told us what the schedule should be.
    task.pop('schedule_error', None)
    task.pop('missed', None)
    return task


class JsonForm:
    """Make a JSON body look like a submitted form (P2-3).

    So the API and the HTML forms share one validator. Two behaviours have to
    be reconciled:

    * `getlist` -- a form repeats a key, JSON uses a list.
    * `in` -- an HTML checkbox is *absent* when unchecked, so the form code
      tests `'enabled' in form`. JSON sends `{"enabled": false}`, where the key
      is present and the naive test would read it as True. Membership here
      therefore means "present and truthy", which is identical for a form
      (a checked box always sends a truthy value) and correct for JSON.
    """

    def __init__(self, data):
        self._data = data if isinstance(data, dict) else {}

    def get(self, key, default=None):
        value = self._data.get(key, default)
        return value if value is None or isinstance(value, str) else str(value)

    def getlist(self, key):
        value = self._data.get(key)
        if value is None:
            return []
        return list(value) if isinstance(value, (list, tuple)) else [value]

    def __contains__(self, key):
        return bool(self._data.get(key))


def reject(message, status=400):
    """Render a validation failure. Kept plain until P2-4 brings in toasts."""
    log.info(f"Rejected form submission: {message}")
    return render_template('error.html', message=message), status
