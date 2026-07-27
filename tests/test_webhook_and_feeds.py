"""Webhook receiver (P5-2 #1) and RSS digest (P5-2 #5)."""
import pytest

from taskhome import constants, create_app, printing, state, storage
from taskhome.listeners import base, feeds, webhook


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(state, 'config', {'theme': 'system', 'max_history': 500,
                                          'hostname': 'taskhome.local'})
    monkeypatch.setattr(state, 'listeners', {})
    monkeypatch.setattr(state, 'history', [])
    for name in ('save_listeners', 'save_history'):
        monkeypatch.setattr(storage, name, lambda: True)
    monkeypatch.setattr(printing, 'print_blocks', lambda b: True)
    monkeypatch.setattr(printing, 'record_history', lambda r: None)
    app = create_app(load=False, with_scheduler=False)
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def enabled(client):
    token = webhook.new_token()
    state.listeners['webhook'] = {'enabled': True, 'token': token, 'max_per_hour': 50}
    return token


# --- push in the interface ----------------------------------------------------

def test_a_push_listener_is_not_polled():
    """Its items arrive through deliver(); a poll sweep must skip it."""
    from datetime import datetime, timezone
    state.listeners['webhook'] = {'enabled': True, 'token': 'x'}
    assert base.run(webhook.listener, datetime.now(timezone.utc)) == 0


def test_push_and_poll_share_the_pipeline(client, monkeypatch):
    """A pushed receipt must dedup, filter, template and queue exactly like a
    polled one -- a push path that reimplemented those would get queueing
    wrong, which is the part that loses receipts."""
    from taskhome import queue
    monkeypatch.setattr(printing, 'print_blocks', lambda b: False)
    state.listeners['webhook'] = {'enabled': True, 'token': 't'}
    item = webhook.listener.parse({'title': 'Offline test'})
    base.deliver(webhook.listener, [item])
    assert len(queue.load_queue()) == 1, 'a failed push print was not queued'


def test_delivery_dedups(client):
    state.listeners['webhook'] = {'enabled': True, 'token': 't'}
    item = webhook.listener.parse({'id': 'same', 'title': 'Once'})
    assert base.deliver(webhook.listener, [item]) == 1
    assert base.deliver(webhook.listener, [item]) == 0


# --- webhook auth and limits --------------------------------------------------

def test_a_disabled_webhook_refuses_everything(client):
    state.listeners['webhook'] = {'enabled': False, 'token': 'secret'}
    assert client.post('/api/inbound/secret', json={'title': 'x'}).status_code == 404


def test_a_wrong_token_is_404_not_403(client, enabled):
    """403 would tell a scanner it had found the right endpoint."""
    assert client.post('/api/inbound/wrong', json={'title': 'x'}).status_code == 404


def test_an_empty_configured_token_never_matches(client):
    """Switching the listener on before generating a token must not leave the
    endpoint open to anyone who finds it."""
    state.listeners['webhook'] = {'enabled': True, 'token': ''}
    config = webhook.listener.config()
    assert webhook.listener.check_token(config, '') is False
    assert webhook.listener.check_token(config, 'anything') is False


def test_a_valid_post_prints(client, enabled):
    response = client.post(f'/api/inbound/{enabled}',
                           json={'title': 'Washing done', 'body': 'Second load in.'})
    assert response.status_code == 200
    assert response.get_json()['data']['printed'] == 1


def test_plain_text_is_accepted(client, enabled):
    """Half the things that will call this are a shell script with -d."""
    response = client.post(f'/api/inbound/{enabled}', data='Bins tonight',
                           content_type='text/plain')
    assert response.status_code == 200
    assert response.get_json()['data']['title'] == 'Bins tonight'


def test_a_body_with_no_title_is_promoted(client):
    """A receipt whose first line is blank reads badly."""
    item = webhook.listener.parse({'body': 'Just the body text'})
    assert item['title'] == 'Just the body text'


def test_an_empty_payload_is_refused(client, enabled):
    assert client.post(f'/api/inbound/{enabled}', json={'foo': 1}).status_code == 400


def test_oversized_input_is_truncated_not_refused():
    """A runaway script posting a 4 MB log would print until the roll ran out;
    refusing outright would lose a legitimate long message."""
    item = webhook.listener.parse({'title': 'x' * 500, 'body': 'y' * 5000})
    assert len(item['title']) <= webhook.MAX_TITLE_CHARS
    assert len(item['body']) <= webhook.MAX_BODY_CHARS


