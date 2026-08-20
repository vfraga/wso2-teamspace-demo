"""Shared logging configuration for the three Teamspace services.

Teamspace is a demonstration app: its purpose is to let a developer watch the
identity workflow happen — token exchange, state transitions, claim contents.
So the default level is DEBUG, deliberately. Production deployments are the
exception, not the rule, and they say so explicitly:

    LOG_LEVEL unset + FLASK_ENV != production  ->  DEBUG   (demo default)
    LOG_LEVEL unset + FLASK_ENV == production  ->  INFO
    LOG_LEVEL set                              ->  that level, always

Third-party libraries get their own floors so a DEBUG root doesn't bury the
application's own narration under httpx wire logs. The floors are clamped to
never be *more* verbose than the root level — asking for WARNING gets WARNING
everywhere, not a chatty `google` logger.
"""

import logging
import os
import sys

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%H:%M:%S"

# Noisy third-party loggers and the quietest level we want from them. Listing
# every service's libraries in one place is harmless — setting a level on a
# logger no service imports has no effect.
# Explicit name->level map rather than logging.getLevelNamesMapping(), which
# is Python 3.11+; pyproject.toml declares 3.10 as the floor and CI tests it.
_LEVEL_NAMES = {
    "CRITICAL": logging.CRITICAL,
    "FATAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
    "NOTSET": logging.NOTSET,
}

_LIBRARY_LEVELS = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "urllib3": logging.WARNING,
    "google": logging.INFO,
    "authlib": logging.INFO,
    "werkzeug": logging.INFO,
}


def is_production() -> bool:
    """True when the deployment has explicitly declared itself production."""
    return os.getenv("FLASK_ENV", "").strip().lower() == "production"


def resolve_level() -> int:
    """Resolve the effective root log level from the environment.

    An explicit ``LOG_LEVEL`` always wins. Falling back, production gets INFO
    and everything else — the demo case — gets DEBUG. An unrecognised
    ``LOG_LEVEL`` is ignored rather than fatal: a typo shouldn't stop the
    demo from booting.
    """
    raw = os.getenv("LOG_LEVEL", "").strip().upper()
    if raw:
        level = _LEVEL_NAMES.get(raw)
        if level is not None:
            return level
        logging.getLogger(__name__).warning(
            "Unrecognised LOG_LEVEL=%r; falling back to the environment default", raw
        )
    return logging.INFO if is_production() else logging.DEBUG


def configure_logging(service_name: str) -> int:
    """Configure root logging for a service. Returns the level applied.

    Safe to call more than once (``force=True`` replaces existing handlers),
    which matters for the Flask app factory — tests build several apps in one
    process.
    """
    level = resolve_level()
    logging.basicConfig(
        level=level,
        format=_LOG_FORMAT,
        datefmt=_DATE_FORMAT,
        stream=sys.stdout,
        force=True,
    )
    for name, floor in _LIBRARY_LEVELS.items():
        # Never let a library be more verbose than the root level.
        logging.getLogger(name).setLevel(max(floor, level))

    logging.getLogger(__name__).info(
        "%s logging configured at %s", service_name, logging.getLevelName(level)
    )
    return level
