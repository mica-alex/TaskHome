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


# --- Mica design system (P2A) -------------------------------------------------

def test_design_tokens_are_vendored_not_fetched():
    tokens = (VENDOR / 'mica-tokens.css').read_text()
    assert '--mica-brand-400' in tokens
    for host in REMOTE_HOSTS:
        assert host not in tokens


def test_inter_is_self_hosted():
    """D-4 picked Inter; it must be local, since the UI works offline."""
    css = (VENDOR / 'inter.css').read_text()
    assert 'fonts/inter-variable.woff2' in css
    assert 'https://' not in css
    assert (VENDOR / 'fonts' / 'inter-variable.woff2').stat().st_size > 1000


def test_brand_palette_matches_the_site_theme():
    """Transcribed from themePrimitives.ts, which is what actually ships --
    the written design page disagrees with it in places."""
    tokens = (VENDOR / 'mica-tokens.css').read_text()
    assert 'hsl(210, 98%, 48%)' in tokens     # brand 400
    assert 'hsl(220, 35%, 3%)' in tokens      # gray 900


def test_appbar_matches_the_site_spec():
    tokens = (VENDOR / 'mica-tokens.css').read_text()
    assert '--mica-appbar-blur: 24px' in tokens
    assert '--mica-appbar-alpha: 0.4' in tokens
    assert '--mica-appbar-offset: 28px' in tokens
    assert '--mica-appbar-breakpoint: 900px' in tokens


def test_both_themes_define_every_semantic_token():
    """A token defined for one theme only renders as an invalid value in the
    other, which fails silently and looks like a styling glitch."""
    import re
    css = (VENDOR / 'mica-tokens.css').read_text()

    def tokens_in(block):
        return set(re.findall(r'(--mica-(?:bg|surface|text|divider|accent|shadow|ambient)[\w-]*)\s*:', block))

    light = css[css.index(":root[data-theme='light']"):css.index(":root[data-theme='dark']")]
    dark = css[css.index(":root[data-theme='dark']"):]
    missing = tokens_in(light) - tokens_in(dark)
    assert not missing, f'dark theme is missing {missing}'


def test_mica_inputs_survive_without_materialize():
    """styles.css hides native selects; the Mica field styles must override
    that or every dropdown vanishes offline (P0-15)."""
    css = (REPO / 'taskhome' / 'static' / 'mica.css').read_text()
    assert 'display: block !important' in css


def test_no_styles_target_the_removed_nav_markup():
    """The old Materialize <nav> appbar is gone (P2A-3).

    Its rules used !important on the bare `nav` element, so they painted a
    full-width blue bar straight through the replacement. Dead CSS that only
    *looks* dead is worse than none: it still matches.
    """
    css = (REPO / 'taskhome' / 'static' / 'styles.css').read_text()
    for selector in ('.nav-wrapper', '.brand-logo-container', '.brand-logo-img',
                     'nav ul.right', 'nav .brand-logo'):
        assert selector not in css, f'{selector} is styled but no longer rendered'

    for template in template_files():
        text = template.read_text()
        for markup in ('nav-wrapper', 'brand-logo-container', 'id="nav-mobile"'):
            assert markup not in text, f'{template.name} still uses {markup}'


def test_legacy_input_overrides_exempt_mica_controls():
    """styles.css forces input colours with !important to keep Materialize
    readable in dark mode. Unscoped, that beats .mica-input and the new
    components silently render in the old palette."""
    css = (REPO / 'taskhome' / 'static' / 'styles.css').read_text()
    assert ':not(.mica-input)' in css, (
        'the legacy input overrides are not exempting Mica controls')


def test_materialize_skips_mica_controls():
    """Materialize's FormSelect replaces a <select> with its own markup and
    hides the original. A control we also style ourselves then renders twice --
    once real, once fake -- which is what produced the doubled dropdown on the
    history filter. Every template that initialises selects must exclude them.
    """
    initialising = [p for p in template_files() if 'FormSelect.init' in p.read_text()]
    assert initialising, 'expected at least one template to initialise selects'
    for path in initialising:
        text = path.read_text()
        assert ':not(.mica-input)' in text, (
            f'{path.name} initialises Materialize selects without excluding '
            f'Mica-styled ones')


def test_mica_control_appearance_is_not_overridden_by_legacy_css():
    """styles.css must not restate colours for controls mica.css owns: a more
    specific legacy selector wins and renders the new UI in the old palette,
    which reads as a design mistake rather than a bug."""
    css = (REPO / 'taskhome' / 'static' / 'styles.css').read_text()
    block_start = css.find('.history-filter input[type="search"]')
    assert block_start != -1
    block = css[block_start:css.index('}', block_start)]
    for property_name in ('background-color', 'color', 'border'):
        assert property_name not in block, (
            f'.history-filter input restates {property_name}; leave appearance '
            f'to .mica-input')
