"""MQTT / Home Assistant (MASTER_PLAN P5-2 #9).

Subscribe to topics and print what arrives. With Home Assistant that means any
automation can print a receipt with one `mqtt.publish` action -- doorbell
snapshots, "washing machine done", a sensor threshold, anything.

**The dependency is optional.** `paho-mqtt` is not in requirements.txt and the
module imports without it; the listener reports itself unavailable and the
settings page explains how to install it. An appliance should not gain a
network dependency for a feature most installs will never switch on, and a
missing import must not stop the whole app loading -- the listener registry is
imported at startup, so a hard import here would take TaskHome down for
everybody who does not use MQTT.

This is the second **push** listener. Unlike the webhook, which is pushed by an
inbound HTTP request handled on a Flask thread, MQTT holds a long-lived
connection and delivers on paho's own network thread. Both go through
`base.deliver()`, and printing is serialised by `printing.PRINT_LOCK` -- which
had to be added for exactly this reason.
"""
import json
import threading
from datetime import datetime, timezone

from . import base
from .. import layouts, receipt
from ..logsetup import log

try:                                     # pragma: no cover - environment dependent
    import paho.mqtt.client as paho
    PAHO_ERROR = None
except Exception as exc:                 # pragma: no cover
    paho = None
    PAHO_ERROR = str(exc)

INSTALL_HINT = '.venv/bin/pip install paho-mqtt'

#: Payload larger than this is almost certainly not meant for a receipt --
#: a camera snapshot published to the wrong topic, typically.
MAX_PAYLOAD = 8 * 1024


def available():
    return paho is not None


