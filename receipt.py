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

FONTS = {
    'a': {'cols': 48, 'cell_w': 12, 'cell_h': 24},
    'b': {'cols': 64, 'cell_w': 9, 'cell_h': 17},
}
DOTS_PER_MM = 8

# Preview frame width: the paper expressed in font b cells, which is the
# narrowest cell and so the finest grid available. Font a is scaled onto it.
PAGE_COLUMNS = FONTS['b']['cols']


# --- block constructors -------------------------------------------------------
# Plain dicts rather than classes: these are meant to be JSON-serialisable, so
# P3-1's user-editable templates can express exactly the same thing.

def text(value, font='b', width=1, height=1, bold=False, align='center'):
    return {'type': 'text', 'value': value, 'font': font, 'width': width,
            'height': height, 'bold': bold, 'align': align}


def qr(value, size=4):
    return {'type': 'qr', 'value': value, 'size': size}


def barcode(value, height=60):
    return {'type': 'barcode', 'value': value, 'height': height}


def rule(char='-', font='b'):
    return {'type': 'rule', 'char': char, 'font': font}


def blank(count=1):
    return {'type': 'blank', 'count': count}


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
    """
    lines = []
    for paragraph in str(value).split('\n'):
        if not paragraph:
            lines.append('')
            continue
        current = ''
        for word in paragraph.split(' '):
            while len(word) > cols:            # break a word too long to fit
                if current:
                    lines.append(current)
                    current = ''
                lines.append(word[:cols])
                word = word[cols:]
            candidate = f'{current} {word}'.strip()
            if len(candidate) <= cols:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        lines.append(current)
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
    """Estimated height in printer dots."""
    kind = block['type']
    if kind == 'blank':
        return FONTS['b']['cell_h'] * block.get('count', 1)
    if kind == 'qr':
        # Plus the quiet zone python-escpos emits around the symbol.
        return (qr_modules(block['value']) + 8) * block.get('size', 4)
    if kind == 'barcode':
        return block.get('height', 60) + FONTS['b']['cell_h']  # symbol + label
    if kind == 'rule':
        return FONTS[block.get('font', 'b')]['cell_h']
    if kind == 'text':
        font = FONTS.get(block.get('font', 'b'), FONTS['b'])
        cols = columns_for(block.get('font', 'b'), block.get('width', 1))
        lines = len(wrap(block['value'], cols))
        return lines * font['cell_h'] * max(int(block.get('height', 1)), 1)
    return 0


def total_height(blocks):
    return sum(block_height(b) for b in blocks)


def height_mm(blocks):
    return total_height(blocks) / DOTS_PER_MM


# --- renderers ----------------------------------------------------------------

def render_escpos(blocks, printer):
    """Emit blocks to a python-escpos printer. Does not cut or close."""
    for block in blocks:
        kind = block['type']
        if kind == 'blank':
            printer.text('\n' * block.get('count', 1))
        elif kind == 'qr':
            printer.set(align='center')
            printer.qr(block['value'], size=block.get('size', 4), model=2)
        elif kind == 'barcode':
            printer.barcode(str(block['value']), 'CODE39',
                            width=2, height=block.get('height', 60),
                            pos='below', align_ct=True)
        elif kind == 'rule':
            font = block.get('font', 'b')
            printer.set(align='center', font=font, bold=False,
                        custom_size=True, width=1, height=1)
            printer.text(block.get('char', '-') * columns_for(font) + '\n')
        elif kind == 'text':
            printer.set(align=block.get('align', 'center'),
                        font=block.get('font', 'b'),
                        bold=block.get('bold', False),
                        custom_size=True,
                        width=max(int(block.get('width', 1)), 1),
                        height=max(int(block.get('height', 1)), 1))
            printer.text(str(block['value']) + '\n')


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
