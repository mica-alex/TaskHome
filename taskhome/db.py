"""SQLite storage (MASTER_PLAN P1-2).

One file, `taskhome.db`, in the data directory. `sqlite3` is in the standard
library, so this adds nothing anyone has to install.

What it buys over four JSON files:

* **Crash safety.** WAL mode plus a transaction per write. The JSON path was
  already atomic per file, but a change touching two stores could land half
  applied; a transaction cannot.
* **No whole-file rewrite.** Appending one history record rewrote the entire
  file, which at a 500-record cap is small but grows with every listener added.
* **History becomes query-shaped**, which is what pagination and the print
  statistics actually want -- `LIMIT/OFFSET` and `COUNT` rather than slicing an
  ever-growing list in memory.

Deliberately under-normalised. `tasks` and `history` get real columns because
they are queried; everything else -- config, listener state, the print queue,
lists, chore charts -- lives in a `kv` table as a JSON blob, because it is
always read and written whole. Normalising those would be work with no reader.

**Migration is automatic and never destroys anything.** On first start with no
database, JSON is imported from the data directory, or from the repo root if
that is where it still lives (the pre-`P1-9` layout). The originals are then
renamed `*.imported-<timestamp>` rather than deleted, so a bad migration is
recoverable by hand -- the same rule the backup system follows.
"""
import json
import os
import sqlite3
import threading
from datetime import datetime

from . import constants
from .logsetup import log

DB_FILENAME = 'taskhome.db'
SCHEMA_VERSION = 1

#: Stores kept whole in `kv`. Always read and written entire, so columns would
#: buy nothing.
KV_STORES = ('config', 'listeners', 'queue', 'lists', 'chores')

_local = threading.local()
_init_lock = threading.Lock()
_initialised = set()


def db_path():
    return os.path.join(constants.DATA_DIR, DB_FILENAME)


def connect():
    """A connection for this thread.

    One per thread, because a sqlite3 connection is not safe to share across
    them, and this app has several: the scheduler, request handlers, and a push
    listener's network thread.
    """
    path = db_path()
    existing = getattr(_local, 'conn', None)
    if existing is not None and getattr(_local, 'path', None) == path:
        return existing

    if existing is not None:
        existing.close()

    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL: readers do not block the writer, which matters because the scheduler
    # writes while a page is being served.
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA foreign_keys=ON')
    _local.conn = conn
    _local.path = path
    ensure_schema(conn)
    return conn


def close():
    conn = getattr(_local, 'conn', None)
    if conn is not None:
        conn.close()
        _local.conn = None
        _local.path = None


