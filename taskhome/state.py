"""Mutable application state, deliberately in one module.

Everything else refers to these as `state.tasks`, `state.config` and so on,
never `from .state import tasks`. A direct import binds the object that existed
at import time, so a later reassignment -- by load_data, or by a test -- would
be invisible to the importer. Going through the module always reads the
current value.

That indirection is also what keeps the code testable: a fixture can point
`state.tasks` at a fresh list and every module follows.
"""
import threading

from .constants import DEFAULT_CONFIG

config = dict(DEFAULT_CONFIG)
tasks = []
history = []
listeners = {}

# Guards structural mutation of the shared state and its serialisation.
# Deliberately NOT held across printing or HTTP fetches: those take seconds and
# would stall every page load. Reentrant because save_* is called from inside
# already-locked sections (record_history does exactly this).
STATE_LOCK = threading.RLock()

# Stores whose load failed. Saving one of these would overwrite a file that is
# damaged but still recoverable by hand, turning a fixable problem into
# permanent data loss (P0-5).
load_failed = set()
