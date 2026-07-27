"""Receipt layout: one definition, rendered to the printer or to text.

This is the foundation MASTER_PLAN `P3-2` calls for. A receipt is a list of
blocks — plain data, no ESC/POS in sight — and two renderers consume it:

    render_escpos(blocks, printer)   -> physical paper
    render_text(blocks)              -> what it will look like

Having one definition is the whole point. A preview built from a second,
parallel implementation drifts from the printer the moment either changes, and
a preview that lies is worse than no preview.

Measurements are for the unit in use (EPSON TM-T20IIIL, 80 mm, 203 dpi),
determined by `scripts/calibrate_printer.py` rather than from datasheets:

    font a: 48 columns, 12x24 dot cells
    font b: 64 columns, 9x17 dot cells   (>=64 measured; 64 is the profile value)

Heights are in printer dots; 203 dpi is almost exactly 8 dots per mm, so
dots/8 gives millimetres. They are estimates used to compare layouts, not
promises — line spacing varies slightly with the active font.
"""
import re

FONTS = {
    'a': {'cols': 48, 'cell_w': 12, 'cell_h': 24},
    'b': {'cols': 64, 'cell_w': 9, 'cell_h': 17},
}
DOTS_PER_MM = 8

# Printer resolution, and the units the ESC 3 line-spacing command uses.
DPI = 203
SPACING_DIVISOR = 180          # ESC 3 n sets n/180 inch; the widely supported one

# Extra vertical space below a line, on top of the character cell height.
#
# The printer's factory default feed is about 34 dots. That is generous for
# font b (17-dot cell) but SHORTER than a double-height font a cell (48 dots),
# so a 2x title had the next line printed into its descenders. Spacing is
# therefore computed per block from the actual cell height.
#
# The value is empirical, from printed paper, and the arithmetic proved
# unreliable: at 10 dots of leading over a documented 17-dot font b cell there
# should have been a clear 10-dot gap, yet the lines still read as touching.
# The documented cell height evidently understates what the head actually lays
# down -- likely the glyph box excludes some of the descender travel -- so the
# real gap is roughly 7 dots smaller than the model suggests.
#
# Read from the test strip (`calibrate_printer.py --confirm --spacing`): six
# leading values printed identically except the largest. The printer floors
# line spacing at the character height -- about 34 dots, exactly the 1/6 inch
# factory default -- and silently clamps anything smaller, so five of the six
# samples came out the same. Leading below that floor is simply ignored.
#
# MIN_LINE_DOTS is therefore what actually controls body-text separation:
# clearing the floor is the only way to get visible space between lines.
LEADING_DOTS = 8
MIN_LINE_DOTS = 40

# Feed used around a QR image. The symbol itself already carries a 1-module
# quiet zone (python-escpos builds it with border=1), so the visible gap was
# the trailing line feed inheriting whatever spacing was current.
QR_LEADING_DOTS = 4

# Preview frame width: the paper expressed in font b cells, which is the
# narrowest cell and so the finest grid available. Font a is scaled onto it.
PAGE_COLUMNS = FONTS['b']['cols']


# --- block constructors -------------------------------------------------------
# Plain dicts rather than classes: these are meant to be JSON-serialisable, so
# P3-1's user-editable templates can express exactly the same thing.

def text(value, font='b', width=1, height=1, bold=False, align='center',
         density=None, leading=None):
    """A run of text.

    density (0-8) and leading (dots) are optional overrides; None means "leave
    the printer's current setting alone", which is what the approved default
    layouts rely on. They exist so P3-1's editable templates can express them
    without the renderer needing to change.
    """
    block = {'type': 'text', 'value': value, 'font': font, 'width': width,
             'height': height, 'bold': bold, 'align': align}
    if density is not None:
        block['density'] = density
    if leading is not None:
        block['leading'] = leading
    return block


def qr(value, size=4):
    return {'type': 'qr', 'value': value, 'size': size}


def barcode(value, height=60):
    return {'type': 'barcode', 'value': value, 'height': height}


#: Printable width in dots. Font A is 48 columns of 12 dots.
PAPER_DOTS = FONTS['a']['cols'] * FONTS['a']['cell_w']

