#!/usr/bin/env python
"""Print sample receipts from the layout definitions in layouts.py.

Lets a layout change be evaluated on paper before it becomes the default, and
lets the ASCII preview be checked against physical output — the whole point of
the shared renderer (MASTER_PLAN P3-2) is that those two cannot disagree, which
is only worth asserting if someone occasionally verifies it.

**This emits physical paper.** It refuses to run without --confirm.

    ./scripts/print_sample.py --confirm              # new layouts
    ./scripts/print_sample.py --confirm --legacy     # the previous ones
    ./scripts/print_sample.py --preview              # print nothing, show ASCII
"""
import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the package, never `app` -- importing the entry point builds the Flask
# app AND starts the scheduler thread, which for a read-only tool means running
# a scheduler against live data. That regression is why this comment exists.
import taskhome  # noqa: E402
from taskhome import layouts  # noqa: E402
from taskhome import receipt  # noqa: E402

taskhome.log.setLevel('WARNING')

SAMPLE_TASK = {
    'id': 'a1b2c3d4-0000-4000-8000-000000000000',
    'title': 'Play with Sara',
    'extra': 'MISS KITTY TIME',
    'recurring': 'daily',
}
SAMPLE_URL = 'http://localhost:5000/task_page#a1b2c3d4-0000-4000-8000-000000000000'
SAMPLE_ISSUE = {'id': 19840471, 'html_url': 'https://seeclickfix.com/issues/19840471'}
SAMPLE_SCF = dict(
    category='Signal Repair',
    address='239-299 S Lincoln St Manchester NH 03103',
    reported_at='5:58 PM 8/25/25',
    status='Acknowledged',
    has_media=True,
    description=('The signal on Lincoln st is broken. Light will only let 5 cars '
                 'go by at a time on to South Willow.'),
)


def build(legacy, when):
    task_fn = layouts.legacy_task_receipt if legacy else layouts.task_receipt
    scf_fn = layouts.legacy_scf_receipt if legacy else layouts.scf_receipt
    return [
        ('task reminder', task_fn(SAMPLE_TASK, SAMPLE_URL, when)),
        ('SCF issue', scf_fn(SAMPLE_ISSUE, when=when, **SAMPLE_SCF)),
    ]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--confirm', action='store_true',
                    help='required to print: acknowledges this emits real paper')
    ap.add_argument('--legacy', action='store_true', help='use the previous layouts')
    ap.add_argument('--preview', action='store_true',
                    help='show the ASCII preview and print nothing')
    ap.add_argument('--only', choices=['task', 'scf'], help='just one sample')
    args = ap.parse_args()

    when = datetime.now()
    samples = build(args.legacy, when)
    if args.only:
        wanted = 'task reminder' if args.only == 'task' else 'SCF issue'
        samples = [s for s in samples if s[0] == wanted]

    if args.preview or not args.confirm:
        for name, blocks in samples:
            print(f"\n=== {name} ({'legacy' if args.legacy else 'new'}) ===")
            print(receipt.preview(blocks))
        if not args.preview:
            print("\n(preview only - pass --confirm to print)")
        return 0

    if not taskhome.printing.is_printer_connected():
        print('Printer not detected. Nothing printed.')
        return 1

    for name, blocks in samples:
        height = receipt.height_mm(blocks)
        print(f'Printing {name} ({height:.0f} mm estimated)...')
        try:
            with taskhome.printing.open_printer() as p:
                receipt.render_escpos(blocks, p)
                p.cut()
        except Exception as e:
            print(f'  failed: {e}')
            return 1
    print('Done. Compare against the ASCII preview (--preview).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
