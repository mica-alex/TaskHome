"""The UI must work with no internet (MASTER_PLAN P0-15).

TaskHome runs on a LAN appliance that is frequently offline. Loading the
interface from CDNs meant it broke exactly when it was most needed, and
`select { display: none }` made every dropdown invisible when Materialize
failed to load.

These are guard-rail tests: they fail if someone reintroduces a remote asset.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / 'taskhome' / 'templates'
VENDOR = REPO / 'taskhome' / 'static' / 'vendor'

REMOTE_HOSTS = ('cdnjs.', 'jsdelivr.', 'fonts.googleapis.com', 'fonts.gstatic.com',
                'unpkg.com', 'ajax.googleapis.com')


def template_files():
    return sorted(TEMPLATES.rglob('*.html'))


@pytest.mark.parametrize('path', template_files(), ids=lambda p: p.name)
def test_templates_load_no_remote_assets(path):
    text = path.read_text()
    for host in REMOTE_HOSTS:
        assert host not in text, f'{path.name} references {host}; vendor it instead'


@pytest.mark.parametrize('name', [
    'materialize.min.css', 'materialize.min.js',
    'flatpickr.min.css', 'flatpickr.min.js',
    'material-icons.css', 'material-icons.ttf',
])
def test_vendored_asset_exists_and_is_not_empty(name):
    path = VENDOR / name
    assert path.exists(), f'{name} is missing from static/vendor'
    assert path.stat().st_size > 0


def test_icon_font_css_points_at_the_local_file():
    """The downloaded CSS references fonts.gstatic.com; it must be rewritten
    to the local .ttf or icons silently fail offline."""
    css = (VENDOR / 'material-icons.css').read_text()
    assert 'gstatic.com' not in css
    assert 'material-icons.ttf' in css


def test_vendored_css_makes_no_runtime_requests():
    """url(...) and @import must all be local. License comments may mention
    URLs, so only look at actual references."""
    for css_file in VENDOR.glob('*.css'):
        text = css_file.read_text()
        refs = re.findall(r'url\(\s*[\'"]?(https?://[^)\'"]+)', text)
        refs += re.findall(r'@import\s+[\'"](https?://[^\'"]+)', text)
        assert refs == [], f'{css_file.name} fetches {refs} at runtime'


def test_native_selects_stay_visible_without_materialize():
    """The bug: an unconditional `select { display: none }` hid every dropdown
    when Materialize didn't load. Hiding must be scoped to selects Materialize
    has actually wrapped."""
    css = (REPO / 'taskhome' / 'static' / 'styles.css').read_text()
    for match in re.finditer(r'([^{}]*)\{([^}]*)\}', css):
        selector, body = match.group(1).strip(), match.group(2)
        if 'display' not in body or 'none' not in body:
            continue
        selectors = [s.strip() for s in selector.split(',')]
        assert 'select' not in selectors, (
            'bare `select` is hidden unconditionally; scope it to '
            '.select-wrapper so an uninitialised select still renders')


def test_destructive_action_confirms():
    """Clear History is irreversible and sits next to Save."""
    settings = (TEMPLATES / 'settings.html').read_text()
    clear_button = settings[settings.index('clear_history'):]
    assert 'confirm(' in clear_button[:400]


def test_theme_mode_and_effective_theme_are_separate():
    """P0-16: overwriting data-theme with a concrete value meant the
    `=== 'system'` test could never match again, so OS light/dark flips were
    ignored for the rest of the session."""
    base = (TEMPLATES / 'base.html').read_text()
    assert 'data-theme-mode' in base
    assert "getAttribute('data-theme-mode')" in base
