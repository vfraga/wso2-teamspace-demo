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
