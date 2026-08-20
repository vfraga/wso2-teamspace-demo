"""Safe logger helpers for OAuth token-related error paths.

Wraps the dangerous `logger.error("...", token_data)` pattern behind a
single, well-named helper so the redaction policy lives in one place.

The redaction policy:
- Token JSON bodies: mask `access_token`, `refresh_token`, `id_token`, `code`,
  `client_secret` values.
- Token response dicts: log only the keys present and the `error` /
  `error_description` fields.
- Never log a raw `access_token` / `refresh_token` / `id_token` / `code` /
  `client_secret` value.
"""
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_TOKEN_FIELD_NAMES = ("access_token", "refresh_token", "id_token", "code", "client_secret")
_REDACT_REGEX = re.compile(
    r'("(?:' + '|'.join(_TOKEN_FIELD_NAMES) + r')"\s*:\s*")[^"]*(")',
)


def redact_token_json(text: str) -> str:
    """Return a copy of `text` with known token fields masked.

    Operates on the raw JSON text. Best-effort: if the text is not JSON or
    doesn't contain quoted token fields, the original text is returned.
    """
    if not text:
        return text
    return _REDACT_REGEX.sub(r"\1[REDACTED]\2", text)


class SafeAuthLogger:
    """Safe logger for OAuth token-error paths.

    The single entry point is `log_token_error`. It accepts either a
    `requests.Response` (for HTTP error paths) or a dict (for parsed-JSON
    error paths) and never logs a raw secret.
    """

    @classmethod
    def log_token_error(
        cls,
        grant_type: str,
        response_or_data: Any,
        *,
        thread_id: str | None = None,
        prefix: str = "Token endpoint error",
    ) -> None:
        # Case 1: requests.Response (HTTP error path)
        status = getattr(response_or_data, "status_code", None)
        if status is not None:
            text = getattr(response_or_data, "text", "") or ""
            logger.error(
                "%s (grant=%s, http=%s, thread=%s): %s",
                prefix, grant_type, status, thread_id or "-",
                redact_token_json(text[:500]),
            )
            return
        # Case 2: parsed dict (missing access_token etc.)
        if isinstance(response_or_data, dict):
            keys = list(response_or_data.keys())
            has_token = any(k in response_or_data for k in _TOKEN_FIELD_NAMES)
            error = response_or_data.get("error") or response_or_data.get("error_description")
            logger.error(
                "%s (grant=%s, has_token=%s, keys=%s, error=%s, thread=%s)",
                prefix, grant_type, has_token, keys, error, thread_id or "-",
            )
            return
        # Case 3: unknown — log the type only
        logger.error(
            "%s (grant=%s, thread=%s, data_type=%s)",
            prefix, grant_type, thread_id or "-", type(response_or_data).__name__,
        )


# ---------------------------------------------------------------------------
# Claim-summary logging
#
# The demo *wants* token claims visible — watching `sub`, `scope` and `act`
# change across a token exchange is the point of the walkthrough. What it does
# not want is `json.dumps(decoded)` of an entire attacker-supplied payload on
# every authenticated call. `format_claims` is the middle ground: the claims
# that teach, with the identifying values shortened and the personal ones
# masked, and everything else reduced to a count.
# ---------------------------------------------------------------------------

# Claims rendered explicitly by `format_claims`; anything else is counted only.
_SUMMARISED_CLAIMS = frozenset({
    "sub", "org_id", "user_org", "email", "scope", "act", "aut",
})


def mask_token(token: str, visible: int = 10) -> str:
    """Shorten a token-shaped value to `visible` leading chars plus its tail.

    Short values are returned unchanged — there is nothing to hide in a value
    too small to be a real credential.
    """
    if not token or len(token) <= visible:
        return token
    return token[:visible] + "..." + token[-4:]


def shorten_id(value: str, visible: int = 8) -> str:
    """Shorten an opaque identifier (a `sub`, an `act.sub`) for correlation.

    Keeps enough of the prefix to match log lines against each other without
    reproducing the whole identifier.
    """
    if not value:
        return "-"
    if len(value) <= visible:
        return value
    return f"{value[:visible]}…"


def mask_email(email: str) -> str:
    """Mask an email address to its first character and domain initial.

    `jane@worklink.com` -> `j***@w***.com`. Enough to tell two users apart in
    a demo log; not enough to harvest.
    """
    if not email or "@" not in email:
        return "-" if not email else "***"
    local, _, domain = email.partition("@")
    domain_head, dot, tld = domain.partition(".")
    masked_domain = f"{domain_head[:1]}***" if domain_head else "***"
    if dot:
        masked_domain = f"{masked_domain}.{tld}"
    return f"{local[:1]}***@{masked_domain}"


def format_claims(decoded: dict) -> str:
    """Render a decoded JWT as a one-line, masked claim summary.

    Shows the claims that make the identity flow legible — subject,
    organisation, auth type, granted scopes, and the RFC 8693 `act` actor —
    and never the raw token, the full payload, or a plaintext email.
    """
    if not isinstance(decoded, dict):
        return "<no claims>"

    org = decoded.get("org_id", "") or decoded.get("user_org", "") or "-"
    scope = decoded.get("scope", "")
    scopes = scope.split() if isinstance(scope, str) else list(scope or [])
    act = decoded.get("act") or {}
    actor = act.get("sub", "") if isinstance(act, dict) else ""

    parts = [
        f"sub={shorten_id(str(decoded.get('sub', '')))}",
        f"org={org}",
        f"aut={decoded.get('aut', '-')}",
        f"scope=[{','.join(scopes)}]",
    ]
    if actor:
        parts.append(f"act.sub={shorten_id(str(actor))}")
    if decoded.get("email"):
        parts.append(f"email={mask_email(str(decoded['email']))}")

    extra = len([k for k in decoded if k not in _SUMMARISED_CLAIMS])
    if extra:
        parts.append(f"(+{extra} more claims)")
    return " ".join(parts)
