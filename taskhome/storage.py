"""Loading, saving and migrating the JSON datastore.

Writes are atomic (temp file + fsync + rename) and a store that failed to load
is write-blocked, so a damaged-but-repairable file is never overwritten with
defaults -- the chain that destroyed a real tasks.json (P0-5).
"""
import json
import os
import uuid
import shutil
import tempfile
from datetime import datetime, timezone

from . import constants, state
from .logsetup import log


def migrate_legacy_data_files(legacy_dir=None, data_dir=None):
    """Move root-level JSON state into data/ (P1-9).

    TaskHome has historically been run straight out of a git clone with its
    four JSON files in the repo root, so existing installs must keep working
    across this change without anyone having to move files by hand.

    Idempotent. Returns a list of human-readable actions taken.

    Deliberate choices about the awkward cases:
      * Each file moves independently, so a failure part-way leaves the rest
        correct rather than half-migrated in an unpredictable way.
      * If a file exists in BOTH places we do not guess which is current. The
        data/ copy wins (it is what the app will read) and the legacy file is
        renamed aside rather than deleted, so nothing is destroyed.
      * A read-only or unwritable filesystem is reported and the app continues
        against the legacy location instead of refusing to start.
      * os.replace is atomic within a filesystem; a cross-device move falls
        back to copy-then-remove, which keeps the source until the copy lands.
    """
    explicit = legacy_dir is not None or data_dir is not None
    legacy_dir = legacy_dir or constants.APP_ROOT
    data_dir = data_dir or constants.DATA_DIR
    actions = []

    # Only migrate into the DEFAULT data directory.
    #
    # TASKHOME_DATA_DIR means "the data lives here" -- typically a scratch
    # directory for a throwaway run. Migrating in that case reaches into the
    # repo root and relocates the real installation's files, which is the
    # opposite of what an override is for. It happened during development: a
    # scratch run moved the live JSON aside and only the never-delete rule
    # below kept the data.
    if not explicit and not constants.DATA_DIR_IS_DEFAULT:
        log.debug(
            f"TASKHOME_DATA_DIR is set ({data_dir}); skipping legacy migration "
            f"so the repo root is left alone")
        return actions

    pending = [
        (name, os.path.join(legacy_dir, filename), os.path.join(data_dir, filename))
        for name, filename in constants.STORE_FILENAMES.items()
    ]
    if not any(os.path.exists(legacy) for _, legacy, _ in pending):
        return actions  # nothing to migrate; the common case after first run

    try:
        os.makedirs(data_dir, exist_ok=True)
    except OSError as e:
        log.error(
            f"Cannot create {data_dir}: {e}. Continuing with the legacy "
            f"location; state will stay in {legacy_dir}.")
        return actions

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    for name, legacy, target in pending:
        if not os.path.exists(legacy):
            continue
        if os.path.exists(target):
            aside = f"{legacy}.superseded-{stamp}"
            try:
                os.replace(legacy, aside)
                actions.append(f"{name}: kept data/ copy, moved legacy file to {aside}")
                log.warning(
                    f"{name} exists in both {legacy_dir} and {data_dir}. Using the "
                    f"data/ copy; the old one is preserved at {aside}.")
            except OSError as e:
                log.error(f"Could not set aside legacy {name}: {e}")
            continue
        try:
            os.replace(legacy, target)
            actions.append(f"{name}: moved into {data_dir}")
        except OSError:
            # Different filesystem, or replace unsupported. Copy first, and
            # only unlink the source once the copy is safely in place.
            try:
                shutil.copy2(legacy, target)
                os.unlink(legacy)
                actions.append(f"{name}: copied into {data_dir}")
            except OSError as e:
                log.error(
                    f"Failed to migrate {name} from {legacy} to {target}: {e}")

    if actions:
        log.warning(
            f"Migrated state into {data_dir} (P1-9): " + '; '.join(actions))
        try:
            with open(os.path.join(legacy_dir, 'DATA_MOVED.txt'), 'w') as f:
                f.write(
                    "TaskHome state moved to the data/ directory.\n\n"
                    f"Migrated {stamp}. Files now live in:\n  {data_dir}\n\n"
                    "This file is only a breadcrumb and can be deleted.\n"
                    "Set TASKHOME_DATA_DIR to use a different location.\n")
        except OSError:
            pass  # breadcrumb is a nicety, never a failure
    return actions


