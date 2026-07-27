"""SeeClickFix listener (MASTER_PLAN P0-7).

Three defects that interact, so they're fixed and tested together:
  * `after` is inclusive and windows overlap, so issues reprinted every cycle
  * pagination was ignored, silently dropping everything past the first 100
  * the watermark was captured before the fetch ran, skipping anything created
    while it was in flight

No network: requests.get is replaced with a fake that serves canned pages.
"""
from datetime import datetime, timezone

import pytest

import taskhome


def utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def issue(issue_id, created_at='2026-03-05T09:00:00Z', **extra):
    data = {
        'id': issue_id,
        'html_url': f'https://seeclickfix.com/issues/{issue_id}',
        'request_type': {'title': 'Pothole'},
        'address': '1 Main St',
        'created_at': created_at,
        'status': 'Open',
    }
    data.update(extra)
    return data


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
def scf(clean_state, monkeypatch):
    """Configured listener plus a controllable fake API."""
    taskhome.state.listeners['scf'] = {
        'enabled': True, 'request_types': '6632', 'interval': 5, 'last_check': None,
    }

    class Api:
        def __init__(self):
            self.pages = {}          # page number -> list of issues
            self.total_pages = 1
            self.requests = []
            self.error = None

        def set_issues(self, issues, per_page=None):
            per_page = per_page or taskhome.listeners.scf.SCF_PER_PAGE
            chunks = [issues[i:i + per_page] for i in range(0, len(issues), per_page)] or [[]]
            self.pages = {n + 1: chunk for n, chunk in enumerate(chunks)}
            self.total_pages = len(chunks)

        def get(self, url, params=None, timeout=None):
            self.requests.append(params or {})
            if self.error:
                raise self.error
            page = int((params or {}).get('page', 1))
            return FakeResponse({
                'issues': self.pages.get(page, []),
                'metadata': {'pagination': {'page': page, 'pages': self.total_pages}},
            })

    api = Api()
    api.set_issues([])
    monkeypatch.setattr(taskhome.listeners.scf.requests, 'get', api.get)
    monkeypatch.setattr(taskhome.printing, 'print_scf_issue',
                        lambda i: clean_state.append(i) or True if clean_state.online else False)
    return api


# --- gating -------------------------------------------------------------------

def test_disabled_listener_does_not_poll(scf, clean_state):
    taskhome.state.listeners['scf']['enabled'] = False
    assert taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:00:00')) == 0
    assert scf.requests == []


def test_empty_request_types_does_not_poll(scf):
    taskhome.state.listeners['scf']['request_types'] = '  '
    assert taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:00:00')) == 0
    assert scf.requests == []


def test_missing_listener_is_harmless(clean_state):
    taskhome.state.listeners.clear()
    assert taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:00:00')) == 0


def test_interval_is_respected(scf):
    taskhome.state.listeners['scf']['last_check'] = '2026-03-05T11:58:00Z'
    taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:00:00'))  # only 2m of a 5m interval
    assert scf.requests == []

    taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:04:00'))  # 6m elapsed
    assert len(scf.requests) == 1


def test_unparseable_last_check_polls_rather_than_stalling(scf):
    taskhome.state.listeners['scf']['last_check'] = 'garbage'
    taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:00:00'))
    assert len(scf.requests) == 1


# --- dedup --------------------------------------------------------------------

def test_new_issues_print(scf, clean_state):
    scf.set_issues([issue(1), issue(2)])
    assert taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:00:00')) == 2
    assert len(clean_state) == 2


def test_same_issue_is_not_reprinted_next_cycle(scf, clean_state):
    """`after` is inclusive, so an issue at exactly the watermark comes back
    in the next window. Without dedup it printed every cycle, forever."""
    scf.set_issues([issue(1)])
    taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:00:00'))
    taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:10:00'))
    assert len(clean_state) == 1


def test_duplicate_ids_within_one_response_print_once(scf, clean_state):
    scf.set_issues([issue(1), issue(1), issue(2)])
    assert taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:00:00')) == 2


def test_issues_without_ids_are_skipped(scf, clean_state):
    bad = issue(1)
    del bad['id']
    scf.set_issues([bad, issue(2)])
    assert taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:00:00')) == 1


def test_seen_list_is_bounded(scf, clean_state, monkeypatch):
    monkeypatch.setattr(taskhome.listeners.scf, 'SCF_SEEN_LIMIT', 5)
    scf.set_issues([issue(i) for i in range(20)])
    taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:00:00'))
    assert len(taskhome.state.listeners['scf']['seen']) == 5