class MQTTListener(base.Listener):
    name = 'mqtt'
    title = 'MQTT / Home Assistant'
    description = ('Prints messages published to topics you subscribe to. Any '
                   'Home Assistant automation can print with one mqtt.publish.')
    accepts_push = True
    max_prints_per_poll = 5          # per message batch

    CONFIG_SCHEMA = (
        base.field('enabled', 'Enabled', 'bool', default=False,
                   help='Hold a connection to the broker and print what arrives.'),
        base.field('host', 'Broker host', 'text', default='', group='Broker',
                   help='Home Assistant users: the machine running Mosquitto, '
                        'often the same one as Home Assistant itself.'),
        base.field('port', 'Port', 'int', default=1883, min=1, max=65535,
                   group='Broker'),
        base.field('username', 'Username', 'text', default='', group='Broker'),
        base.field('password', 'Password', 'secret', default='', group='Broker'),
        base.field('tls', 'Use TLS', 'bool', default=False, group='Broker',
                   help='Port is usually 8883 with TLS.'),
        base.field('topics', 'Topics', 'multiselect',
                   default=['taskhome/print/#'], group='Subscriptions',
                   help='MQTT wildcards work: + for one level, # for the rest.'),
        base.field('max_per_hour', 'Maximum receipts per hour', 'int', default=30,
                   min=1, max=300, group='Subscriptions',
                   help='A chatty sensor on a wildcard topic can empty a roll '
                        'in an afternoon. This is the backstop.'),
        base.field('retained', 'Print retained messages', 'bool', default=False,
                   group='Subscriptions',
                   help='Off by default. A retained message is redelivered on '
                        'every reconnect, so this reprints the same receipt '
                        'each time the connection blips.'),
    )

    PLACEHOLDERS = {
        'title': 'Washing machine finished',
        'body': 'Cycle complete. Second load is in the basket.',
        'topic': 'taskhome/print/laundry',
        'received': '6:42 PM 7/27/26',
    }

    # --- connection ----------------------------------------------------------

    _client = None
    _lock = threading.Lock()
    _connected = False
    _last_error = None

    def parse(self, topic, payload, retained=False):
        """A message -> an item. Raises ValueError with a reason.

        Accepts JSON or plain text, like the webhook: an automation that
        publishes `"Bins tonight"` should work without wrapping it in an
        object.
        """
        if len(payload) > MAX_PAYLOAD:
            raise ValueError(f'Payload over {MAX_PAYLOAD} bytes')
        text = payload.decode('utf-8', errors='replace').strip()
        if not text:
            raise ValueError('Empty payload')

        data = None
        if text.startswith('{'):
            try:
                data = json.loads(text)
            except ValueError:
                data = None
        if not isinstance(data, dict):
            data = {'title': text}

        title = str(data.get('title') or data.get('message') or '').strip()
        body = str(data.get('body') or '').strip()
        if not title and not body:
            raise ValueError('No title or body')
        if not title:
            title, body = body[:120], body[120:].strip()

        return {
            # Topic plus timestamp: two identical messages a minute apart are
            # two events, not a duplicate.
            'id': f"{topic}@{datetime.now(timezone.utc).isoformat()}",
            'title': title[:120],
            'body': body[:1200],
            'topic': topic,
            'retained': retained,
            'received': datetime.now(timezone.utc).isoformat(),
        }

    def within_rate_limit(self, config, now=None):
        from datetime import timedelta
        now = now or datetime.now(timezone.utc)
        runtime = self.state()
        cutoff = now - timedelta(hours=1)
        recent = []
        for stamp in runtime.get('recent') or []:
            try:
                if datetime.fromisoformat(stamp) > cutoff:
                    recent.append(stamp)
            except (TypeError, ValueError):
                continue
        runtime['recent'] = recent[-400:]
        try:
            limit = max(int(config.get('max_per_hour', 30)), 1)
        except (TypeError, ValueError):
            limit = 30
        return len(recent) < limit, len(recent), limit

    def note_delivery(self, now=None):
        now = now or datetime.now(timezone.utc)
        runtime = self.state()
        runtime['recent'] = (runtime.get('recent') or [])[-399:] + [now.isoformat()]

    def on_message(self, topic, payload, retained=False):
        """Called from paho's network thread. Must not raise into paho."""
        try:
            config = self.config()
            if retained and not config.get('retained'):
                log.debug(f'MQTT: ignoring retained message on {topic}')
                return
            allowed, used, limit = self.within_rate_limit(config)
            if not allowed:
                log.warning(f'MQTT rate limit reached ({used}/{limit} this hour)')
                return
            item = self.parse(topic, payload, retained)
            self.note_delivery()
            base.deliver(self, [item])
        except ValueError as e:
            log.info(f'MQTT: ignoring message on {topic}: {e}')
        except Exception as e:
            # An exception escaping into paho's loop kills the network thread
            # silently, and the listener then looks connected while receiving
            # nothing at all.
            log.error(f'MQTT: failed handling {topic}: {e}', exc_info=True)

    def ensure_connected(self):
        """Connect if configured and not already connected. Safe to call often.

        Driven from the scheduler tick rather than at import: it doubles as the
        reconnect path, and it means a dev server started without a scheduler
        holds no broker connection.
        """
        config = self.config()
        if not config.get('enabled') or not config.get('host'):
            self.disconnect()
            return False
        if not available():
            self._last_error = f'paho-mqtt is not installed ({INSTALL_HINT})'
            return False

        with self._lock:
            if self._client is not None and self._connected:
                return True
            if self._client is not None:
                return False        # paho is retrying on its own
            try:
                self._client = self._build_client(config)
                self._client.connect_async(config['host'],
                                           int(config.get('port') or 1883), 60)
                self._client.loop_start()
                log.info(f"MQTT connecting to {config['host']}")
                return True
            except Exception as e:
                self._last_error = str(e)
                self._client = None
                log.error(f'MQTT connect failed: {e}')
                return False

    def _build_client(self, config):
        client = paho.Client(
            paho.CallbackAPIVersion.VERSION2,
            client_id=f'taskhome-{datetime.now(timezone.utc).timestamp():.0f}',
            clean_session=True)
        if config.get('username'):
            client.username_pw_set(config['username'], config.get('password') or None)
        if config.get('tls'):
            client.tls_set()

        listener = self

        def on_connect(client_, userdata, flags, reason_code, properties=None):
            if getattr(reason_code, 'is_failure', False) or reason_code != 0:
                listener._connected = False
                listener._last_error = f'Broker refused the connection ({reason_code})'
                log.error(f'MQTT: {listener._last_error}')
                return
            listener._connected = True
            listener._last_error = None
            for topic in config.get('topics') or []:
                if topic.strip():
                    client_.subscribe(topic.strip(), qos=1)
                    log.info(f'MQTT subscribed to {topic.strip()}')

        def on_disconnect(client_, userdata, flags=None, reason_code=None,
                          properties=None):
            listener._connected = False
            log.warning(f'MQTT disconnected ({reason_code}); paho will retry')

        def on_message(client_, userdata, message):
            listener.on_message(message.topic, message.payload,
                                bool(getattr(message, 'retain', False)))

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        # paho's own backoff, so a broker that is down does not become a busy
        # loop and does not need reconnect logic here.
        client.reconnect_delay_set(min_delay=1, max_delay=120)
        return client

    def disconnect(self):
        with self._lock:
            if self._client is None:
                return
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception as e:
                log.warning(f'MQTT disconnect: {e}')
            self._client = None
            self._connected = False

    # --- UI ------------------------------------------------------------------

    def notice(self):
        if not available():
            return {
                'title': 'paho-mqtt is not installed',
                'body': 'This listener needs one extra package. It is optional '
                        'so that installs which do not use MQTT carry no extra '
                        'dependency.',
                'code': INSTALL_HINT,
            }
        config = self.config()
        if not config.get('host'):
            return {
                'title': 'Publish to print',
                'body': 'Point this at your broker, then any Home Assistant '
                        'automation can print with one action.',
                'code': ('service: mqtt.publish\n'
                         'data:\n'
                         '  topic: taskhome/print/laundry\n'
                         '  payload: >-\n'
                         '    {"title": "Washing machine finished"}'),
            }
        return {
            'title': 'Connected' if self._connected else 'Not connected',
            'body': self._last_error or
                    f"Subscribed to {', '.join(config.get('topics') or [])}.",
            'code': ('service: mqtt.publish\n'
                     'data:\n'
                     '  topic: taskhome/print/laundry\n'
                     '  payload: >-\n'
                     '    {"title": "Washing machine finished"}'),
        }

    def summary(self):
        if not available():
            return 'paho-mqtt is not installed.'
        config = self.config()
        if not config.get('host'):
            return 'No broker configured -- nothing will print.'
        state_word = 'connected' if self._connected else 'not connected'
        return f"{config['host']} ({state_word})"

    # --- receipt -------------------------------------------------------------

    def dedup_key(self, item):
        return item.get('id')

    def describe(self, item):
        return f"{item.get('topic', 'mqtt')}: {item.get('title', '')[:40]}"

    def context(self, item):
        return {
            'title': item.get('title', ''),
            'body': item.get('body', ''),
            'topic': item.get('topic', ''),
            'received': layouts._stamp(),
        }

    def blocks_from_context(self, context):
        blocks = [
            receipt.text(context['title'], font='a', width=2, height=2, bold=True),
        ]
        if context.get('body'):
            blocks.append(receipt.gap(6))
            blocks.append(receipt.text(context['body'], font='b', align='left'))
        blocks.append(receipt.rule())
        blocks.append(receipt.text(f"{context['topic']}  -  {context['received']}",
                                   font='b'))
        return blocks

    def receipt_blocks(self, item):
        return self.blocks_from_context(self.context(item))

    def template_presets(self):
        markers = {key: '{%s}' % key for key in self.PLACEHOLDERS}
        return [(f'{self.name}-default', self.blocks_from_context(markers))]

    def history_record(self, item):
        return {
            'type': 'mqtt',
            'id': item.get('id'),
            'category': item.get('topic', 'MQTT'),
            'title': item.get('title', ''),
            'description': item.get('body', '')[:500],
            'reported_at': item.get('received', ''),
            'print_time': datetime.now().isoformat(),
        }


listener = base.register(MQTTListener())
