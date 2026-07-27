"""User-editable receipt templates (MASTER_PLAN P3-1).

A template is the same block list `receipt.py` already renders, with
`{placeholder}` markers in the text. That is deliberate: inventing a templating
DSL would mean a second thing that can disagree with the printer, and the
entire value of the shared renderer (P3-2) is that there is exactly one.

    {
      "name": "task-default",
      "kind": "task",
      "version": 1,
      "blocks": [
        {"type": "qr",   "value": "{qr_url}", "size": 4},
        {"type": "text", "value": "{title}", "font": "a", "width": 2,
                         "height": 2, "bold": true}
      ]
    }

Templates live in `data/styles/<kind>/<name>.json`. Built-in presets come from
`layouts.py` and are never written to disk, so an upgrade improves them without
clobbering anything the user has edited.
"""
import json
import os
import re

from . import constants, layouts, receipt, state, storage
from .logsetup import log

#: Receipt kinds that are not listeners. Everything else comes from the
#: listener registry, so a new listener's receipts are editable in the Studio
#: without touching this module (P3-5).
BUILTIN_KINDS = ('task', 'scf')


def kinds():
    """Every editable receipt kind, built-ins first."""
    from .listeners import base
    return BUILTIN_KINDS + tuple(sorted(
        name for name in base.registry() if name not in BUILTIN_KINDS))


def kind_label(kind):
    """Human name for a kind, for the Studio's tabs."""
    from .listeners import base
    listener = base.get(kind)
    if listener is not None:
        return listener.title
    return {'task': 'Tasks', 'scf': 'SeeClickFix'}.get(kind, kind)
STYLES_DIRNAME = 'styles'
TEMPLATE_VERSION = 1

#: Placeholders each kind offers, with a sample value used for previewing.
#: Sample values are realistic rather than pretty -- a preview built on
#: "Lorem ipsum" hides exactly the wrapping problems it should reveal.
BUILTIN_PLACEHOLDERS = {
    'task': {
        'title': 'Play with Sara',
        'extra': 'MISS KITTY TIME',
        'recurrence': 'Daily',
        'printed': '8:30 AM 7/27/26',
        'id': 'a1b2c3d4',
        'qr_url': 'http://taskhome.local:5000/task_page#a1b2c3d4',
    },
    'scf': {
        'category': 'Signal Repair',
        'address': '239-299 S Lincoln St Manchester NH 03103',
        'status': 'Acknowledged',
        'reported': '5:58 PM 8/25/25',
        'media': 'Photo',
        'description': ('The signal on Lincoln st is broken. Light will only '
                        'let 5 cars go by at a time on to South Willow.'),
        'id': '19840471',
        'printed': '8:30 AM 7/27/26',
        'qr_url': 'https://seeclickfix.com/issues/19840471',
    },
}


def placeholders(kind):
    """Placeholder names and sample values for a kind.

    A listener supplies its own through the PLACEHOLDERS class attribute, which
    is also what its receipt template is validated against -- so a typo'd
    placeholder is refused at save time rather than rendering as literal text
    on paper.
    """
    if kind in BUILTIN_PLACEHOLDERS:
        return dict(BUILTIN_PLACEHOLDERS[kind])
    from .listeners import base
    listener = base.get(kind)
    return dict(listener.PLACEHOLDERS) if listener is not None else {}


_PLACEHOLDER_RE = re.compile(r'\{(\w+)\}')


class TemplateError(ValueError):
    """A template is malformed. Message is fit to show a user."""


# --- storage ------------------------------------------------------------------

def styles_dir(kind=None):
    base = os.path.join(constants.DATA_DIR, STYLES_DIRNAME)
    return os.path.join(base, kind) if kind else base


def builtin_templates(kind):
    """Every shipped preset for a kind, default first.

    Generated rather than duplicated, so a preset and the code that actually
    prints cannot drift apart. A listener may offer more than one -- NWS ships
    a large layout for warnings and a compact one for advisories, which is what
    the per-event `style` column selects between.
    """
    from .listeners import base
    listener = base.get(kind)
    if listener is not None:
        presets = listener.template_presets()
        return [{'name': name, 'kind': kind, 'version': TEMPLATE_VERSION,
                 'builtin': True, 'blocks': _generalise(blocks)}
                for name, blocks in presets]
    return [builtin_template(kind)]