def ensure_schema(conn):
    path = db_path()
    with _init_lock:
        if path in _initialised:
            return
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY, value TEXT);

            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY, value TEXT NOT NULL);

            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                position INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL);

            CREATE TABLE IF NOT EXISTS history (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT UNIQUE,
                type TEXT NOT NULL,
                print_time TEXT NOT NULL,
                payload TEXT NOT NULL);

            CREATE INDEX IF NOT EXISTS history_time ON history(print_time DESC);
            CREATE INDEX IF NOT EXISTS history_type ON history(type);
        ''')
        conn.execute('INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)',
                     ('schema_version', str(SCHEMA_VERSION)))
        conn.commit()
        _initialised.add(path)


def exists():
    return os.path.exists(db_path())


def discard():
    """Delete the database. Used only when a migration must not half-commit.

    A partially imported database is worse than none, because its existence is
    what makes the backend switch to it.
    """
    forget()
    for suffix in ('', '-wal', '-shm'):
        path = db_path() + suffix
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                log.error(f'Could not remove {path}: {e}')
    return True


def forget():
    """Drop cached connections and init state. For tests moving DATA_DIR."""
    close()
    with _init_lock:
        _initialised.clear()


# --- key/value stores ---------------------------------------------------------

def get_kv(key, default=None):
    row = connect().execute('SELECT value FROM kv WHERE key = ?', (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row['value'])
    except ValueError:
        log.error(f'kv store {key!r} holds unreadable JSON')
        return default


def set_kv(key, value):
    conn = connect()
    with conn:
        conn.execute('INSERT INTO kv (key, value) VALUES (?, ?) '
                     'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                     (key, json.dumps(value)))
    return True


# --- tasks --------------------------------------------------------------------

def get_tasks():
    rows = connect().execute(
        'SELECT payload FROM tasks ORDER BY position, rowid').fetchall()
    tasks = []
    for row in rows:
        try:
            tasks.append(json.loads(row['payload']))
        except ValueError:
            log.error('A task row holds unreadable JSON; skipping it')
    return tasks


def set_tasks(tasks):
    """Replace the whole set, preserving order.

    Whole-set replacement rather than per-task updates because the caller holds
    the list in memory and mutates it freely; a transaction makes this atomic,
    which is the property that matters.
    """
    conn = connect()
    with conn:
        conn.execute('DELETE FROM tasks')
        conn.executemany(
            'INSERT INTO tasks (id, position, payload) VALUES (?, ?, ?)',
            [(str(task.get('id', index)), index, json.dumps(task))
             for index, task in enumerate(tasks)])
    return True


# --- history ------------------------------------------------------------------

def get_history(limit=None):
    """Newest first, matching the in-memory list's order."""
    sql = 'SELECT payload FROM history ORDER BY print_time DESC, rowid DESC'
    params = ()
    if limit:
        sql += ' LIMIT ?'
        params = (int(limit),)
    records = []
    for row in connect().execute(sql, params).fetchall():
        try:
            records.append(json.loads(row['payload']))
        except ValueError:
            log.error('A history row holds unreadable JSON; skipping it')
    return records


def set_history(records):
    conn = connect()
    with conn:
        conn.execute('DELETE FROM history')
        conn.executemany(
            'INSERT OR REPLACE INTO history (uid, type, print_time, payload) '
            'VALUES (?, ?, ?, ?)',
            [(record.get('uid'), record.get('type', 'task'),
              record.get('print_time', ''), json.dumps(record))
             for record in records])
    return True


def add_history(record, cap=None):
    """Append one record and trim to `cap`. The append-only fast path.

    This is why history has its own table: adding one receipt used to rewrite
    the entire file.
    """
    conn = connect()
    with conn:
        conn.execute(
            'INSERT OR REPLACE INTO history (uid, type, print_time, payload) '
            'VALUES (?, ?, ?, ?)',
            (record.get('uid'), record.get('type', 'task'),
             record.get('print_time', ''), json.dumps(record)))
        if cap:
            conn.execute(
                'DELETE FROM history WHERE rowid NOT IN ('
                '  SELECT rowid FROM history ORDER BY print_time DESC, rowid DESC'
                '  LIMIT ?)', (int(cap),))
    return True


def count_history(kind=None):
    sql = 'SELECT COUNT(*) AS n FROM history'
    params = ()
    if kind:
        sql += ' WHERE type = ?'
        params = (kind,)
    return connect().execute(sql, params).fetchone()['n']


# --- migration ----------------------------------------------------------------

def _legacy_json_paths():
    """Where JSON might be, newest layout first.

    The repo root is the pre-P1-9 layout, from when the app ran with the
    checkout as its working directory.
    """
    return [constants.DATA_DIR, constants.APP_ROOT]


def find_json_source():
    """The directory holding a JSON datastore to import, or None."""
    for directory in _legacy_json_paths():
        for name in ('tasks.json', 'config.json', 'history.json'):
            if os.path.exists(os.path.join(directory, name)):
                return directory
    return None


def migrate_from_json(directory=None):
    """Import a JSON datastore into a fresh database. Returns a report.

    Never deletes: the originals are renamed `*.imported-<timestamp>`, so a bad
    migration is recoverable by hand. Anything that fails to parse is left
    exactly where it is and reported, rather than being imported as empty --
    silently starting with no tasks is the failure this codebase has already
    had once.
    """
    directory = directory or find_json_source()
    report = {'source': directory, 'imported': {}, 'failed': {}, 'renamed': []}
    if not directory:
        return report

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    to_rename = []

    for name, default in (('config', {}), ('listeners', {}), ('queue', []),
                          ('lists', []), ('chores', []),
                          ('tasks', []), ('history', [])):
        path = os.path.join(directory, f'{name}.json')
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding='utf-8') as handle:
                value = json.load(handle)
        except Exception as e:
            log.error(f'Could not import {path}: {e}')
            report['failed'][name] = str(e)
            continue

        if name == 'tasks':
            set_tasks(value if isinstance(value, list) else [])
            report['imported']['tasks'] = len(value or [])
        elif name == 'history':
            set_history(value if isinstance(value, list) else [])
            report['imported']['history'] = len(value or [])
        else:
            set_kv(name, value if value is not None else default)
            report['imported'][name] = (len(value) if hasattr(value, '__len__')
                                        else 1)
        to_rename.append(path)

    # Only rename once every store has been read, so a failure part-way leaves
    # the whole set untouched.
    if not report['failed']:
        for path in to_rename:
            target = f'{path}.imported-{stamp}'
            try:
                os.rename(path, target)
                report['renamed'].append(os.path.basename(target))
            except OSError as e:
                log.warning(f'Could not rename {path}: {e}')

    if report['imported']:
        log.info(f"Imported JSON datastore from {directory}: "
                 f"{report['imported']}")
    return report


def export_json(directory):
    """Write every store back out as JSON, for backup symmetry."""
    os.makedirs(directory, exist_ok=True)
    written = []
    for name in KV_STORES:
        value = get_kv(name)
        if value is None:
            continue
        path = os.path.join(directory, f'{name}.json')
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, indent=2)
        written.append(path)
    for name, value in (('tasks', get_tasks()), ('history', get_history())):
        path = os.path.join(directory, f'{name}.json')
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, indent=2)
        written.append(path)
    return written


def stats():
    conn = connect()
    return {
        'path': db_path(),
        'tasks': conn.execute('SELECT COUNT(*) AS n FROM tasks').fetchone()['n'],
        'history': count_history(),
        'size_bytes': os.path.getsize(db_path()) if exists() else 0,
    }
