"""Checklists (MASTER_PLAN P5-2 #11).

A list in the web UI; "Print" produces a receipt of `[ ]` checkboxes. Less a
listener than a mini-app -- nothing polls, nothing fires on a schedule -- but
the plan is right that it is thermal paper's most obvious use: a shopping list
you tick off with a pen in a shop, where a phone is a nuisance.

Two decisions worth stating.

**Printing does not clear the list.** A shopping list is used repeatedly and
mostly contains the same things; clearing it after every print would make it
useless. Items are ticked off in the UI when you want them gone.

**Items keep their order.** Not alphabetical, not by date added -- the order
someone puts them in is usually the order of the aisles, and re-sorting a
shopping list is actively unhelpful.
"""
import os
import uuid
from datetime import datetime

from . import constants, layouts, receipt, storage
from .logsetup import log

LISTS_FILENAME = 'lists.json'
MAX_LISTS = 20
MAX_ITEMS = 200
MAX_NAME = 60


def lists_path():
    return os.path.join(constants.DATA_DIR, LISTS_FILENAME)


def load_lists():
    value, ok = storage._load_json_file('lists', lists_path(), [])
    if not ok or not isinstance(value, list):
        return []
    return value


def save_lists(lists):
    return storage._save_json_file('lists', lists_path(), lists)


def all_lists():
    return load_lists()


def get_list(list_id):
    return next((l for l in load_lists() if l.get('id') == list_id), None)


def create_list(name):
    name = (name or '').strip()[:MAX_NAME]
    if not name:
        raise ValueError('Give the list a name.')
    lists = load_lists()
    if len(lists) >= MAX_LISTS:
        raise ValueError(f'That is the maximum of {MAX_LISTS} lists.')
    new = {'id': str(uuid.uuid4()), 'name': name, 'items': [],
           'created': datetime.now().isoformat()}
    lists.append(new)
    save_lists(lists)
    return new


def rename_list(list_id, name):
    name = (name or '').strip()[:MAX_NAME]
    if not name:
        raise ValueError('Give the list a name.')
    lists = load_lists()
    for entry in lists:
        if entry.get('id') == list_id:
            entry['name'] = name
            save_lists(lists)
            return entry
    raise ValueError('No such list.')


def delete_list(list_id):
    lists = load_lists()
    remaining = [l for l in lists if l.get('id') != list_id]
    if len(remaining) == len(lists):
        raise ValueError('No such list.')
    save_lists(remaining)
    return True


def add_item(list_id, text):
    """Add one item, or several if the text has newlines.

    Multi-line paste is worth supporting: people arrive with a list already
    written somewhere else, and adding fifteen things one at a time is the
    fastest way to make them stop using this.
    """
    lists = load_lists()
    entry = next((l for l in lists if l.get('id') == list_id), None)
    if entry is None:
        raise ValueError('No such list.')

    added = []
    for line in str(text or '').splitlines():
        line = line.strip()
        if not line:
            continue
        if len(entry['items']) >= MAX_ITEMS:
            log.warning(f"List {entry['name']!r} is full at {MAX_ITEMS} items")
            break
        item = {'id': uuid.uuid4().hex[:12], 'text': line[:120], 'done': False}
        entry['items'].append(item)
        added.append(item)

    if not added:
        raise ValueError('Nothing to add.')
    save_lists(lists)
    return added


def set_item(list_id, item_id, done=None, text=None):
    lists = load_lists()
    entry = next((l for l in lists if l.get('id') == list_id), None)
    if entry is None:
        raise ValueError('No such list.')
    for item in entry['items']:
        if item.get('id') == item_id:
            if done is not None:
                item['done'] = bool(done)
            if text is not None:
                cleaned = str(text).strip()[:120]
                if not cleaned:
                    raise ValueError('An item needs some text.')
                item['text'] = cleaned
            save_lists(lists)
            return item
    raise ValueError('No such item.')


def remove_item(list_id, item_id):
    lists = load_lists()
    entry = next((l for l in lists if l.get('id') == list_id), None)
    if entry is None:
        raise ValueError('No such list.')
    before = len(entry['items'])
    entry['items'] = [i for i in entry['items'] if i.get('id') != item_id]
    if len(entry['items']) == before:
        raise ValueError('No such item.')
    save_lists(lists)
    return True


def clear_done(list_id):
    lists = load_lists()
    entry = next((l for l in lists if l.get('id') == list_id), None)
    if entry is None:
        raise ValueError('No such list.')
    removed = len([i for i in entry['items'] if i.get('done')])
    entry['items'] = [i for i in entry['items'] if not i.get('done')]
    save_lists(lists)
    return removed


# --- printing -----------------------------------------------------------------

def printable_items(entry, include_done=False):
    """What goes on the paper.

    Ticked items are left off by default: the point of printing is to carry
    what is still outstanding, and a list of things you already have is noise.
    """
    return [i for i in entry.get('items', [])
            if include_done or not i.get('done')]


def list_blocks(entry, include_done=False):
    """A checkbox receipt.

    Font A at double height, because this gets read at arm's length in a shop
    aisle while holding something else, and the checkbox has to be big enough
    to hit with a pen.
    """
    items = printable_items(entry, include_done)
    blocks = [
        receipt.text(entry.get('name', 'List'), font='a', width=2, height=2,
                     bold=True),
        receipt.gap(6),
        receipt.text(f"{len(items)} item(s)  -  {layouts._stamp()}", font='b'),
        receipt.rule(),
    ]
    for item in items:
        mark = '[x]' if item.get('done') else '[ ]'
        blocks.append(receipt.text(f"{mark} {item.get('text', '')}",
                                   font='a', width=1, height=2, align='left'))
    if not items:
        blocks.append(receipt.text('Nothing outstanding.', font='b'))
    blocks.append(receipt.rule())
    return blocks


def print_list(list_id, include_done=False):
    """Print one list. Returns True only if paper came out."""
    from . import printing, queue

    entry = get_list(list_id)
    if entry is None:
        raise ValueError('No such list.')

    blocks = list_blocks(entry, include_done)
    history = {
        'type': 'list',
        'id': entry['id'],
        'category': 'Checklist',
        'title': entry.get('name', 'List'),
        'description': ', '.join(
            i['text'] for i in printable_items(entry, include_done))[:500],
        'print_time': datetime.now().isoformat(),
    }

    if printing.print_blocks(blocks):
        printing.record_history(history)
        return True

    # Queued like a listener receipt rather than dropped: someone pressed a
    # button and is waiting for paper, and there is no schedule to retry from.
    queue.enqueue('list', blocks, description=f"List: {entry.get('name', '')}",
                  history=history)
    return False


def stats():
    lists = load_lists()
    return {
        'lists': len(lists),
        'items': sum(len(l.get('items', [])) for l in lists),
        'outstanding': sum(len([i for i in l.get('items', []) if not i.get('done')])
                           for l in lists),
    }