def test_issues_print_oldest_first(scf, clean_state):
    scf.set_issues([
        issue(3, '2026-03-05T11:00:00Z'),
        issue(1, '2026-03-05T09:00:00Z'),
        issue(2, '2026-03-05T10:00:00Z'),
    ])
    taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:00:00'))
    assert [i['id'] for i in clean_state] == [1, 2, 3]


# --- pagination ---------------------------------------------------------------

def test_all_pages_are_fetched(scf, clean_state, monkeypatch):
    monkeypatch.setattr(taskhome.listeners.scf, 'SCF_PER_PAGE', 10)
    # Raise the per-poll cap out of the way; this test is about paging, and
    # the cap is covered separately below.
    taskhome.state.listeners['scf']['max_prints_per_poll'] = 100
    scf.set_issues([issue(i) for i in range(25)], per_page=10)
    assert taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:00:00')) == 25
    assert len(scf.requests) == 3


def test_page_guard_stops_runaway_pagination(scf, clean_state, monkeypatch):
    monkeypatch.setattr(taskhome.listeners.scf, 'SCF_MAX_PAGES', 3)
    monkeypatch.setattr(taskhome.listeners.scf, 'SCF_PER_PAGE', 2)
    scf.set_issues([issue(i) for i in range(100)], per_page=2)
    taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:00:00'))
    assert len(scf.requests) == 3


def test_missing_pagination_metadata_falls_back_to_short_page(scf, clean_state, monkeypatch):
    """If the API stops returning pagination metadata, a short page must still
    terminate the loop rather than spinning to the page guard."""
    monkeypatch.setattr(taskhome.listeners.scf, 'SCF_PER_PAGE', 10)

    def get(url, params=None, timeout=None):
        scf.requests.append(params or {})
        page = int((params or {}).get('page', 1))
        return FakeResponse({'issues': [issue(i) for i in range(5)] if page == 1 else []})

    monkeypatch.setattr(taskhome.listeners.scf.requests, 'get', get)
    taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:00:00'))
    assert len(scf.requests) == 1


# --- windowing ----------------------------------------------------------------

def test_watermark_is_taken_before_the_fetch(scf, clean_state):
    """Issues created during the request must be caught next cycle, so the
    watermark cannot be a timestamp from after the fetch completed."""
    now = utc('2026-03-05T12:00:00')
    taskhome.listeners.scf.poll_scf_listener(now)
    assert taskhome.state.listeners['scf']['last_check'] == '2026-03-05T12:00:00Z'


def test_first_poll_looks_back_one_hour(scf, clean_state):
    taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:00:00'))
    assert scf.requests[0]['after'] == '2026-03-05T11:00:00Z'


def test_subsequent_poll_uses_last_check(scf, clean_state):
    taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:00:00'))
    taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:10:00'))
    assert scf.requests[1]['after'] == '2026-03-05T12:00:00Z'


# --- failures and backoff -----------------------------------------------------

def test_fetch_failure_does_not_advance_the_watermark(scf, clean_state):
    """Advancing on failure would skip the window permanently."""
    taskhome.state.listeners['scf']['last_check'] = '2026-03-05T11:00:00Z'
    scf.error = RuntimeError('connection reset')
    taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:00:00'))
    assert taskhome.state.listeners['scf']['last_check'] == '2026-03-05T11:00:00Z'


def test_failures_back_off_and_recover(scf, clean_state):
    scf.error = RuntimeError('boom')
    taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:00:00'))
    state = taskhome.state.listeners['scf']
    assert state['consecutive_failures'] == 1
    assert 'backoff_until' in state

    # Inside the backoff window: no request attempted.
    before = len(scf.requests)
    taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:01:00'))
    assert len(scf.requests) == before

    # After it expires and the API recovers.
    scf.error = None
    scf.set_issues([issue(1)])
    taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T13:00:00'))
    assert taskhome.state.listeners['scf']['consecutive_failures'] == 0
    assert 'backoff_until' not in taskhome.state.listeners['scf']


def test_backoff_grows_with_repeated_failures(scf, clean_state):
    scf.error = RuntimeError('boom')
    taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:00:00'))
    first = taskhome.listeners.scf.parse_utc(taskhome.state.listeners['scf']['backoff_until'])

    taskhome.state.listeners['scf'].pop('backoff_until')
    taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:00:00'))
    second = taskhome.listeners.scf.parse_utc(taskhome.state.listeners['scf']['backoff_until'])

    assert second > first


