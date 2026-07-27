"""History paging, search and filtering (P2-1).

Pure functions over a list -- no Flask, no state -- so they are trivially
testable and will transplant unchanged onto a SQL query when P1-2 lands.
"""

# The history table rendered every record on every page load. With a 500-record
# cap that is a large page and a slow render, and it gets worse as listeners
# are added. Filtering and paging happen server-side so the browser is never
# handed the whole list.

PAGE_SIZES = (25, 50, 100, 250)
DEFAULT_PAGE_SIZE = 25
HISTORY_KINDS = ('task', 'scf')


def history_search_text(record):
    """The fields a user would reasonably search by, lowercased."""
    parts = [record.get('title'), record.get('extra'), record.get('category'),
             record.get('address'), record.get('summary'),
             record.get('description'), record.get('status'),
             str(record.get('id', ''))]
    return ' '.join(p for p in parts if p).lower()


def filter_history(records, query='', kind=''):
    """Filter by free text and record type. Order is preserved (newest first)."""
    result = records
    if kind in HISTORY_KINDS:
        result = [r for r in result if r.get('type', 'task') == kind]
    query = (query or '').strip().lower()
    if query:
        terms = query.split()
        result = [r for r in result
                  if all(term in history_search_text(r) for term in terms)]
    return result


def paginate(items, page=1, per_page=DEFAULT_PAGE_SIZE):
    """Slice `items` into a page, tolerating any input.

    Out-of-range pages clamp to the last page rather than showing an empty
    table, which is what a stale bookmark or a deletion would otherwise cause.
    """
    try:
        per_page = int(per_page)
    except (TypeError, ValueError):
        per_page = DEFAULT_PAGE_SIZE
    if per_page not in PAGE_SIZES:
        per_page = DEFAULT_PAGE_SIZE

    total = len(items)
    pages = max(1, (total + per_page - 1) // per_page)

    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    page = max(1, min(page, pages))

    start = (page - 1) * per_page
    end = min(start + per_page, total)
    return {
        'items': items[start:end],
        'page': page,
        'pages': pages,
        'total': total,
        'per_page': per_page,
        'has_prev': page > 1,
        'has_next': page < pages,
        'start': start + 1 if total else 0,
        'end': end,
        'page_sizes': PAGE_SIZES,
    }


def page_numbers(page, pages, window=2):
    """Page numbers to show, with None marking an elided run.

    A 500-record history at 25 per page is 20 pages; rendering every number is
    noise, and rendering only prev/next makes distant pages unreachable.
    """
    if pages <= 7:
        return list(range(1, pages + 1))
    numbers = {1, pages}
    numbers.update(range(max(1, page - window), min(pages, page + window) + 1))
    ordered = sorted(numbers)
    out = []
    previous = 0
    for number in ordered:
        if number - previous > 1:
            out.append(None)
        out.append(number)
        previous = number
    return out
