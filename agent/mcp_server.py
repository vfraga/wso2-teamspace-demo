import contextvars
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import jwt
from mcp.server import Server
from mcp.types import Tool, TextContent

from agent.auth_manager import AuthManager
from agent.state_manager import StateManager, FlowState
from agent.config import settings
from agent.tool_schemas import make_meeting_schema
from common.jwt_validation import CLOCK_SKEW_SECONDS

# SECURITY: The SSE/messages endpoints in agent/main.py accept the MCP
# bearer token via `?token=` because browser EventSource cannot set custom
# Authorization headers. Any request/URL logging touching those endpoints
# MUST redact `token` and `access_token` query params or the OBO bearer
# will leak into access logs.

logger = logging.getLogger(__name__)

mcp_server = Server("Meeting Agent")

mcp_token_ctx = contextvars.ContextVar("mcp_token_ctx")

_JWKS_TTL_SECONDS = 300
_JWKS_FETCH_TIMEOUT_SECONDS = 5

_BUSINESS_API_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)


class JWKSCache:
    _data: dict[str, Any] | None = None
    _time: float = 0.0
    _lock = threading.Lock()
    TTL_SECONDS = _JWKS_TTL_SECONDS

    @classmethod
    def get_fresh(cls) -> dict[str, Any] | None:
        with cls._lock:
            if cls._data is not None and (time.time() - cls._time) < cls.TTL_SECONDS:
                return cls._data
            return None

    @classmethod
    def get_stale_snapshot(cls) -> tuple[dict[str, Any] | None, float]:
        with cls._lock:
            return cls._data, cls._time

    @classmethod
    def set(cls, data: dict[str, Any]) -> dict[str, Any]:
        with cls._lock:
            cls._data = data
            cls._time = time.time()
        return data


def get_jwks() -> dict[str, Any]:
    """Fetch JWKS keys from WSO2 IS with a 300s TTL cache.

    On fetch failure returns the previously cached JWKS (if any) rather than
    caching the error response. Raises RuntimeError only when no cached copy
    is available.
    """
    fresh = JWKSCache.get_fresh()
    if fresh is not None:
        return fresh

    url = f"{settings.IS_BASE_URL}{settings.TENANT_PATH}/oauth2/jwks"
    logger.debug("MCP Server fetching JWKS from %s", url)
    try:
        resp = httpx.get(url, verify=settings.IS_VERIFY_TLS, timeout=_JWKS_FETCH_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.exception("MCP Server failed to fetch JWKS from %s", url)
        stale, stale_time = JWKSCache.get_stale_snapshot()
        if stale is not None:
            logger.warning(
                "Returning stale JWKS cache (age=%.0fs) due to fetch error",
                time.time() - stale_time,
            )
            return stale
        raise RuntimeError(
            f"Unable to fetch JWKS and no cached copy available: {exc}"
        ) from exc

    JWKSCache.set(data)
    keys = data.get("keys", [])
    logger.info("MCP Server loaded %d JWKS keys", len(keys))
    return data


def validate_mcp_token(token: str) -> dict[str, Any]:
    """Validate RS256 JWT signature, audience, and issuer against WSO2 IS JWKS."""
    jwks = get_jwks()
    header = jwt.get_unverified_header(token)
    key = None
    for k in jwks.get("keys", []):
        if k["kid"] == header["kid"]:
            key = jwt.get_algorithm_by_name("RS256").from_jwk(k)
            break
    if not key:
        raise ValueError(f"Signing key not found for kid={header.get('kid')}")

    # Same clock-skew tolerance as every other verifier in the stack, so an MCP
    # tool call cannot fail on `iat` while the identical token is accepted by
    # the Business API. See common/jwt_validation.CLOCK_SKEW_SECONDS.
    decoded = jwt.decode(
        token,
        key=key,
        algorithms=["RS256"],
        options={"verify_aud": True},
        audience=settings.CLIENT_ID,
        issuer=f"{settings.IS_BASE_URL}{settings.TENANT_PATH}/oauth2/token",
        leeway=CLOCK_SKEW_SECONDS,
    )
    return decoded


def _require_scope(scopes: set[str], base_scope: str) -> bool:
    """Return True if `base_scope` or its `_agent` variant is granted."""
    return base_scope in scopes or f"{base_scope}_agent" in scopes


def _require_agent_scope(scopes: set[str], base_scope: str) -> bool:
    """Return True only if the OBO `<base_scope>_agent` scope is granted.

    Destructive action tools must operate exclusively under delegated
    authority; the plain user scope is not sufficient.
    """
    return f"{base_scope}_agent" in scopes


async def _call_business_api(
    method: str,
    path: str,
    token: str,
    json_body: dict[str, Any] | None = None,
) -> httpx.Response:
    url = f"{settings.BUSINESS_API_URL}{path}"
    headers = {"Authorization": f"Bearer {token}"}
    method_upper = method.upper()
    async with httpx.AsyncClient(verify=settings.IS_VERIFY_TLS, timeout=_BUSINESS_API_TIMEOUT) as client:
        if method_upper == "GET":
            return await client.get(url, headers=headers, follow_redirects=True)
        if method_upper == "POST":
            return await client.post(url, json=json_body, headers=headers, follow_redirects=True)
        if method_upper == "PUT":
            return await client.put(url, json=json_body, headers=headers, follow_redirects=True)
        if method_upper == "DELETE":
            return await client.delete(url, headers=headers, follow_redirects=True)
        raise ValueError(f"Unsupported HTTP method: {method}")


def _finalize_action(state_mgr: StateManager, auth_mgr: AuthManager, thread_id: str) -> None:
    state_mgr.set_state(thread_id, FlowState.INITIAL)
    state_mgr.clear_pending_meeting(thread_id)
    auth_mgr.clear_obo_tokens(thread_id)


def _text(payload: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload))]


