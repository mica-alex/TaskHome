"""Inbound webhook receiver (MASTER_PLAN P5-2 #1).

`POST /api/inbound/<token>` with `{"title": "...", "body": "..."}` prints a
receipt. That one endpoint is the highest leverage-per-line integration in the
plan: it makes TaskHome a printer for Apple Shortcuts, Home Assistant, IFTTT,
Zapier, cron, a CI pipeline, or three lines of curl, without any of them
needing to know anything about this codebase.

It is a **push** listener -- nothing is polled. `base.deliver()` runs the same
tail as a polled listener (dedup, cap, filtering, template, history, queue on
failure), so a webhook receipt behaves exactly like a weather alert once it is
in the system.

Security, such as it is: a shared secret in the URL, over a LAN, with no TLS.
That is honest about what this is -- if the network is hostile, the token is
visible. What the token does buy is that a stray request to a scanned port
cannot print, and that a leaked token can be rotated without touching anything
else. Rate limiting matters more than authentication here, because the failure
mode that actually costs something is a loop emptying the paper roll.
"""
import secrets
from datetime import datetime, timedelta, timezone

from . import base
from .. import layouts, receipt

#: Bodies longer than this are truncated. A runaway script posting a 4 MB log
#: would otherwise print until the roll ran out.
MAX_BODY_CHARS = 1200
MAX_TITLE_CHARS = 120

#: Sliding-window rate limit. The point is not abuse prevention but a stuck
#: loop: something retrying every second overnight is thousands of receipts.
DEFAULT_MAX_PER_HOUR = 20


def new_token():
    """A URL-safe secret. 32 bytes, because it is going in a URL that may end
    up in someone's shell history and a short token invites guessing."""
    return secrets.token_urlsafe(32)


