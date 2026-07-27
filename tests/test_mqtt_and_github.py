"""MQTT / Home Assistant (P5-2 #9) and GitHub (P5-2 #8)."""
import json

import pytest

from taskhome import constants, printing, state, storage
from taskhome.listeners import base, github, mqtt


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(state, 'listeners', {})
    monkeypatch.setattr(state, 'history', [])
    monkeypatch.setattr(storage, 'save_listeners', lambda: True)
    monkeypatch.setattr(printing, 'print_blocks', lambda b: True)
    monkeypatch.setattr(printing, 'record_history', lambda r: None)
    yield


# --- MQTT: the dependency is optional -----------------------------------------

def test_the_module_imports_without_paho():
    """The listener registry is imported at startup, so a hard import here
    would take TaskHome down for everyone who does not use MQTT."""
    assert mqtt.listener is not None
    assert isinstance(mqtt.available(), bool)


def test_an_absent_dependency_is_reported_not_raised(store, monkeypatch):
    monkeypatch.setattr(mqtt, 'paho', None)
    assert mqtt.listener.ensure_connected() is False
    notice = mqtt.listener.notice()
    assert mqtt.INSTALL_HINT in notice['code']


def test_the_notice_names_the_broker_not_just_the_package(store, monkeypatch):
    """Two things are missing for most people, not one. Saying only "install
    paho-mqtt" sends someone off to run a pip command and come back to a
    listener that still cannot do anything, because the library is a client
    and there is nothing for it to connect to."""
    monkeypatch.setattr(mqtt, 'paho', None)
    notice = mqtt.listener.notice()
    text = f"{notice['title']} {notice['body']}".lower()
    assert 'broker' in text
    assert 'webhook' in text, 'no pointer to the option that needs no broker'


def test_it_is_registered_as_a_push_listener():
    assert mqtt.MQTTListener.accepts_push is True
    assert 'mqtt' in base.registry()


def test_a_push_listener_is_skipped_by_the_poll_sweep(store):
    from datetime import datetime, timezone
    state.listeners['mqtt'] = {'enabled': True, 'host': 'broker'}
    assert base.run(mqtt.listener, datetime.now(timezone.utc)) == 0


# --- MQTT: message handling ---------------------------------------------------

def test_json_and_plain_text_payloads_both_work(store):
    """An automation publishing a bare string should work without wrapping it
    in an object."""
    item = mqtt.listener.parse('taskhome/print/x', b'Bins tonight')
    assert item['title'] == 'Bins tonight'
    item = mqtt.listener.parse(
        'taskhome/print/x', json.dumps({'title': 'Washing', 'body': 'Done'}).encode())
    assert item['title'] == 'Washing' and item['body'] == 'Done'


def test_malformed_json_falls_back_to_text(store):
    item = mqtt.listener.parse('t', b'{not really json')
    assert item['title'] == '{not really json'


@pytest.mark.parametrize('payload', [b'', b'   ', b'{}'])
def test_an_empty_payload_is_refused(store, payload):
    with pytest.raises(ValueError):
        mqtt.listener.parse('t', payload)


def test_an_enormous_payload_is_refused(store):
    """Usually a camera snapshot published to the wrong topic."""
    with pytest.raises(ValueError, match='over'):
        mqtt.listener.parse('t', b'x' * (mqtt.MAX_PAYLOAD + 1))


def test_two_identical_messages_are_two_events(store):
    """Same text a minute apart is two events, not a duplicate."""
    first = mqtt.listener.parse('t', b'Doorbell')
    second = mqtt.listener.parse('t', b'Doorbell')
    assert first['id'] != second['id']


def test_retained_messages_are_ignored_by_default(store):
    """A retained message is redelivered on every reconnect, so printing it
    would reprint the same receipt each time the connection blips."""
    printed = []
    state.listeners['mqtt'] = {'enabled': True, 'host': 'b'}
    base_deliver = base.deliver
    try:
        base.deliver = lambda listener, items: printed.extend(items) or len(items)
        mqtt.listener.on_message('t', b'Hello', retained=True)
        assert printed == []
        mqtt.listener.on_message('t', b'Hello', retained=False)
        assert len(printed) == 1
    finally:
        base.deliver = base_deliver


