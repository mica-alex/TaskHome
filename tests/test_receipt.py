"""Shared receipt renderer (MASTER_PLAN P3-2).

The guarantee worth protecting: one block list drives both the printer and the
preview, so the preview cannot drift from what comes out. These tests pin the
column model (measured on real hardware, see docs/printing.md), the wrapping,
and that both renderers consume the same input.
"""
import pytest

import layouts
import receipt as r


# --- column model -------------------------------------------------------------

def test_measured_column_counts():
    """48/64 was measured on the unit, not taken from a datasheet. The often
    quoted 42/56 would make the preview wrap where the printer does not."""
    assert r.FONTS['a']['cols'] == 48
    assert r.FONTS['b']['cols'] == 64


@pytest.mark.parametrize('font,mult,expected', [
    ('a', 1, 48), ('a', 2, 24), ('a', 3, 16),
    ('b', 1, 64), ('b', 2, 32),
])
def test_width_multiplier_halves_columns(font, mult, expected):
    assert r.columns_for(font, mult) == expected


def test_unknown_font_falls_back():
    assert r.columns_for('zzz') == r.FONTS['b']['cols']


@pytest.mark.parametrize('mult', [0, -1, 'x'])
def test_bad_multiplier_does_not_crash(mult):
    try:
        assert r.columns_for('a', mult) >= 1
    except (TypeError, ValueError):
        pytest.fail('columns_for should tolerate bad input')


# --- wrapping -----------------------------------------------------------------

def test_short_text_is_one_line():
    assert r.wrap('hello', 48) == ['hello']


def test_wraps_on_word_boundaries():
    assert r.wrap('aaa bbb ccc', 7) == ['aaa bbb', 'ccc']


def test_breaks_words_too_long_to_fit():
    """A 40-character word on a 24-column title must not overflow silently."""
    assert r.wrap('x' * 30, 10) == ['x' * 10, 'x' * 10, 'x' * 10]


def test_newlines_are_honoured():
    assert r.wrap('one\ntwo', 48) == ['one', 'two']


def test_no_line_exceeds_the_column_count():
    text = ('The signal on Lincoln st is broken. Light will only let 5 cars go '
            'by at a time on to South Willow.')
    assert all(len(line) <= 64 for line in r.wrap(text, 64))


# --- height estimation --------------------------------------------------------

def test_double_height_text_scales_the_cell_not_the_leading():
    """Height is cell*multiplier + leading, so 2x is not exactly twice as
    tall -- the leading is added once per line either way."""
    single = r.block_height(r.text('x', font='a', height=1))
    double = r.block_height(r.text('x', font='a', height=2))
    cell = r.FONTS['a']['cell_h']
    assert single == cell + r.LEADING_DOTS
    assert double == cell * 2 + r.LEADING_DOTS


def test_leading_prevents_tall_text_overlapping():
    """The clipping bug: the printer's default feed (~34 dots) is shorter than
    a double-height cell (48), so the next line printed into the descenders."""
    assert r.line_dots(r.text('x', font='a', height=2)) > r.FONTS['a']['cell_h'] * 2


def test_spacing_units_stay_in_command_range():
    for dots in (0, 1, 54, 500, -5):
        assert 0 <= r.spacing_units(dots) <= 255


def test_gap_contributes_height_but_no_row():
    blocks = [r.gap(6)]
    assert r.block_height(blocks[0]) == 6
    assert r.render_text(blocks) == []


def test_wrapped_text_is_taller():
    one = r.block_height(r.text('short', font='b'))
    many = r.block_height(r.text('word ' * 40, font='b'))
    assert many > one


def test_font_b_is_shorter_than_font_a():
    assert r.block_height(r.text('x', font='b')) < r.block_height(r.text('x', font='a'))


def test_qr_grows_with_size():
    small = r.block_height(r.qr('https://example.com/x', size=3))
    large = r.block_height(r.qr('https://example.com/x', size=6))
    assert large > small


