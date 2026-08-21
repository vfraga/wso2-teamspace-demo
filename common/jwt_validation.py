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

import logging
import os
from typing import Any

import jwt

logger = logging.getLogger(__name__)

# RS256 only. Never widen this, and never derive it from the token header:
# accepting `alg` from the token is how JWT verification gets bypassed.
ALLOWED_ALGORITHMS = ["RS256"]

#: Tolerance for clock differences between WSO2 IS and the verifying service,
#: applied to `exp`, `nbf` and `iat`.
#:
#: Not optional in practice. IS and the services are separate machines whenever
#: the stack runs in containers (a Docker VM's clock drifts against the host,
#: especially after the host sleeps), and a token issued a fraction of a second
#: ahead of the verifier's clock fails `iat` validation with "The token is not
#: yet valid". That surfaced as intermittent 401s on the M2M and OBO paths
#: while the very same login worked seconds later.
#:
#: 60s matches Authlib's own default for id_token validation (120s) in spirit
#: while staying well inside a 3600s token lifetime. It shortens no expiry
#: check by more than a minute.
DEFAULT_CLOCK_SKEW_SECONDS = 60


def _resolve_leeway() -> int:
    raw = os.getenv("JWT_CLOCK_SKEW_SECONDS", "").strip()
    if not raw:
        return DEFAULT_CLOCK_SKEW_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Unrecognised JWT_CLOCK_SKEW_SECONDS=%r; using %ds",
            raw, DEFAULT_CLOCK_SKEW_SECONDS,
        )
        return DEFAULT_CLOCK_SKEW_SECONDS
    if value < 0:
        logger.warning("JWT_CLOCK_SKEW_SECONDS must not be negative; using 0")
        return 0
    return value


#: Resolved once at import, like every other setting in this codebase.
CLOCK_SKEW_SECONDS = _resolve_leeway()


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


def decode_rs256(
    token: str,
    key: Any,
    *,
    audience: str,
    issuer: str,
    leeway: int | None = None,
) -> dict[str, Any]:
    """Verify and decode a token with signature, audience, issuer and expiry enforced.

    `leeway` absorbs clock skew between the issuer and this service on `exp`,
    `nbf` and `iat`; it defaults to `CLOCK_SKEW_SECONDS`. Signature, audience
    and issuer are unaffected — only the time-based claims get the tolerance.

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
        leeway=CLOCK_SKEW_SECONDS if leeway is None else leeway,
    )


def token_issuer(is_base_url: str, tenant_path: str) -> str:
    """The `iss` WSO2 IS stamps on tokens for a given base URL and tenant."""
    return f"{is_base_url}{tenant_path}/oauth2/token"


def jwks_url(is_base_url: str, tenant_path: str) -> str:
    """The JWKS endpoint for a given base URL and tenant."""
    return f"{is_base_url}{tenant_path}/oauth2/jwks"