def _error(message: str) -> list[TextContent]:
    return [TextContent(type="text", text=message)]


@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    """Expose available meeting tools to any MCP Client."""
    return [
        Tool(
            name="list_meetings",
            description="List scheduled meetings for the B2B organization.",
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string", "description": "Active session thread ID"}
                },
                "required": ["thread_id"]
            }
        ),
        Tool(
            name="schedule_meeting_preview",
            description="Show a meeting preview to the user and request OBO authorization.",
            inputSchema=make_meeting_schema(with_thread_id=True, variant="mcp_schedule_preview")
        ),
        Tool(
            name="schedule_meeting",
            description="Finalize and create a scheduled meeting after delegation consent.",
            inputSchema=make_meeting_schema(brief=True, with_thread_id=True, variant="mcp_schedule_preview")
        ),
        Tool(
            name="update_meeting_preview",
            description="Preview meeting modifications and request delegation authorization.",
            inputSchema=make_meeting_schema(brief=True, with_thread_id=True, with_meeting_id=True, variant="mcp_schedule_preview")
        ),
        Tool(
            name="update_meeting",
            description="Finalize and update an existing meeting after delegation consent.",
            inputSchema=make_meeting_schema(brief=True, with_thread_id=True, with_meeting_id=True, variant="mcp_schedule_preview")
        ),
        Tool(
            name="delete_meeting_preview",
            description="Preview meeting deletion and request authorization.",
            inputSchema={
                "type": "object",
                "properties": {
                    "meeting_id": {"type": "string"},
                    "topic": {"type": "string"},
                    "thread_id": {"type": "string"}
                },
                "required": ["meeting_id", "topic", "thread_id"]
            }
        ),
        Tool(
            name="delete_meeting",
            description="Finalize and delete a meeting after delegation consent.",
            inputSchema={
                "type": "object",
                "properties": {
                    "meeting_id": {"type": "string"},
                    "thread_id": {"type": "string"}
                },
                "required": ["meeting_id", "thread_id"]
            }
        )
    ]


async def _handle_list_meetings(
    *,
    token: str,
    arguments: dict[str, Any],
    scopes: set[str],
    thread_id: str,
    auth_mgr: AuthManager,
    state_mgr: StateManager,
    agent_name: str,
) -> list[TextContent]:
    if not _require_scope(scopes, "list_meetings"):
        return _error("Error: Forbidden. Missing scope: list_meetings_agent")

    obo_token = auth_mgr.get_obo_token(thread_id)
    if not obo_token or token != obo_token:
        logger.debug("MCP Server: No OBO token found for list_meetings, requesting authorization")
        auth_mgr.clear_obo_tokens(thread_id)
        agent_id, _ = state_mgr.get_agent_credentials(thread_id)
        auth_url = auth_mgr.get_obo_authorization_url(
            thread_id=thread_id,
            scopes=["list_meetings_agent"],
            agent_id=agent_id,
            action="list",
        )
        state_mgr.set_state(thread_id, FlowState.LIST_PREVIEW_INITIATED)
        state_mgr.set_auth_url(thread_id, auth_url)
        return _text({
            "status": "preview_ready",
            "authorization_url": auth_url,
            "message": f"Please authorize {agent_name} to view your meetings.",
        })

    resp = await _call_business_api("GET", "/meetings/", token)
    if resp.status_code == 200:
        current_state = state_mgr.get_state(thread_id)
        if current_state in (FlowState.LIST_AUTHORIZED, FlowState.LIST_PREVIEW_INITIATED):
            state_mgr.set_state(thread_id, FlowState.INITIAL)
            auth_mgr.clear_obo_tokens(thread_id)
        return _text({"status": "success", "meetings": resp.json()})

    auth_mgr.clear_obo_tokens(thread_id)
    agent_id, _ = state_mgr.get_agent_credentials(thread_id)
    auth_url = auth_mgr.get_obo_authorization_url(
        thread_id=thread_id,
        scopes=["list_meetings_agent"],
        agent_id=agent_id,
        action="list",
    )
    state_mgr.set_state(thread_id, FlowState.LIST_PREVIEW_INITIATED)
    state_mgr.set_auth_url(thread_id, auth_url)
    return _text({
        "status": "preview_ready",
        "authorization_url": auth_url,
        "message": f"Session expired or unauthorized. Please re-authorize {agent_name} to view your meetings.",
    })