#: Default cap for a printed photo. Full paper width is 576 dots, which for a
#: portrait phone photo is about 100mm of paper for one picture -- more than
#: the rest of the receipt put together. 384 keeps it recognisable at roughly
#: two thirds the cost.
DEFAULT_IMAGE_DOTS = 384


def image(src, width=DEFAULT_IMAGE_DOTS, max_height=DEFAULT_IMAGE_DOTS, alt='Photo'):
    """A raster image block.

    `src` is a URL, resolved at print time rather than stored: a receipt
    template holds a placeholder like {media_url}, and the bytes are fetched
    when the receipt is actually printed.

    `alt` is what the text preview and a failed fetch show, so a receipt is
    never silently missing something it promised.
    """
    return {'type': 'image', 'src': src, 'width': width,
            'max_height': max_height, 'alt': alt}


def rule(char='-', font='b'):
    return {'type': 'rule', 'char': char, 'font': font}


def blank(count=1):
    return {'type': 'blank', 'count': count}


def gap(dots=8):
    """A sub-line vertical space, for when a full blank line is too much."""
    return {'type': 'gap', 'dots': dots}


# --- helpers ------------------------------------------------------------------

def columns_for(font, width_multiplier=1):
    """Characters per line for a font at a given width multiplier.

    Tolerates bad input rather than raising: these values will come from
    user-editable templates (P3-1), and a malformed one should render oddly,
    not break the receipt.
    """
    base = FONTS.get(font, FONTS['b'])['cols']
    try:
        multiplier = max(int(width_multiplier), 1)
    except (TypeError, ValueError):
        multiplier = 1
    return max(base // multiplier, 1)


def wrap(value, cols):
    """Word-wrap, breaking over-long words rather than overflowing.

    Mirrors what the printer does: it wraps at the column count, and a word
    longer than a line is split. Newlines in the source are honoured.

    **Runs of spaces are preserved.** This used to join words with a single
    space, which silently collapsed both indentation and column padding --
    and since text is pre-wrapped before being sent to the device, that meant
    the printer could not receive an aligned column layout at all. A departure
    board is columns; so is any "label     value" line.

    Trailing whitespace is dropped at a line break, because padding at the end
    of a line is invisible on paper and only risks pushing the wrap early.
    """
    lines = []
    for paragraph in str(value).split('\n'):
        if not paragraph:
            lines.append('')
            continue

        current = ''
        # Words and whitespace runs alternate, so both survive the round trip.
        for token in re.findall(r'\S+|\s+', paragraph):
            if token.isspace():
                # Whitespace only joins; it never forces a break on its own.
                # Kept even at the start of a line, because leading spaces are
                # deliberate indentation -- dropping them is what made an
                # indented source line read as a second headline.
                current += token
                continue

            word = token
            while len(word) > cols:            # break a word too long to fit
                if current:
                    lines.append(current.rstrip())
                    current = ''
                lines.append(word[:cols])
                word = word[cols:]

            if len(current) + len(word) <= cols:
                current += word
            else:
                if current:
                    lines.append(current.rstrip())
                current = word
        lines.append(current.rstrip())
    return lines


def qr_modules(value):
    """Module count per side for the QR version that fits `value`.

    Byte mode, error correction level M, which is python-escpos's default.
    Only the thresholds we can actually reach matter here.
    """
    length = len(str(value))
    for version, (capacity, modules) in enumerate(
            [(14, 21), (26, 25), (42, 29), (62, 33), (84, 37), (106, 41),
             (122, 45), (152, 49), (180, 53), (213, 57)], start=1):
        if length <= capacity:
            return modules
    return 57


# --- height estimation --------------------------------------------------------

def block_height(block):
    """Estimated height in printer dots, including per-line leading.

    Leading is part of the height because the renderer sets line spacing per
    block; ignoring it would make the estimate systematically low and the
    preview's millimetre figure useless for comparing layouts.
    """
    kind = block['type']
    if kind == 'blank':
        return (FONTS['b']['cell_h'] + LEADING_DOTS) * block.get('count', 1)
    if kind == 'gap':
        return block.get('dots', 8)
    if kind == 'qr':
        # The symbol carries a 1-module quiet zone, plus the trailing feed.
        return (qr_modules(block['value']) + 2) * block.get('size', 4) + QR_LEADING_DOTS
    if kind == 'barcode':
        return block.get('height', 60) + FONTS['b']['cell_h']  # symbol + label
    if kind == 'image':
        # Without the real aspect ratio the best estimate is the cap, which is
        # what a portrait photo hits anyway. A landscape one prints shorter,
        # so the preview's millimetre figure is an upper bound rather than a
        # lie in the dangerous direction.
        return block.get('max_height', DEFAULT_IMAGE_DOTS) + LEADING_DOTS
    if kind == 'rule':
        return line_dots(block)
    if kind == 'text':
        cols = columns_for(block.get('font', 'b'), block.get('width', 1))
        return len(wrap(block['value'], cols)) * line_dots(block)
    return 0


def total_height(blocks):
    return sum(block_height(b) for b in blocks)


def height_mm(blocks):
    return total_height(blocks) / DOTS_PER_MM


# --- renderers ----------------------------------------------------------------

def spacing_units(dots, divisor=SPACING_DIVISOR):
    """Convert dots to ESC 3 units, clamped to the command's valid range.

    python-escpos names the parameter `divisor=180`, which reads as "n/180
    inch" -- but ESC 3 n actually sets n x the printer's *vertical motion
    unit*, and on the TM-T20III that unit is 1/203 inch. One unit is therefore
    one dot, and converting by 180/203 was shrinking every value by 12%.
    """
    return max(0, min(255, round(dots)))


def line_dots(block):
    """Vertical space one line of this block needs.

    max(cell + leading, MIN_LINE_DOTS): the floor is what gives single-height
    text visible separation, since the printer ignores anything below it. Tall
    text exceeds the floor on its own and uses cell + leading directly.
    """
    font = FONTS.get(block.get('font', 'b'), FONTS['b'])
    try:
        height = max(int(block.get('height', 1) or 1), 1)
    except (TypeError, ValueError):
        height = 1
    leading = block.get('leading', LEADING_DOTS)
    return max(font['cell_h'] * height + leading, MIN_LINE_DOTS)


def render_escpos(blocks, printer):
    """Emit blocks to a python-escpos printer. Does not cut or close.

    Sets line spacing per block. Leaving it at the printer default made
    double-height text overlap the line beneath it, and made the feed after a
    QR image larger than intended. Spacing is reset at the end so the printer
    is not left in a modified state for whatever prints next.
    """
    try:
        for block in blocks:
            kind = block['type']
            if kind == 'blank':
                printer.line_spacing(spacing_units(FONTS['b']['cell_h'] + LEADING_DOTS))
                printer.text('\n' * block.get('count', 1))
            elif kind == 'gap':
                # A partial line: set the feed to exactly this many dots.
                printer.line_spacing(spacing_units(block.get('dots', 8)))
                printer.text('\n')
            elif kind == 'qr':
                printer.line_spacing(spacing_units(QR_LEADING_DOTS))
                printer.set(align='center')
                printer.qr(block['value'], size=block.get('size', 4), model=2)
            elif kind == 'image':
                # Fetched here, at print time, rather than stored in the block:
                # a template holds a placeholder and the bytes belong to this
                # one receipt. A failure prints the alt text instead of
                # abandoning the whole receipt (P0-8's build-before-print
                # principle does not apply -- the network cannot be checked in
                # advance).
                from . import images
                images.throttle()
                prepared = images.load(block.get('src'), block.get('width', 384),
                                       block.get('max_height'))
                if prepared is None:
                    printer.line_spacing(spacing_units(line_dots({'font': 'b'})))
                    printer.set(align='center', font='b', bold=False,
                                custom_size=True, width=1, height=1)
                    printer.text(f"[{block.get('alt', 'Photo')} unavailable]\n")
                else:
                    printer.set(align='center')
                    printer.image(prepared, center=True)
                    printer.line_spacing(spacing_units(LEADING_DOTS))
                    printer.text('\n')
            elif kind == 'barcode':
                printer.barcode(str(block['value']), 'CODE39',
                                width=2, height=block.get('height', 60),
                                pos='below', align_ct=True)
            elif kind == 'rule':
                font = block.get('font', 'b')
                printer.line_spacing(spacing_units(line_dots(block)))
                printer.set(align='center', font=font, bold=False,
                            custom_size=True, width=1, height=1)
                printer.text(block.get('char', '-') * columns_for(font) + '\n')
            elif kind == 'text':
                printer.line_spacing(spacing_units(line_dots(block)))
                printer.set(align=block.get('align', 'center'),
                            font=block.get('font', 'b'),
                            bold=block.get('bold', False),
                            custom_size=True,
                            width=max(int(block.get('width', 1)), 1),
                            height=max(int(block.get('height', 1)), 1),
                            density=block.get('density'))
                # Wrap here rather than letting the printer do it. The printer
                # hard-wraps at the column limit, splitting words mid-way
                # ("...5 cars g" / "o by at a time"), while the preview wraps
                # on word boundaries -- so the two disagreed on real paper.
                # Emitting pre-wrapped lines makes the preview authoritative.
                cols = columns_for(block.get('font', 'b'), block.get('width', 1))
                for line in wrap(block['value'], cols):
                    printer.text(line + '\n')
    finally:
        printer.line_spacing()          # restore the printer default


def stretch(line, source_cols, frame_width):
    """Scale a line from its font's column count onto a common frame.

    Fonts differ in cell width — font a fits 48 characters across the paper,
    font b fits 64 — so a preview that gave every font the same number of
    frame columns would draw font b 33% too wide. Each character is instead
    repeated proportionally, with the fractional part accumulated so the line
    does not drift.
    """
    if source_cols <= 0:
        return line
    scale = frame_width / source_cols
    out, carry = [], 0.0
    for ch in line:
        carry += scale
        repeat = int(carry)
        carry -= repeat
        out.append(ch * repeat)
    return ''.join(out)


def render_text(blocks, page_width=PAGE_COLUMNS, proportional=False):
    """Render to text for previewing. Returns a list of lines.

    With proportional=True every block is drawn to the same physical paper
    width, so a 2x title really does occupy twice the width of a 1x one. That
    is faithful but hard to read in a terminal, because each glyph is repeated.
    The default renders text at its natural column count, centred in the frame:
    readable, correct about wrap points and ordering, and paired with an exact
    height in millimetres, which is the number that actually matters when
    comparing layouts. The browser preview (P3-4) can be faithful *and*
    readable because it scales with CSS rather than characters.
    """
    out = []
    for block in blocks:
        kind = block['type']
        if kind == 'blank':
            out.extend([''] * block.get('count', 1))
        elif kind == 'gap':
            pass   # sub-line space; contributes height but no visible row
        elif kind == 'qr':
            modules = qr_modules(block['value'])
            size = block.get('size', 4)
            # Approximate footprint: the symbol scaled into character cells.
            box_w = min(max(modules * size // FONTS['b']['cell_w'], 8), page_width)
            box_h = max(modules * size // FONTS['a']['cell_h'], 4)
            out.append(('┌' + '─' * (box_w - 2) + '┐').center(page_width))
            for i in range(box_h - 2):
                label = 'QR' if i == (box_h - 2) // 2 else ''
                out.append(('│' + label.center(box_w - 2) + '│').center(page_width))
            out.append(('└' + '─' * (box_w - 2) + '┘').center(page_width))
        elif kind == 'image':
            # A box of the right proportions, so the preview's height figure
            # reflects what the paper will actually cost.
            width_cols = min(page_width - 4, 28)
            rows = max(2, round(block.get('max_height', 384) / line_dots({'font': 'b'})))
            out.append(('┌' + '─' * width_cols + '┐').center(page_width))
            for i in range(rows):
                label = block.get('alt', 'Photo') if i == rows // 2 else ''
                out.append(('│' + label.center(width_cols) + '│').center(page_width))
            out.append(('└' + '─' * width_cols + '┘').center(page_width))
        elif kind == 'barcode':
            out.append(('║' * min(page_width - 4, 30)).center(page_width))
            out.append(str(block['value']).center(page_width))
        elif kind == 'rule':
            out.append(block.get('char', '-') * page_width)
        elif kind == 'text':
            font = block.get('font', 'b')
            w = max(int(block.get('width', 1)), 1)
            h = max(int(block.get('height', 1)), 1)
            cols = columns_for(font, w)
            for line in wrap(block['value'], cols):
                # Scale onto the shared frame. Bold is deliberately NOT shown
                # by altering the text: the printer does not change glyphs, and
                # a preview that uppercased would misrepresent the output.
                shown = stretch(line, cols, page_width) if proportional else line
                align = block.get('align', 'center')
                if align == 'center':
                    rendered = shown.center(page_width)
                elif align == 'right':
                    rendered = shown.rjust(page_width)
                else:
                    rendered = shown.ljust(page_width)
                for _ in range(h):     # tall text occupies more vertical space
                    out.append(rendered.rstrip())
    return out


def preview(blocks, page_width=PAGE_COLUMNS, border=True, proportional=False):
    """A printable string preview, with paper edges and a height readout."""
    lines = render_text(blocks, page_width, proportional=proportional)
    if not border:
        return '\n'.join(lines)
    edge = '+' + '-' * (page_width + 2) + '+'
    body = '\n'.join(f'| {line:<{page_width}} |' for line in lines)
    footer = f'{height_mm(blocks):.0f} mm ({total_height(blocks)} dots)'
    return f'{edge}\n{body}\n{edge}\n{footer}'


# --- HTML preview -------------------------------------------------------------

def render_html(blocks, page_columns=PAGE_COLUMNS):
    """Render blocks to HTML for the browser preview (P3-4).

    The text renderer repeats a line once per height multiplier to represent
    vertical space. That is fine for a terminal, but on screen it reads as the
    title being printed twice -- which is not what the printer does; it prints
    it once, taller. HTML can scale instead.

    Sizing is derived, not guessed. A line's font size is scaled by
    `page_columns / columns_for(font, width)`, so a block that fits 24 columns
    occupies exactly the same paper width as one that fits 64. Height is
    applied on top as a vertical stretch, because ESC/POS scales width and
    height independently.

    Returns a list of {'kind', 'html'} rows so the caller can style them.
    """
    rows = []
    for block in blocks:
        kind = block['type']
        if kind == 'blank':
            for _ in range(block.get('count', 1)):
                rows.append({'kind': 'blank', 'text': '', 'w': 1, 'h': 1,
                             'align': 'center', 'bold': False, 'scale': 1.0})
        elif kind == 'gap':
            rows.append({'kind': 'gap', 'dots': block.get('dots', 8)})
        elif kind == 'qr':
            rows.append({'kind': 'qr',
                         'modules': qr_modules(block['value']),
                         'size': block.get('size', 4),
                         'value': block['value']})
        elif kind == 'image':
            rows.append({
                'kind': 'image',
                'src': block.get('src', ''),
                'alt': block.get('alt', 'Photo'),
                # Proportional to the paper, so the Studio shows the real cost
                # of a photo rather than a thumbnail that looks free.
                'width_pct': round(100 * block.get('width', 384) / PAPER_DOTS, 1),
                'height_dots': block.get('max_height', 384),
            })
        elif kind == 'barcode':
            rows.append({'kind': 'barcode', 'value': str(block['value']),
                         'height': block.get('height', 60)})
        elif kind == 'rule':
            font = block.get('font', 'b')
            cols = columns_for(font)
            rows.append({'kind': 'text', 'text': block.get('char', '-') * cols,
                         'w': 1, 'h': 1, 'align': 'center', 'bold': False,
                         'scale': page_columns / cols})
        elif kind == 'text':
            font = block.get('font', 'b')
            width = max(int(block.get('width', 1) or 1), 1)
            height = max(int(block.get('height', 1) or 1), 1)
            cols = columns_for(font, width)
            for line in wrap(block['value'], cols):
                rows.append({
                    'kind': 'text',
                    'text': line,
                    'w': width,
                    'h': height,
                    'align': block.get('align', 'center'),
                    'bold': bool(block.get('bold')),
                    # How much wider each character is than the base grid.
                    'scale': page_columns / cols,
                })
    return rows