def test_the_rate_limit_stops_a_stuck_loop(client):
    """The failure that actually costs something is a script retrying every
    second overnight."""
    token = webhook.new_token()
    state.listeners['webhook'] = {'enabled': True, 'token': token, 'max_per_hour': 3}
    codes = [client.post(f'/api/inbound/{token}', json={'title': f'n{i}'}).status_code
             for i in range(5)]
    assert codes.count(200) == 3
    assert codes[-1] == 429


def test_a_rate_limited_response_says_when_to_retry(client):
    token = webhook.new_token()
    state.listeners['webhook'] = {'enabled': True, 'token': token, 'max_per_hour': 1}
    client.post(f'/api/inbound/{token}', json={'title': 'a'})
    response = client.post(f'/api/inbound/{token}', json={'title': 'b'})
    assert response.headers.get('Retry-After')


def test_source_filtering(client):
    token = webhook.new_token()
    state.listeners['webhook'] = {'enabled': True, 'token': token,
                                  'allow_sources': ['home-assistant']}
    blocked = client.post(f'/api/inbound/{token}',
                          json={'title': 'x', 'source': 'random'})
    assert blocked.get_json()['data']['printed'] == 0, 'filter did not apply'
    allowed = client.post(f'/api/inbound/{token}',
                          json={'title': 'y', 'source': 'home-assistant'})
    assert allowed.get_json()['data']['printed'] == 1


def test_rotating_the_token_invalidates_the_old_one(client, enabled):
    new = client.post('/api/webhook/token').get_json()['data']['token']
    assert new != enabled
    assert client.post(f'/api/inbound/{enabled}', json={'title': 'x'}).status_code == 404
    assert client.post(f'/api/inbound/{new}', json={'title': 'x'}).status_code == 200


def test_the_settings_page_shows_the_url(client, enabled):
    body = client.get('/listener/settings/webhook').get_data(as_text=True)
    assert '/api/inbound/' in body and enabled in body


def test_without_a_token_the_page_offers_to_make_one(client):
    state.listeners['webhook'] = {'enabled': True, 'token': ''}
    body = client.get('/listener/settings/webhook').get_data(as_text=True)
    assert 'Generate token' in body


# --- feeds --------------------------------------------------------------------

RSS = b'''<?xml version="1.0"?><rss version="2.0"><channel>
<title>Example News</title>
<item><title>First &amp; foremost</title><link>https://x/1</link>
<guid>g1</guid><description>&lt;p&gt;Body&lt;/p&gt;</description></item>
<item><title>Second</title><link>https://x/2</link><guid>g2</guid></item>
</channel></rss>'''

ATOM = b'''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<title>Releases</title>
<entry><title>v1.2.3</title><id>tag:1</id>
<link rel="alternate" href="https://x/v123"/></entry>
</feed>'''


@pytest.mark.parametrize('content,expected_title,expected_count', [
    (RSS, 'Example News', 2), (ATOM, 'Releases', 1)])
def test_both_feed_formats_parse(content, expected_title, expected_count):
    title, entries = feeds.parse_feed(content)
    assert title == expected_title and len(entries) == expected_count


def test_entities_and_tags_are_stripped():
    """Feed titles routinely carry entities and inline markup."""
    _, entries = feeds.parse_feed(RSS)
    assert entries[0]['title'] == 'First & foremost'
    assert '<p>' not in entries[0]['summary']


def test_the_first_poll_of_a_feed_does_not_print_its_backlog(monkeypatch, tmp_path):
    """A busy feed carries 30-40 items. Printed a few per digest that is a
    week of catching up on old news -- the same reason SCF has a catch-up
    policy."""
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(state, 'listeners', {})
    monkeypatch.setattr(storage, 'save_listeners', lambda: True)
    monkeypatch.setattr(feeds, 'fetch_feed',
                        lambda url, etag=None, modified=None: (
                            feeds.parse_feed(RSS)[1], 'Example News', {}))
    config = dict(feeds.listener.config(), urls=['https://x/feed'])
    assert feeds.listener.poll(config, None) == []
    assert len(feeds.listener.state()['seen_links']) == 2


