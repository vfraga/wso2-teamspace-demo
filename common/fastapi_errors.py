"""Shared FastAPI error handlers used by both the Business API and the AI Agent.

Both services import from common/ to avoid cross-service coupling.
"""

import json
import logging

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.requests import Request

logger = logging.getLogger(__name__)


async def mask_request_body(request: Request) -> str:
    """Return a sanitised copy of the request body suitable for logging."""
    try:
        body_bytes = await request.body()
        body_json = json.loads(body_bytes) if body_bytes else {}
    except Exception as e:
        logger.debug("request body unreadable: %s", e)
        return "<unreadable>"
    masked_body = "<empty>"
    if isinstance(body_json, dict):
        try:

            def mask_dict(d: dict) -> None:
                for k, v in d.items():
                    if isinstance(v, dict):
                        mask_dict(v)
                    elif any(s in k.lower() for s in ["secret", "key", "password", "token"]):
                        d[k] = "[MASKED]"

            mask_dict(body_json)
            masked_body = json.dumps(body_json)
        except Exception as e:
            logger.debug("body mask failed: %s", e)
    return masked_body


async def handle_validation_error(
    request: Request, exc: RequestValidationError, _logger: logging.Logger, app_name: str
) -> JSONResponse:
    masked_body = await mask_request_body(request)
    _logger.error(
        "%s Validation Error! Path: %s, Errors: %s, Masked Body: %s",
        app_name,
        request.url.path,
        exc.errors(),
        masked_body,
    )
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors(), "body": masked_body},
    )


async def handle_global_error(
    request: Request, exc: Exception, _logger: logging.Logger, app_name: str
) -> JSONResponse:
    if isinstance(exc, (HTTPException, RequestValidationError)):
        raise exc
    masked_body = await mask_request_body(request)
    # Log the full traceback + exception message server-side only (2.1.6)
    _logger.exception(
        "Unhandled exception in %s! Path: %s, Query: %s, Masked Body: %s  Detail: %s",
        app_name,
        request.url.path,
        dict(request.query_params),
        masked_body,
        str(exc),
    )
    # Never leak exception details to the client
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error"},
    )
