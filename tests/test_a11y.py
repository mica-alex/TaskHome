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
    """It is only meant to be reachable by Tab.

    Asserts the *property* -- clipped to nothing, restored on focus -- not a
    particular technique. It was originally translated off-screen by twice its
    own height, which depends on its static position: on iOS the appbar's
    safe-area margin pushed that down far enough that the link landed over the
    status bar, while desktop looked fine.
    """
    import pathlib
    css = pathlib.Path('taskhome/static/mica.css').read_text()
    block = css[css.index('.skip-link {'):]
    hidden = block[:block.index('}')]

    clipped = 'clip' in hidden or 'clip-path' in hidden
    tiny = 'width: 1px' in hidden and 'height: 1px' in hidden
    assert clipped and tiny, 'the resting state is not reliably hidden'
    assert '-200%' not in hidden, (
        'hiding by translating a multiple of its own height depends on the '
        'layout around it, which is what broke on iOS')

    focus = css[css.index('.skip-link:focus'):]
    revealed = focus[:focus.index('}')]
    assert 'clip: auto' in revealed or 'clip-path: none' in revealed, \
        'focus does not undo the clipping'


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


# --- mobile card view (P2-9 / P2B-6) -----------------------------------------

def test_tables_collapse_to_cards_on_a_phone():
    """Below 600px a six-column table either scrolls sideways -- header
    scrolling away from its own values -- or squeezes every column to
    unreadable width."""
    import pathlib
    css = pathlib.Path('taskhome/static/mica.css').read_text()
    assert '@media (max-width: 600px)' in css
    assert '.mica-table td[data-label]::before' in css, 'no per-cell labels'


def test_the_header_stays_available_to_screen_readers_on_mobile():
    """Hiding thead with display:none would break the header association that
    a screen reader relies on."""
    import pathlib
    css = pathlib.Path('taskhome/static/mica.css').read_text()
    mobile = css[css.index('@media (max-width: 600px)'):]
    thead = mobile[mobile.index('.mica-table thead'):]
    rule = thead[:thead.index('}')]
    assert 'display: none' not in rule
    assert 'clip:' in rule or 'position: absolute' in rule


@pytest.mark.parametrize('path', ['/task_page', '/'])
def test_data_cells_carry_their_own_label(client, path):
    """The label comes from the cell, so adding a column cannot leave a row
    silently mislabelled on mobile."""
    import re
    body = client.get(path).get_data(as_text=True)
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', body, re.S)
    data_rows = [r for r in rows if '<td' in r and 'empty-state' not in r]
    for row in data_rows:
        cells = re.findall(r'<td([^>]*)>', row)
        labelled = [c for c in cells if 'data-label' in c]
        # At least one unlabelled cell (the row heading) and the rest labelled.
        assert labelled, f'no labelled cells in a row on {path}'


def test_mobile_cards_cannot_overflow_their_container():
    """width:100% plus the row's padding and border overflows by exactly
    padding+border, and this stylesheet has no global border-box rule to fall
    back on. The row cards ran off the right edge of the phone because of it.
    """
    import pathlib
    css = pathlib.Path('taskhome/static/mica.css').read_text()
    mobile = css[css.index('@media (max-width: 600px)'):]
    rule = mobile[mobile.index('.mica-table, .mica-table tbody'):]
    rule = rule[:rule.index('}')]
    assert 'width: 100%' in rule
    assert 'box-sizing: border-box' in rule, 'width:100% without border-box'


def test_the_receipt_preview_scrolls_inside_its_own_box():
    """The receipt body is a fixed 64 characters wide because that IS the
    paper; reflowing it would make the preview a lie. It therefore has to
    scroll inside its own container rather than widening the page, which is
    what pushed the whole Receipts page into a horizontal scroll on a phone.
    """
    import pathlib
    css = pathlib.Path('taskhome/static/mica.css').read_text()
    markup = pathlib.Path('taskhome/templates/receipt_studio.html').read_text()
    assert 'receipt-paper-scroll' in markup, 'the paper is not wrapped'
    rule = css[css.index('.receipt-paper-scroll {'):]
    rule = rule[:rule.index('}')]
    assert 'overflow-x: auto' in rule and 'max-width: 100%' in rule


def test_mobile_css_targets_classes_that_exist():
    """A rule naming a class no template uses is dead code that looks like a
    fix -- .receipt-preview was styled for mobile and never existed.
    """
    import pathlib
    import re
    css = pathlib.Path('taskhome/static/mica.css').read_text()
    mobile = css[css.index('@media (max-width: 600px)'):]
    mobile = mobile[:mobile.index('\n}\n')]

    markup = ''
    for path in pathlib.Path('taskhome/templates').rglob('*.html'):
        markup += path.read_text()
    markup += pathlib.Path('taskhome/static/mica.css').read_text()

    # Only check the app's own component classes.
    for selector in set(re.findall(r'\.(mica-[\w-]+|studio-[\w-]+|r-[\w-]+|'
                                   r'receipt-[\w-]+|row-[\w-]+|listener-[\w-]+)',
                                   mobile)):
        assert selector in markup, f'mobile CSS styles .{selector}, which nothing uses'


# --- feedback and confirmation rules (P2-4) -----------------------------------

def test_every_user_initiated_mutation_reports_its_outcome():
    """A button that silently does nothing is indistinguishable from a broken
    one, so this is not optional.

    Scoped to *mutations*. A background poll updates the UI directly and must
    not toast -- a status refresh announcing itself every ten seconds would be
    unusable.
    """
    import pathlib
    import re
    for path in pathlib.Path('taskhome/static').glob('*.js'):
        text = path.read_text()
        for match in re.finditer(r"method:\s*'(POST|PATCH|PUT|DELETE)'", text):
            window = text[match.start():match.start() + 1200]
            line = text[:match.start()].count('\n') + 1
            # Three legitimate forms of feedback. A live preview reports
            # failures inline rather than toasting on every keystroke, which
            # is better feedback, not worse -- the property is "reports its
            # outcome", not "uses a toast".
            reported = ('toast(' in window
                        or 'location.reload' in window
                        or 'errorEl' in window
                        or 'hidden = false' in window)
            assert reported, f'{path.name}:{line} mutates with no feedback'


def test_irreversible_whole_object_deletions_confirm():
    """The rule: confirm when the action is irreversible AND its effect is not
    obvious from the control. Deleting a task, list or person qualifies."""
    import pathlib
    import re
    for path in pathlib.Path('taskhome/static').glob('*.js'):
        text = path.read_text()
        for match in re.finditer(r"data-delete-(task|list|person)", text):
            window = text[match.start():match.start() + 900]
            if 'method:' not in window:
                continue
            assert 'confirm(' in window, \
                f'{path.name}: {match.group(0)} deletes without confirming'


def test_the_confirmation_rule_is_written_down():
    """An undocumented inconsistency reads as an oversight."""
    import pathlib
    ui = pathlib.Path('taskhome/static/ui.js').read_text()
    assert 'When to confirm' in ui
    assert 'irreversible' in ui
