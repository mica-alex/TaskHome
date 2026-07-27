"""Installable web app support (MASTER_PLAN P2B).

The goal is a home-screen icon on a phone that opens TaskHome full-screen, so
adding a task from the kitchen is as quick as it is from a laptop.

What works over plain HTTP, which is all this LAN appliance speaks today:

* iOS "Add to Home Screen" -- standalone display, the icon, the status bar
  colour and the splash screen all come from meta tags, no secure context
  needed. This is the platform the request was actually about.
* The manifest itself, the icons, the theme colour, and every touch
  ergonomics fix.

What does not, and is therefore feature-detected rather than assumed:

* The service worker. `navigator.serviceWorker` is undefined outside a secure
  context, so registration is guarded on `isSecureContext`. On localhost that
  is true today; on `http://server.local:5000` it is false and the app simply
  runs without offline caching. When HTTPS lands (deferred by decision) the
  offline shell starts working with no further changes.
* Android's install prompt, which requires a service worker and therefore
  HTTPS. The manifest is still correct and complete, so this is one TLS
  certificate away rather than a rewrite.

The manifest is a route rather than a static file because start_url has to
agree with the port the app is actually listening on, which is configurable.
"""
from flask import Blueprint, jsonify, make_response, render_template, url_for

from .. import constants, state

bp = Blueprint('pwa', __name__)

#: hsl(210, 98%, 48%) -- the brand colour, so the phone's status bar and task
#: switcher match the appbar rather than flashing white.
THEME_LIGHT = '#0a5cd7'
THEME_DARK = '#0b0e14'


def icon(size, maskable=False):
    name = f'maskable-{size}.png' if maskable else f'icon-{size}.png'
    return {
        'src': url_for('static', filename=f'icons/{name}'),
        'sizes': f'{size}x{size}',
        'type': 'image/png',
        # 'any' and 'maskable' are declared on separate entries, not combined:
        # a single icon claiming both gets cropped by Android's mask on a
        # picture that was not drawn with a safe zone.
        'purpose': 'maskable' if maskable else 'any',
    }


@bp.route('/manifest.webmanifest')
def manifest():
    name = state.config.get('app_name') or 'TaskHome'
    payload = {
        'name': name,
        'short_name': name,
        'description': 'Scheduled tasks and alerts, printed on a receipt printer.',
        'start_url': url_for('main.index'),
        'scope': '/',
        'display': 'standalone',
        'orientation': 'any',
        'background_color': THEME_DARK,
        'theme_color': THEME_LIGHT,
        'icons': [icon(s) for s in (48, 72, 96, 128, 144, 152, 192, 256, 384, 512)]
                 + [icon(192, True), icon(512, True)],
        # Long-press the home screen icon to jump straight to a job. The two
        # things anyone opens this app on a phone to do.
        'shortcuts': [
            {'name': 'Add a task', 'short_name': 'Add',
             'url': url_for('main.task_page') + '#new',
             'icons': [icon(192)]},
            {'name': 'Print queue', 'short_name': 'Queue',
             'url': url_for('main.print_queue'),
             'icons': [icon(192)]},
        ],
    }
    response = jsonify(payload)
    response.headers['Content-Type'] = 'application/manifest+json'
    # Short cache: the manifest embeds a configurable name and port, so a stale
    # copy would send an installed icon to the wrong place.
    response.headers['Cache-Control'] = 'public, max-age=300'
    return response


@bp.route('/service-worker.js')
def service_worker():
    """Served from the root so its scope covers the whole app.

    A worker at /static/service-worker.js can only control /static/, which is
    the single most common reason an otherwise correct PWA never goes offline.
    """
    response = make_response(render_template('service-worker.js'))
    response.headers['Content-Type'] = 'application/javascript'
    # Never cached: this file is how a stale cache gets replaced, so a cached
    # copy of it is a permanently stuck app.
    response.headers['Cache-Control'] = 'no-cache'
    return response


@bp.app_context_processor
def inject_pwa():
    return {
        'pwa_theme_light': THEME_LIGHT,
        'pwa_theme_dark': THEME_DARK,
        'app_version': constants.VERSION,
    }