class WebhookListener(base.Listener):
    name = 'webhook'
    title = 'Webhook'
    description = ('Prints whatever is POSTed to your private URL. Works with '
                   'Shortcuts, Home Assistant, IFTTT, cron, or curl.')
    accepts_push = True
    max_prints_per_poll = 10        # per delivery

    CONFIG_SCHEMA = (
        base.field('enabled', 'Enabled', 'bool', default=False,
                   help='Accept inbound requests.'),
        base.field('token', 'Secret token', 'secret', default='', group='Access',
                   help='The last part of your webhook URL. Treat it as a '
                        'password; anyone who has it can print.'),
        base.field('max_per_hour', 'Maximum receipts per hour', 'int',
                   default=DEFAULT_MAX_PER_HOUR, min=1, max=200, group='Access',
                   help='A stuck script retrying every second is thousands of '
                        'receipts overnight. This is the backstop.'),
        base.field('allow_sources', 'Allowed sources', 'multiselect', default=[],
                   group='Access',
                   help='Optional. If set, only requests whose "source" field '
                        'matches one of these will print.'),
    )

    PLACEHOLDERS = {
        'title': 'Washing machine finished',
        'body': 'Cycle complete at 6:42 PM. Second load is in the basket.',
        'source': 'home-assistant',
        'url': 'http://homeassistant.local/lovelace/laundry',
        'received': '6:42 PM 7/27/26',
    }

    # --- push ----------------------------------------------------------------

    def parse(self, payload):
        """Turn a posted body into an item. Raises ValueError with a message.

        Accepts either a JSON object or a bare string, because half the things
        that will call this are a shell script with `-d "Bins tonight"`.
        """
        if isinstance(payload, str):
            payload = {'title': payload}
        if not isinstance(payload, dict):
            raise ValueError('Expected a JSON object or a string.')

        title = str(payload.get('title') or payload.get('text') or '').strip()
        body = str(payload.get('body') or payload.get('message') or '').strip()
        if not title and not body:
            raise ValueError('Provide at least a title or a body.')
        if not title:
            # A receipt with only body text reads badly, and the first line is
            # what someone sees on the paper. Promote it.
            title, body = body[:MAX_TITLE_CHARS], body[MAX_TITLE_CHARS:].strip()

        return {
            'id': str(payload.get('id') or secrets.token_hex(8)),
            'title': title[:MAX_TITLE_CHARS],
            'body': body[:MAX_BODY_CHARS],
            'source': str(payload.get('source') or '').strip()[:40],
            'url': str(payload.get('url') or '').strip(),
            'received': datetime.now(timezone.utc).isoformat(),
        }

    def check_token(self, config, token):
        """Constant-time comparison, and a refusal when no token is set.

        An empty configured token must never match an empty supplied one, or
        switching the listener on before generating a token would leave the
        endpoint open to anyone who finds it.
        """
        expected = config.get('token') or ''
        if not expected or not token:
            return False
        return secrets.compare_digest(str(expected), str(token))

    def within_rate_limit(self, config, now=None):
        """Sliding one-hour window over recent delivery timestamps."""
        now = now or datetime.now(timezone.utc)
        runtime = self.state()
        window_start = now - timedelta(hours=1)

        recent = []
        for stamp in runtime.get('recent') or []:
            try:
                when = datetime.fromisoformat(stamp)
            except (TypeError, ValueError):
                continue
            if when > window_start:
                recent.append(stamp)

        limit = config.get('max_per_hour', DEFAULT_MAX_PER_HOUR)
        try:
            limit = max(int(limit), 1)
        except (TypeError, ValueError):
            limit = DEFAULT_MAX_PER_HOUR

        runtime['recent'] = recent[-200:]
        return len(recent) < limit, len(recent), limit

    def note_delivery(self, now=None):
        now = now or datetime.now(timezone.utc)
        runtime = self.state()
        runtime['recent'] = (runtime.get('recent') or [])[-199:] + [now.isoformat()]

    def should_print(self, config, item, now=None):
        allowed = config.get('allow_sources') or []
        if allowed and item.get('source') not in allowed:
            return False, f"source {item.get('source') or '(none)'!r} is not allowed"
        return True, ''

    # --- receipt -------------------------------------------------------------

    def dedup_key(self, item):
        return item.get('id')

    def describe(self, item):
        return item.get('title', 'Webhook')[:60]

    def context(self, item):
        return {
            'title': item.get('title', ''),
            'body': item.get('body', ''),
            'source': item.get('source', ''),
            'url': item.get('url', ''),
            'received': layouts._stamp(),
        }

    def blocks_from_context(self, context, qr=True):
        blocks = []
        if qr and context.get('url'):
            blocks.append(receipt.qr(context['url'], size=4))
        blocks.append(receipt.text(context['title'], font='a', width=2, height=2,
                                   bold=True))
        if context.get('body'):
            blocks.append(receipt.gap(6))
            blocks.append(receipt.text(context['body'], font='b', align='left'))
        blocks.append(receipt.rule())
        footer = context.get('source') or 'Webhook'
        blocks.append(receipt.text(f"{footer}  -  {context['received']}", font='b'))
        return blocks

    def receipt_blocks(self, item):
        return self.blocks_from_context(self.context(item))

    def template_presets(self):
        markers = {key: '{%s}' % key for key in self.PLACEHOLDERS}
        return [
            (f'{self.name}-default', self.blocks_from_context(markers, qr=True)),
            (f'{self.name}-plain', self.blocks_from_context(markers, qr=False)),
        ]

    def history_record(self, item):
        return {
            'type': 'webhook',
            'id': item.get('id'),
            'title': item.get('title', ''),
            'category': item.get('source') or 'Webhook',
            'description': item.get('body', '')[:500],
            'url': item.get('url', ''),
            'reported_at': item.get('received', ''),
            'print_time': datetime.now().isoformat(),
        }

    def notice(self):
        from .. import settings, state, constants
        config = self.config()
        if not config.get('token'):
            return {
                'title': 'No token yet',
                'body': 'Generate one to get your webhook URL. Anyone with the '
                        'URL can print, so treat it as a password.',
                'action': {'label': 'Generate token', 'endpoint': '/api/webhook/token'},
            }
        host = state.config.get('hostname', constants.DEFAULT_CONFIG['hostname'])
        url = f"http://{host}:{settings.get_port()}/api/inbound/{config['token']}"
        return {
            'title': 'Your webhook URL',
            'body': 'POST JSON like {"title": "...", "body": "..."} here, or '
                    'just plain text. Anyone with this URL can print.',
            'code': f'curl -X POST {url} \\\n  -d \'{{"title": "Bins tonight"}}\'',
            'copy': url,
            'action': {'label': 'Rotate token', 'endpoint': '/api/webhook/token',
                       'confirm': 'Rotate the token? Anything using the old URL '
                                  'will stop working.'},
        }

    def summary(self):
        config = self.config()
        if not config.get('token'):
            return 'No token yet -- generate one to get your URL.'
        runtime = self.state()
        count = len(runtime.get('recent') or [])
        return f'{count} receipt(s) in the last hour'


listener = base.register(WebhookListener())