def test_new_items_after_the_first_poll_do_print(monkeypatch, tmp_path):
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(state, 'listeners', {})
    monkeypatch.setattr(storage, 'save_listeners', lambda: True)

    payloads = [RSS, RSS.replace(b'<item><title>Second',
                                 b'<item><title>Brand new</title><link>https://x/3</link>'
                                 b'<guid>g3</guid></item><item><title>Second')]
    calls = {'n': 0}

    def fake(url, etag=None, modified=None):
        content = payloads[min(calls['n'], 1)]
        calls['n'] += 1
        return feeds.parse_feed(content)[1], 'Example News', {}

    monkeypatch.setattr(feeds, 'fetch_feed', fake)
    config = dict(feeds.listener.config(), urls=['https://x/feed'])
    feeds.listener.poll(config, None)                 # first: backlog suppressed
    items = feeds.listener.poll(config, None)
    assert len(items) == 1
    assert items[0]['entries'][0]['title'] == 'Brand new'


def test_one_broken_feed_does_not_stop_the_digest(monkeypatch, tmp_path):
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(state, 'listeners', {})
    monkeypatch.setattr(storage, 'save_listeners', lambda: True)

    def fake(url, etag=None, modified=None):
        if 'broken' in url:
            raise RuntimeError('502')
        return feeds.parse_feed(RSS)[1], 'Example News', {}

    monkeypatch.setattr(feeds, 'fetch_feed', fake)
    config = dict(feeds.listener.config(),
                  urls=['https://broken/feed', 'https://good/feed'])
    feeds.listener.poll(config, None)
    assert feeds.listener.state()['last_failures'] == ['https://broken/feed']


def test_a_digest_is_one_receipt():
    """The whole design decision: forty articles is forty receipts otherwise."""
    assert feeds.FeedListener.max_prints_per_poll == 1


def test_per_feed_cap_stops_one_feed_crowding_out_others(monkeypatch, tmp_path):
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(state, 'listeners', {'feeds': {
        'known_feeds': ['https://x/feed'], 'seen_links': []}})
    monkeypatch.setattr(storage, 'save_listeners', lambda: True)
    monkeypatch.setattr(feeds, 'fetch_feed',
                        lambda url, etag=None, modified=None: (
                            feeds.parse_feed(RSS)[1], 'Example News', {}))
    config = dict(feeds.listener.config(), urls=['https://x/feed'], max_per_feed=1)
    items = feeds.listener.poll(config, None)
    assert len(items[0]['entries']) == 1


def test_the_source_line_survives_wrapping():
    """wrap() strips leading whitespace, so an indented source line would
    silently read as a second headline."""
    context = feeds.listener.context({'entries': [
        {'title': 'Something', 'feed': 'BBC News', 'link': 'https://x/1'}], 'feeds': 1})
    assert '- BBC News' in context['items']


def test_quiet_hours_hold_rather_than_drop(monkeypatch):
    """The runtime only marks an item seen when it prints, so the next poll
    outside quiet hours rebuilds a digest with the same entries."""
    config = dict(feeds.listener.config(),
                  quiet_hours={'start': '00:00', 'end': '23:59'})
    ok, reason = feeds.listener.should_print(config, {'entries': []})
    assert ok is False and reason == 'quiet hours'


def test_conditional_requests_are_sent(monkeypatch):
    """A feed polled hourly is almost always unchanged, and publishers
    rate-limit clients that ignore ETags."""
    captured = {}

    class Resp:
        status_code = 304
        headers = {}
        def raise_for_status(self): pass

    def fake_get(url, headers=None, **kwargs):
        captured.update(headers or {})
        return Resp()

    monkeypatch.setattr(feeds.requests, 'get', fake_get)
    entries, _, validators = feeds.fetch_feed('https://x/feed', etag='"abc"',
                                              modified='Mon, 01 Jan 2026 00:00:00 GMT')
    assert captured['If-None-Match'] == '"abc"'
    assert captured['If-Modified-Since']
    assert entries == [] and validators['etag'] == '"abc"'


def test_both_new_listeners_are_registered_and_editable():
    from taskhome import styles
    for name in ('webhook', 'feeds'):
        assert name in base.registry()
        assert name in styles.kinds(), f'{name} receipts are not editable'
        assert styles.builtin_templates(name)
