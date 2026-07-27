"""The SeeClickFix listener.

Three defects that interact, fixed together (P0-7): `after` is inclusive so
windows overlap and need id-based dedup; pagination was ignored, silently
dropping everything past the first 100; and the watermark was taken after the
fetch, so anything created during the request fell between windows.

There is a per-poll receipt ceiling because fixing pagination removed the
accidental protection the old truncation provided.
"""
from datetime import datetime, timedelta, timezone

import requests
from dateutil import parser

from .. import printing, state, storage
from ..logsetup import log


SCF_ISSUES_URL = "https://seeclickfix.com/api/v2/issues"
SCF_PER_PAGE = 100
SCF_MAX_PAGES = 20          # 2000 issues per poll; a guard, not a real limit
SCF_SEEN_LIMIT = 2000       # bounded dedup memory
SCF_MAX_BACKOFF_MINUTES = 60

# Ceiling on receipts emitted by a single poll. Fixing the pagination bug
# removed the accidental protection the old 100-issue truncation provided: a
# wide window (a long outage, a cleared last_check, or a freshly enabled
# listener looking an hour back) could now genuinely print hundreds of
# receipts. Excess issues are marked seen so they do not simply reappear next
# cycle, and a single notice receipt reports how many were suppressed --
# never a silent drop. Override per listener with 'max_prints_per_poll'.
SCF_MAX_PRINTS_PER_POLL = 20


def parse_utc(value, default=None):
    """Parse an ISO 8601 timestamp to an aware UTC datetime, or return default."""
    if not value:
        return default
    try:
        parsed = parser.parse(str(value).strip())
    except (ValueError, TypeError, OverflowError) as e:
        log.warning(f"Failed to parse timestamp {value!r}: {e}")
        return default
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fetch_scf_issues(request_types, after):
    """Fetch every page of SCF issues created after `after`.

    The old code requested one page of 100 and ignored
    metadata.pagination entirely, so a busy interval silently dropped
    everything past the first page (P0-7). Raises on transport/HTTP errors so
    the caller can back off without advancing its watermark.
    """
    collected = []
    page = 1
    while page <= SCF_MAX_PAGES:
        params = {
            'status': 'open,acknowledged',
            'request_types': request_types,
            'after': after,
            'per_page': str(SCF_PER_PAGE),
            'page': str(page),
        }
        log.info(f"Fetching SCF issues after {after}, page {page}")
        resp = requests.get(SCF_ISSUES_URL, params=params, timeout=15)
        resp.raise_for_status()
        payload = resp.json()

        issues = payload.get('issues') or []
        collected.extend(issues)

        pagination = (payload.get('metadata') or {}).get('pagination') or {}
        total_pages = pagination.get('pages')
        if total_pages is None:
            # Fall back to "a short page means the last page".
            if len(issues) < SCF_PER_PAGE:
                break
        elif page >= total_pages:
            break
        page += 1
    else:
        log.warning(
            f"SCF pagination hit the {SCF_MAX_PAGES}-page guard; "
            f"some issues were not fetched this cycle")
    return collected


def print_scf_notice(headline, detail):
    """Small receipt announcing that the listener suppressed something.

    Kept deliberately minimal: it exists so a capped poll is visible on paper
    rather than only in a log nobody reads.
    """
    if not printing.is_printer_connected():
        log.warning(f"Printer not connected; notice not printed: {headline}")
        return False
    try:
        with printing.open_printer() as p:
            p.set(align='center', font='a', bold=True, custom_size=True,
                  width=1, height=1)
            p.text('SeeClickFix notice\n')
            p.set(align='center', font='b', bold=False, custom_size=True,
                  width=1, height=1)
            p.text(headline + '\n\n')
            p.text(detail + '\n\n')
            p.text(datetime.now().strftime('%I:%M %p, %m/%d/%Y') + '\n')
            p.cut()
        return True
    except Exception as e:
        log.error(f"Failed to print SCF notice: {e}")
        return False


def scf_due(scf, now_utc):
    """Whether the SCF listener should poll now."""
    backoff_until = parse_utc(scf.get('backoff_until'))
    if backoff_until and now_utc < backoff_until:
        return False
    last_check = parse_utc(scf.get('last_check'))
    if last_check is None:
        return True
    try:
        interval_minutes = int(scf.get('interval', 10))
    except (TypeError, ValueError):
        interval_minutes = 10
    return (now_utc - last_check) >= timedelta(minutes=max(interval_minutes, 1))


