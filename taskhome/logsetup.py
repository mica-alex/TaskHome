"""Logging setup (P1-5).

A single named logger for the whole package, so modules that have nothing to
do with the web layer do not need a Flask app object just to log.
"""
import logging
import os
from logging.handlers import RotatingFileHandler

from . import constants, state

log = logging.getLogger('taskhome')

LEVELS = ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')


def configure_logging(level=None, log_dir=None):
    """Set a sane level, a rotating file, and no duplicate handlers.

    The level was previously hardcoded to DEBUG with no file handler, which
    meant every recurrence step printed a line -- hundreds when catching up a
    year-old task -- and none of it survived a restart. The volume was not just
    untidy: it buried the output of tooling that imports this package.

    Level resolves TASKHOME_LOG_LEVEL > config['log_level'] > INFO.
    """
    level = (level
             or os.environ.get('TASKHOME_LOG_LEVEL')
             or state.config.get('log_level')
             or 'INFO')
    level = str(level).upper()
    if level not in LEVELS:
        level = 'INFO'

    log_dir = (log_dir or os.environ.get('TASKHOME_LOG_DIR')
               or os.path.join(constants.APP_ROOT, 'logs'))
    formatter = logging.Formatter(
        '%(asctime)s %(levelname)-7s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    log.setLevel(level)
    # Do not also hand records to the root logger: Flask/werkzeug install one,
    # which printed every line twice in a different format.
    log.propagate = False
    # Re-running must not stack handlers: the package can be imported twice,
    # and each extra handler would duplicate every line.
    for handler in list(log.handlers):
        if getattr(handler, '_taskhome', False):
            log.removeHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console._taskhome = True
    log.addHandler(console)

    try:
        os.makedirs(log_dir, exist_ok=True)
        file_handler = RotatingFileHandler(
            os.path.join(log_dir, 'taskhome.log'),
            maxBytes=2 * 1024 * 1024, backupCount=5, encoding='utf-8')
        file_handler.setFormatter(formatter)
        file_handler._taskhome = True
        log.addHandler(file_handler)
    except OSError as e:
        # A missing log file must never stop the appliance from running.
        log.warning(f"File logging disabled ({log_dir}): {e}")
    return level
