"""Installable web app support (P2B).

These protect the details that are invisible until someone tries to install
the app on a phone, at which point they are the difference between working and
not.
"""
import json

import pytest

from taskhome import constants, create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    app = create_app(load=False, with_scheduler=False)
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


def manifest(client):
    return json.loads(client.get('/manifest.webmanifest').get_data(as_text=True))


def test_manifest_is_served_with_the_right_content_type(client):
    response = client.get('/manifest.webmanifest')
    assert response.status_code == 200
    assert response.headers['Content-Type'].startswith('application/manifest+json')


def test_manifest_has_what_installability_requires(client):
    data = manifest(client)
    assert data['name'] and data['short_name']
    assert data['start_url'] and data['display'] == 'standalone'
    sizes = {i['sizes'] for i in data['icons']}
    # Android refuses to offer installation without both of these.
    assert '192x192' in sizes and '512x512' in sizes


def test_maskable_and_any_icons_are_separate_entries(client):
    """An icon claiming both purposes gets cropped by Android's mask on a
    picture that was not drawn with a safe zone."""
    for i in manifest(client)['icons']:
        assert i['purpose'] in ('any', 'maskable')
    purposes = {i['purpose'] for i in manifest(client)['icons']}
    assert purposes == {'any', 'maskable'}


def test_every_icon_the_manifest_promises_actually_exists(client):
    for i in manifest(client)['icons']:
        assert client.get(i['src']).status_code == 200, f"missing {i['src']}"


def test_shortcut_targets_resolve(client):
    for shortcut in manifest(client)['shortcuts']:
        assert client.get(shortcut['url'].split('#')[0]).status_code in (200, 302)


def test_service_worker_is_served_from_the_root(client):
    """Scope. A worker under /static/ can only control /static/, which is the
    most common reason an otherwise correct PWA never goes offline."""
    response = client.get('/service-worker.js')
    assert response.status_code == 200
    assert 'javascript' in response.headers['Content-Type']


def test_service_worker_is_not_itself_cached(client):
    """A cached service worker is a permanently stuck app: the file that
    replaces a stale cache cannot be the thing that is stale."""
    assert 'no-cache' in client.get('/service-worker.js').headers.get('Cache-Control', '')


def test_service_worker_cache_is_versioned(client):
    body = client.get('/service-worker.js').get_data(as_text=True)
    assert constants.VERSION in body


def test_service_worker_does_not_cache_state_changing_requests(client):
    """A POST replayed from a cache hours later would print a duplicate
    receipt, which is worse than an honest failure."""
    body = client.get('/service-worker.js').get_data(as_text=True)
    assert "request.method !== 'GET'" in body


def test_ios_meta_tags_are_present(client):
    """The iOS half is what works over plain HTTP, so it carries the feature."""
    body = client.get('/').get_data(as_text=True)
    for tag in ('apple-mobile-web-app-capable',
                'apple-mobile-web-app-status-bar-style',
                'apple-touch-icon',
                'viewport-fit=cover'):
        assert tag in body, f'missing {tag}'


def test_service_worker_registration_is_gated_on_a_secure_context(client):
    """Registering unconditionally throws over plain HTTP, which is what this
    app actually serves on a LAN."""
    body = client.get('/').get_data(as_text=True)
    assert 'isSecureContext' in body


def test_theme_colour_is_declared_for_both_schemes(client):
    body = client.get('/').get_data(as_text=True)
    assert body.count('name="theme-color"') == 2


def test_app_name_flows_into_the_manifest(client):
    from taskhome import state
    state.config['app_name'] = 'Kitchen Printer'
    try:
        assert manifest(client)['name'] == 'Kitchen Printer'
    finally:
        state.config.pop('app_name', None)


def test_shell_assets_referenced_by_the_worker_all_exist(client):
    """A 404 in the precache list used to take the whole cache down with it;
    each asset is now added independently, but a missing one is still a bug."""
    import re
    body = client.get('/service-worker.js').get_data(as_text=True)
    shell = re.search(r'const SHELL = \[(.*?)\];', body, re.S).group(1)
    for url in re.findall(r"'([^']+)'", shell):
        assert client.get(url).status_code == 200, f'{url} is in SHELL but 404s'
