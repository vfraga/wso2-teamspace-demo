"""Shared configuration defaults and one-shot env loader.

Centralises duplicated default values and the ``load_dotenv()`` call so the
three service config modules (webapp, api, agent) read from a single source
of truth while still honouring per-service env var names and class shapes.
"""

from dotenv import load_dotenv

_ENV_LOADED = False


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
