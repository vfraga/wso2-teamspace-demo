"""Shared RS256 JWT verification primitives.

`api/auth.py` and `agent/mcp_server.py` both verify WSO2 IS tokens, and the new
M2M service-token path is a third caller. The *caching* policies around JWKS
differ on purpose — the MCP server falls back to a stale copy so an IS blip
can't kill an in-flight tool call, while the Business API fails closed — so
only the security-critical middle is shared here:

    kid -> signing key   ->   decode with the algorithm, audience and issuer pinned

Keeping that in one place means a third caller cannot quietly acquire weaker
verification than the first two.
"""

from typing import Any

import jwt

# RS256 only. Never widen this, and never derive it from the token header:
# accepting `alg` from the token is how JWT verification gets bypassed.
ALLOWED_ALGORITHMS = ["RS256"]


class SigningKeyNotFound(Exception):
    """No JWKS entry matched the token's `kid`."""


def select_signing_key(jwks: dict[str, Any], token: str) -> Any:
    """Return the JWKS key matching the token's `kid` header.

    Raises `SigningKeyNotFound` if there is no match — callers map that onto
    whatever their transport needs (a 401, a RuntimeError).
    """
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    for entry in jwks.get("keys", []):
        if entry.get("kid") == kid:
            return jwt.get_algorithm_by_name("RS256").from_jwk(entry)
    raise SigningKeyNotFound(f"Signing key not found for kid={kid}")


def decode_rs256(token: str, key: Any, *, audience: str, issuer: str) -> dict[str, Any]:
    """Verify and decode a token with signature, audience, issuer and expiry enforced.

    Propagates PyJWT's exceptions (`ExpiredSignatureError`,
    `InvalidTokenError`, ...) so each caller keeps its own error mapping.
    """
    return jwt.decode(
        token,
        key=key,
        algorithms=ALLOWED_ALGORITHMS,
        options={"verify_aud": True},
        audience=audience,
        issuer=issuer,
    )


def token_issuer(is_base_url: str, tenant_path: str) -> str:
    """The `iss` WSO2 IS stamps on tokens for a given base URL and tenant."""
    return f"{is_base_url}{tenant_path}/oauth2/token"


def jwks_url(is_base_url: str, tenant_path: str) -> str:
    """The JWKS endpoint for a given base URL and tenant."""
    return f"{is_base_url}{tenant_path}/oauth2/jwks"
