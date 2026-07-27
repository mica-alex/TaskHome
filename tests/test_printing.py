"""Printing layer: honest return values, handle cleanup, payload guards.

These use a fake ESC/POS device rather than the real printer, so they run with
no hardware attached and never emit paper.
"""
import pytest

import taskhome


class FakeEscpos:
    """Stands in for escpos.printer.Usb. Records calls; can fail on demand."""

    def __init__(self, fail_on=None):
        self.calls = []
        self.closed = False
        self.fail_on = fail_on

    def _record(self, name, *args, **kwargs):
        self.calls.append(name)
        if name == self.fail_on:
            raise RuntimeError(f'device error during {name}')

    def set(self, *a, **k):
        self._record('set')

    def line_spacing(self, spacing=None, divisor=180):
        # The renderer sets spacing per block; without this the fake diverges
        # from the real escpos surface and every print appears to fail.
        self._record('line_spacing')

    def text(self, *a, **k):
        self._record('text')

    def qr(self, *a, **k):
        self._record('qr')

    def barcode(self, *a, **k):
        self._record('barcode')

    def cut(self, *a, **k):
        self._record('cut')

    def close(self):
        self.closed = True


@pytest.fixture
def fake_printer(monkeypatch):
    """Install a fake device and report it as connected."""
    devices = []

    def factory(fail_on=None):
        device = FakeEscpos(fail_on=fail_on)
        devices.append(device)
        monkeypatch.setattr(taskhome.printing, 'Usb', lambda *a, **k: device)
        return device

    monkeypatch.setattr(taskhome.printing, 'is_printer_connected', lambda: True)
    factory.devices = devices
    return factory


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(taskhome.state, 'history', [])
    monkeypatch.setattr(taskhome.state, 'config', dict(taskhome.constants.DEFAULT_CONFIG))
    monkeypatch.setattr(taskhome.storage, 'save_history', lambda: True)
    # A failed print now enqueues (P6-3), so the queue file must be redirected
    # too -- otherwise these tests write to the real data directory, which the
    # conftest guard correctly refuses.
    monkeypatch.setattr(taskhome.constants, 'DATA_DIR', str(tmp_path))


TASK = {'id': 'abc', 'title': 'Take Medicine', 'recurring': 'daily',
        'next_time': '2026-03-01T09:00:00', 'enabled': True}


# --- return values (P0-10) ----------------------------------------------------

def test_successful_print_returns_true(isolated, fake_printer):
    fake_printer()
    assert taskhome.printing.print_task(dict(TASK)) is True
    assert len(taskhome.state.history) == 1


def test_disconnected_printer_returns_false(isolated, monkeypatch):
    monkeypatch.setattr(taskhome.printing, 'is_printer_connected', lambda: False)
    assert taskhome.printing.print_task(dict(TASK)) is False
    assert taskhome.state.history == []


def test_failure_mid_receipt_returns_false(isolated, fake_printer):
    fake_printer(fail_on='cut')
    assert taskhome.printing.print_task(dict(TASK)) is False


def test_failed_print_is_not_recorded_in_history(isolated, fake_printer):
    """History is the record of paper that exists. A failed print must not
    appear there, or reprint-from-history would be lying."""
    fake_printer(fail_on='text')
    taskhome.printing.print_task(dict(TASK))
    assert taskhome.state.history == []


# --- handle cleanup (P0-11) ---------------------------------------------------

def test_handle_is_closed_on_success(isolated, fake_printer):
    device = fake_printer()
    taskhome.printing.print_task(dict(TASK))
    assert device.closed is True


def test_handle_is_closed_on_failure(isolated, fake_printer):
    """The leak that could wedge the device until it was physically replugged:
    close() used to run only on the success path."""
    device = fake_printer(fail_on='text')
    taskhome.printing.print_task(dict(TASK))
    assert device.closed is True


def test_close_error_does_not_mask_success(isolated, fake_printer, monkeypatch):
    device = fake_printer()
    monkeypatch.setattr(device, 'close',
                        lambda: (_ for _ in ()).throw(RuntimeError('usb wedged')))
    assert taskhome.printing.print_task(dict(TASK)) is True


# --- history cap --------------------------------------------------------------

def test_history_is_capped(isolated, fake_printer):
    fake_printer()
    taskhome.state.config['max_history'] = 3
    for i in range(5):
        taskhome.printing.print_task(dict(TASK, id=f'task-{i}'))
    assert len(taskhome.state.history) == 3
    assert taskhome.state.history[0]['id'] == 'task-4'  # newest first


def test_invalid_max_history_falls_back(isolated, fake_printer):
    fake_printer()
    taskhome.state.config['max_history'] = 'lots'
    assert taskhome.printing.print_task(dict(TASK)) is True
    assert len(taskhome.state.history) == 1


def test_missing_config_keys_do_not_break_printing(isolated, fake_printer):
    """config used to be replaced wholesale on load, so a file missing
    'hostname' raised KeyError *after* the receipt had physically printed."""
    fake_printer()
    taskhome.state.config.clear()
    assert taskhome.printing.print_task(dict(TASK)) is True


# --- SCF payload guards (P0-8) ------------------------------------------------

BASE_ISSUE = {
    'id': 999, 'html_url': 'https://seeclickfix.com/issues/999',
    'request_type': {'title': 'Pothole'}, 'address': '1 Main St',
    'created_at': '2026-03-01T09:00:00Z', 'status': 'Open',
}


