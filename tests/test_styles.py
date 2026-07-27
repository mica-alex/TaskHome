"""Editable receipt templates (MASTER_PLAN P3-1).

A template is the same block list receipt.py renders, with {placeholder}
markers. That is deliberate: a bespoke templating DSL would be a second thing
that can disagree with the printer, and the whole value of the shared renderer
is that there is exactly one.
"""
import json

import pytest

from taskhome import constants, receipt, state, storage, styles


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, 'APP_ROOT', str(tmp_path / 'repo'))
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(constants, 'CONFIG_FILE', str(tmp_path / 'config.json'))
    monkeypatch.setattr(state, 'config', dict(constants.DEFAULT_CONFIG))
    state.load_failed.clear()
    yield tmp_path
    state.load_failed.clear()


def template(kind='task', name='custom', blocks=None):
    # `is None` rather than truthiness: an explicitly empty list is a case
    # under test, not a request for the default.
    if blocks is None:
        blocks = [{'type': 'text', 'value': '{title}'}]
    return {'kind': kind, 'name': name, 'version': 1, 'blocks': blocks}


# --- built-in presets ---------------------------------------------------------

@pytest.mark.parametrize('kind', ['task', 'scf'])
def test_builtin_is_generated_from_the_layouts(kind):
    """Generated, not duplicated, so the shipped preset and the code that
    actually prints cannot drift apart."""
    built = styles.builtin_template(kind)
    assert built['builtin'] is True
    assert built['blocks']
    rendered = '\n'.join(b.get('value', '') for b in built['blocks'])
    assert '{' in rendered, 'the preset should be expressed with placeholders'


@pytest.mark.parametrize('kind', ['task', 'scf'])
def test_builtin_previews_without_error(kind):
    out = styles.preview(styles.builtin_template(kind))
    assert out['lines']
    assert out['height_mm'] > 0
    assert out['columns'] == receipt.PAGE_COLUMNS


@pytest.mark.parametrize('kind', ['task', 'scf'])
def test_builtin_keeps_the_qr(kind):
    assert any(b['type'] == 'qr' for b in styles.builtin_template(kind)['blocks'])


# --- validation ---------------------------------------------------------------

def test_valid_template_round_trips():
    assert styles.validate_template(template())['name'] == 'custom'


@pytest.mark.parametrize('name', ['', '   ', '../escape', 'a/b', '.hidden', 'x' * 60])
def test_bad_names_are_rejected(name):
    """Names become filenames, so anything that could escape the directory or
    collide with a dotfile has to be refused."""
    with pytest.raises(styles.TemplateError):
        styles.validate_template(template(name=name))


def test_unknown_kind_is_rejected():
    with pytest.raises(styles.TemplateError, match='kind'):
        styles.validate_template(template(kind='weather'))


def test_empty_block_list_is_rejected():
    with pytest.raises(styles.TemplateError, match='at least one block'):
        styles.validate_template(template(blocks=[]))


def test_too_many_blocks_is_rejected():
    blocks = [{'type': 'text', 'value': 'x'} for _ in range(61)]
    with pytest.raises(styles.TemplateError, match='at most 60'):
        styles.validate_template(template(blocks=blocks))


def test_unknown_placeholder_is_reported_by_name():
    """The message goes on screen, so it has to say which placeholder."""
    with pytest.raises(styles.TemplateError, match=r'\{nonsense\}'):
        styles.validate_template(template(blocks=[{'type': 'text', 'value': '{nonsense}'}]))


def test_placeholders_are_per_kind():
    """{category} is an SCF field; a task template must not accept it."""
    with pytest.raises(styles.TemplateError):
        styles.validate_template(template(blocks=[{'type': 'text', 'value': '{category}'}]))
    styles.validate_template(template(kind='scf', blocks=[{'type': 'text', 'value': '{category}'}]))


def test_unknown_block_type_is_rejected():
    with pytest.raises(styles.TemplateError, match='unknown type'):
        styles.validate_template(template(blocks=[{'type': 'hologram'}]))


@pytest.mark.parametrize('block,message', [
    ({'type': 'text', 'value': 'x', 'font': 'z'}, 'font'),
    ({'type': 'text', 'value': 'x', 'width': 99}, 'width'),
    ({'type': 'text', 'value': 'x', 'height': 0}, 'height'),
    ({'type': 'text', 'value': 'x', 'align': 'sideways'}, 'align'),
    ({'type': 'qr', 'value': 'x', 'size': 50}, 'size'),
    ({'type': 'gap', 'dots': 0}, 'dots'),
])
def test_out_of_range_values_are_rejected(block, message):
    with pytest.raises(styles.TemplateError, match=message):
        styles.validate_template(template(blocks=[block]))


def test_validation_normalises_missing_fields():
    out = styles.validate_template(template(blocks=[{'type': 'text', 'value': 'x'}]))
    block = out['blocks'][0]
    assert block['font'] == 'b' and block['width'] == 1 and block['align'] == 'center'


# --- filling ------------------------------------------------------------------

def test_placeholders_resolve():
    blocks = styles.fill(template(blocks=[{'type': 'text', 'value': 'Hi {title}'}]),
                         {'title': 'Sara'})
    assert blocks[0]['value'] == 'Hi Sara'