#: Store names that live in the database rather than in a file. Caches
#: (`nws-zones`, SCF request types) are deliberately absent: they are derived
#: data, safe to delete, and have no business in the datastore.
DB_STORES = ('config', 'state.config', 'tasks', 'history', 'listeners',
             'queue', 'lists', 'chores')


def use_db():
    """Whether the SQLite backend is active.

    True once the database exists. Migration creates it, so this flips exactly
    once, on the first start after upgrading.
    """
    from . import db
    return db.exists()


def _db_key(name):
    # load_data() reads config under the name 'state.config' for its error
    # messages; the store itself is 'config'.
    return 'config' if name == 'state.config' else name


def migrate_to_database():
    """Move a JSON datastore into SQLite, once (P1-2).

    Runs before anything is read. Does nothing if the database already exists,
    so this is a one-way door that opens exactly once.

    Handles both layouts: `data/` as it is now, and the repo root as it was
    before P1-9, for an install that skipped straight from the old version.
    Nothing is deleted -- the JSON is renamed `*.imported-<timestamp>`.
    """
    from . import db
    if db.exists():
        return None
    source = db.find_json_source()
    if not source:
        # A genuinely fresh install. Touch the database so the backend is
        # active from the first write rather than the second start.
        db.connect()
        log.info(f'Created a new database at {db.db_path()}')
        return None

    log.info(f'Migrating the JSON datastore at {source} into SQLite')
    report = db.migrate_from_json(source)

    if report['failed']:
        # All or nothing. A half-migrated database is worse than none: it
        # exists, so the backend switches to it, the store that failed to
        # import reads as empty, nothing is marked as failed, and the next
        # save writes that emptiness over the only surviving copy. That is
        # precisely the P0-5 chain, reintroduced at the migration boundary.
        #
        # Discarding it means the JSON path stays active, the corrupt file is
        # detected there as it always was, and writes to it stay blocked --
        # and the migration is retried on the next start, failing just as
        # loudly, until someone fixes the file.
        db.discard()
        log.error(
            f"Migration abandoned: {', '.join(report['failed'])} did not "
            f"parse. Nothing was moved and the database was removed, so "
            f"TaskHome is still reading JSON. Fix the file and restart.")
        report['abandoned'] = True
    return report


def _load_json_file(name, path, default):
    """Load one store. Returns (value, ok).

    A missing file is fine — it yields the default and ok=True. A file that
    exists but won't parse is NOT fine: it returns ok=False so that saves are
    blocked rather than silently overwriting the user's data with defaults.
    """
    if name in DB_STORES and use_db():
        from . import db
        key = _db_key(name)
        try:
            if key == 'tasks':
                return db.get_tasks(), True
            if key == 'history':
                return db.get_history(), True
            value = db.get_kv(key)
            return (default if value is None else value), True
        except Exception as e:
            # A database that will not read is the same class of problem as a
            # JSON file that will not parse: block writes rather than
            # overwrite what might be recoverable.
            log.error(f"FAILED to load {name} from the database: {e}")
            state.load_failed.add(name)
            return default, False

    if not os.path.exists(path):
        log.warning(f"{name} file not found: {path}")
        return default, True
    try:
        with open(path, 'r') as f:
            return json.load(f), True
    except (json.JSONDecodeError, OSError) as e:
        log.error(
            f"FAILED to load {name} from {path}: {e}. "
            f"Refusing to overwrite it; fix or remove the file and restart. "
            f"A copy of the current in-memory state will NOT be saved over it.")
        state.load_failed.add(name)
        return default, False