def builtin_template(kind, name=None):
    """The shipped default for a kind, or a named preset."""
    from .listeners import base
    if base.get(kind) is not None:
        presets = builtin_templates(kind)
        for preset in presets:
            if preset['name'] == name:
                return preset
        return presets[0]

    if kind == 'task':
        blocks = layouts.task_receipt(
            {'id': '{id}', 'title': '{title}', 'extra': '{extra}',
             'recurring': 'daily'},
            '{qr_url}')
        # layouts formats these; templates express them as placeholders.
        blocks = _replace_value(blocks, 'Daily  -  Printed', '{recurrence}  -  Printed')
    else:
        blocks = layouts.scf_receipt(
            {'id': '{id}', 'html_url': '{qr_url}'},
            category='{category}', address='{address}', status='{status}',
            reported_at='{reported}', has_media=True, description='{description}')
        blocks = _replace_value(blocks, 'Photo', '{media}')
    return {
        'name': f'{kind}-default',
        'kind': kind,
        'version': TEMPLATE_VERSION,
        'builtin': True,
        'blocks': _generalise(blocks),
    }


def _replace_value(blocks, needle, replacement):
    for block in blocks:
        if block.get('type') == 'text' and needle in str(block.get('value', '')):
            block['value'] = str(block['value']).replace(needle, replacement)
    return blocks


def _generalise(blocks):
    """Turn concrete timestamps and ids into placeholders."""
    out = []
    for block in blocks:
        block = dict(block)
        if block.get('type') == 'text':
            value = str(block.get('value', ''))
            value = re.sub(r'\d{1,2}:\d{2} [AP]M \d{1,2}/\d{1,2}/\d{2}', '{printed}', value)
            block['value'] = value
        out.append(block)
    return out


def list_templates(kind):
    """Templates for a kind: the built-in presets first, then user ones."""
    templates = list(builtin_templates(kind))
    directory = styles_dir(kind)
    try:
        names = sorted(f for f in os.listdir(directory) if f.endswith('.json'))
    except OSError:
        names = []
    for filename in names:
        try:
            with open(os.path.join(directory, filename)) as f:
                template = json.load(f)
            template['kind'] = kind
            template.setdefault('name', filename[:-5])
            template['builtin'] = False
            templates.append(template)
        except (OSError, ValueError) as e:
            log.warning(f"Skipping unreadable template {filename}: {e}")
    return templates


def get_template(kind, name=None):
    """A template by name, falling back to the built-in preset.

    Falls back rather than raising: a receipt must still print when the
    selected template has been deleted or corrupted.
    """
    if not name or name == f'{kind}-default':
        return builtin_template(kind)
    for preset in builtin_templates(kind):
        if preset['name'] == name:
            return preset
    for template in list_templates(kind):
        if template.get('name') == name:
            return template
    log.warning(f"Template {name!r} not found for {kind}; using the built-in default")
    return builtin_template(kind)


def save_template(template):
    """Validate and persist. Returns the stored template."""
    template = validate_template(template)
    if template.get('builtin'):
        raise TemplateError('The built-in template cannot be overwritten. '
                            'Save it under a new name instead.')
    directory = styles_dir(template['kind'])
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{template['name']}.json")
    storage.backup_store(f"style-{template['name']}", path)
    if not storage._save_json_file(f"style-{template['name']}", path, template):
        raise TemplateError('Could not write the template; see the log.')
    return template


def delete_template(kind, name):
    if any(p['name'] == name for p in builtin_templates(kind)):
        raise TemplateError('The built-in template cannot be deleted.')
    path = os.path.join(styles_dir(kind), f'{name}.json')
    try:
        os.unlink(path)
        return True
    except OSError as e:
        raise TemplateError(f'Could not delete {name}: {e}')


def active_template_name(kind):
    """Which template the given receipt kind currently prints with."""
    styles = state.config.get('styles')
    if isinstance(styles, dict):
        return styles.get(kind) or f'{kind}-default'
    return f'{kind}-default'


def set_active_template(kind, name):
    styles = state.config.get('styles')
    if not isinstance(styles, dict):
        styles = {}
    styles[kind] = name
    state.config['styles'] = styles
    storage.save_config()


# --- validation ---------------------------------------------------------------

SAFE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 _-]{0,48}$')