def test_blocks_that_resolve_to_nothing_are_dropped():
    """A task with no `extra` must not leave a blank line behind."""
    blocks = styles.fill(
        template(blocks=[{'type': 'text', 'value': '{title}'},
                         {'type': 'text', 'value': '{extra}'}]),
        {'title': 'Feed cat', 'extra': ''})
    assert len(blocks) == 1


def test_missing_context_key_is_treated_as_empty():
    blocks = styles.fill(template(blocks=[{'type': 'text', 'value': 'a{title}b'}]), {})
    assert blocks[0]['value'] == 'ab'


def test_non_text_blocks_survive_filling():
    blocks = styles.fill(template(blocks=[{'type': 'rule', 'char': '-'},
                                          {'type': 'text', 'value': '{title}'}]),
                         {'title': 'x'})
    assert blocks[0]['type'] == 'rule'


# --- storage ------------------------------------------------------------------

def test_saving_and_loading(store):
    styles.save_template(template(name='mine'))
    names = [t['name'] for t in styles.list_templates('task')]
    assert 'mine' in names
    assert styles.get_template('task', 'mine')['name'] == 'mine'


def test_builtin_cannot_be_overwritten(store):
    built = styles.builtin_template('task')
    with pytest.raises(styles.TemplateError, match='built-in'):
        styles.save_template(built)


def test_builtin_cannot_be_deleted(store):
    with pytest.raises(styles.TemplateError, match='built-in'):
        styles.delete_template('task', 'task-default')


def test_missing_template_falls_back_to_the_builtin(store):
    """A receipt must still print when the selected template is gone."""
    got = styles.get_template('task', 'deleted-yesterday')
    assert got['builtin'] is True


def test_corrupt_template_file_is_skipped_not_fatal(store):
    directory = store / 'styles' / 'task'
    directory.mkdir(parents=True)
    (directory / 'broken.json').write_text('{not json')
    (directory / 'fine.json').write_text(json.dumps(template(name='fine')))
    names = [t['name'] for t in styles.list_templates('task')]
    assert 'fine' in names and 'broken' not in names


def test_active_template_defaults_to_the_builtin(store):
    assert styles.active_template_name('task') == 'task-default'


def test_active_template_is_remembered(store):
    styles.set_active_template('task', 'mine')
    assert styles.active_template_name('task') == 'mine'


def test_saving_snapshots_the_previous_version(store):
    styles.save_template(template(name='mine', blocks=[{'type': 'text', 'value': 'v1'}]))
    styles.save_template(template(name='mine', blocks=[{'type': 'text', 'value': 'v2'}]))
    assert storage.list_backups('style-mine')


# --- preview ------------------------------------------------------------------

def test_preview_uses_realistic_sample_values():
    """Sample text is realistic rather than pretty: 'Lorem ipsum' would hide
    the wrapping problems a preview exists to reveal."""
    sample = styles.sample_context('scf')
    assert len(sample['description']) > 60
    assert 'Manchester' in sample['address']


def test_preview_reports_height_and_columns():
    out = styles.preview(styles.builtin_template('task'))
    assert out['height_dots'] > 0
    assert out['columns'] == 64


def test_preview_rejects_an_invalid_template():
    with pytest.raises(styles.TemplateError):
        styles.preview({'kind': 'task', 'name': 'x', 'blocks': []})


def test_preview_wraps_where_the_printer_wraps():
    """The preview must not word-wrap where the printer would not, nor the
    reverse -- that mismatch is the bug the shared renderer exists to stop."""
    long_text = 'word ' * 40
    out = styles.preview(template(kind='scf', blocks=[
        {'type': 'text', 'value': long_text, 'font': 'b', 'align': 'left'}]))
    assert all(len(line) <= receipt.PAGE_COLUMNS for line in out['lines'])


# --- separators stranded by empty placeholders --------------------------------

@pytest.mark.parametrize('raw,expected', [
    ('Open  -  01:36 PM  -  ', 'Open  -  01:36 PM'),      # the SCF no-photo case
    ('Open  -  01:36 PM  -  Photo', 'Open  -  01:36 PM  -  Photo'),
    ('  -  Open', 'Open'),
    ('Open  -    -  Photo', 'Open - Photo'),
    ('Severe - ', 'Severe'),                              # NWS with no urgency
    ('Just text', 'Just text'),
    ('01:36 PM, 08/26/2025', '01:36 PM, 08/26/2025'),     # must not mangle a date
])
def test_tidy_separators(raw, expected):
    assert styles.tidy_separators(raw) == expected


def test_an_scf_receipt_with_no_photo_has_no_dangling_dash():
    """The built-in template is generated with has_media=True, so it bakes in
    '{status} - {reported} - {media}'. An issue with no photo used to print
    'Open - 01:36 PM, 08/26/2025 -'.

    A template is a flat string with no conditionals -- P3-1 chose a block list
    over a DSL -- so this has to be handled at fill time.
    """
    template = styles.get_template('scf', 'scf-default')
    blocks = styles.fill(template, styles.sample_context('scf', {'media': ''}))
    # Text blocks only -- a rule block is legitimately a row of dashes.
    for block in blocks:
        if block.get('type') != 'text':
            continue
        assert not block['value'].rstrip().endswith('-'), \
            f"dangling separator: {block['value']!r}"


def test_a_receipt_with_media_still_shows_it():
    from taskhome import receipt
    template = styles.get_template('scf', 'scf-default')
    blocks = styles.fill(template, styles.sample_context('scf', {'media': 'Photo'}))
    assert 'Photo' in '\n'.join(receipt.render_text(blocks))
