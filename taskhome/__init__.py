"""TaskHome: scheduled receipts on a USB thermal printer.

Assembled by create_app(), which exists mainly so the scheduler thread is
started deliberately rather than as a side effect of importing a module
(P0-12). Importing this package now does nothing but define things -- no files
are read, no thread starts, no printer is touched -- which is what makes the
package safe to import from scripts and tests.
"""
import os

from flask import Flask

from . import constants, state
from .logsetup import configure_logging, log
from .settings import get_host, get_port

# Submodules are imported eagerly so `taskhome.printing`, `taskhome.recurrence`
# and so on resolve after a plain `import taskhome`. This is only safe because
# importing them has no side effects -- no files read, no threads started, no
# hardware touched. Keep it that way.
from . import layouts, receipt, recurrence, printing, scheduler, storage, styles
from . import listeners, settings, web, logsetup

__all__ = [
    'create_app', 'init_app', 'get_host', 'get_port', 'log',
    # Re-exported so `import taskhome` gives access to every module.
    'constants', 'state', 'storage', 'recurrence', 'printing', 'scheduler',
    'receipt', 'layouts', 'listeners', 'settings', 'styles', 'web', 'logsetup',
]




def create_app(load=True, with_scheduler=False):
    """Build the Flask app.

    with_scheduler defaults to False: a factory may be called more than once (tests,
    a reloader, a multi-worker server) and each call starting another thread
    would mean duplicate receipts. The entry point opts in explicitly.
    """
    from .web import bp

    app = Flask(__name__)
    app.register_blueprint(bp)


    configure_logging()
    if os.environ.get('TASKHOME_DEV') == '1':
        # Re-read templates on every request so edits show up on refresh.
        # Deliberately opt-in: it costs a stat per template per request, which
        # is pointless on an appliance that changes once a month.
        app.config['TEMPLATES_AUTO_RELOAD'] = True
        app.jinja_env.auto_reload = True
        log.info('Dev mode: templates reload on every request')
    if load:
        storage.load_data()
        configure_logging()   # re-apply now that config['log_level'] is known
    if with_scheduler:
        scheduler.start_scheduler()
    return app


def init_app(load=True, scheduler=True):
    """Backwards-compatible alias used by the entry point."""
    return create_app(load=load, with_scheduler=scheduler)
