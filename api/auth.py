import json
import logging
import threading
import time
from typing import Any, Optional

import httpx
import jwt
from fastapi import HTTPException, Header, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

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

    logger.debug("Fetching JWKS from %s%s/oauth2/jwks", is_base_url, tenant_path)
    try:
        resp = httpx.get(
            f"{is_base_url}{tenant_path}/oauth2/jwks",
            verify=settings.IS_VERIFY_TLS,
            timeout=5,
        )
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
        header = jwt.get_unverified_header(token)
        key = None
        for k in jwks.get("keys", []):
            if k["kid"] == header["kid"]:
                key = jwt.get_algorithm_by_name("RS256").from_jwk(k)
                break
        if not key:
            logger.warning("JWT signing key not found for kid=%s", header.get("kid"))
            raise HTTPException(status_code=401, detail="Signing key not found")

        decoded = jwt.decode(
            token,
            key=key,
            algorithms=["RS256"],
            options={"verify_aud": True},
            audience=settings.CLIENT_ID,
            issuer=f"{settings.IS_BASE_URL}{settings.TENANT_PATH}/oauth2/token",
        )
        logger.debug("Decoded JWT: %s", json.dumps(decoded, indent=2))
        user = UserInfo(decoded)
        logger.info(
            "Authenticated: sub=%s, org=%s, scope=%s",
            user.user_id,
            user.org,
            user.scopes,
        )
        logger.debug(
            "JWT auth detail: sub=%s, org=%s, aut=%s, scopes=%s",
            user.user_id, user.org, user.auth_type, user.scopes,
        )
        return user
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


def require_internal_secret(
    x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret"),
) -> None:
    """Authenticate a service-to-service call via a shared secret.

    The webapp and the agent service both know `settings.INTERNAL_SECRET`
    (from the deployment's env). When calling an endpoint that opts in
    to M2M auth, the caller presents the secret in the `X-Internal-Secret`
    header. We fail closed: if the secret is unset on either side, or if
    the header doesn't match, the call is rejected.
    """
    if not settings.INTERNAL_SECRET:
        logger.warning("INTERNAL_SECRET is not set on the API; rejecting M2M call")
        raise HTTPException(status_code=503, detail="Internal auth not configured")
    if not x_internal_secret or x_internal_secret != settings.INTERNAL_SECRET:
        logger.warning("X-Internal-Secret header missing or invalid on M2M call")
        raise HTTPException(status_code=401, detail="Invalid or missing X-Internal-Secret")
