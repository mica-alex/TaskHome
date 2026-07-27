"""Logging configuration (MASTER_PLAN P1-5).

The level was hardcoded to DEBUG with no file handler, so a year-old task
emitted hundreds of lines per catch-up, none of it survived a restart, and the
volume buried the output of any tool importing this module.
"""
import logging

import pytest

import app as taskhome


@pytest.fixture(autouse=True)
def restore_logger():
    before_level = taskhome.app.logger.level
    before_handlers = list(taskhome.app.logger.handlers)
    yield
    taskhome.app.logger.setLevel(before_level)
    taskhome.app.logger.handlers[:] = before_handlers


def test_defaults_to_info_not_debug(tmp_path, monkeypatch):
    monkeypatch.delenv('TASKHOME_LOG_LEVEL', raising=False)
    monkeypatch.setattr(taskhome, 'config', dict(taskhome.DEFAULT_CONFIG))
    assert taskhome.configure_logging(log_dir=str(tmp_path)) == 'INFO'
    assert taskhome.app.logger.level == logging.INFO


def test_env_var_wins(tmp_path, monkeypatch):
    monkeypatch.setenv('TASKHOME_LOG_LEVEL', 'debug')
    assert taskhome.configure_logging(log_dir=str(tmp_path)) == 'DEBUG'


def test_config_level_is_used(tmp_path, monkeypatch):
    monkeypatch.delenv('TASKHOME_LOG_LEVEL', raising=False)
    monkeypatch.setattr(taskhome, 'config', dict(taskhome.DEFAULT_CONFIG, log_level='WARNING'))
    assert taskhome.configure_logging(log_dir=str(tmp_path)) == 'WARNING'


@pytest.mark.parametrize('bad', ['LOUD', '', 42, None])
def test_invalid_level_falls_back(tmp_path, monkeypatch, bad):
    monkeypatch.delenv('TASKHOME_LOG_LEVEL', raising=False)
    monkeypatch.setattr(taskhome, 'config', dict(taskhome.DEFAULT_CONFIG, log_level=bad))
    assert taskhome.configure_logging(log_dir=str(tmp_path)) == 'INFO'


def test_writes_a_rotating_log_file(tmp_path, monkeypatch):
    monkeypatch.delenv('TASKHOME_LOG_LEVEL', raising=False)
    taskhome.configure_logging(level='INFO', log_dir=str(tmp_path))
    taskhome.app.logger.info('hello from the test')
    for handler in taskhome.app.logger.handlers:
        handler.flush()
    assert (tmp_path / 'taskhome.log').exists()
    assert 'hello from the test' in (tmp_path / 'taskhome.log').read_text()


def test_repeat_configuration_does_not_stack_handlers(tmp_path):
    taskhome.configure_logging(level='INFO', log_dir=str(tmp_path))
    first = len([h for h in taskhome.app.logger.handlers
                 if getattr(h, '_taskhome', False)])
    for _ in range(3):
        taskhome.configure_logging(level='INFO', log_dir=str(tmp_path))
    after = len([h for h in taskhome.app.logger.handlers
                 if getattr(h, '_taskhome', False)])
    assert after == first, 'handlers accumulated; each line would log N times'


def test_unwritable_log_dir_does_not_stop_startup(tmp_path, monkeypatch):
    """An appliance must still run when it cannot write a log."""
    monkeypatch.setattr(taskhome.os, 'makedirs',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('read-only')))
    assert taskhome.configure_logging(level='INFO', log_dir=str(tmp_path)) == 'INFO'


def test_catchup_does_not_log_per_recurrence_step(tmp_path, caplog, make_task):
    """The flood: one line per step, hundreds of them for an old task."""
    taskhome.configure_logging(level='DEBUG', log_dir=str(tmp_path))
    task = make_task('2025-03-05T09:00:00', 'daily')
    with caplog.at_level(logging.DEBUG, logger='app'):
        taskhome.advance_schedule(task, taskhome.datetime(2026, 3, 5, 12, 0))

    per_step = [r for r in caplog.records if 'Calculating next time' in r.message]
    assert per_step == [], f'{len(per_step)} per-step lines logged'
    # But the summary is there.
    assert any('Advanced daily schedule over' in r.message for r in caplog.records)
