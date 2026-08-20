import logging
import threading
import time
from typing import Any, Optional

import httpx
import jwt
from fastapi import HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from common.jwt_validation import (
    SigningKeyNotFound,
    decode_rs256,
    jwks_url,
    select_signing_key,
    token_issuer,
)
from common.m2m_auth import make_service_auth_dependency
from common.safe_auth_logger import format_claims
from api.config import settings

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)

_JWKS_TTL_SECONDS = 300


class JWKSCache:
    _data: dict[str, Any] | None = None
    _time: float = 0.0
    _key: tuple[str, str] | None = None
    _lock = threading.Lock()
    TTL_SECONDS = _JWKS_TTL_SECONDS

    @classmethod
    def get(cls, key: tuple[str, str]) -> dict[str, Any] | None:
        with cls._lock:
            if (
                cls._data is not None
                and cls._key == key
                and (time.monotonic() - cls._time) < cls.TTL_SECONDS
            ):
                return cls._data
            return None

    @classmethod
    def set(cls, key: tuple[str, str], data: dict[str, Any]) -> dict[str, Any]:
        with cls._lock:
            cls._data = data
            cls._time = time.monotonic()
            cls._key = key
        return data


def get_jwks(is_base_url: str, tenant_path: str) -> dict[str, Any]:
    """Return WSO2 IS JWKS, cached for ``_JWKS_TTL_SECONDS`` seconds.

    The cache is keyed on the (is_base_url, tenant_path) pair so that tests
    that swap settings don't get stale keys. On any non-2xx response or
    transport error we raise immediately *before* touching the cache, so a
    temporary WSO2 outage cannot poison the cache.
    """
    key = (is_base_url, tenant_path)
    cached = JWKSCache.get(key)
    if cached is not None:
        return cached

    url = jwks_url(is_base_url, tenant_path)
    logger.debug("Fetching JWKS from %s", url)
    try:
        resp = httpx.get(url, verify=settings.IS_VERIFY_TLS, timeout=5)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as e:
        logger.warning("JWKS fetch failed: %s", e)
        raise HTTPException(status_code=503, detail="JWKS unavailable") from e

    logger.info("JWKS fetched, %d keys", len(data.get("keys", [])))
    return JWKSCache.set(key, data)


class UserInfo:
    def __init__(self, decoded: dict):
        self.user_id = decoded.get("sub", "")
        self.org = decoded.get("org_id", "") or decoded.get("user_org", "")

        self.email = decoded.get("email", "")
        self.scopes = decoded.get("scope", "").split()
        self.groups = decoded.get("groups", [])
        act = decoded.get("act", {})
        self.actor_user_id = act.get("sub", "")
        self.auth_type = decoded.get("aut", "APPLICATION_USER")


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(security),
) -> Optional[UserInfo]:
    if credentials is None:
        return None
    token = credentials.credentials
    try:
        jwks = get_jwks(settings.IS_BASE_URL, settings.TENANT_PATH)
        key = select_signing_key(jwks, token)
        decoded = decode_rs256(
            token,
            key,
            audience=settings.CLIENT_ID,
            issuer=token_issuer(settings.IS_BASE_URL, settings.TENANT_PATH),
        )
        user = UserInfo(decoded)
        # One masked summary instead of the previous full-payload dump plus a
        # near-duplicate INFO line. Everything that makes the identity flow
        # legible stays; the raw payload and the plaintext email do not.
        logger.debug("Authenticated JWT claims: %s", format_claims(decoded))
        return user
    except SigningKeyNotFound as e:
        logger.warning("JWT rejected: %s", e)
        raise HTTPException(status_code=401, detail="Signing key not found") from None
    except jwt.ExpiredSignatureError:
        logger.warning("JWT token expired")
        raise HTTPException(status_code=401, detail="Token expired") from None
    except jwt.InvalidTokenError as e:
        logger.warning("JWT validation failed: %s", e)
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}") from e


def require_current_user(
    user: Optional[UserInfo] = Security(get_current_user),
) -> UserInfo:
    """Require a valid authenticated user.

    Use this instead of `get_current_user` directly when the route MUST
    have a user context (vs. optionally accepting M2M auth).
    """
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required: provide Authorization Bearer token",
        )
    return user


def require_scope(scope: str):
    def checker(user: UserInfo = Security(get_current_user)):
        agent_scope = f"{scope}_agent"
        if scope not in user.scopes and agent_scope not in user.scopes:
            raise HTTPException(
                status_code=403, detail=f"Missing required scope: {scope} or {agent_scope}"
            )
        return user
    return checker


# Inbound M2M authentication. Built from the shared factory so this and the
# agent's equivalent cannot drift apart; the Business API supplies its own
# fail-closed JWKS getter (`get_jwks` raises 503 rather than serving stale keys).
require_service_auth = make_service_auth_dependency(
    jwks_getter=lambda: get_jwks(settings.IS_BASE_URL, settings.TENANT_PATH),
    audience_getter=lambda: settings.CLIENT_ID,
    issuer_getter=lambda: token_issuer(settings.IS_BASE_URL, settings.TENANT_PATH),
    label="Business API",
)
