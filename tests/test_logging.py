"""Logging configuration (MASTER_PLAN P1-5).

The level was hardcoded to DEBUG with no file handler, so a year-old task
emitted hundreds of lines per catch-up, none of it survived a restart, and the
volume buried the output of any tool importing this module.
"""
import logging
from datetime import datetime

import pytest

import taskhome


@pytest.fixture(autouse=True)
def restore_logger():
    before_level = taskhome.logsetup.log.level
    before_handlers = list(taskhome.logsetup.log.handlers)
    yield
    taskhome.logsetup.log.setLevel(before_level)
    taskhome.logsetup.log.handlers[:] = before_handlers


def test_defaults_to_info_not_debug(tmp_path, monkeypatch):
    monkeypatch.delenv('TASKHOME_LOG_LEVEL', raising=False)
    monkeypatch.setattr(taskhome.state, 'config', dict(taskhome.constants.DEFAULT_CONFIG))
    assert taskhome.logsetup.configure_logging(log_dir=str(tmp_path)) == 'INFO'
    assert taskhome.logsetup.log.level == logging.INFO


def test_env_var_wins(tmp_path, monkeypatch):
    monkeypatch.setenv('TASKHOME_LOG_LEVEL', 'debug')
    assert taskhome.logsetup.configure_logging(log_dir=str(tmp_path)) == 'DEBUG'


def test_config_level_is_used(tmp_path, monkeypatch):
    monkeypatch.delenv('TASKHOME_LOG_LEVEL', raising=False)
    monkeypatch.setattr(taskhome.state, 'config', dict(taskhome.constants.DEFAULT_CONFIG, log_level='WARNING'))
    assert taskhome.logsetup.configure_logging(log_dir=str(tmp_path)) == 'WARNING'


@pytest.mark.parametrize('bad', ['LOUD', '', 42, None])
def test_invalid_level_falls_back(tmp_path, monkeypatch, bad):
    monkeypatch.delenv('TASKHOME_LOG_LEVEL', raising=False)
    monkeypatch.setattr(taskhome.state, 'config', dict(taskhome.constants.DEFAULT_CONFIG, log_level=bad))
    assert taskhome.logsetup.configure_logging(log_dir=str(tmp_path)) == 'INFO'


def test_writes_a_rotating_log_file(tmp_path, monkeypatch):
    monkeypatch.delenv('TASKHOME_LOG_LEVEL', raising=False)
    taskhome.logsetup.configure_logging(level='INFO', log_dir=str(tmp_path))
    taskhome.logsetup.log.info('hello from the test')
    for handler in taskhome.logsetup.log.handlers:
        handler.flush()
    assert (tmp_path / 'taskhome.log').exists()
    assert 'hello from the test' in (tmp_path / 'taskhome.log').read_text()


def test_repeat_configuration_does_not_stack_handlers(tmp_path):
    taskhome.logsetup.configure_logging(level='INFO', log_dir=str(tmp_path))
    first = len([h for h in taskhome.logsetup.log.handlers
                 if getattr(h, '_taskhome', False)])
    for _ in range(3):
        taskhome.logsetup.configure_logging(level='INFO', log_dir=str(tmp_path))
    after = len([h for h in taskhome.logsetup.log.handlers
                 if getattr(h, '_taskhome', False)])
    assert after == first, 'handlers accumulated; each line would log N times'


def test_unwritable_log_dir_does_not_stop_startup(tmp_path, monkeypatch):
    """An appliance must still run when it cannot write a log."""
    monkeypatch.setattr(taskhome.storage.os, 'makedirs',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('read-only')))
    assert taskhome.logsetup.configure_logging(level='INFO', log_dir=str(tmp_path)) == 'INFO'


def test_catchup_does_not_log_per_recurrence_step(tmp_path, caplog, make_task):
    """The flood: one line per step, hundreds of them for an old task."""
    taskhome.logsetup.configure_logging(level='DEBUG', log_dir=str(tmp_path))
    task = make_task('2025-03-05T09:00:00', 'daily')
    # propagate is deliberately False (Flask installs a root handler and every
    # line printed twice), so caplog must be attached to our logger directly.
    taskhome.logsetup.log.addHandler(caplog.handler)
    with caplog.at_level(logging.DEBUG, logger='taskhome'):
        taskhome.recurrence.advance_schedule(task, datetime(2026, 3, 5, 12, 0))

    per_step = [r for r in caplog.records if 'Calculating next time' in r.message]
    assert per_step == [], f'{len(per_step)} per-step lines logged'
    # But the summary is there.
    assert any('Advanced daily schedule over' in r.message for r in caplog.records)