async def _handle_schedule_meeting_preview(
    *,
    token: str,
    arguments: dict[str, Any],
    scopes: set[str],
    thread_id: str,
    auth_mgr: AuthManager,
    state_mgr: StateManager,
    agent_name: str,
) -> list[TextContent]:
    if not _require_scope(scopes, "create_meeting"):
        return _error("Error: Forbidden. Missing required scope: create_meeting_agent")

    agent_id, _ = state_mgr.get_agent_credentials(thread_id)
    auth_url = auth_mgr.get_obo_authorization_url(
        thread_id=thread_id,
        scopes=["create_meeting_agent"],
        agent_id=agent_id,
            action="book",
    )
    state_mgr.set_state(thread_id, FlowState.BOOKING_PREVIEW_INITIATED)

    pending_meeting = {k: v for k, v in arguments.items() if k != "thread_id"}
    state_mgr.set_pending_meeting(thread_id, pending_meeting)
    state_mgr.set_auth_url(thread_id, auth_url)

    logger.debug("Generated OBO preview authorization URL for thread=%s", thread_id)
    return _text({
        "status": "preview_ready",
        "meeting": pending_meeting,
        "authorization_url": auth_url,
        "message": f"Please authorize {agent_name} to book this meeting on your behalf.",
    })


async def _handle_schedule_meeting(
    *,
    token: str,
    arguments: dict[str, Any],
    scopes: set[str],
    thread_id: str,
    auth_mgr: AuthManager,
    state_mgr: StateManager,
    agent_name: str,
) -> list[TextContent]:
    if not _require_agent_scope(scopes, "create_meeting"):
        return _error("Error: Forbidden. Missing scope: create_meeting_agent")

    state = state_mgr.get_state(thread_id)
    obo_token = auth_mgr.get_obo_token(thread_id)
    if state != FlowState.BOOKING_AUTHORIZED or not obo_token or token != obo_token:
        return _text({"status": "waiting_for_authorization"})

    post_data = {k: v for k, v in arguments.items() if k != "thread_id"}
    resp = await _call_business_api("POST", "/meetings/", token, json_body=post_data)
    if resp.status_code == 201:
        _finalize_action(state_mgr, auth_mgr, thread_id)
        return _text({"status": "booked", "meeting": resp.json()})
    return _error(f"Error: {resp.status_code} — {resp.text}")


async def _handle_delete_meeting_preview(
    *,
    token: str,
    arguments: dict[str, Any],
    scopes: set[str],
    thread_id: str,
    auth_mgr: AuthManager,
    state_mgr: StateManager,
    agent_name: str,
) -> list[TextContent]:
    if not _require_scope(scopes, "delete_meeting"):
        return _error("Error: Forbidden. Missing scope: delete_meeting_agent")

    agent_id, _ = state_mgr.get_agent_credentials(thread_id)
    auth_url = auth_mgr.get_obo_authorization_url(
        thread_id=thread_id,
        scopes=["list_meetings_agent", "delete_meeting_agent"],
        agent_id=agent_id,
        action="delete",
    )
    state_mgr.set_state(thread_id, FlowState.DELETE_PREVIEW_INITIATED)
    pending = {k: v for k, v in arguments.items() if k != "thread_id"}
    state_mgr.set_pending_meeting(thread_id, pending)
    state_mgr.set_auth_url(thread_id, auth_url)
    return _text({
        "status": "preview_ready",
        "meeting_id": arguments.get("meeting_id"),
        "topic": arguments.get("topic"),
        "authorization_url": auth_url,
        "message": f"Please authorize {agent_name} to delete the meeting '{arguments.get('topic')}'.",
    })