def test_a_handler_exception_never_escapes_into_paho(store, monkeypatch):
    """An exception escaping paho's loop kills the network thread silently,
    and the listener then looks connected while receiving nothing."""
    monkeypatch.setattr(mqtt.MQTTListener, 'parse',
                        lambda self, *a, **k: 1 / 0)
    state.listeners['mqtt'] = {'enabled': True, 'host': 'b'}
    mqtt.listener.on_message('t', b'anything')      # must not raise


def test_the_rate_limit_holds(store):
    """A chatty sensor on a wildcard topic can empty a roll in an afternoon."""
    state.listeners['mqtt'] = {'enabled': True, 'host': 'b', 'max_per_hour': 2}
    config = mqtt.listener.config()
    for _ in range(2):
        assert mqtt.listener.within_rate_limit(config)[0] is True
        mqtt.listener.note_delivery()
    assert mqtt.listener.within_rate_limit(config)[0] is False


def test_disabled_or_unconfigured_does_not_connect(store):
    state.listeners['mqtt'] = {'enabled': True, 'host': ''}
    assert mqtt.listener.ensure_connected() is False
    state.listeners['mqtt'] = {'enabled': False, 'host': 'broker'}
    assert mqtt.listener.ensure_connected() is False


# --- printing is serialised ---------------------------------------------------

def test_printing_is_serialised_across_threads():
    """MQTT delivers on paho's network thread while the scheduler prints on
    its own; two threads opening the same USB device interleave bytes."""
    import threading
    assert isinstance(printing.PRINT_LOCK, type(threading.Lock()))


# --- GitHub -------------------------------------------------------------------

@pytest.mark.parametrize('raw,expected', [
    ('python/cpython', 'python/cpython'),
    ('https://github.com/python/cpython', 'python/cpython'),
    ('https://github.com/python/cpython.git', 'python/cpython'),
    ('  owner/name  ', 'owner/name'),
    ('not a repo', None),
    ('a/b/c', None),
    ('', None),
])
def test_repo_parsing(raw, expected):
    assert github.parse_repo(raw) == expected


def test_a_token_is_optional(store):
    """A listener that demands a PAT before it does anything is one most
    people never switch on."""
    spec = next(f for f in github.GitHubListener.CONFIG_SCHEMA if f['key'] == 'token')
    assert spec['default'] == ''
    assert github.listener.notice()['title'] == 'Working without a token'


def test_a_token_is_sent_when_present(store):
    headers = github.listener._headers({'token': 'ghp_x'})
    assert headers['Authorization'] == 'Bearer ghp_x'
    assert 'Authorization' not in github.listener._headers({'token': ''})


def test_conditional_requests_are_sent_and_304_is_not_an_error(store, monkeypatch):
    """A 304 does not count against the rate limit, which is what makes the
    unauthenticated tier usable."""
    captured = {}

    class Resp:
        status_code = 304
        headers = {}
        text = ''
        def raise_for_status(self): pass

    def fake_get(url, headers=None, **kwargs):
        captured.update(headers or {})
        return Resp()

    monkeypatch.setattr(github.requests, 'get', fake_get)
    payload, etag, unchanged = github.listener._get({}, '/x', '"abc"')
    assert captured['If-None-Match'] == '"abc"'
    assert unchanged is True and etag == '"abc"' and payload is None


def test_rate_limiting_says_what_to_do_about_it(store, monkeypatch):
    class Resp:
        status_code = 403
        headers = {'X-RateLimit-Remaining': '0'}
        text = 'API rate limit exceeded'
        def raise_for_status(self): pass

    monkeypatch.setattr(github.requests, 'get', lambda *a, **k: Resp())
    with pytest.raises(RuntimeError, match='token'):
        github.listener._get({}, '/x')


def test_a_missing_repo_explains_itself(store, monkeypatch):
    class Resp:
        status_code = 404
        headers = {}
        text = ''
        def raise_for_status(self): pass

    monkeypatch.setattr(github.requests, 'get', lambda *a, **k: Resp())
    with pytest.raises(RuntimeError, match='private'):
        github.listener._get({}, '/x')


