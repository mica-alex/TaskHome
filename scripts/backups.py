#!/usr/bin/env python
"""Inspect and restore TaskHome data backups (MASTER_PLAN P6-2).

Snapshots are taken automatically of the file about to be overwritten, so the
previous good state survives a bad write. They live in `data/backups/<store>/`.

    ./scripts/backups.py list                    # what exists, per store
    ./scripts/backups.py list tasks              # one store, with sizes
    ./scripts/backups.py show tasks 20260727T130000Z
    ./scripts/backups.py restore tasks 20260727T130000Z

Restoring backs up the current file first, so a restore is itself undoable.
Stop TaskHome before restoring: the scheduler rewrites tasks.json and
listeners.json on its own and would overwrite what you just put back.
"""
import argparse
import json
import os
import sys

os.environ.setdefault('TASKHOME_NO_INIT', '1')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from taskhome import constants, storage  # noqa: E402
from taskhome.logsetup import log  # noqa: E402

log.setLevel('WARNING')

STORES = list(constants.STORE_FILENAMES)


def human_size(num):
    return f'{num / 1024:.1f} KB' if num >= 1024 else f'{num} B'


def describe(name, entry):
    path = os.path.join(storage.backup_dir(name), entry)
    try:
        size = os.path.getsize(path)
        with open(path) as f:
            data = json.load(f)
        count = len(data) if isinstance(data, (list, dict)) else '?'
    except (OSError, ValueError):
        return f'{entry}  (unreadable)'
    stamp = entry.replace('.json', '')
    return f'{stamp}  {human_size(size):>9}  {count} entries'


def cmd_list(args):
    names = [args.store] if args.store else STORES
    for name in names:
        entries = storage.list_backups(name)
        print(f'\n{name}  ({len(entries)} snapshot(s), keeping {storage.backup_keep()})')
        if not entries:
            print('  none yet')
            continue
        for entry in entries[:args.limit]:
            print('  ' + describe(name, entry))
        if len(entries) > args.limit:
            print(f'  ... {len(entries) - args.limit} older')
    return 0


def resolve(name, stamp):
    """Match a snapshot by full name or unique prefix."""
    entries = storage.list_backups(name)
    matches = [e for e in entries if e.startswith(stamp) or e == f'{stamp}.json']
    if not matches:
        print(f'No snapshot of {name} matching {stamp!r}.')
        print('Try: ./scripts/backups.py list ' + name)
        return None
    if len(matches) > 1:
        print(f'{stamp!r} matches {len(matches)} snapshots; be more specific:')
        for m in matches:
            print('  ' + m.replace('.json', ''))
        return None
    return os.path.join(storage.backup_dir(name), matches[0])


def cmd_show(args):
    path = resolve(args.store, args.stamp)
    if not path:
        return 1
    with open(path) as f:
        print(json.dumps(json.load(f), indent=2))
    return 0


def cmd_restore(args):
    path = resolve(args.store, args.stamp)
    if not path:
        return 1
    target = constants.data_path(constants.STORE_FILENAMES[args.store])

    with open(path) as f:
        payload = json.load(f)          # refuse to restore something unparseable
    count = len(payload) if isinstance(payload, (list, dict)) else '?'

    print(f'Restoring {args.store}: {count} entries')
    print(f'  from {path}')
    print(f'  to   {target}')
    if not args.confirm:
        print('\nRe-run with --confirm to proceed. Stop TaskHome first: the '
              'scheduler rewrites tasks.json and listeners.json on its own.')
        return 1

    # The current file is snapshotted first, so a mistaken restore is undoable.
    if storage.backup_store(args.store, target):
        print('  (current file snapshotted first)')
    if storage._save_json_file(args.store, target, payload):
        print('Restored.')
        return 0
    print('Restore failed; see the log.')
    return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='command', required=True)

    p = sub.add_parser('list', help='show snapshots')
    p.add_argument('store', nargs='?', choices=STORES)
    p.add_argument('--limit', type=int, default=10)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser('show', help='print a snapshot')
    p.add_argument('store', choices=STORES)
    p.add_argument('stamp')
    p.set_defaults(func=cmd_show)

    p = sub.add_parser('restore', help='restore a snapshot over the live file')
    p.add_argument('store', choices=STORES)
    p.add_argument('stamp')
    p.add_argument('--confirm', action='store_true',
                   help='required: this overwrites live data')
    p.set_defaults(func=cmd_restore)

    args = ap.parse_args()
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