def validate_template(template):
    """Check a template well enough that rendering it cannot explode.

    Returns a normalised copy. Raises TemplateError with a message worth
    showing a user -- these come from a form, so "invalid" is not good enough.
    """
    if not isinstance(template, dict):
        raise TemplateError('A template must be an object.')

    kind = template.get('kind')
    if kind not in kinds():
        raise TemplateError(f"Unknown receipt kind {kind!r}.")

    name = str(template.get('name', '')).strip()
    if not SAFE_NAME.match(name):
        raise TemplateError(
            'Name must be 1-49 characters: letters, numbers, spaces, - and _ only.')
    # Names become filenames; refuse anything that could escape the directory.
    if os.path.basename(name) != name or name.startswith('.'):
        raise TemplateError('That name is not allowed.')

    blocks = template.get('blocks')
    if not isinstance(blocks, list) or not blocks:
        raise TemplateError('A template needs at least one block.')
    if len(blocks) > 60:
        raise TemplateError('A template may have at most 60 blocks.')

    known = set(placeholders(kind))
    clean = []
    for index, block in enumerate(blocks, start=1):
        clean.append(_validate_block(block, index, known))

    return {
        'name': name,
        'kind': kind,
        'version': int(template.get('version', TEMPLATE_VERSION) or TEMPLATE_VERSION),
        'builtin': bool(template.get('builtin')),
        'blocks': clean,
    }


def _validate_block(block, index, known_placeholders):
    if not isinstance(block, dict):
        raise TemplateError(f'Block {index} is not an object.')
    kind = block.get('type')
    if kind not in ('text', 'qr', 'barcode', 'rule', 'blank', 'gap'):
        raise TemplateError(f'Block {index}: unknown type {kind!r}.')

    out = {'type': kind}
    if kind in ('text', 'qr', 'barcode'):
        value = block.get('value', '')
        if not isinstance(value, str):
            raise TemplateError(f'Block {index}: value must be text.')
        unknown = set(_PLACEHOLDER_RE.findall(value)) - known_placeholders
        if unknown:
            raise TemplateError(
                f"Block {index}: unknown placeholder(s) "
                f"{', '.join('{' + u + '}' for u in sorted(unknown))}.")
        out['value'] = value

    if kind == 'text':
        out['font'] = block.get('font', 'b')
        if out['font'] not in receipt.FONTS:
            raise TemplateError(f"Block {index}: font must be 'a' or 'b'.")
        for key, limit in (('width', 4), ('height', 4)):
            out[key] = _bounded_int(block.get(key, 1), 1, limit, index, key)
        out['bold'] = bool(block.get('bold'))
        align = block.get('align', 'center')
        if align not in ('left', 'center', 'right'):
            raise TemplateError(f'Block {index}: align must be left, center or right.')
        out['align'] = align
        if block.get('density') is not None:
            out['density'] = _bounded_int(block['density'], 0, 8, index, 'density')
    elif kind == 'qr':
        out['size'] = _bounded_int(block.get('size', 4), 1, 10, index, 'size')
    elif kind == 'barcode':
        out['height'] = _bounded_int(block.get('height', 60), 10, 200, index, 'height')
    elif kind == 'rule':
        char = str(block.get('char', '-'))[:1] or '-'
        out['char'] = char
        out['font'] = block.get('font', 'b') if block.get('font') in receipt.FONTS else 'b'
    elif kind == 'blank':
        out['count'] = _bounded_int(block.get('count', 1), 1, 5, index, 'count')
    elif kind == 'gap':
        out['dots'] = _bounded_int(block.get('dots', 8), 1, 100, index, 'dots')
    return out


def _bounded_int(value, low, high, index, field):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise TemplateError(f'Block {index}: {field} must be a number.')
    if not low <= number <= high:
        raise TemplateError(f'Block {index}: {field} must be between {low} and {high}.')
    return number


# --- rendering ----------------------------------------------------------------

def fill(template, context):
    """Substitute placeholders, producing blocks the renderer can consume.

    A block whose text resolves to nothing is dropped, so an optional field
    (a task with no `extra`) leaves no blank line behind.
    """
    blocks = []
    for block in template.get('blocks', []):
        block = dict(block)
        if 'value' in block:
            resolved = _PLACEHOLDER_RE.sub(
                lambda m: str(context.get(m.group(1), '') or ''), block['value'])
            resolved = resolved.strip()
            if not resolved and block['type'] in ('text', 'qr', 'barcode'):
                continue
            block['value'] = resolved
        blocks.append(block)
    return blocks


def sample_context(kind, overrides=None):
    context = dict(placeholders(kind))
    if overrides:
        context.update({k: v for k, v in overrides.items() if v is not None})
    return context


def preview(template, context=None):
    """Render to preview lines plus a height. Never raises on bad input."""
    template = validate_template(template)
    blocks = fill(template, context or sample_context(template['kind']))
    return {
        # `rows` drives the on-screen preview; `lines` is the plain-text form,
        # kept for tooling and tests. Both come from the same blocks.
        'rows': receipt.render_html(blocks),
        'lines': receipt.render_text(blocks),
        'height_mm': round(receipt.height_mm(blocks), 1),
        'height_dots': receipt.total_height(blocks),
        'columns': receipt.PAGE_COLUMNS,
    }
