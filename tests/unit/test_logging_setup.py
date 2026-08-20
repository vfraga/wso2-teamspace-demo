"""Tests for the shared log-level resolution.

Teamspace is a demo whose value depends on verbose output, so DEBUG-by-default
is a deliberate product decision rather than an oversight. These tests pin that
default alongside the production override, so a future change to either is a
conscious one.
"""
import logging

import pytest

from common.logging_setup import configure_logging, is_production, resolve_level


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Isolate env vars *and* global logging state.

    `configure_logging` calls basicConfig(force=True), which replaces the root
    handlers process-wide. Without restoring them, whichever test ran last here
    would dictate the log level for the rest of the suite.
    """
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("FLASK_ENV", raising=False)

    root = logging.getLogger()
    saved_level = root.level
    saved_handlers = root.handlers[:]
    saved_lib_levels = {
        name: logging.getLogger(name).level
        for name in ("httpx", "httpcore", "urllib3", "google", "authlib", "werkzeug")
    }
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    for name, level in saved_lib_levels.items():
        logging.getLogger(name).setLevel(level)


@pytest.mark.parametrize(
    ("log_level", "flask_env", "expected"),
    [
        # Nothing set: the demo default. Developers see the whole workflow.
        (None, None, logging.DEBUG),
        (None, "development", logging.DEBUG),
        # Production opts out of DEBUG without needing LOG_LEVEL.
        (None, "production", logging.INFO),
        (None, "PRODUCTION", logging.INFO),
        # An explicit LOG_LEVEL always wins, in both directions.
        ("WARNING", None, logging.WARNING),
        ("warning", None, logging.WARNING),
        ("DEBUG", "production", logging.DEBUG),
        ("ERROR", "production", logging.ERROR),
        ("critical", None, logging.CRITICAL),
    ],
)
def test_resolve_level_matrix(monkeypatch, log_level, flask_env, expected):
    if log_level is not None:
        monkeypatch.setenv("LOG_LEVEL", log_level)
    if flask_env is not None:
        monkeypatch.setenv("FLASK_ENV", flask_env)
    assert resolve_level() == expected


def test_unrecognised_log_level_falls_back_instead_of_crashing(monkeypatch):
    # A typo in LOG_LEVEL must not stop a service from booting.
    monkeypatch.setenv("LOG_LEVEL", "VERBOSE")
    assert resolve_level() == logging.DEBUG


def test_is_production_only_matches_the_exact_flag(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "production")
    assert is_production() is True
    monkeypatch.setenv("FLASK_ENV", " Production ")
    assert is_production() is True
    monkeypatch.setenv("FLASK_ENV", "prod")
    assert is_production() is False


def test_configure_logging_clamps_noisy_libraries_to_the_root_level(monkeypatch):
    # httpx has a WARNING floor so a DEBUG root doesn't drown in wire logs...
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    assert configure_logging("test") == logging.DEBUG
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("google").level == logging.INFO

    # ...but asking for ERROR must not leave a library *more* verbose than root.
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    assert configure_logging("test") == logging.ERROR
    assert logging.getLogger("httpx").level == logging.ERROR
    assert logging.getLogger("google").level == logging.ERROR


def test_configure_logging_is_repeatable(monkeypatch):
    # The Flask app factory runs per-app; tests build several in one process.
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    assert configure_logging("first") == logging.INFO
    assert configure_logging("second") == logging.INFO
    assert logging.getLogger().level == logging.INFO