def test_unprinted_issue_is_retried_next_cycle(scf, clean_state):
    """An offline printer must not consume the issue silently (P0-4)."""
    clean_state.online = False
    scf.set_issues([issue(1)])
    assert taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:00:00')) == 0
    assert taskhome.state.listeners['scf'].get('seen') in (None, [])

    clean_state.online = True
    assert taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T12:10:00')) == 1


# --- per-poll print cap -------------------------------------------------------

def test_poll_caps_receipts_and_says_so(scf, clean_state, monkeypatch):
    """Fixing pagination removed the accidental protection the old 100-issue
    truncation gave. A wide window must not print hundreds of receipts."""
    notices = []
    monkeypatch.setattr(taskhome.listeners.scf, 'print_scf_notice',
                        lambda h, d: notices.append(h) or True)
    taskhome.state.listeners['scf']['max_prints_per_poll'] = 5
    scf.set_issues([issue(i, f'2026-03-05T{i:02d}:00:00Z') for i in range(20)])

    printed = taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T23:00:00'))

    assert printed == 5
    assert len(notices) == 1
    assert '15 older issue' in notices[0]


def test_capped_issues_are_not_reprinted_next_poll(scf, clean_state, monkeypatch):
    """Suppressed issues must be marked seen, or the same flood recurs every
    cycle forever."""
    monkeypatch.setattr(taskhome.listeners.scf, 'print_scf_notice', lambda h, d: True)
    taskhome.state.listeners['scf']['max_prints_per_poll'] = 5
    scf.set_issues([issue(i, f'2026-03-05T{i:02d}:00:00Z') for i in range(20)])

    taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T23:00:00'))
    before = len(clean_state)
    taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T23:30:00'))

    assert len(clean_state) == before
    assert len(taskhome.state.listeners['scf']['seen']) == 20


def test_cap_keeps_the_newest(scf, clean_state, monkeypatch):
    monkeypatch.setattr(taskhome.listeners.scf, 'print_scf_notice', lambda h, d: True)
    taskhome.state.listeners['scf']['max_prints_per_poll'] = 3
    scf.set_issues([issue(i, f'2026-03-05{"T%02d:00:00Z" % i}') for i in range(10)])

    taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T23:00:00'))

    assert [i['id'] for i in clean_state] == [7, 8, 9]


def test_default_cap_applies_without_config(scf, clean_state, monkeypatch):
    monkeypatch.setattr(taskhome.listeners.scf, 'print_scf_notice', lambda h, d: True)
    monkeypatch.setattr(taskhome.listeners.scf, 'SCF_MAX_PRINTS_PER_POLL', 4)
    scf.set_issues([issue(i) for i in range(10)])
    assert taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T23:00:00')) == 4


def test_under_the_cap_prints_no_notice(scf, clean_state, monkeypatch):
    notices = []
    monkeypatch.setattr(taskhome.listeners.scf, 'print_scf_notice',
                        lambda h, d: notices.append(h) or True)
    taskhome.state.listeners['scf']['max_prints_per_poll'] = 50
    scf.set_issues([issue(i) for i in range(3)])

    taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T23:00:00'))
    assert notices == []


@pytest.mark.parametrize('bad', ['abc', None, -1])
def test_invalid_cap_falls_back_to_default(scf, clean_state, monkeypatch, bad):
    """A malformed cap must not silently disable printing."""
    monkeypatch.setattr(taskhome.listeners.scf, 'print_scf_notice', lambda h, d: True)
    taskhome.state.listeners['scf']['max_prints_per_poll'] = bad
    scf.set_issues([issue(i) for i in range(3)])
    assert taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T23:00:00')) == 3


def test_zero_cap_is_valid_monitor_only_mode(scf, clean_state, monkeypatch):
    """0 is deliberately meaningful: track issues without printing any."""
    notices = []
    monkeypatch.setattr(taskhome.listeners.scf, 'print_scf_notice',
                        lambda h, d: notices.append(h) or True)
    taskhome.state.listeners['scf']['max_prints_per_poll'] = 0
    scf.set_issues([issue(i) for i in range(3)])

    assert taskhome.listeners.scf.poll_scf_listener(utc('2026-03-05T23:00:00')) == 0
    assert clean_state == []
    assert len(taskhome.state.listeners['scf']['seen']) == 3  # tracked, not printed
    assert len(notices) == 1
