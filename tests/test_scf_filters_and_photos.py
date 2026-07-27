"""SCF filters (P4-5) and photos on receipts (P4-7).

The API behaviours asserted here were verified live before the code was
written -- particularly the keyword rule, which is a hard constraint rather
than a preference.
"""
import io

import pytest

from taskhome import receipt, styles
from taskhome.listeners import scf


# --- filters ------------------------------------------------------------------

def test_defaults_are_open_and_acknowledged():
    assert scf.get_filters({})['status'] == ['open', 'acknowledged']


@pytest.mark.parametrize('raw,expected', [
    ('42.95, -71.5, 43.03, -71.4',
     {'min_lat': 42.95, 'min_lng': -71.5, 'max_lat': 43.03, 'max_lng': -71.4}),
    ('', None),
])
def test_bbox_parsing(raw, expected):
    assert scf.parse_bbox(raw) == expected


@pytest.mark.parametrize('bad', ['1,2,3', '1,2,3,4,5', 'a,b,c,d',
                                 '43.03,-71.4,42.95,-71.5'])
def test_a_malformed_bbox_is_refused_with_a_reason(bad):
    with pytest.raises(ValueError) as excinfo:
        scf.parse_bbox(bad)
    assert str(excinfo.value)


def test_a_keyword_needs_an_area():
    """Not a style rule. Verified against the live API: `search=pothole` alone
    does not return within 60 seconds -- it scans ~850,000 issues -- while the
    same search with place_url answers in about 6. A bare keyword would
    configure a listener that times out every poll, forever.
    """
    with pytest.raises(ValueError, match='Place or a bounding box'):
        scf.validate_filters({'status': ['open'], 'search': 'pothole'})


@pytest.mark.parametrize('area', [{'place_url': 'manchester'},
                                  {'bbox': '42.95,-71.5,43.03,-71.4'}])
def test_a_keyword_with_an_area_is_allowed(area):
    assert scf.validate_filters({'status': ['open'], 'search': 'pothole', **area})


def test_no_status_is_refused():
    """Nothing could ever match, and a listener that silently prints nothing
    looks identical to one that is broken."""
    with pytest.raises(ValueError, match='at least one status'):
        scf.validate_filters({'status': []})


def test_an_unknown_status_is_refused():
    with pytest.raises(ValueError):
        scf.validate_filters({'status': ['open', 'pending']})


def test_filters_become_query_parameters():
    params = scf.filter_params({
        'status': ['open', 'closed'], 'place_url': 'manchester',
        'bbox': '42.95,-71.5,43.03,-71.4', 'search': 'pothole'})
    assert params['status'] == 'open,closed'
    assert params['place_url'] == 'manchester'
    assert params['search'] == 'pothole'
    assert params['min_lat'] == 42.95 and params['max_lng'] == -71.4


def test_the_fetch_actually_sends_the_filters(monkeypatch):
    captured = {}

    class Resp:
        def raise_for_status(self): pass
        def json(self): return {'issues': [], 'metadata': {'pagination': {'pages': 1}}}

    def fake_get(url, params=None, **kwargs):
        captured.update(params or {})
        return Resp()

    monkeypatch.setattr(scf.requests, 'get', fake_get)
    scf.fetch_scf_issues('6632', '2026-07-27T00:00:00Z',
                         {'status': ['open'], 'place_url': 'manchester'})
    assert captured['place_url'] == 'manchester'
    assert captured['status'] == 'open'
    assert captured['request_types'] == '6632'


# --- muting -------------------------------------------------------------------

def test_muting_matches_by_request_type_id():
    issue = {'id': 1, 'request_type': {'id': 6632, 'title': 'Signal Repair'}}
    assert scf.is_muted(issue, {'muted_types': ['6632']}) is True
    assert scf.is_muted(issue, {'muted_types': ['9999']}) is False
    assert scf.is_muted(issue, {}) is False


def test_muting_tolerates_ids_stored_as_numbers():
    issue = {'id': 1, 'request_type': {'id': 6632}}
    assert scf.is_muted(issue, {'muted_types': [6632]}) is True


def test_a_muted_issue_keeps_its_subscription():
    """Muting is applied after the fetch rather than by removing the id, so
    unmuting restores it without hunting for the number again."""
    spec = next(f for f in scf.FILTER_SCHEMA if f['key'] == 'muted_types')
    assert spec['type'] == 'multiselect'
    # The request_types field is untouched by muting.
    assert 'request_types' not in {f['key'] for f in scf.FILTER_SCHEMA}


# --- photos -------------------------------------------------------------------

def png_bytes(size=(40, 60), colour=128):
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('L', size, colour).save(buffer, format='PNG')
    return buffer.getvalue()


def test_prepare_produces_a_one_bit_image_at_the_right_width():
    from taskhome import images
    prepared = images.prepare(png_bytes((400, 600)), width=384, max_height=1200)
    assert prepared.mode == '1', 'a thermal printer has one ink level'
    assert prepared.width == 384


