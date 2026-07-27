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
from . import (chores, db, layouts, lists, receipt, recurrence, printing,
               queue, scheduler, storage, styles)
from . import listeners, settings, web, logsetup

__all__ = [
    'create_app', 'init_app', 'get_host', 'get_port', 'log',
    # Re-exported so `import taskhome` gives access to every module. Listing
    # them here is also what tells pyflakes the imports are deliberate.
    'constants', 'state', 'storage', 'recurrence', 'printing', 'scheduler',
    'receipt', 'layouts', 'listeners', 'queue', 'settings', 'styles', 'web',
    'logsetup', 'chores', 'db', 'lists',
]




def create_app(load=True, with_scheduler=False):
    """Build the Flask app.

    with_scheduler defaults to False: a factory may be called more than once (tests,
    a reloader, a multi-worker server) and each call starting another thread
    would mean duplicate receipts. The entry point opts in explicitly.
    """
    from .web import bp
    from .web import api, health, pwa

    app = Flask(__name__)
    app.register_blueprint(bp)
    app.register_blueprint(pwa.bp)
    app.register_blueprint(health.bp)
    app.register_blueprint(api.bp)


    @app.context_processor
    def _versioned_static():
        """Stamp static URLs with the app version.

        Without this a browser can hold a stale mica.css indefinitely, and the
        symptom is baffling: the markup is new, the CSS on disk is new, and the
        page renders with half the styling missing. That is exactly how a skip
        link meant to be invisible until Tab ended up displayed as a bare blue
        link in the corner.

        Overriding url_for in the template context rather than editing every
        template means nothing has to remember to do this.
        """
        from flask import url_for as flask_url_for

        def url_for(endpoint, **values):
            if endpoint == 'static' and 'v' not in values:
                values['v'] = constants.VERSION
            return flask_url_for(endpoint, **values)

        return {'url_for': url_for}

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