@pytest.mark.parametrize('media', [
    None, {}, {'image_full': None}, {'image_square_100x100': 'x'}, 'not-a-dict',
])
def test_media_shapes_do_not_crash(isolated, fake_printer, media):
    fake_printer()
    assert taskhome.printing.print_scf_issue(dict(BASE_ISSUE, media=media)) is True


@pytest.mark.parametrize('request_type', [None, {}, {'title': None}, 'string'])
def test_request_type_shapes_do_not_crash(isolated, fake_printer, request_type):
    fake_printer()
    issue = dict(BASE_ISSUE, request_type=request_type)
    assert taskhome.printing.print_scf_issue(issue) is True
    assert taskhome.state.history[0]['category'] == 'Unknown Category'


def test_missing_optional_fields_do_not_crash(isolated, fake_printer):
    fake_printer()
    assert taskhome.printing.print_scf_issue({'id': 1}) is True


def test_unparseable_created_at_is_tolerated(isolated, fake_printer):
    fake_printer()
    assert taskhome.printing.print_scf_issue(dict(BASE_ISSUE, created_at='yesterday')) is True


def test_scf_returns_false_when_disconnected(isolated, monkeypatch):
    monkeypatch.setattr(taskhome.printing, 'is_printer_connected', lambda: False)
    assert taskhome.printing.print_scf_issue(dict(BASE_ISSUE)) is False


def test_scf_handle_closed_on_failure(isolated, fake_printer):
    device = fake_printer(fail_on='cut')
    taskhome.printing.print_scf_issue(dict(BASE_ISSUE))
    assert device.closed is True


def test_bad_payload_fails_before_opening_the_printer(isolated, fake_printer):
    """Fields are resolved before the device is opened, so a malformed payload
    can't waste paper on a half-printed receipt."""
    device = fake_printer()
    taskhome.printing.print_scf_issue({'id': 2, 'media': object()})
    assert 'cut' in device.calls  # completed rather than dying midway


# --- the shared renderer is actually what prints -------------------------------

def test_print_task_renders_the_active_template(isolated, fake_printer):
    """print_task goes through the template layer, not a hand-rolled sequence.

    The built-in template is generated from layouts.py, so the default output
    is identical either way -- but having one path means an edited template
    cannot behave differently from the shipped one.
    """
    device = fake_printer()
    blocks = taskhome.printing.task_blocks(dict(TASK))
    rendered = '\n'.join(taskhome.receipt.render_text(blocks))

    assert 'Take Medicine' in rendered          # placeholders resolved
    assert '{title}' not in rendered
    assert taskhome.printing.print_task(dict(TASK)) is True
    assert 'cut' in device.calls


def test_task_blocks_use_the_selected_template(isolated, fake_printer, monkeypatch):
    monkeypatch.setattr(taskhome.styles, 'get_template',
                        lambda kind, name: {'name': 'x', 'kind': 'task', 'version': 1,
                                            'blocks': [{'type': 'text', 'value': 'CUSTOM {title}'}]})
    blocks = taskhome.printing.task_blocks(dict(TASK))
    assert 'CUSTOM Take Medicine' in '\n'.join(taskhome.receipt.render_text(blocks))


def test_print_scf_renders_the_active_template(isolated, fake_printer):
    fake_printer()
    issue = dict(BASE_ISSUE, media={'image_full': 'x', 'video_url': 'v'},
                 description='Broken')
    assert taskhome.printing.print_scf_issue(issue) is True
    assert taskhome.state.history[0]['has_video'] is True   # was previously ignored


def test_scf_blocks_resolve_every_placeholder(isolated):
    blocks = taskhome.printing.scf_blocks(
        BASE_ISSUE, category='Pothole', address='1 Main St', reported_at='9:00 AM',
        status='Open', has_media=True, has_video=True, description='A hole')
    rendered = '\n'.join(taskhome.receipt.render_text(blocks))
    for expected in ('Pothole', '1 Main St', 'Open', '9:00 AM', 'Photo & Video', 'A hole'):
        assert expected in rendered, f'{expected!r} missing'
    assert '{' not in rendered, 'an unresolved placeholder reached the receipt'


def test_no_barcode_on_the_new_scf_layout(isolated, fake_printer):
    """The CODE39 barcode was removed; its ~10mm bought nothing the QR and the
    printed id did not already carry."""
    device = fake_printer()
    taskhome.printing.print_scf_issue(dict(BASE_ISSUE))
    assert 'barcode' not in device.calls


def test_video_only_issue_is_recorded_as_having_media(isolated, fake_printer):
    fake_printer()
    issue = dict(BASE_ISSUE, media={'image_full': None, 'video_url': 'https://v'})
    taskhome.printing.print_scf_issue(issue)
    assert taskhome.state.history[0]['has_video'] is True
    assert taskhome.state.history[0]['has_media'] is False


def test_task_qr_url_prefers_an_explicit_url(isolated):
    assert taskhome.printing.task_qr_url({'id': 'x', 'url': 'https://custom'}) == 'https://custom'


def test_task_qr_url_falls_back_to_the_app(isolated):
    taskhome.state.config['hostname'] = 'printer.local'
    assert 'printer.local' in taskhome.printing.task_qr_url({'id': 'x'})