def test_qr_grows_with_content_length():
    short = r.block_height(r.qr('x' * 10, size=4))
    long = r.block_height(r.qr('x' * 150, size=4))
    assert long > short


def test_height_mm_matches_dots():
    blocks = [r.text('x')]
    assert r.height_mm(blocks) == pytest.approx(r.total_height(blocks) / 8)


# --- renderers agree ----------------------------------------------------------

class RecordingPrinter:
    def __init__(self):
        self.calls = []

    def set(self, **kwargs):
        self.calls.append(('set', kwargs))

    def line_spacing(self, spacing=None, divisor=180):
        self.calls.append(('line_spacing', spacing))

    def text(self, value):
        self.calls.append(('text', value))

    def qr(self, value, **kwargs):
        self.calls.append(('qr', value))

    def barcode(self, value, bc=None, **kwargs):
        # Matches python-escpos: barcode(code, bc, ...) with bc positional.
        self.calls.append(('barcode', value))


def test_spacing_is_reset_after_rendering():
    """Leaving the printer in a modified spacing state would corrupt whatever
    prints next."""
    p = RecordingPrinter()
    r.render_escpos([r.text('x', font='a', height=2)], p)
    assert p.calls[-1] == ('line_spacing', None)


def test_spacing_is_set_before_each_text_block():
    p = RecordingPrinter()
    r.render_escpos([r.text('x', font='a', height=2)], p)
    spacings = [c[1] for c in p.calls if c[0] == 'line_spacing' and c[1] is not None]
    assert spacings == [r.spacing_units(r.line_dots(r.text('x', font='a', height=2)))]


def test_escpos_renderer_emits_every_block():
    blocks = [r.qr('https://example.com'), r.text('Title', font='a', width=2),
              r.rule(), r.text('body'), r.barcode('123')]
    p = RecordingPrinter()
    r.render_escpos(blocks, p)
    kinds = [c[0] for c in p.calls]
    assert 'qr' in kinds and 'barcode' in kinds
    assert p.calls.count(('text', 'Title\n')) == 1


def test_both_renderers_consume_the_same_blocks():
    """The anti-drift property: neither renderer needs its own input."""
    blocks = layouts.task_receipt(
        {'id': 'x', 'title': 'T', 'recurring': 'daily'}, 'http://x/y')
    p = RecordingPrinter()
    r.render_escpos(blocks, p)          # must not raise
    lines = r.render_text(blocks)       # must not raise
    assert p.calls and lines


def test_text_renderer_respects_the_frame_width():
    blocks = [r.text('word ' * 40, font='b', align='left')]
    for line in r.render_text(blocks, page_width=64):
        assert len(line) <= 64


def test_proportional_mode_widens_larger_text():
    normal = r.render_text([r.text('AB', font='a', width=1)], proportional=True)
    doubled = r.render_text([r.text('AB', font='a', width=2)], proportional=True)
    assert len(doubled[0].strip()) > len(normal[0].strip())


def test_preview_reports_height():
    out = r.preview([r.text('x')])
    assert 'mm' in out and 'dots' in out


# --- the layouts themselves ---------------------------------------------------

TASK = {'id': 'a1b2c3d4-1111-2222-3333-444444444444', 'title': 'Play with Sara',
        'extra': 'MISS KITTY TIME', 'recurring': 'daily'}
URL = 'http://localhost:5000/task_page#a1b2c3d4'
ISSUE = {'id': 19840471, 'html_url': 'https://seeclickfix.com/issues/19840471'}
SCF = dict(category='Signal Repair', address='239-299 S Lincoln St Manchester NH',
           reported_at='5:58 PM 8/25/25', status='Acknowledged', has_media=True,
           description='Broken signal on Lincoln st.')


def test_new_task_layout_is_shorter():
    old = r.height_mm(layouts.legacy_task_receipt(TASK, URL))
    new = r.height_mm(layouts.task_receipt(TASK, URL))
    assert new < old * 0.75, f'expected a real saving, got {old:.0f} -> {new:.0f} mm'


