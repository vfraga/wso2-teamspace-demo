"""The portal's OAuth 2.0 client-credentials token for service-to-service calls.

The webapp is the only service that reads its configuration from
``current_app.config`` rather than a module-level settings object — ``TENANT_PATH``
is computed in the app factory, and the test suite builds several apps in one
process. So the config provider resolves against the live app context on every
call, and the client is shared: its cache is keyed on (token URL, client id,
scope), so two apps with different configuration cannot read each other's token.
"""

import logging
import threading

from flask import current_app

from common.m2m_auth import M2MConfig, ServiceTokenClient

logger = logging.getLogger(__name__)

_client: ServiceTokenClient | None = None
_client_lock = threading.Lock()


def _config() -> M2MConfig:
    cfg = current_app.config
    return M2MConfig(
        is_base_url=cfg.get("IS_BASE_URL", ""),
        tenant_path=cfg.get("TENANT_PATH", ""),
        client_id=cfg.get("CLIENT_ID", ""),
        client_secret=cfg.get("CLIENT_SECRET", ""),
        verify_tls=cfg.get("IS_VERIFY_TLS", False),
    )


def service_token_client() -> ServiceTokenClient:
    """Return the portal's shared service-token client."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = ServiceTokenClient(_config, label="Web Portal")
    return _client


def service_auth_headers(*, force_refresh: bool = False) -> dict[str, str]:
    """Headers authenticating this service to the agent or the Business API.

    Returns an empty dict when no token can be obtained; every call site
    degrades gracefully rather than failing the user's request outright.
    """
    return service_token_client().auth_headers(force_refresh=force_refresh)
