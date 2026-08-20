"""OAuth 2.0 client-credentials authentication for service-to-service calls.

This replaces the previous `X-Internal-Secret` shared secret. That scheme was a
static, long-lived, unscoped symmetric value with no caller identity: anyone who
obtained it gained full trusted-service access, including the ability to start
an OBO flow for an arbitrary user.

What replaces it:

* **Callers** obtain a short-lived token from WSO2 IS with
  ``grant_type=client_credentials`` and the ``internal_service`` scope, and send
  it in the ``X-Service-Authorization`` header.
* **Receivers** verify it like any other WSO2 token — RS256 against JWKS, with
  audience, issuer and expiry enforced — then require the ``internal_service``
  scope.

``Authorization`` is deliberately left for the *end-user* JWT. The Business API's
``GET /agent-config/org/{org_id}`` wants both principals at once: the service as
the trust gate, the user for the audit line. A separate header keeps them
distinct with no precedence rules to get wrong, and the same shape works on the
webapp -> agent hop where no user token is sent at all.

Why ``internal_service`` is sufficient as the gate: it is authorized to the
application with ``NO_POLICY`` (see ``setup_is.py``) and granted to no user
role, so a user-bearing token can never carry it.
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import httpx
import requests

from common.safe_auth_logger import SafeAuthLogger

logger = logging.getLogger(__name__)

#: Scope marking a token as an internal service credential.
SERVICE_SCOPE = "internal_service"

#: Header carrying the service token. Kept separate from ``Authorization``,
#: which continues to carry the end-user JWT.
SERVICE_AUTH_HEADER = "X-Service-Authorization"

#: Refresh this many seconds before expiry, so a token can't die in flight.
_EXPIRY_SKEW_SECONDS = 30

#: Fallback lifetime when the IS response omits ``expires_in``.
_DEFAULT_LIFETIME_SECONDS = 300

_TOKEN_REQUEST_TIMEOUT_SECONDS = 10


class ServiceAuthError(Exception):
    """A presented service token is missing, malformed, or insufficient."""


@dataclass(frozen=True)
class M2MConfig:
    """The settings a service needs to request a client-credentials token.

    Snapshotted per call rather than captured once, because every service's
    settings object is mutable at runtime (tests swap ``IS_BASE_URL`` and
    ``TENANT_PATH``, and the webapp computes ``TENANT_PATH`` in its app factory).
    """

    is_base_url: str
    tenant_path: str
    client_id: str
    client_secret: str
    verify_tls: bool
    scope: str = SERVICE_SCOPE

    @property
    def token_url(self) -> str:
        return f"{self.is_base_url}{self.tenant_path}/oauth2/token"

    @property
    def is_usable(self) -> bool:
        return bool(self.client_id and self.client_secret and self.is_base_url)

    @property
    def cache_key(self) -> tuple:
        return (self.token_url, self.client_id, self.scope)


@dataclass
class _CachedToken:
    value: str
    expires_at: float


class ServiceTokenClient:
    """Acquires and caches a client-credentials token for one calling service.

    Returns ``None`` rather than raising when a token cannot be obtained: every
    existing call site already degrades gracefully when its credential is
    missing, and a demo should log the reason rather than 500.
    """

    def __init__(self, config_provider: Callable[[], M2MConfig], label: str):
        self._config_provider = config_provider
        self._label = label
        self._lock = threading.Lock()
        self._cache: dict[tuple, _CachedToken] = {}

    # -- cache -------------------------------------------------------------

    def _cached(self, config: M2MConfig) -> Optional[str]:
        with self._lock:
            entry = self._cache.get(config.cache_key)
            if entry is not None and time.monotonic() < entry.expires_at:
                return entry.value
            return None

    def _store(self, config: M2MConfig, token: str, expires_in: Any) -> str:
        try:
            lifetime = int(expires_in)
        except (TypeError, ValueError):
            lifetime = _DEFAULT_LIFETIME_SECONDS
        # Never cache past a sane floor, and always refresh before expiry.
        ttl = max(lifetime - _EXPIRY_SKEW_SECONDS, 1)
        with self._lock:
            self._cache[config.cache_key] = _CachedToken(token, time.monotonic() + ttl)
        logger.debug(
            "%s cached a service token for %ss (scope=%s)", self._label, ttl, config.scope
        )
        return token

    def invalidate(self) -> None:
        """Drop every cached token. Used after a downstream 401."""
        with self._lock:
            self._cache.clear()

    # -- request helpers ---------------------------------------------------

    def _form(self, config: M2MConfig) -> dict[str, str]:
        return {
            "grant_type": "client_credentials",
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "scope": config.scope,
        }

    def _handle_payload(self, config: M2MConfig, payload: dict) -> Optional[str]:
        token = payload.get("access_token")
        if not token:
            SafeAuthLogger.log_token_error(
                "client_credentials", payload, prefix=f"{self._label} service-token error"
            )
            return None
        granted = payload.get("scope", "")
        if config.scope not in granted.split():
            # Almost always the NO_POLICY misconfiguration described in
            # setup_is.py: the token is valid but carries no usable scope.
            logger.error(
                "%s obtained a service token WITHOUT the %r scope (granted=%r). The "
                "%r API resource is likely authorized with policyIdentifier=RBAC "
                "instead of NO_POLICY, so application-only tokens get no scopes.",
                self._label, config.scope, granted, config.scope,
            )
            return None
        return self._store(config, token, payload.get("expires_in"))

    def _unusable(self, config: M2MConfig) -> None:
        logger.warning(
            "%s cannot request a service token: CLIENT_ID/CLIENT_SECRET are not "
            "configured, so M2M calls will be skipped", self._label,
        )

    # -- sync (requests) ---------------------------------------------------

    def get_token(self, *, force_refresh: bool = False) -> Optional[str]:
        config = self._config_provider()
        if not config.is_usable:
            self._unusable(config)
            return None
        if not force_refresh:
            cached = self._cached(config)
            if cached is not None:
                return cached
        try:
            resp = requests.post(
                config.token_url,
                data=self._form(config),
                verify=config.verify_tls,
                timeout=_TOKEN_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            logger.warning("%s service-token request failed: %s", self._label, exc)
            return None
        if resp.status_code != 200:
            SafeAuthLogger.log_token_error(
                "client_credentials", resp, prefix=f"{self._label} service-token error"
            )
            return None
        try:
            return self._handle_payload(config, resp.json())
        except ValueError:
            logger.error("%s service-token response was not JSON", self._label)
            return None

    def auth_headers(self, *, force_refresh: bool = False) -> dict[str, str]:
        token = self.get_token(force_refresh=force_refresh)
        return {SERVICE_AUTH_HEADER: f"Bearer {token}"} if token else {}

    # -- async (httpx) -----------------------------------------------------

    async def aget_token(self, *, force_refresh: bool = False) -> Optional[str]:
        config = self._config_provider()
        if not config.is_usable:
            self._unusable(config)
            return None
        if not force_refresh:
            cached = self._cached(config)
            if cached is not None:
                return cached
        try:
            async with httpx.AsyncClient(
                verify=config.verify_tls, timeout=_TOKEN_REQUEST_TIMEOUT_SECONDS
            ) as client:
                resp = await client.post(config.token_url, data=self._form(config))
        except httpx.RequestError as exc:
            logger.warning("%s service-token request failed: %s", self._label, exc)
            return None
        if resp.status_code != 200:
            SafeAuthLogger.log_token_error(
                "client_credentials", resp, prefix=f"{self._label} service-token error"
            )
            return None
        try:
            return self._handle_payload(config, resp.json())
        except ValueError:
            logger.error("%s service-token response was not JSON", self._label)
            return None

    async def aauth_headers(self, *, force_refresh: bool = False) -> dict[str, str]:
        token = await self.aget_token(force_refresh=force_refresh)
        return {SERVICE_AUTH_HEADER: f"Bearer {token}"} if token else {}


# ---------------------------------------------------------------------------
# Receiving side
# ---------------------------------------------------------------------------


def parse_bearer(header_value: Optional[str]) -> Optional[str]:
    """Extract the token from a ``Bearer <token>`` header value.

    Tolerates a bare token so a hand-rolled curl during a demo still works.
    """
    if not header_value:
        return None
    parts = header_value.strip().split(None, 1)
    if not parts:
        return None
    if parts[0].lower() == "bearer":
        # "Bearer <token>", or a bare "Bearer" with nothing after it.
        return parts[1].strip() or None if len(parts) == 2 else None
    return parts[0] if len(parts) == 1 else None


def verify_service_claims(decoded: dict, *, scope: str = SERVICE_SCOPE) -> None:
    """Assert a decoded token is a service credential. Raises `ServiceAuthError`.

    The scope is the hard gate — it is authorized application-only, so no
    user-bearing token can hold it. The ``aut`` claim is checked as
    defence-in-depth but only when present, so a WSO2 version that omits it
    doesn't break every M2M call.
    """
    granted = decoded.get("scope", "")
    scopes = granted.split() if isinstance(granted, str) else list(granted or [])
    if scope not in scopes:
        raise ServiceAuthError(f"Service token is missing the required {scope!r} scope")

    auth_type = decoded.get("aut")
    if auth_type is not None and auth_type != "APPLICATION":
        raise ServiceAuthError(
            f"Token has the {scope!r} scope but aut={auth_type!r}; expected an "
            "application-only (client-credentials) token"
        )


def make_service_auth_dependency(
    *,
    jwks_getter: Callable[[], dict],
    audience_getter: Callable[[], str],
    issuer_getter: Callable[[], str],
    label: str,
    scope: str = SERVICE_SCOPE,
):
    """Build the FastAPI dependency that authenticates inbound M2M calls.

    A factory rather than a plain function because the Business API and the
    agent resolve their JWKS and issuer from different settings objects — and
    because the agent deliberately keeps a stale-tolerant JWKS cache while the
    Business API fails closed. The verification itself is identical, so it
    lives here once instead of in both services.

    The returned callable is usable two ways: as a FastAPI dependency, and
    called directly with a header value (which `api/routers/agent_configs.py`
    does, because that route accepts either a service token or a user JWT).
    """
    # Imported here so `common.m2m_auth` stays importable by non-FastAPI code.
    from fastapi import Header, HTTPException

    from common.jwt_validation import (
        SigningKeyNotFound,
        decode_rs256,
        select_signing_key,
    )
    from common.safe_auth_logger import format_claims

    def require_service_auth(
        service_authorization: Optional[str] = Header(None, alias=SERVICE_AUTH_HEADER),
    ) -> dict:
        import jwt as _jwt  # local, to keep this module's import surface small

        token = parse_bearer(service_authorization)
        if not token:
            logger.warning("%s: %s header missing on M2M call", label, SERVICE_AUTH_HEADER)
            raise HTTPException(
                status_code=401,
                detail=f"Missing {SERVICE_AUTH_HEADER} header with a service token",
            )
        try:
            decoded = decode_rs256(
                token,
                select_signing_key(jwks_getter(), token),
                audience=audience_getter(),
                issuer=issuer_getter(),
            )
            verify_service_claims(decoded, scope=scope)
        except SigningKeyNotFound as exc:
            logger.warning("%s: service token rejected: %s", label, exc)
            raise HTTPException(status_code=401, detail="Signing key not found") from None
        except _jwt.ExpiredSignatureError:
            logger.warning("%s: service token expired", label)
            raise HTTPException(status_code=401, detail="Service token expired") from None
        except _jwt.InvalidTokenError as exc:
            logger.warning("%s: service token validation failed: %s", label, exc)
            raise HTTPException(status_code=401, detail=f"Invalid service token: {exc}") from exc
        except ServiceAuthError as exc:
            logger.warning("%s: service token insufficient: %s", label, exc)
            raise HTTPException(status_code=403, detail=str(exc)) from None

        logger.debug("%s: service token accepted: %s", label, format_claims(decoded))
        return decoded

    return require_service_auth