def test_new_scf_layout_is_shorter():
    old = r.height_mm(layouts.legacy_scf_receipt(ISSUE, **SCF))
    new = r.height_mm(layouts.scf_receipt(ISSUE, **SCF))
    assert new < old * 0.75


def test_task_layout_keeps_the_qr():
    """Explicitly required: the QR must survive any compaction."""
    blocks = layouts.task_receipt(TASK, URL)
    assert any(b['type'] == 'qr' for b in blocks)


def test_scf_layout_keeps_the_qr():
    assert any(b['type'] == 'qr' for b in layouts.scf_receipt(ISSUE, **SCF))


def test_scf_layout_drops_the_barcode():
    """Removed by agreement: ~10mm for information already in the QR and
    printed as text."""
    assert not any(b['type'] == 'barcode' for b in layouts.scf_receipt(ISSUE, **SCF))


def test_no_information_is_lost_from_the_task_receipt():
    rendered = '\n'.join(r.render_text(layouts.task_receipt(TASK, URL)))
    assert 'Play with Sara' in rendered
    assert 'MISS KITTY TIME' in rendered
    assert 'Daily' in rendered
    assert 'a1b2c3d4' in rendered          # short id still cross-references


def test_no_information_is_lost_from_the_scf_receipt():
    rendered = '\n'.join(r.render_text(layouts.scf_receipt(ISSUE, **SCF)))
    for expected in ('Signal Repair', 'Lincoln St', 'Acknowledged',
                     '5:58 PM', 'photo', 'Broken signal', '19840471'):
        assert expected in rendered, f'{expected!r} missing from the receipt'


def test_task_without_extra_omits_the_line():
    blocks = layouts.task_receipt({'id': 'x', 'title': 'T', 'recurring': 'none'}, URL)
    rendered = '\n'.join(r.render_text(blocks))
    assert 'One-off' in rendered


def test_scf_without_description_omits_the_section():
    blocks = layouts.scf_receipt(ISSUE, **dict(SCF, description=''))
    assert len(blocks) < len(layouts.scf_receipt(ISSUE, **SCF))


def test_long_title_wraps_rather_than_overflowing():
    task = dict(TASK, title='An extremely long task title that will not fit on one line')
    for line in r.render_text(layouts.task_receipt(task, URL)):
        assert len(line) <= r.PAGE_COLUMNS


# --- preview/print agreement (found on real paper) ----------------------------

def test_printer_receives_pre_wrapped_lines():
    """The printer hard-wraps at the column limit, splitting words mid-way,
    while the preview wraps on word boundaries. Emitting pre-wrapped lines is
    what makes them agree -- without it the shared renderer's central promise
    is false, as an actual receipt demonstrated ("...5 cars g" / "o by at...").
    """
    body = ('The signal on Lincoln st is broken. Light will only let 5 cars go '
            'by at a time on to South Willow.')
    p = RecordingPrinter()
    r.render_escpos([r.text(body, font='b')], p)

    printed = [c[1].rstrip('\n') for c in p.calls if c[0] == 'text']
    assert printed == r.wrap(body, 64)
    assert all(len(line) <= 64 for line in printed)
    assert not any(line.endswith(' g') for line in printed)


def test_no_printed_line_exceeds_its_column_budget():
    p = RecordingPrinter()
    blocks = layouts.scf_receipt(ISSUE, **SCF)
    r.render_escpos(blocks, p)
    # Reconstruct which font each text call used from the preceding set().
    font, width = 'b', 1
    for kind, payload in p.calls:
        if kind == 'set':
            font = payload.get('font', font)
            width = payload.get('width', width)
        elif kind == 'text':
            line = payload.rstrip('\n')
            if line:
                assert len(line) <= r.columns_for(font, width), \
                    f'{line!r} exceeds {r.columns_for(font, width)} columns'


def test_leading_keeps_adjacent_body_lines_apart():
    """6 dots let adjacent font b lines touch on paper; the cell is 17."""
    gap = r.line_dots(r.text('x', font='b')) - r.FONTS['b']['cell_h']
    assert gap >= 8, 'body lines will look cramped or touch'
