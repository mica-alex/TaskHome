"""The listener plugin interface (MASTER_PLAN P5-1).

Adding a listener used to mean editing five places: a config blob, a hardcoded
branch in the scheduler loop, a bespoke print function, and the settings page's
GET and POST. This replaces all of it with one class.

A listener declares what it needs (`CONFIG_SCHEMA`), fetches items (`poll`),
identifies them (`dedup_key`) and describes them for a receipt (`context`).
Everything else -- interval gating, watermarks, dedup, backoff, per-poll caps,
the settings form, receipt templates -- is provided.

The schema is the important part. `D-7` commits this project to being highly
configurable, and that is only sustainable if a new setting costs a schema
entry rather than a hand-written form. If a listener needs bespoke settings
markup, the schema is missing a field type.
"""
from datetime import timedelta

from .. import state, storage
from ..logsetup import log

#: Field types the settings renderer understands. `matrix` is the one that
#: makes P5-3's per-event-type configuration possible without a bespoke page.
FIELD_TYPES = ('bool', 'int', 'text', 'secret', 'select', 'multiselect',
               'duration', 'time_range', 'matrix')


def field(key, label, type='text', default=None, help='', group=None,
          depends_on=None, **extra):
    """Declare one setting.

    `group` collects related fields under a heading; `depends_on` hides a field
    until another is set, which is how a long settings page stays short for
    someone who only wants the common options (X-4's progressive disclosure).
    """
    if type not in FIELD_TYPES:
        raise ValueError(f'Unknown field type {type!r}')
    spec = {'key': key, 'label': label, 'type': type, 'default': default,
            'help': help, 'group': group, 'depends_on': depends_on}
    spec.update(extra)
    return spec


class Listener:
    """Base class. Subclasses set the class attributes and implement poll()."""

    name = ''                 # 'scf'
    title = ''                # 'SeeClickFix'
    description = ''
    CONFIG_SCHEMA = ()
    #: Placeholders a receipt template for this listener may use, with sample
    #: values for previewing.
    PLACEHOLDERS = {}
    default_interval = 10
    max_prints_per_poll = 20

    # --- to implement --------------------------------------------------------

    def poll(self, config, since):
        """Return items created after `since`. Raise to trigger backoff.

        Must not print, save, or dedup -- the runtime does all of that. A
        listener that prints directly cannot be capped, queued or retried.
        """
        raise NotImplementedError

    def dedup_key(self, item):
        """Stable identity for an item, so overlapping windows are harmless."""
        return str(item.get('id'))

    def context(self, item):
        """Placeholder values for the receipt template."""
        raise NotImplementedError

    def sort_key(self, item):
        """Oldest first, so a backlog prints in the order it happened."""
        return item.get('created_at') or ''

    def describe(self, item):
        """A one-line summary for logs and the print queue."""
        return f'{self.title} {self.dedup_key(item)}'

    # --- provided ------------------------------------------------------------

    def config(self):
        """This listener's stored config, merged over the schema defaults.

        Merged rather than replaced, for the same reason the app config is: a
        stored blob missing a key must not break code that reads it (P1-6).
        """
        stored = state.listeners.get(self.name)
        merged = {spec['key']: spec['default'] for spec in self.CONFIG_SCHEMA}
        merged.setdefault('enabled', False)
        merged.setdefault('interval', self.default_interval)
        if isinstance(stored, dict):
            merged.update(stored)
        return merged

    def save_config(self, values):
        """Validate against the schema and store."""
        config = dict(state.listeners.get(self.name) or {})
        config.update(self.validate(values))
        state.listeners[self.name] = config
        storage.save_listeners()
        return config

    def validate(self, values):
        """Coerce submitted values using the schema. Raises ValueError."""
        clean = {}
        for spec in self.CONFIG_SCHEMA:
            key = spec['key']
            if key not in values:
                continue
            clean[key] = coerce_field(spec, values[key])
        return clean

    def enabled(self):
        return bool(self.config().get('enabled'))

    def interval_minutes(self):
        try:
            return max(int(self.config().get('interval', self.default_interval)), 1)
        except (TypeError, ValueError):
            return self.default_interval

    def state(self):
        """Runtime state: watermark, seen keys, backoff. Never user-edited."""
        return state.listeners.setdefault(self.name, {})