def test_one_failing_repo_does_not_stop_the_others(store, monkeypatch):
    def fetch(self, config, repo, kind, etag):
        if repo == 'broken/repo':
            raise RuntimeError('boom')
        return [{'id': 'x', 'kind': 'Release', 'repo': repo, 'created_at': '2026'}], None

    monkeypatch.setattr(github.GitHubListener, '_fetch_kind', fetch)
    config = dict(github.listener.config(),
                  repos=['broken/repo', 'good/repo'], events=['releases'])
    assert len(github.listener.poll(config, None)) == 1
    assert github.listener.state()['last_failures']


def test_a_release_with_no_name_falls_back_to_its_tag(store):
    """Verified against pallets/flask, whose releases have an empty name."""
    item = github.listener._release_item('pallets/flask',
                                         {'id': 1, 'name': '', 'tag_name': '3.1.1'})
    assert item['title'] == '3.1.1'


def test_bots_are_filtered_only_on_issues_and_pull_requests(store):
    """Verified against pallets/flask: every release there is published by
    github-actions[bot], so filtering releases by author silently prints
    nothing at all."""
    config = {'ignore_bots': True}
    for kind in ('Release', 'Build failed'):
        ok, _ = github.listener.should_print(
            config, {'kind': kind, 'author': 'github-actions[bot]',
                     'author_type': 'Bot'})
        assert ok is True, f'{kind} by a CI bot was filtered out'
    for kind in ('Issue', 'Pull request'):
        ok, _ = github.listener.should_print(
            config, {'kind': kind, 'author': 'dependabot[bot]', 'author_type': 'Bot'})
        assert ok is False


def test_bot_filtering_can_be_switched_off(store):
    ok, _ = github.listener.should_print(
        {'ignore_bots': False},
        {'kind': 'Issue', 'author': 'dependabot[bot]', 'author_type': 'Bot'})
    assert ok is True


def test_issues_and_pulls_are_told_apart(store, monkeypatch):
    payload = [{'number': 1, 'title': 'A bug', 'user': {'login': 'a'}},
               {'number': 2, 'title': 'A change', 'user': {'login': 'b'},
                'pull_request': {'url': 'x'}}]

    monkeypatch.setattr(github.GitHubListener, '_get',
                        lambda self, c, p, e=None, params=None: (payload, None, False))
    issues, _ = github.listener._fetch_kind({}, 'o/r', 'issues', None)
    pulls, _ = github.listener._fetch_kind({}, 'o/r', 'pulls', None)
    assert [i['kind'] for i in issues] == ['Issue']
    assert [i['kind'] for i in pulls] == ['Pull request']


def test_only_failed_runs_are_reported(store, monkeypatch):
    payload = {'workflow_runs': [
        {'id': 1, 'name': 'CI', 'conclusion': 'success'},
        {'id': 2, 'name': 'CI', 'conclusion': 'failure'},
        {'id': 3, 'name': 'CI', 'conclusion': 'timed_out'},
    ]}
    monkeypatch.setattr(github.GitHubListener, '_get',
                        lambda self, c, p, e=None, params=None: (payload, None, False))
    runs, _ = github.listener._fetch_kind({}, 'o/r', 'failed_runs', None)
    assert len(runs) == 2


def test_draft_releases_are_skipped(store, monkeypatch):
    payload = [{'id': 1, 'tag_name': 'v1', 'draft': True},
               {'id': 2, 'tag_name': 'v2', 'draft': False}]
    monkeypatch.setattr(github.GitHubListener, '_get',
                        lambda self, c, p, e=None, params=None: (payload, None, False))
    releases, _ = github.listener._fetch_kind({}, 'o/r', 'releases', None)
    assert len(releases) == 1 and releases[0]['title'] == 'v2'


# --- both are first-class -----------------------------------------------------

@pytest.mark.parametrize('name', ['mqtt', 'github'])
def test_registered_and_editable(name):
    from taskhome import styles
    assert name in base.registry()
    assert name in styles.kinds()
    assert styles.builtin_templates(name)
