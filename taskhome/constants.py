"""Constants and paths.

No mutable state and no imports from the rest of the package, so anything may
import this without a cycle.

APP_ROOT is the repo root -- one level above this file, since the package now
lives in taskhome/.
"""
import os

DEFAULT_HOST = '0.0.0.0'
DEFAULT_PORT = 5000
VID = 0x04b8
PID = 0x0e27
# Where mutable state lives (P1-9). Anchored to the repo root rather than the
# process CWD, so TaskHome no longer has to be started from a particular
# directory to find its own data. TASKHOME_DATA_DIR overrides it -- tests and
# any throwaway run should set it rather than pointing at the real files.
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA_DIR = os.path.join(APP_ROOT, 'data')
DATA_DIR = os.environ.get('TASKHOME_DATA_DIR') or DEFAULT_DATA_DIR

# True when the data directory is the built-in one rather than an override.
# The legacy migration keys off this: see storage.migrate_legacy_data_files.
DATA_DIR_IS_DEFAULT = not os.environ.get('TASKHOME_DATA_DIR')

STORE_FILENAMES = {
    'config': 'config.json',
    'tasks': 'tasks.json',
    'history': 'history.json',
    'listeners': 'listeners.json',
}


def data_path(filename):
    return os.path.join(DATA_DIR, filename)


CONFIG_FILE = data_path('config.json')
TASKS_FILE = data_path('tasks.json')
HISTORY_FILE = data_path('history.json')
LISTENERS_FILE = data_path('listeners.json')  # New file for listener configs

# Catch-up policy (P1-10): what happens to occurrences that came due while
# TaskHome was down. Resolution order is per-task 'catchup', then
# oneoff_policy for one-offs, then policy. See docs/scheduling.md.
RECURRENCE_MODES = ('none', 'daily', 'weekly', 'monthly', 'every_weekday',
                    'first_day_month', 'custom')
THEMES = ('system', 'light', 'dark')

CATCHUP_POLICIES = ('skip', 'print_once', 'print_all', 'print_if_recent')
DEFAULT_CATCHUP = {
    'policy': 'skip',              # recurring: losing one of many occurrences
    'oneoff_policy': 'print_once',  # one-off: skipping means it NEVER prints
    'recent_window_hours': 6,
    'max_prints': 20,
}

PRINTER_MANUFACTURER = 'Epson'
PRINTER_MODEL = 'TM-T20III'
PRINTER_CONNECTION = 'USB'

DEFAULT_CONFIG = {'max_history': 500, 'hostname': 'localhost', 'theme': 'system'}



