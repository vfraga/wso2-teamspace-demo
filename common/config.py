"""Shared configuration defaults and one-shot env loader.

Centralises duplicated default values and the ``load_dotenv()`` call so the
three service config modules (webapp, api, agent) read from a single source
of truth while still honouring per-service env var names and class shapes.
"""

import logging
import os
from pathlib import Path
from typing import Union

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_ENV_LOADED = False

#: What `requests` and `httpx` accept for their `verify=` argument: True to
#: verify against the system/certifi trust store, False to skip verification,
#: or a path to a CA bundle to verify against instead.
VerifyTLS = Union[bool, str]

_TRUE_VALUES = ("1", "true", "yes", "on")
_FALSE_VALUES = ("0", "false", "no", "off")


def resolve_verify_tls(raw: str | None, *, label: str, default: bool = True) -> VerifyTLS:
    """Interpret a TLS-verification setting as a bool or a CA-bundle path.

    The demo runs against certificates that no public CA signed — either WSO2's
    self-signed default or the local CA under ``pki/``. Python does not consult
    the macOS keychain (it uses certifi), so "verify against my own CA" cannot
    be expressed as a boolean. Hence three states:

        unset            -> `default`
        true/false/1/0   -> that boolean
        anything else    -> treated as a path to a CA bundle

    A path that does not exist is a configuration error, and silently falling
    back to `False` would turn it into a silent downgrade to no verification.
    We fail closed to `True` instead, so the failure surfaces as a loud TLS
    error rather than an unverified connection.
    """
    if raw is None:
        return default
    value = raw.strip()
    if not value:
        return default
    lowered = value.lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        logger.warning(
            "%s: TLS verification is DISABLED. Expected only for local development "
            "against a self-signed certificate; never set this in production.", label,
        )
        return False
    if not Path(value).is_file():
        logger.error(
            "%s: CA bundle %r does not exist. Falling back to full verification, which "
            "will fail against a privately-signed certificate — fix the path (see pki/).",
            label, value,
        )
        return True
    logger.info("%s: verifying TLS against CA bundle %s", label, value)
    return value


def verify_tls_from_env(name: str, *, label: str, default: bool = True) -> VerifyTLS:
    """`resolve_verify_tls` for an environment variable."""
    return resolve_verify_tls(os.getenv(name), label=label, default=default)


def load_env() -> None:
    """Call :func:`dotenv.load_dotenv` at most once per process.

    Idempotent so it is safe for every service's config module to invoke
    unconditionally at import time.
    """
    global _ENV_LOADED
    if not _ENV_LOADED:
        load_dotenv()
        _ENV_LOADED = True


class CommonDefaults:
    """Single source of truth for default values shared across services.

    Each service reads env vars with one of these constants as the fallback
    so the dev-friendly localhost defaults live in exactly one place.
    """

    IS_BASE_URL = "https://localhost:9443"
    IS_VERIFY_TLS = False
    FLASK_HOST = "localhost"
    FLASK_PORT = 5001
    BUSINESS_API_URL = "http://localhost:9091"
    AGENT_SERVICE_URL = "http://localhost:8000"
    AGENT_REDIRECT_URI = "http://localhost:8000/callback"
    # Base URL for the project's demo image assets on the jsDelivr CDN. The
    # Teamspace logo/favicon defaults below and the setup scripts all derive
    # their asset URLs from this single source.
    CDN_IMG_BASE_URL = (
        "https://cdn.jsdelivr.net/gh/vfraga/wso2-teamspace-demo@main/"
        "webapp/static/img"
    )
    DEFAULT_LOGO_URL = f"{CDN_IMG_BASE_URL}/teamspace-logo.svg"
    DEFAULT_FAVICON_URL = f"{CDN_IMG_BASE_URL}/teamspace-favicon.svg"
