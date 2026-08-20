"""Shared helpers for minting WSO2-IS-shaped JWTs in tests.

Promoted out of `tests/unit/test_jwt_validation.py` when the M2M cutover made
every suite need to sign a token: the Business API and the agent both verify
inbound service tokens against JWKS, so tests must produce real RS256 tokens
rather than a magic string.

The RSA keypair is generated once per session — 2048-bit keygen is slow enough
that doing it per test noticeably drags the suite.
"""

import base64
import time
from functools import lru_cache
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from common.m2m_auth import SERVICE_AUTH_HEADER, SERVICE_SCOPE

KID = "test-key-1"


def _b64uint(value: int) -> str:
    byte_length = (value.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(value.to_bytes(byte_length, "big")).rstrip(b"=").decode()


@lru_cache(maxsize=1)
def keypair() -> tuple[bytes, dict]:
    """Return (private_pem, jwk) for the session's signing key."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private_key.public_key().public_numbers()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    jwk = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": KID,
        "n": _b64uint(numbers.n),
        "e": _b64uint(numbers.e),
    }
    return private_pem, jwk


def jwks() -> dict:
    """A JWKS document containing only the session's signing key."""
    _, jwk = keypair()
    return {"keys": [jwk]}


def sign(payload: dict, *, kid: str = KID, private_pem: bytes | None = None) -> str:
    if private_pem is None:
        private_pem, _ = keypair()
    return jwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": kid})


def issuer_for(is_base_url: str, tenant_path: str = "") -> str:
    return f"{is_base_url}{tenant_path}/oauth2/token"


def service_token(
    *,
    audience: str,
    issuer: str,
    scope: str = SERVICE_SCOPE,
    aut: str | None = "APPLICATION",
    subject: str = "service-client",
    expires_in: int = 3600,
    extra: dict | None = None,
) -> str:
    """Mint a client-credentials-shaped token.

    Defaults match what WSO2 IS issues for `grant_type=client_credentials`
    against the Teamspace application: no user claims, an `aut` of APPLICATION,
    and the service scope.
    """
    now = int(time.time())
    payload = {
        "sub": subject,
        "aud": audience,
        "iss": issuer,
        "iat": now,
        "exp": now + expires_in,
        "scope": scope,
    }
    if aut is not None:
        payload["aut"] = aut
    if extra:
        payload.update(extra)
    return sign(payload)


def service_auth_header(**kwargs) -> dict[str, str]:
    """The `X-Service-Authorization` header carrying a fresh service token."""
    return {SERVICE_AUTH_HEADER: f"Bearer {service_token(**kwargs)}"}


def user_token(
    *,
    audience: str,
    issuer: str,
    scope: str = "openid email create_meeting",
    subject: str = "user-1",
    org: str = "org-1",
    expires_in: int = 3600,
    extra: dict | None = None,
) -> str:
    """Mint an end-user token (`aut=APPLICATION_USER`)."""
    now = int(time.time())
    payload = {
        "sub": subject,
        "aud": audience,
        "iss": issuer,
        "iat": now,
        "exp": now + expires_in,
        "scope": scope,
        "user_org": org,
        "aut": "APPLICATION_USER",
    }
    if extra:
        payload.update(extra)
    return sign(payload)


def patch_api_jwks():
    """Patch the Business API's JWKS fetch to serve the test key."""
    return patch("api.auth.get_jwks", return_value=jwks())


def patch_agent_jwks():
    """Patch the agent's JWKS fetch to serve the test key."""
    return patch("agent.mcp_server.get_jwks", return_value=jwks())
