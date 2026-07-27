"""History pagination, search and filtering (MASTER_PLAN P2-1).

The history table rendered every record on every page load — with a 500-record
cap that is a large page and a slow render, and it only gets worse as listeners
are added. Filtering and paging now happen server-side.
"""
import pytest

import app as taskhome


@pytest.fixture
def client(clean_state):
    taskhome.app.config['TESTING'] = True
    with taskhome.app.test_client() as c:
        yield c


def task_record(n):
    return {'type': 'task', 'id': f'task-{n}', 'title': f'Task {n}',
            'print_time': f'2026-03-{(n % 28) + 1:02d}T09:00:00'}


def scf_record(n):
    return {'type': 'scf', 'id': 1000 + n, 'category': 'Pothole Patch',
            'address': f'{n} Elm St', 'description': 'A hole in the road',
            'status': 'Open', 'print_time': f'2026-03-{(n % 28) + 1:02d}T10:00:00'}


# --- paginate -----------------------------------------------------------------

def test_slices_the_requested_page():
    page = taskhome.paginate(list(range(100)), page=2, per_page=25)
    assert page['items'] == list(range(25, 50))
    assert page['start'] == 26 and page['end'] == 50
    assert page['pages'] == 4


def test_last_page_may_be_short():
    page = taskhome.paginate(list(range(72)), page=3, per_page=25)
    assert len(page['items']) == 22
    assert page['end'] == 72
    assert page['has_next'] is False


def test_empty_history():
    page = taskhome.paginate([], page=1, per_page=25)
    assert page['items'] == []
    assert page['pages'] == 1
    assert page['total'] == 0
    assert page['start'] == 0


@pytest.mark.parametrize('requested', [0, -5, 999, 'abc', None])
def test_out_of_range_page_clamps_rather_than_emptying(requested):
    """A stale bookmark or a deletion must not produce a blank table."""
    page = taskhome.paginate(list(range(50)), page=requested, per_page=25)
    assert 1 <= page['page'] <= page['pages']
    assert page['items']


@pytest.mark.parametrize('size', [7, 0, -1, 'lots', None, 10000])
def test_unsupported_page_size_falls_back(size):
    page = taskhome.paginate(list(range(50)), page=1, per_page=size)
    assert page['per_page'] == taskhome.DEFAULT_PAGE_SIZE


def test_supported_page_sizes_are_honoured():
    for size in taskhome.PAGE_SIZES:
        assert taskhome.paginate(list(range(500)), 1, size)['per_page'] == size


# --- page_numbers -------------------------------------------------------------

def test_short_runs_show_every_page():
    assert taskhome.page_numbers(1, 5) == [1, 2, 3, 4, 5]


def test_long_runs_elide_the_middle():
    numbers = taskhome.page_numbers(10, 20)
    assert numbers[0] == 1 and numbers[-1] == 20
    assert None in numbers
    assert 10 in numbers


def test_first_and_last_are_always_reachable():
    for page in (1, 7, 20):
        numbers = taskhome.page_numbers(page, 20)
        assert 1 in numbers and 20 in numbers


def test_no_duplicate_page_numbers():
    numbers = [n for n in taskhome.page_numbers(2, 20) if n is not None]
    assert len(numbers) == len(set(numbers))


# --- filtering ----------------------------------------------------------------

def test_filter_by_type():
    records = [task_record(1), scf_record(2), task_record(3)]
    assert len(taskhome.filter_history(records, kind='task')) == 2
    assert len(taskhome.filter_history(records, kind='scf')) == 1


def test_unknown_type_is_ignored_rather_than_matching_nothing():
    records = [task_record(1), scf_record(2)]
    assert len(taskhome.filter_history(records, kind='nonsense')) == 2


def test_search_matches_task_titles():
    records = [task_record(1), task_record(2)]
    assert len(taskhome.filter_history(records, query='Task 1')) == 1