def load_data():
    # No `global` needed: these assign attributes on the state module, which
    # is exactly why every other module reads them as state.x.
    log.debug("Entering load_data")
    state.load_failed.clear()

    # Must run before anything reads or writes: it decides where the files are.
    migrate_legacy_data_files()
    migrate_to_database()
    try:
        os.makedirs(constants.DATA_DIR, exist_ok=True)
    except OSError as e:
        log.error(f"Cannot create data directory {constants.DATA_DIR}: {e}")

    loaded_config, ok = _load_json_file('state.config', os.path.abspath(constants.CONFIG_FILE), None)
    if ok and loaded_config is not None:
        # Merge over defaults rather than replacing, so a file missing a key
        # can't break code that reads state.config['hostname'] (P1-6).
        merged = dict(constants.DEFAULT_CONFIG)
        if isinstance(loaded_config, dict):
            merged.update(loaded_config)
        if merged.get('theme') == 'high-contrast':
            merged['theme'] = 'system'
            log.debug("Converted high-contrast theme to system")
        state.config = merged
        log.debug(f"Loaded state.config with {len(state.config)} keys")

    loaded_tasks, ok = _load_json_file('tasks', os.path.abspath(constants.TASKS_FILE), None)
    if ok and loaded_tasks is not None:
        state.tasks = loaded_tasks
        for task in state.tasks:
            if 'enabled' not in task:
                task['enabled'] = True
        log.debug(f"Loaded {len(state.tasks)} state.tasks")

    loaded_history, ok = _load_json_file('history', os.path.abspath(constants.HISTORY_FILE), None)
    if ok and loaded_history is not None:
        state.history = loaded_history
        backfilled = 0
        for item in state.history:  # Add type to existing state.history if missing
            if 'type' not in item:
                item['type'] = 'task'
            # A stable handle for the row (P4-6). History holds three record
            # types whose ids come from different namespaces -- a task id and
            # an SCF issue id can collide -- and a list position stops being
            # an identity once the list is filtered, paged or trimmed.
            if 'uid' not in item:
                item['uid'] = uuid.uuid4().hex
                backfilled += 1
        if backfilled:
            # Persisted immediately, because these are random. Leaving them in
            # memory only would mint a different set on every start, so a uid
            # rendered into a page would 404 the moment the app restarted --
            # and the page would look completely normal until it did.
            log.info(f"Assigned ids to {backfilled} older history record(s)")
            save_history()
        log.debug(f"Loaded {len(state.history)} state.history records")

    listeners_path = os.path.abspath(constants.LISTENERS_FILE)
    loaded_listeners, ok = _load_json_file('listeners', listeners_path, None)
    if ok:
        if loaded_listeners is not None:
            state.listeners = loaded_listeners
            log.debug(f"Loaded state.listeners: {list(state.listeners)}")
        else:
            state.listeners = {'scf': {'enabled': False, 'request_types': '6632,6634',
                                 'interval': 10, 'last_check': None}}
            save_listeners()
            log.warning(f"Listeners file not found, created default: {listeners_path}")

    if state.load_failed:
        log.error(
            f"Startup completed with unreadable stores: {sorted(state.load_failed)}. "
            f"Writes to these are disabled until the files are fixed and TaskHome restarts.")
    log.debug("Exiting load_data")


def _save_json_file(name, path, data):
    """Write JSON atomically, refusing to clobber a store that failed to load.

    Non-atomic whole-file rewrites were losing data on interruption: the file
    is truncated the moment it's opened for writing, so a crash or a kill
    between truncate and flush leaves nothing (P0-5). Write to a temp file in
    the same directory, fsync, then rename — rename is atomic, so a reader
    sees either the old file or the new one, never a partial one.
    """
    if name in state.load_failed:
        log.error(
            f"Refusing to save {name}: it failed to load, so writing "
            f"would destroy recoverable data. Fix {path} and restart.")
        return False

    if name in DB_STORES and use_db():
        from . import db
        key = _db_key(name)
        try:
            with state.STATE_LOCK:
                # Snapshot inside the lock for the same reason the JSON path
                # serialises inside it: a concurrent append to the live list
                # would otherwise be read half-written.
                snapshot = list(data) if isinstance(data, list) else dict(data)
            if key == 'tasks':
                return db.set_tasks(snapshot)
            if key == 'history':
                return db.set_history(snapshot)
            return db.set_kv(key, snapshot)
        except Exception as e:
            log.error(f"Failed to save {name} to the database: {e}")
            return False
    # Snapshot what is about to be replaced, before replacing it.
    backup_store(name, path)

    directory = os.path.dirname(os.path.abspath(path)) or '.'
    tmp_path = None
    try:
        with state.STATE_LOCK:
            # Serialise inside the lock: json.dump iterates the live list, and
            # a concurrent append would raise or emit a torn file.
            payload = json.dumps(data, indent=2)
        fd, tmp_path = tempfile.mkstemp(prefix=f'.{name}-', suffix='.tmp', dir=directory)
        with os.fdopen(fd, 'w') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        return True
    except OSError as e:
        log.error(f"Failed to save {name} to {path}: {e}")
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return False