def test_a_tall_photo_is_scaled_not_cropped():
    """A cropped photo of a pothole may not contain the pothole."""
    from taskhome import images
    prepared = images.prepare(png_bytes((400, 1600)), width=384, max_height=384)
    assert prepared.height <= 384
    assert prepared.width < 384, 'aspect ratio was not preserved'


def test_an_oversized_download_is_refused(monkeypatch):
    from taskhome import images

    class Resp:
        headers = {'content-type': 'image/jpeg'}
        def raise_for_status(self): pass
        def iter_content(self, n): 
            for _ in range(10):
                yield b'x' * 1024 * 1024      # 10 MB
    monkeypatch.setattr(images.requests, 'Session',
                        lambda: type('S', (), {'get': lambda s, *a, **k: Resp(),
                                               'close': lambda s: None,
                                               'max_redirects': 0})())
    assert images.fetch('https://x/big.jpg') is None


def test_a_non_image_response_is_refused(monkeypatch):
    from taskhome import images

    class Resp:
        headers = {'content-type': 'text/html'}
        def raise_for_status(self): pass
        def iter_content(self, n): yield b'<html>'
    monkeypatch.setattr(images.requests, 'Session',
                        lambda: type('S', (), {'get': lambda s, *a, **k: Resp(),
                                               'close': lambda s: None,
                                               'max_redirects': 0})())
    assert images.fetch('https://x/page.html') is None


def test_a_failed_fetch_returns_none_rather_than_raising(monkeypatch):
    """A receipt missing its photo is a good outcome; a receipt that failed to
    print because a CDN was down is not."""
    from taskhome import images
    monkeypatch.setattr(images, 'fetch', lambda *a, **k: None)
    assert images.load('https://x/gone.jpg', 384) is None


def test_unreadable_bytes_return_none():
    from taskhome import images
    assert images.prepare(b'not an image', 384, 384) is None


def test_photos_are_off_by_default():
    """They roughly double the paper per issue and add a download to the print
    path."""
    assert scf.get_filters({})['print_photos'] is False


def test_no_photo_setting_means_no_image_url(monkeypatch):
    from taskhome import printing
    monkeypatch.setattr(scf, 'get_filters', lambda *a: {'print_photos': False})
    issue = {'media': {'image_full': 'https://x/p.jpg'}}
    assert printing.scf_image_url(issue) == ''


def test_photo_setting_on_yields_the_url(monkeypatch):
    from taskhome import printing
    monkeypatch.setattr(scf, 'get_filters', lambda *a: {'print_photos': True})
    issue = {'media': {'image_full': 'https://x/p.jpg'}}
    assert printing.scf_image_url(issue) == 'https://x/p.jpg'


def test_an_issue_with_no_photo_leaves_no_gap():
    """fill() drops the block, so the receipt is not a blank space or a
    '[Photo unavailable]' for a photo that never existed."""
    template = styles.get_template('scf', 'scf-default')
    blocks = styles.fill(template, styles.sample_context('scf', {'media_url': ''}))
    assert not any(b['type'] == 'image' for b in blocks)


def test_an_issue_with_a_photo_gets_an_image_block():
    template = styles.get_template('scf', 'scf-default')
    blocks = styles.fill(template,
                         styles.sample_context('scf', {'media_url': 'https://x/p.jpg'}))
    images_in = [b for b in blocks if b['type'] == 'image']
    assert len(images_in) == 1 and images_in[0]['src'] == 'https://x/p.jpg'


def test_a_photo_roughly_doubles_the_receipt():
    """The claim the settings help text makes, kept honest."""
    template = styles.get_template('scf', 'scf-default')
    without = receipt.height_mm(styles.fill(template, styles.sample_context('scf')))
    with_photo = receipt.height_mm(styles.fill(
        template, styles.sample_context('scf', {'media_url': 'https://x/p.jpg'})))
    assert with_photo > without * 1.5


def test_image_blocks_are_valid_in_a_template():
    template = {'name': 'photo', 'kind': 'scf', 'version': 1, 'blocks': [
        {'type': 'image', 'src': '{media_url}', 'width': 384}]}
    clean = styles.validate_template(template)
    assert clean['blocks'][0]['src'] == '{media_url}'


def test_an_image_block_with_an_unknown_placeholder_is_refused():
    template = {'name': 'photo', 'kind': 'scf', 'version': 1, 'blocks': [
        {'type': 'image', 'src': '{not_a_field}'}]}
    with pytest.raises(styles.TemplateError):
        styles.validate_template(template)


def test_image_width_is_bounded_by_the_paper():
    template = {'name': 'photo', 'kind': 'scf', 'version': 1, 'blocks': [
        {'type': 'image', 'src': '{media_url}', 'width': 5000}]}
    with pytest.raises(styles.TemplateError):
        styles.validate_template(template)


def test_the_preview_shows_a_photo_costs_paper():
    """A thumbnail that looked free would hide the whole reason this is
    opt-in."""
    blocks = [receipt.image('https://x/p.jpg', width=384, max_height=384)]
    assert receipt.height_mm(blocks) > 40
    text = '\n'.join(receipt.render_text(blocks))
    assert 'Photo' in text
