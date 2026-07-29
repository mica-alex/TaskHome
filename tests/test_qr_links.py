"""Every QR on a receipt has to be a working link.

The bug these exist for: `/` is one of the separators tidy_separators collapses
for a template, so the `//` in `https://seeclickfix.com/issues/1` was rewritten
to ` / `. The QR then encoded `https: / seeclickfix.com/issues/1`, which a phone
percent-encodes into `https:%20/%20seeclickfix.com/...` and refuses to open.

Every kind went through the same fill(), so every QR on every receipt was
broken, not just SeeClickFix's -- hence the sweep over the whole registry rather
than a single case.
"""
import pytest

from taskhome import printing, styles
from taskhome.listeners import base, feeds, mqtt, nws, packages


def qr_values(blocks):
    return [b['value'] for b in blocks if b.get('type') == 'qr']


# --- the mangling itself ------------------------------------------------------

@pytest.mark.parametrize('url', [
    'https://seeclickfix.com/issues/19840471',
    'http://taskhome.local:5000/task_page#a1b2c3d4',
    'https://t.17track.net/en#nums=1Z999AA10123456784',
    'https://forecast.weather.gov/zipcity.php?inputstring=03101',
])
def test_a_url_survives_tidying(url):
    assert styles.tidy_separators(url) == url


def test_a_url_inside_prose_survives_tidying():
    """The separator cleanup still has to work around it."""
    assert (styles.tidy_separators('Open - https://example.com/a/b - ')
            == 'Open - https://example.com/a/b')


def test_a_stranded_separator_is_still_removed():
    assert styles.tidy_separators('Open  -  01:36 PM  -  ') == 'Open  -  01:36 PM'


# --- through the template layer, for every receipt kind -----------------------

def test_no_preset_mangles_its_qr_link():
    for kind in styles.kinds():
        for preset in styles.builtin_templates(kind):
            blocks = styles.fill(preset, styles.sample_context(kind))
            for value in qr_values(blocks):
                assert '%20' not in value and ' / ' not in value, \
                    f'{preset["name"]} encodes {value!r}'
                assert value.startswith(('http://', 'https://')), \
                    f'{preset["name"]} encodes {value!r}'


def test_a_qr_value_keeps_its_trailing_slash():
    """A trailing `/` is part of the URL, not a stranded separator."""
    blocks = styles.fill(
        {'name': 't', 'kind': 'task', 'blocks': [{'type': 'qr', 'value': '{qr_url}'}]},
        {'qr_url': 'https://example.com/issues/'})
    assert qr_values(blocks) == ['https://example.com/issues/']


def test_the_scf_receipt_links_to_the_issue():
    blocks = printing.scf_blocks(
        {'id': 19840471, 'html_url': 'https://seeclickfix.com/issues/19840471'},
        category='Signal Repair', address='S Lincoln St', reported_at='5:58 PM',
        status='Acknowledged', has_media=False)
    assert qr_values(blocks) == ['https://seeclickfix.com/issues/19840471']


def test_the_task_receipt_links_back_to_the_app():
    blocks = printing.task_blocks(
        {'id': 'a1b2c3d4-0000', 'title': 'Play with Sara', 'recurring': 'daily'})
    assert qr_values(blocks) == [printing.task_qr_url(
        {'id': 'a1b2c3d4-0000', 'title': 'Play with Sara'})]
    assert '://' in qr_values(blocks)[0]


# --- the links themselves -----------------------------------------------------

def test_nws_points_at_the_forecast_for_the_alerted_zip():
    url = nws.listener.alert_url(
        {'_zones': {'NHZ005': {'zip': '03104'}, 'NHC011': {'zip': '03101'}}})
    assert url == 'https://forecast.weather.gov/zipcity.php?inputstring=03101'


def test_nws_prints_no_qr_when_no_zip_resolved():
    """An unresolved ZIP has nowhere to point, and a symbol that leads nowhere
    is worse than none."""
    blocks = nws.listener.blocks_from_context(
        {**nws.listener.PLACEHOLDERS, 'url': ''}, qr=True)
    assert qr_values(blocks) == []


