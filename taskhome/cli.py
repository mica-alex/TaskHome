"""Command line entry point (MASTER_PLAN P6-5).

`taskhome` on PATH after `pip install -e .`, so a service unit does not have to
know where the checkout lives or which interpreter to use.

`--data-dir` is the one that matters. Until now the datastore was anchored to
the repo root, which means a checkout is not just code -- it holds the user's
tasks, history and unprinted receipts, and `git clean` or a fresh clone
elsewhere loses them. Pointing it at `~/.taskhome` finally decouples the two.

The default is unchanged, because changing where an existing install looks for
its data would silently strand it.
"""
import argparse
import os
import sys


def build_parser():
    parser = argparse.ArgumentParser(
        prog='taskhome',
        description='Print scheduled tasks and alerts on a thermal receipt printer.')
    parser.add_argument('--host', help='Interface to bind (default 0.0.0.0).')
    parser.add_argument('--port', type=int,
                        help='Port to serve on (default 5000; 5001 on macOS, '
                             'where AirPlay Receiver holds 5000).')
    parser.add_argument('--data-dir', metavar='PATH',
                        help='Where config/tasks/history/queue live. '
                             'Defaults to <repo>/data. Use ~/.taskhome to keep '
                             'your data out of the checkout.')
    parser.add_argument('--no-scheduler', action='store_true',
                        help='Serve the web UI without the background thread. '
                             'Nothing polls and nothing prints on its own.')
    parser.add_argument('--check', action='store_true',
                        help='Report health and exit; 0 if healthy, 1 if not.')
    parser.add_argument('--version', action='store_true', help='Print the version and exit.')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    from . import constants, create_app, settings

    # Setting TASKHOME_DATA_DIR here would be too late: reaching this module
    # imported the package, which already resolved every store path. The flag
    # has to repoint them explicitly, or it is silently ignored -- which would
    # mean writing to the real datastore while claiming to use a scratch one.
    if args.data_dir:
        constants.set_data_dir(args.data_dir)
        os.environ['TASKHOME_DATA_DIR'] = constants.DATA_DIR

    if args.version:
        print(f'taskhome {constants.VERSION}')
        return 0

    if args.check:
        return _check()

    host = args.host or settings.get_host()
    port = args.port or settings.get_port()

    # with_scheduler defaults to on here, unlike create_app: this is the entry
    # point a person runs deliberately, and a TaskHome that never prints is not
    # what they asked for.
    app = create_app(load=True, with_scheduler=not args.no_scheduler)
    print(f'TaskHome {constants.VERSION} on http://{host}:{port}  '
          f'(data: {constants.DATA_DIR})', file=sys.stderr)
    if args.no_scheduler:
        print('Scheduler disabled: nothing will print on its own.', file=sys.stderr)
    app.run(host=host, port=port)
    return 0


def _check():
    """Health without starting a server, for a cron or a pre-flight check."""
    from . import create_app
    from .web import health

    create_app(load=True, with_scheduler=False)
    data = health.snapshot()
    problems = health.problems(data)

    print(f"printer   : {'connected' if data['printer']['connected'] else 'not connected'}")
    print(f"scheduler : {data['scheduler']['status']}")
    print(f"queue     : {data['queue']['waiting']} waiting, {data['queue']['parked']} parked")
    print(f"tasks     : {data['stores']['tasks']}")
    for problem in problems:
        print(f'PROBLEM   : {problem}')
    print('healthy' if not problems else 'UNHEALTHY')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