def test_search_matches_scf_address_and_description():
    records = [scf_record(7), task_record(1)]
    assert len(taskhome.filter_history(records, query='Elm')) == 1
    assert len(taskhome.filter_history(records, query='hole in the road')) == 1


def test_search_is_case_insensitive():
    assert len(taskhome.filter_history([scf_record(1)], query='POTHOLE')) == 1


def test_all_terms_must_match():
    records = [scf_record(1), task_record(1)]
    assert len(taskhome.filter_history(records, query='pothole elm')) == 1
    assert len(taskhome.filter_history(records, query='pothole nonsense')) == 0


def test_search_matches_id():
    assert len(taskhome.filter_history([scf_record(5)], query='1005')) == 1


def test_search_and_type_combine():
    records = [scf_record(1), task_record(1)]
    assert len(taskhome.filter_history(records, query='1', kind='task')) == 1


def test_filter_preserves_order():
    records = [task_record(n) for n in range(10)]
    assert taskhome.filter_history(records, query='Task') == records


def test_records_missing_fields_do_not_crash():
    assert taskhome.filter_history([{'type': 'task'}], query='anything') == []


# --- the route ----------------------------------------------------------------

def test_page_renders_only_one_page_of_records(client, clean_state):
    taskhome.history.extend(task_record(n) for n in range(200))
    body = client.get('/task_page').get_data(as_text=True)
    assert 'Task 0' in body
    assert 'Task 199' not in body       # not on page 1
    assert 'of 200' in body


def test_second_page_shows_different_records(client, clean_state):
    taskhome.history.extend(task_record(n) for n in range(200))
    page2 = client.get('/task_page?page=2').get_data(as_text=True)
    assert 'Task 25' in page2
    assert 'Task 0<' not in page2


def test_search_narrows_the_table(client, clean_state):
    taskhome.history.extend([task_record(1), scf_record(2)])
    body = client.get('/task_page?q=Pothole').get_data(as_text=True)
    assert 'Pothole' in body
    assert 'Task 1' not in body


def test_type_filter_narrows_the_table(client, clean_state):
    taskhome.history.extend([task_record(1), scf_record(2)])
    body = client.get('/task_page?type=task').get_data(as_text=True)
    assert 'Task 1' in body
    assert 'Elm St' not in body


def test_no_matches_shows_an_empty_state(client, clean_state):
    taskhome.history.extend([task_record(1)])
    body = client.get('/task_page?q=zzzzz').get_data(as_text=True)
    assert 'No history matches' in body


def test_pager_links_carry_the_search(client, clean_state):
    """Paging must not silently drop the user's filter."""
    taskhome.history.extend(task_record(n) for n in range(100))
    body = client.get('/task_page?q=Task&per_page=25').get_data(as_text=True)
    assert 'q=Task' in body


def test_out_of_range_page_still_renders(client, clean_state):
    taskhome.history.extend([task_record(1)])
    assert client.get('/task_page?page=999').status_code == 200


@pytest.mark.parametrize('qs', ['page=abc', 'per_page=abc', 'page=-1',
                                'type=../etc', 'q=' + 'x' * 500])
def test_malformed_query_strings_do_not_500(client, clean_state, qs):
    taskhome.history.extend(task_record(n) for n in range(30))
    assert client.get(f'/task_page?{qs}').status_code == 200


def test_history_is_not_mutated_by_viewing(client, clean_state):
    taskhome.history.extend(task_record(n) for n in range(50))
    before = list(taskhome.history)
    client.get('/task_page?q=Task&page=2')
    assert taskhome.history == before


def test_index_still_shows_only_recent_history(client, clean_state):
    """The dashboard's five-item list is deliberately unpaginated."""
    taskhome.history.extend(task_record(n) for n in range(50))
    body = client.get('/').get_data(as_text=True)
    assert 'Task 0' in body
    assert 'Task 40' not in body