def poll_scf_listener(now_utc):
    """Poll SeeClickFix and print new issues. Returns the number printed.

    Fixes the three P0-7 defects together, because they interact:
      * dedup by issue id, since `after` is inclusive and windows overlap
      * follow pagination rather than silently truncating at 100
      * take the watermark BEFORE the request, so issues created while the
        request is in flight are caught next cycle instead of being skipped
    """
    scf = state.listeners.get('scf')
    if not scf or not scf.get('enabled'):
        return 0

    request_types = (scf.get('request_types') or '').strip()
    if not request_types:
        log.warning("SCF listener enabled but request_types empty; skipping check")
        return 0

    if not scf_due(scf, now_utc):
        return 0

    log.debug("Checking SCF listener")
    last_check = parse_utc(scf.get('last_check'))
    # The watermark for the NEXT poll is taken now, before the request. Using
    # a timestamp captured after the fetch would skip anything created while
    # the fetch was running.
    watermark = now_utc
    after_dt = last_check if last_check else (now_utc - timedelta(hours=1))
    after = after_dt.strftime('%Y-%m-%dT%H:%M:%SZ')

    try:
        issues = fetch_scf_issues(request_types, after)
    except Exception as e:
        failures = scf.get('consecutive_failures', 0) + 1
        # Exponential backoff, capped. last_check is deliberately NOT advanced,
        # so the window is retried rather than skipped.
        delay = min(2 ** min(failures, 6), SCF_MAX_BACKOFF_MINUTES)
        scf['consecutive_failures'] = failures
        scf['backoff_until'] = (now_utc + timedelta(minutes=delay)).strftime('%Y-%m-%dT%H:%M:%SZ')
        scf['last_error'] = str(e)
        storage.save_listeners()
        log.error(
            f"SCF listener error (failure {failures}, retrying in {delay}m): {e}")
        return 0

    seen = scf.get('seen') or []
    seen_set = set(seen)

    fresh = []
    for issue in issues:
        issue_id = issue.get('id')
        if issue_id is None or issue_id in seen_set:
            continue
        seen_set.add(issue_id)
        fresh.append(issue)

    fresh.sort(key=lambda i: i.get('created_at') or '')

    # Cap receipts per poll. Keep the newest, since those are the ones still
    # worth acting on, and mark the rest seen so they don't reappear forever.
    try:
        per_poll_cap = int(scf.get('max_prints_per_poll', SCF_MAX_PRINTS_PER_POLL))
    except (TypeError, ValueError):
        per_poll_cap = SCF_MAX_PRINTS_PER_POLL
    if per_poll_cap < 0:
        # Negative is meaningless; fall back rather than clamping to 0, which
        # would silently turn the listener into monitor-only. 0 IS valid and
        # means exactly that, deliberately.
        log.warning(
            f"Invalid max_prints_per_poll {per_poll_cap}, using {SCF_MAX_PRINTS_PER_POLL}")
        per_poll_cap = SCF_MAX_PRINTS_PER_POLL

    suppressed = []
    if len(fresh) > per_poll_cap:
        suppressed = fresh[:-per_poll_cap] if per_poll_cap else fresh
        fresh = fresh[-per_poll_cap:] if per_poll_cap else []
        log.warning(
            f"SCF poll found {len(suppressed) + len(fresh)} new issues, over the "
            f"{per_poll_cap}-per-poll cap. Printing the {len(fresh)} newest and "
            f"suppressing {len(suppressed)}.")

    printed = 0
    for issue in fresh:
        if printing.print_scf_issue(issue):
            seen.append(issue.get('id'))
            printed += 1
        else:
            # Printing failed (offline printer). Leave it out of `seen` so the
            # next overlapping window picks it up again (P0-4).
            log.warning(
                f"SCF issue {issue.get('id')} not printed; will retry next cycle")

    if suppressed:
        # Mark suppressed issues seen so the next poll doesn't reprint them,
        # then say so on paper -- a silent drop would misrepresent the feed.
        for issue in suppressed:
            seen.append(issue.get('id'))
        print_scf_notice(
            f"{len(suppressed)} older issue(s) were not printed",
            f"Poll exceeded the {per_poll_cap}-per-poll limit. "
            f"See the TaskHome state.history and log for what was skipped.")

    if len(seen) > SCF_SEEN_LIMIT:
        del seen[:-SCF_SEEN_LIMIT]  # keep the most recent ids
    scf['seen'] = seen
    scf['last_check'] = watermark.strftime('%Y-%m-%dT%H:%M:%SZ')
    scf['consecutive_failures'] = 0
    scf.pop('backoff_until', None)
    scf.pop('last_error', None)
    storage.save_listeners()
    log.info(
        f"SCF listener checked at {scf['last_check']}: "
        f"{len(issues)} fetched, {len(fresh)} new, {printed} printed")
    return printed