async def _handle_delete_meeting(
    *,
    token: str,
    arguments: dict[str, Any],
    scopes: set[str],
    thread_id: str,
    auth_mgr: AuthManager,
    state_mgr: StateManager,
    agent_name: str,
) -> list[TextContent]:
    if not _require_agent_scope(scopes, "delete_meeting"):
        return _error("Error: Forbidden. Missing scope: delete_meeting_agent")

    state = state_mgr.get_state(thread_id)
    obo_token = auth_mgr.get_obo_token(thread_id)
    if state != FlowState.DELETE_AUTHORIZED or not obo_token or token != obo_token:
        return _text({"status": "waiting_for_authorization"})

    meeting_id = arguments.get("meeting_id")
    resp = await _call_business_api("DELETE", f"/meetings/{meeting_id}", token)
    if resp.status_code == 204:
        _finalize_action(state_mgr, auth_mgr, thread_id)
        return _text({"status": "deleted", "meeting_id": meeting_id})
    return _error(f"Error: {resp.status_code} — {resp.text}")


async def _handle_update_meeting_preview(
    *,
    token: str,
    arguments: dict[str, Any],
    scopes: set[str],
    thread_id: str,
    auth_mgr: AuthManager,
    state_mgr: StateManager,
    agent_name: str,
) -> list[TextContent]:
    if not _require_scope(scopes, "update_meeting"):
        return _error("Error: Forbidden. Missing scope: update_meeting_agent")

    agent_id, _ = state_mgr.get_agent_credentials(thread_id)
    auth_url = auth_mgr.get_obo_authorization_url(
        thread_id=thread_id,
        scopes=["list_meetings_agent", "update_meeting_agent"],
        agent_id=agent_id,
        action="update",
    )
    state_mgr.set_state(thread_id, FlowState.UPDATE_PREVIEW_INITIATED)
    pending = {k: v for k, v in arguments.items() if k != "thread_id"}
    state_mgr.set_pending_meeting(thread_id, pending)
    state_mgr.set_auth_url(thread_id, auth_url)
    return _text({
        "status": "preview_ready",
        "meeting": pending,
        "authorization_url": auth_url,
        "message": f"Please authorize {agent_name} to update the meeting '{arguments.get('topic')}' on your behalf.",
    })


async def _handle_update_meeting(
    *,
    token: str,
    arguments: dict[str, Any],
    scopes: set[str],
    thread_id: str,
    auth_mgr: AuthManager,
    state_mgr: StateManager,
    agent_name: str,
) -> list[TextContent]:
    if not _require_agent_scope(scopes, "update_meeting"):
        return _error("Error: Forbidden. Missing scope: update_meeting_agent")

    state = state_mgr.get_state(thread_id)
    obo_token = auth_mgr.get_obo_token(thread_id)
    if state != FlowState.UPDATE_AUTHORIZED or not obo_token or token != obo_token:
        return _text({"status": "waiting_for_authorization"})

    meeting_id = arguments.get("meeting_id")
    meeting_data = {
        "topic": arguments.get("topic"),
        "date": arguments.get("date"),
        "start_time": arguments.get("start_time"),
        "duration": arguments.get("duration"),
        "time_zone": arguments.get("time_zone"),
    }
    resp = await _call_business_api(
        "PUT", f"/meetings/{meeting_id}", token, json_body=meeting_data
    )
    if resp.status_code == 200:
        _finalize_action(state_mgr, auth_mgr, thread_id)
        return _text({"status": "updated", "meeting": resp.json()})
    return _error(f"Error: {resp.status_code} — {resp.text}")


_ToolHandler = Callable[..., Awaitable[list[TextContent]]]

_TOOL_HANDLERS: dict[str, _ToolHandler] = {
    "list_meetings": _handle_list_meetings,
    "schedule_meeting_preview": _handle_schedule_meeting_preview,
    "schedule_meeting": _handle_schedule_meeting,
    "delete_meeting_preview": _handle_delete_meeting_preview,
    "delete_meeting": _handle_delete_meeting,
    "update_meeting_preview": _handle_update_meeting_preview,
    "update_meeting": _handle_update_meeting,
}


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Execute tools securely by validating JWT tokens and routing to a handler."""
    logger.debug("MCP Server executing tool '%s'", name)
    token = mcp_token_ctx.get(None)
    if not token:
        logger.warning("MCP execution failed: No bearer token provided")
        return _error("Error: Unauthorized. Missing bearer token.")

    try:
        decoded = validate_mcp_token(token)
        scopes = set(decoded.get("scope", "").split())
    except Exception as e:
        logger.exception("MCP Server JWT validation failed")
        return _error(f"Error: Unauthorized. Invalid token: {e}")

    thread_id = arguments.get("thread_id", "")
    auth_mgr = AuthManager.get_instance()
    state_mgr = StateManager.get_instance()
    agent_name = state_mgr.get_agent_name(thread_id)

    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return _error(f"Error: Unknown tool: {name}")

    return await handler(
        token=token,
        arguments=arguments,
        scopes=scopes,
        thread_id=thread_id,
        auth_mgr=auth_mgr,
        state_mgr=state_mgr,
        agent_name=agent_name,
    )
