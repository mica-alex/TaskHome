#!/usr/bin/env python
"""Print a calibration receipt to measure the printer's real capabilities.

Answers the question MASTER_PLAN P3-3 depends on: how many characters fit on a
line, per font. The Style Studio's live preview is only honest if the browser
models the same column count the printer actually uses, and the published
numbers disagree — escpos's TM-T20II profile reports 48 columns for font A and
64 for font B at 80 mm, while Epson's own documentation is often quoted as
42/56. One ruler print settles it.

Measured on the unit in use (EPSON TM-T20IIIL, 80 mm):

    font A = 48 columns   (confirms escpos's TM-T20II profile; the widely
                           quoted 42 is wrong for this model)
    font B = 64+ columns  (did not wrap at 64; re-run with --width 96)

**This emits physical paper.** It refuses to run without --confirm.

    ./scripts/calibrate_printer.py --confirm

Reading the result: each ruler is a 64-character line. Find where it wraps onto
the next line — that column is the printer's width for that font. The digits
above each ruler mark tens.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the package, never `app` -- importing the entry point builds the Flask
# app AND starts the scheduler thread, which for a read-only tool means running
# a scheduler against live data. That regression is why this comment exists.
import taskhome  # noqa: E402


def ruler(width=64):
    """A standard column ruler: '----+----1----+----2...' where each digit
    marks a multiple of ten."""
    out = []
    for i in range(1, width + 1):
        if i % 10 == 0:
            out.append(str((i // 10) % 10))
        elif i % 5 == 0:
            out.append('+')
        else:
            out.append('-')
    return ''.join(out)


SAMPLE = ('Descenders like g, j, p, q and y are what touch the line below, '
          'so this sample deliberately contains plenty of them: judgy pygmy '
          'paging quietly.')


def spacing_strip():
    """Print the same paragraph at a range of leading values.

    The character cell height is documented as 17 dots for font b, but the
    printed result at 10 dots of leading still read as touching -- so the
    documented figure does not match what the head actually lays down. Rather
    than keep guessing, print the options and measure by eye.
    """
    from taskhome import receipt

    print('Printing line-spacing test strip...')
    try:
        with taskhome.printing.open_printer() as p:
            p.line_spacing()
            p.set(align='center', font='a', bold=True, custom_size=True,
                  width=1, height=1)
            p.text('LINE SPACING TEST\n')
            for leading in (6, 10, 14, 18, 22, 26):
                total = receipt.FONTS['b']['cell_h'] + leading
                p.line_spacing()
                p.set(align='left', font='a', bold=True, custom_size=True,
                      width=1, height=1)
                p.text(f'-- leading {leading} dots ({total} total) --\n')
                p.line_spacing(receipt.spacing_units(total))
                p.set(align='left', font='b', bold=False, custom_size=True,
                      width=1, height=1)
                for wrapped in receipt.wrap(SAMPLE, receipt.columns_for('b')):
                    p.text(wrapped + '\n')
            p.line_spacing()
            p.set(align='center', font='b', custom_size=True, width=1, height=1)
            p.text('\nPick the smallest that reads cleanly.\n\n')
            p.cut()
    except Exception as e:
        print(f'Spacing strip failed: {e}')
        return 1
    print('Done. Tell me which leading value looks right; it becomes '
          'receipt.LEADING_DOTS.')
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--confirm', action='store_true',
                    help='required: acknowledges this produces physical paper')
    ap.add_argument('--minimal', action='store_true',
                    help='only the two font rulers (~5cm instead of ~15cm)')
    ap.add_argument('--spacing', action='store_true',
                    help='print a line-spacing test strip: the same paragraph '
                         'at several leading values, labelled, so the most '
                         'readable one can be chosen by eye rather than by '
                         'arithmetic')
    ap.add_argument('--width', type=int, default=64,
                    help='ruler length in characters (default 64). Font B is '
                         'wider than 64 on the TM-T20IIIL, so use --width 96 '
                         'to find its wrap point.')
    args = ap.parse_args()

    if not args.confirm:
        ap.error('refusing to print without --confirm (this emits real paper)')

    if not taskhome.printing.is_printer_connected():
        print('Printer not detected at '
              f'{taskhome.constants.VID:04x}:{taskhome.constants.PID:04x}. Nothing printed.')
        return 1

    if args.spacing:
        return spacing_strip()

    line = ruler(args.width)
    print('Printing calibration receipt...')

    try:
        with taskhome.printing.open_printer() as p:
            p.set(align='center', font='a', bold=True, custom_size=True,
                  width=1, height=1)
            p.text('TASKHOME CALIBRATION\n')
            p.set(align='left', font='b', bold=False, custom_size=True,
                  width=1, height=1)
            p.text(f'profile TM-T20II / {taskhome.constants.PRINTER_MODEL}\n')
            p.text('-' * 40 + '\n')

            # The measurement itself. Left-aligned so the wrap point is
            # unambiguous; centring would hide it.
            p.set(align='left', font='a', bold=False, custom_size=True,
                  width=1, height=1)
            p.text(f'FONT A (default) - ruler {args.width}\n')
            p.text(line + '\n')

            p.set(align='left', font='b', bold=False, custom_size=True,
                  width=1, height=1)
            p.text(f'FONT B (small) - ruler {args.width}\n')
            p.text(line + '\n')

            if not args.minimal:
                # How the size multipliers actually render, so the preview's
                # character-cell scaling can be checked against reality.
                p.set(align='left', font='b', bold=False, custom_size=True,
                      width=1, height=1)
                p.text('-' * 40 + '\n')
                p.text('SIZE MULTIPLIERS (font a)\n')
                for n in (1, 2, 3):
                    p.set(align='left', font='a', bold=False, custom_size=True,
                          width=n, height=n)
                    p.text(f'{n}x ABCDEFGHIJ\n')

                p.set(align='left', font='b', bold=False, custom_size=True,
                      width=1, height=1)
                p.text('-' * 40 + '\n')
                p.text('BOLD / DENSITY\n')
                p.set(align='left', font='a', bold=True, custom_size=True,
                      width=1, height=1)
                p.text('bold on   ABCDEFGHIJ\n')
                p.set(align='left', font='a', bold=False, custom_size=True,
                      width=1, height=1, density=0)
                p.text('density 0 ABCDEFGHIJ\n')
                p.set(align='left', font='a', bold=False, custom_size=True,
                      width=1, height=1, density=8)
                p.text('density 8 ABCDEFGHIJ\n')

            p.set(align='left', font='b', bold=False, custom_size=True,
                  width=1, height=1)
            p.text('-' * 40 + '\n')
            p.text('Count where each ruler wraps.\n')
            p.text('That column = the font width.\n')
            p.text('\n')
            p.cut()
    except Exception as e:
        print(f'Calibration print failed: {e}')
        return 1

    print('Done. Read the wrap column off each ruler and record it in')
    print('docs/printing.md and the Style Studio template paper settings.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
