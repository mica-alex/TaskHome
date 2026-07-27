"""Keyboard access and accessibility (P2-7).

These are structural checks, not a substitute for testing with a screen
reader. They exist because every one of these regressed at least once during
the UI rework -- a new page simply forgot the caption, or a new control was
added without a label -- and nothing failed.
"""
import re

import pytest

from taskhome import constants, create_app, state


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(state, 'config', {'theme': 'system', 'max_history': 500})
    app = create_app(load=False, with_scheduler=False)
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


PAGES = ['/', '/task_page', '/settings', '/settings/receipts', '/listener',
         '/listener/scf', '/listener/settings/nws', '/queue']


@pytest.mark.parametrize('path', PAGES)
def test_every_page_has_a_skip_link_first(client, path):
    """Without it a keyboard user tabs through five nav links and three appbar
    controls before reaching the content, on every page load."""
    body = client.get(path).get_data(as_text=True)
    assert 'class="skip-link"' in body
    assert body.index('skip-link') < body.index('mica-appbar'), \
        'the skip link is not the first tab stop'


@pytest.mark.parametrize('path', PAGES)
def test_every_page_has_a_main_landmark(client, path):
    body = client.get(path).get_data(as_text=True)
    assert re.search(r'<main[^>]*id="main"', body)


@pytest.mark.parametrize('path', PAGES)
def test_every_table_has_a_caption(client, path):
    """A screen reader listing tables out of context gets nothing useful from
    'table with 4 columns'."""
    body = client.get(path).get_data(as_text=True)
    tables = body.count('<table')
    captions = body.count('<caption')
    assert captions >= tables, f'{tables} table(s), {captions} caption(s)'


@pytest.mark.parametrize('path', PAGES)
def test_no_control_is_left_unlabelled(client, path):
    """An icon-only button with no accessible name is announced as 'button'."""
    body = client.get(path).get_data(as_text=True)
    for match in re.finditer(r'<button\b[^>]*>(.*?)</button>', body, re.S):
        tag, inner = match.group(0), re.sub(r'<[^>]+>', '', match.group(1)).strip()
        assert inner or 'aria-label' in tag or 'title=' in tag, \
            f'unlabelled button on {path}: {tag[:90]}'


@pytest.mark.parametrize('path', PAGES)
def test_every_input_has_a_label(client, path):
    """A bare input is announced as 'edit text, blank'."""
    body = client.get(path).get_data(as_text=True)
    for match in re.finditer(r'<input\b[^>]*>', body):
        tag = match.group(0)
        if re.search(r'type="(hidden|submit|button)"', tag):
            continue
        ident = re.search(r'id="([^"]+)"', tag)
        # Four ways to name an input, all valid: an explicit for=, an
        # aria-label, a placeholder, or being wrapped in a <label>.
        before = body[:match.start()]
        wrapped = before.rfind('<label') > before.rfind('</label>')
        labelled = ('aria-label' in tag
                    or (ident and f'for="{ident.group(1)}"' in body)
                    or 'placeholder=' in tag
                    or wrapped)
        assert labelled, f'unlabelled input on {path}: {tag[:90]}'


def test_focus_is_visible_and_scoped_to_keyboard_use():
    """:focus-visible, not :focus -- a ring left behind by a mouse click is
    why people delete focus styling and break keyboard access."""
    css = (constants.APP_ROOT and __import__('pathlib').Path(
        'taskhome/static/mica.css').read_text())
    assert ':focus-visible' in css
    assert 'outline: 2px solid var(--mica-accent)' in css


def test_high_contrast_modes_are_handled():
    """forced-colors strips the colour-mixed backgrounds that most chips and
    badges here rely on."""
    css = __import__('pathlib').Path('taskhome/static/mica.css').read_text()
    assert 'forced-colors: active' in css
    assert 'prefers-contrast: more' in css


def test_reduced_motion_is_respected():
    css = __import__('pathlib').Path('taskhome/static/mica.css').read_text()
    assert 'prefers-reduced-motion' in css


@pytest.mark.parametrize('path', PAGES)
def test_pages_declare_a_language(client, path):
    body = client.get(path).get_data(as_text=True)
    assert re.search(r'<html[^>]*\blang="', body)


def test_dialogs_use_the_native_element():
    """<dialog>.showModal() gives focus trapping, Escape-to-close and an inert
    background for free. Hand-rolled modals get all three wrong."""
    import pathlib
    tasks = pathlib.Path('taskhome/templates/tasks.html').read_text()
    if 'dialog' in tasks:
        assert '<dialog' in tasks, 'a modal was built without <dialog>'
    ui = pathlib.Path('taskhome/static/ui.js').read_text()
    assert 'showModal' in ui


@pytest.mark.parametrize('path', PAGES)
def test_status_updates_are_announced(client, path):
    """Toasts are the only feedback for async actions; without a live region a
    screen reader user gets silence."""
    body = client.get(path).get_data(as_text=True)
    assert 'aria-live' in body or 'role="status"' in body


def test_the_skip_link_is_hidden_until_focused():
    """It is only meant to be reachable by Tab. It appeared as a bare blue
    link in the corner once, because the browser was serving a cached
    stylesheet that predated the rule -- so this asserts the rule exists and
    that focus is what reveals it.
    """
    import pathlib
    css = pathlib.Path('taskhome/static/mica.css').read_text()
    block = css[css.index('.skip-link {'):]
    hidden = block[:block.index('}')]
    assert 'translate' in hidden and '-200%' in hidden, 'not moved off-screen'
    assert '.skip-link:focus' in css, 'nothing brings it back'


@pytest.mark.parametrize('path', PAGES)
def test_static_urls_are_version_stamped(client, path):
    """Without this a browser holds a stale stylesheet indefinitely, and the
    symptom is baffling: new markup, new CSS on disk, half the styling
    missing."""
    import re
    from taskhome import constants
    body = client.get(path).get_data(as_text=True)
    for href in re.findall(r'(?:href|src)="(/static/[^"]+)"', body):
        assert f'v={constants.VERSION}' in href, f'unstamped asset: {href}'
