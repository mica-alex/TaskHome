"""SeeClickFix request-type names and discovery (MASTER_PLAN P4-1/P4-2/P4-3).

The config stores raw numeric ids, which say nothing about what you have
subscribed to. Names are looked up once and cached, so opening the settings
page does not depend on the network.
"""
import json

import pytest

from taskhome import constants
from taskhome.listeners import scf


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    return tmp_path


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')

    def json(self):
        return self._payload


@pytest.fixture
def api(monkeypatch):
    calls = []

    def get(url, params=None, timeout=None):
        calls.append(url)
        if url.endswith('/9999'):
            return FakeResponse({}, status=404)
        if 'issues/new' in url:
            return FakeResponse({'request_types': [
                {'id': 2, 'title': 'Pothole Patch', 'organization': 'DPW'},
                {'id': 1, 'title': 'Signal Repair', 'organization': 'DPW'},
                {'id': 3, 'title': 'Parking', 'organization': 'Parking'},
            ]})
        rid = url.rsplit('/', 1)[-1]
        return FakeResponse({'id': int(rid), 'title': f'Type {rid}',
                             'organization': 'City of Manchester DPW'})

    monkeypatch.setattr(scf.requests, 'get', get)
    get.calls = calls
    return get


def test_names_are_looked_up_and_described(cache_dir, api):
    described = scf.describe_request_types('6632,6634')
    assert [d['title'] for d in described] == ['Type 6632', 'Type 6634']
    assert all(d['known'] for d in described)


def test_configured_order_is_preserved(cache_dir, api):
    described = scf.describe_request_types('30,10,20')
    assert [d['id'] for d in described] == ['30', '10', '20']


def test_second_call_uses_the_cache(cache_dir, api):
    scf.describe_request_types('6632')
    before = len(api.calls)
    scf.describe_request_types('6632')
    assert len(api.calls) == before, 'the cache was not used'


def test_cache_persists_to_disk(cache_dir, api):
    scf.describe_request_types('6632')
    cached = json.loads((cache_dir / 'cache' / 'scf_request_types.json').read_text())
    assert cached['6632']['title'] == 'Type 6632'


def test_stale_entries_are_refetched(cache_dir, api):
    scf.describe_request_types('6632')
    path = cache_dir / 'cache' / 'scf_request_types.json'
    data = json.loads(path.read_text())
    data['6632']['fetched_at'] = '2020-01-01T00:00:00+00:00'
    path.write_text(json.dumps(data))

    before = len(api.calls)
    scf.describe_request_types('6632')
    assert len(api.calls) > before


def test_a_missing_id_is_remembered_not_retried(cache_dir, api):
    """An id that no longer exists would otherwise mean a network round trip
    every time the settings page is opened."""
    described = scf.describe_request_types('9999')
    assert described[0]['missing'] is True
    assert described[0]['known'] is False

    before = len(api.calls)
    scf.describe_request_types('9999')
    assert len(api.calls) == before


def test_unknown_ids_still_appear(cache_dir, api):
    """A failed lookup must not silently drop a subscription from the page."""
    described = scf.describe_request_types('9999')
    assert len(described) == 1 and described[0]['id'] == '9999'


def test_network_failure_keeps_stale_names(cache_dir, api, monkeypatch):
    """An out-of-date name beats no name."""
    scf.describe_request_types('6632')
    monkeypatch.setattr(scf.requests, 'get',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('offline')))
    path = cache_dir / 'cache' / 'scf_request_types.json'
    data = json.loads(path.read_text())
    data['6632']['fetched_at'] = '2020-01-01T00:00:00+00:00'
    path.write_text(json.dumps(data))

    described = scf.describe_request_types('6632')
    assert described[0]['title'] == 'Type 6632'


def test_empty_and_messy_input(cache_dir, api):
    assert scf.describe_request_types('') == []
    assert scf.describe_request_types(None) == []
    assert len(scf.describe_request_types(' 1 , ,2 , ')) == 2


def test_unreadable_cache_is_ignored(cache_dir, api):
    (cache_dir / 'cache').mkdir()
    (cache_dir / 'cache' / 'scf_request_types.json').write_text('{not json')
    assert scf.describe_request_types('6632')[0]['known'] is True


def test_browse_groups_by_organization(api):
    types = scf.browse_request_types(42.9956, -71.4548)
    assert [t['organization'] for t in types] == ['DPW', 'DPW', 'Parking']
    assert [t['title'] for t in types[:2]] == ['Pothole Patch', 'Signal Repair']


def test_browse_returns_string_ids(api):
    """Ids are compared against the config's comma string, so type matters."""
    assert all(isinstance(t['id'], str) for t in scf.browse_request_types(1, 2))
