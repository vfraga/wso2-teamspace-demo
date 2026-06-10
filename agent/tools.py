import json
import logging
from typing import Any

import httpx  # noqa: F401  # re-exported so tests can patch httpx.AsyncClient via agent.tools.httpx
from google.genai import types

from agent.agent_config_cache import fetch_and_cache_agent_config
from agent.auth_manager import AuthManager
from agent.state_manager import StateManager
from agent.tool_schemas import make_meeting_schema

logger = logging.getLogger(__name__)

TOOL_DEFINITIONS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="schedule_meeting_preview",
                description="Show a meeting preview to the user and request authorization to book on their behalf",
                parameters=make_meeting_schema(variant="genai_schedule_preview"),
            ),
            types.FunctionDeclaration(
                name="schedule_meeting",
                description="Finalize and create the meeting after user has authorized",
                parameters=make_meeting_schema(brief=True, variant="genai_schedule_preview"),
            ),
            types.FunctionDeclaration(
                name="list_meetings",
                description="List all scheduled meetings for the organization. If not authorized, this will initiate authorization.",
                parameters={
                    "type": "object",
                    "properties": {},
                },
            ),
            types.FunctionDeclaration(
                name="delete_meeting_preview",
                description="Show a preview of the meeting deletion and request authorization to delete it on their behalf",
                parameters={
                    "type": "object",
                    "properties": {
                        "meeting_id": {"type": "string", "description": "ID of the meeting to delete"},
                        "topic": {"type": "string", "description": "Topic of the meeting being deleted (for confirmation)"},
                    },
                    "required": ["meeting_id", "topic"],
                },
            ),
            types.FunctionDeclaration(
                name="delete_meeting",
                description="Finalize and delete the meeting after user has authorized",
                parameters={
                    "type": "object",
                    "properties": {
                        "meeting_id": {"type": "string"},
                    },
                    "required": ["meeting_id"],
                },
            ),
            types.FunctionDeclaration(
                name="update_meeting_preview",
                description="Show a preview of the meeting changes and request authorization to update the meeting on their behalf",
                parameters=make_meeting_schema(with_meeting_id=True, variant="genai_update_preview"),
            ),
            types.FunctionDeclaration(
                name="update_meeting",
                description="Finalize and update the meeting after user has authorized",
                parameters=make_meeting_schema(brief=True, with_meeting_id=True, variant="genai_update_preview"),
            ),
        ]
    )
]


async def dispatch_tool(name: str, args: dict[str, Any], thread_id: str) -> dict[str, Any]:
    logger.info("MCP Client: dispatching tool '%s' for thread=%s", name, thread_id)
    logger.debug("Tool args: %s", args)

    auth_mgr = AuthManager.get_instance()
    state_mgr = StateManager.get_instance()

    # 1. Determine which token to send: OBO token if available, otherwise Agent credentials token
    token = auth_mgr.get_obo_token(thread_id)
    if not token:
        agent_id, agent_secret = state_mgr.get_agent_credentials(thread_id)
        if not (agent_id and agent_secret):
            org_name = state_mgr.get_org_name(thread_id)
            if org_name:
                logger.debug(
                    "Agent credentials missing for thread=%s; fetching via M2M for org=%s",
                    thread_id, org_name,
                )
                cached = await fetch_and_cache_agent_config(org_name)
                if cached:
                    new_id = cached.get("agent_id", "")
                    new_secret = cached.get("agent_secret", "")
                    if new_id and new_secret:
                        state_mgr.set_agent_credentials(thread_id, new_id, new_secret)
                        agent_id, agent_secret = new_id, new_secret
                        logger.info(
                            "Populated agent credentials from M2M cache for thread=%s, org=%s",
                            thread_id, org_name,
                        )
        try:
            logger.debug("No OBO token found, fetching agent credentials token for thread=%s", thread_id)
            token = await auth_mgr.fetch_agent_token(agent_id, agent_secret, thread_id=thread_id)
        except Exception:
            logger.exception("Failed to fetch agent token for MCP call")
    if not token:
        logger.error("No token available for thread=%s — refusing to dispatch tool '%s'", thread_id, name)
        return {"status": "error", "message": "Agent authentication failed"}

    # 2. Package parameters
    mcp_args = dict(args)
    mcp_args["thread_id"] = thread_id

    # 3. Local in-process MCP Server invocation using contextvars
    from agent.mcp_server import mcp_token_ctx, call_tool as mcp_call_tool

    logger.info("MCP Client: Invoking MCP Server tool '%s' programmatically", name)
    ctx_token = mcp_token_ctx.set(token)
    try:
        content_list = await mcp_call_tool(name, mcp_args)
    finally:
        mcp_token_ctx.reset(ctx_token)

    # 4. Parse the TextContent response
    if not content_list:
        return {"status": "success", "message": "No content returned"}

    text_data = content_list[0].text
    try:
        if text_data.strip().startswith("{"):
            return json.loads(text_data)
        elif text_data.startswith("Error:"):
            return {"status": "error", "message": text_data}
        else:
            return {"status": "success", "message": text_data}
    except Exception as e:
        logger.exception("Failed to parse MCP response")
        return {"status": "error", "message": f"Failed to parse response: {e}"}