def coerce_field(spec, value):
    """Turn a submitted value into the type the schema declares.

    Errors carry the field's label rather than its key, because the message is
    shown to whoever is filling the form.
    """
    kind = spec['type']
    label = spec.get('label', spec['key'])

    if kind == 'bool':
        return bool(value) and value not in ('false', '0', '', 'off')
    if kind == 'int':
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f'{label} must be a number.')
        low, high = spec.get('min'), spec.get('max')
        if low is not None and number < low:
            raise ValueError(f'{label} must be at least {low}.')
        if high is not None and number > high:
            raise ValueError(f'{label} must be at most {high}.')
        return number
    if kind == 'select':
        options = [o[0] if isinstance(o, (list, tuple)) else o
                   for o in spec.get('options', ())]
        if options and value not in options:
            raise ValueError(f'{value!r} is not a valid {label}.')
        return value
    if kind == 'multiselect':
        items = value if isinstance(value, list) else [
            v.strip() for v in str(value).split(',') if v.strip()]
        return items
    if kind == 'matrix':
        if not isinstance(value, dict):
            raise ValueError(f'{label} must be an object.')
        return value
    return str(value).strip()


# --- runtime ------------------------------------------------------------------

_REGISTRY = {}


def register(listener):
    _REGISTRY[listener.name] = listener
    return listener


def registry():
    return dict(_REGISTRY)


def get(name):
    return _REGISTRY.get(name)


def parse_utc(value, default=None):
    from .scf import parse_utc as _parse
    return _parse(value, default)


def run(listener, now_utc, printer=None):
    """Poll one listener and print what is new. Returns the number printed.

    This is every behaviour the SCF listener had to grow the hard way -- the
    interval gate, the pre-fetch watermark, id dedup, the per-poll cap, backoff
    that does not advance the watermark -- provided once so the next listener
    does not have to rediscover them.
    """
    from .. import printing, queue, styles

    config = listener.config()
    if not config.get('enabled'):
        return 0

    runtime = listener.state()
    backoff_until = parse_utc(runtime.get('backoff_until'))
    if backoff_until and now_utc < backoff_until:
        return 0

    last_check = parse_utc(runtime.get('last_check'))
    if last_check and (now_utc - last_check) < timedelta(minutes=listener.interval_minutes()):
        return 0

    # Taken BEFORE the request: a watermark stamped afterwards skips anything
    # created while the fetch was in flight.
    watermark = now_utc
    since = last_check or (now_utc - timedelta(hours=1))

    try:
        items = listener.poll(config, since)
    except Exception as e:
        failures = runtime.get('consecutive_failures', 0) + 1
        delay = min(2 ** min(failures, 6), 60)
        runtime['consecutive_failures'] = failures
        runtime['backoff_until'] = (now_utc + timedelta(minutes=delay)).strftime(
            '%Y-%m-%dT%H:%M:%SZ')
        runtime['last_error'] = str(e)
        storage.save_listeners()
        log.error(f"{listener.title} poll failed ({failures}), retrying in {delay}m: {e}")
        return 0

    seen = runtime.get('seen') or []
    seen_set = set(seen)
    fresh = []
    for item in items:
        key = listener.dedup_key(item)
        if key is None or key in seen_set:
            continue
        seen_set.add(key)
        fresh.append(item)
    fresh.sort(key=listener.sort_key)

    cap = config.get('max_prints_per_poll', listener.max_prints_per_poll)
    try:
        cap = max(int(cap), 0)
    except (TypeError, ValueError):
        cap = listener.max_prints_per_poll
    suppressed = []
    if len(fresh) > cap:
        suppressed, fresh = fresh[:-cap] if cap else fresh, fresh[-cap:] if cap else []
        log.warning(f"{listener.title}: capped at {cap}, suppressing {len(suppressed)}")

    template = styles.get_template(listener.name, styles.active_template_name(listener.name)) \
        if listener.name in styles.KINDS else None

    printed = 0
    for item in fresh:
        blocks = (styles.fill(template, listener.context(item)) if template
                  else listener.receipt_blocks(item))
        if printing.print_blocks(blocks):
            printing.record_history(listener.history_record(item))
            seen.append(listener.dedup_key(item))
            printed += 1
        else:
            # Queued rather than lost: the polling window has already moved on
            # (P6-3).
            queue.enqueue(listener.name, blocks,
                          description=listener.describe(item),
                          history=listener.history_record(item))
            seen.append(listener.dedup_key(item))
            printed += 1

    for item in suppressed:
        seen.append(listener.dedup_key(item))

    del seen[:-2000]
    runtime['seen'] = seen
    runtime['last_check'] = watermark.strftime('%Y-%m-%dT%H:%M:%SZ')
    runtime['consecutive_failures'] = 0
    runtime.pop('backoff_until', None)
    runtime.pop('last_error', None)
    storage.save_listeners()
    log.info(f"{listener.title}: {len(items)} fetched, {len(fresh)} new, {printed} printed")
    return printed


def run_all(now_utc):
    total = 0
    for listener in _REGISTRY.values():
        try:
            total += run(listener, now_utc)
        except Exception as e:
            # One broken listener must not stop the others, for the same reason
            # one broken task must not stall the scheduler (P0-6).
            log.error(f"Listener {listener.name} failed: {e}", exc_info=True)
    return total