def save_config():
    return _save_json_file('config', constants.CONFIG_FILE, state.config)


def save_tasks():
    return _save_json_file('tasks', constants.TASKS_FILE, state.tasks)


def save_history():
    return _save_json_file('history', constants.HISTORY_FILE, state.history)


def save_listeners():
    return _save_json_file('listeners', constants.LISTENERS_FILE, state.listeners)


# --- backups (P6-2) -----------------------------------------------------------

def backup_dir(name):
    return os.path.join(constants.DATA_DIR, constants.BACKUP_DIRNAME, name)


def backup_keep():
    """How many snapshots to retain per store."""
    raw = (state.config.get('backups') or {}).get('keep', constants.DEFAULT_BACKUP_KEEP)
    try:
        keep = int(raw)
    except (TypeError, ValueError):
        return constants.DEFAULT_BACKUP_KEEP
    return max(keep, 1)


def backups_enabled():
    backups = state.config.get('backups')
    if isinstance(backups, dict) and 'enabled' in backups:
        return bool(backups['enabled'])
    return True


def list_backups(name):
    """Snapshots for a store, newest first."""
    directory = backup_dir(name)
    try:
        entries = [e for e in os.listdir(directory) if e.endswith('.json')]
    except OSError:
        return []
    return sorted(entries, reverse=True)


def backup_store(name, path):
    """Snapshot the file at `path` before it is overwritten.

    Deliberately a *pre-image*: the copy is of what is currently on disk, so
    the previous good state survives a bad write. Backing up after writing
    would only ever preserve the new content, which is useless for recovery.

    Skipped when the content is unchanged since the last snapshot -- the
    scheduler rewrites tasks.json whenever anything moves, and keeping twenty
    identical copies would push the real history out of the retention window.

    Never raises: a backup failure must not prevent the save it precedes.
    """
    if not backups_enabled() or not os.path.exists(path):
        return None
    try:
        with open(path, 'rb') as f:
            current = f.read()
        if not current:
            return None

        directory = backup_dir(name)
        existing = list_backups(name)
        if existing:
            try:
                with open(os.path.join(directory, existing[0]), 'rb') as f:
                    if f.read() == current:
                        return None      # unchanged since the last snapshot
            except OSError:
                pass

        os.makedirs(directory, exist_ok=True)
        # Microsecond precision, because names are sorted lexicographically to
        # order them. At second resolution two saves in the same second
        # collided, and the disambiguating suffix ('...Z-1.json') sorted
        # *before* the plain name -- so "newest" was actually the oldest, which
        # broke both the unchanged-content check and pruning.
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        target = os.path.join(directory, f'{stamp}.json')
        suffix = 1
        while os.path.exists(target):
            target = os.path.join(directory, f'{stamp}_{suffix:03d}.json')
            suffix += 1
        with open(target, 'wb') as f:
            f.write(current)
            f.flush()
            os.fsync(f.fileno())

        prune_backups(name)
        return target
    except OSError as e:
        log.warning(f"Could not back up {name}: {e}")
        return None


def prune_backups(name):
    """Keep only the newest `backup_keep()` snapshots."""
    directory = backup_dir(name)
    stale = list_backups(name)[backup_keep():]
    for entry in stale:
        try:
            os.unlink(os.path.join(directory, entry))
        except OSError as e:
            log.debug(f"Could not prune backup {entry}: {e}")