def test_only_the_full_nws_layout_carries_a_qr():
    """The short layouts exist to save paper; a QR is another 25mm of it."""
    presets = dict(nws.listener.template_presets())
    assert qr_values(presets['nws-default'])
    assert not qr_values(presets['nws-compact'])
    assert not qr_values(presets['nws-minimal'])


def test_packages_links_to_the_carrier_neutral_tracking_page():
    assert (packages.listener.tracking_url('1Z999AA10123456784')
            == 'https://t.17track.net/en#nums=1Z999AA10123456784')
    assert packages.listener.tracking_url('') == ''


def test_a_parcel_with_no_number_prints_no_qr():
    blocks = packages.listener.blocks_from_context(
        {'number': '', 'carrier': 'UPS', 'status': 'In transit', 'location': '',
         'detail': '', 'url': '', 'printed': '8:30 AM'})
    assert qr_values(blocks) == []


def test_an_mqtt_payload_may_carry_a_url():
    item = mqtt.listener.parse(
        'taskhome/print/laundry',
        b'{"title": "Washing machine finished", '
        b'"url": "http://homeassistant.local/lovelace/laundry"}')
    assert item['url'] == 'http://homeassistant.local/lovelace/laundry'
    assert qr_values(mqtt.listener.blocks_from_context(
        mqtt.listener.context(item))) == [item['url']]


def test_an_mqtt_message_without_a_url_prints_no_qr():
    item = mqtt.listener.parse('taskhome/print/laundry', b'Bins tonight')
    assert qr_values(mqtt.listener.blocks_from_context(
        mqtt.listener.context(item))) == []


# --- the digest ---------------------------------------------------------------

DIGEST = {
    'id': 'digest-20260727T0800',
    'feeds': 2,
    'entries': [
        {'title': 'Something happened today', 'feed': 'The Guardian',
         'link': 'https://www.theguardian.com/world/2026/jul/27/something'},
        {'title': 'A release shipped 4.2', 'feed': 'GitHub',
         'link': 'https://github.com/python/cpython/releases/tag/v4.2'},
    ],
}


def test_the_digest_prints_one_qr_per_headline():
    blocks = feeds.listener.receipt_blocks(DIGEST)
    assert qr_values(blocks) == [e['link'] for e in DIGEST['entries']]


def test_the_digest_headlines_still_read_in_order():
    lines = [b['value'] for b in feeds.listener.receipt_blocks(DIGEST)
             if b.get('type') == 'text']
    assert any(line.startswith('1. Something happened today') for line in lines)
    assert any(line.startswith('2. A release shipped 4.2') for line in lines)


def test_an_entry_with_no_link_still_prints_its_headline():
    digest = {**DIGEST, 'entries': [{'title': 'No link here', 'feed': 'Somewhere',
                                     'link': ''}]}
    blocks = feeds.listener.receipt_blocks(digest)
    assert qr_values(blocks) == []
    assert any('No link here' in b.get('value', '') for b in blocks)


def test_the_plain_digest_has_no_qr_codes():
    """Ten QR codes is roughly a hand of extra paper; the choice stays."""
    blocks = styles.fill(styles.get_template('feeds', 'feeds-plain'),
                         feeds.listener.context(DIGEST))
    assert qr_values(blocks) == []


def test_a_reprinted_digest_uses_the_headlines_that_printed():
    """Not the Studio's samples. A reprint that invents headlines is worse than
    no reprint at all."""
    from taskhome.web import routes
    record = feeds.listener.history_record(DIGEST)
    blocks = routes.reprint_blocks(record)
    assert qr_values(blocks) == [e['link'] for e in DIGEST['entries']]


# --- the property, so a new listener cannot regress it ------------------------

def test_every_listener_that_offers_a_url_puts_it_in_a_qr():
    """A url placeholder that no preset encodes is a link nobody can follow."""
    for name, listener in base.registry().items():
        if 'url' not in listener.PLACEHOLDERS:
            continue
        presets = dict(listener.template_presets())
        assert any(qr_values(blocks) for blocks in presets.values()), \
            f'{name} declares a url but no preset prints a QR for it'
