import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.mcp_server import call_tool, mcp_token_ctx
from agent.tools import dispatch_tool
from agent.tool_schemas import MEETING_BASE_ARGS


@pytest.fixture
def mock_auth_manager():
    mock_instance = MagicMock()
    mock_instance.get_obo_jwt_raw.return_value = "mock-obo-jwt-payload"
    mock_instance.get_agent_jwt_raw.return_value = "mock-agent-jwt-payload"
    mock_instance.fetch_agent_token = AsyncMock(return_value="mock-agent-token-123")
    mock_instance.exchange_obo_code = AsyncMock(return_value="mock-obo-token-123")

    with patch("agent.auth_manager.AuthManager") as mock_class1, \
         patch("agent.main.AuthManager") as mock_class2, \
         patch("agent.tools.AuthManager") as mock_class3, \
         patch("agent.mcp_server.AuthManager") as mock_class4:
        mock_class1.get_instance.return_value = mock_instance
        mock_class2.get_instance.return_value = mock_instance
        mock_class3.get_instance.return_value = mock_instance
        mock_class4.get_instance.return_value = mock_instance
        yield mock_instance


def _run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_concurrent_dispatch_tool_contextvars_isolated(mock_auth_manager):
    mock_auth_manager.get_obo_authorization_url.return_value = "http://mock-auth-url"
    mock_auth_manager.get_obo_token.return_value = None
    mock_auth_manager.fetch_agent_token = AsyncMock(return_value="mock-agent-token")
    mock_auth_manager.exchange_obo_code = AsyncMock(return_value="mock-obo-token")

    captured = {}

    async def fake_call_tool(name, arguments):
        thread_id = arguments.get("thread_id", "")
        token = mcp_token_ctx.get()
        await asyncio.sleep(0.01)
        token_after = mcp_token_ctx.get()
        captured[thread_id] = (token, token_after)
        from mcp.types import TextContent
        return [TextContent(type="text", text='{"status": "preview_ready", "authorization_url": "x"}')]

    args_a = {**MEETING_BASE_ARGS, "thread_id": "thread-conc-A"}
    args_b = {**MEETING_BASE_ARGS, "thread_id": "thread-conc-B"}

    with patch("agent.mcp_server.call_tool", side_effect=fake_call_tool):
        async def gather_calls():
            return await asyncio.gather(
                dispatch_tool("schedule_meeting_preview", args_a, "thread-conc-A"),
                dispatch_tool("schedule_meeting_preview", args_b, "thread-conc-B"),
            )

        results = _run_async(gather_calls())

    assert len(results) == 2
    for r in results:
        assert r["status"] == "preview_ready"
    assert "thread-conc-A" in captured
    assert "thread-conc-B" in captured


@pytest.mark.asyncio
async def test_mcp_call_tool_contextvars_remain_isolated_under_gather():
    from agent.mcp_server import mcp_token_ctx

    fake_call_results = []

    async def run_one(thread_id, token_value):
        token_handle = mcp_token_ctx.set(token_value)
        try:
            await asyncio.sleep(0.005)
            fake_call_results.append((thread_id, mcp_token_ctx.get()))
            return thread_id
        finally:
            mcp_token_ctx.reset(token_handle)

    results = await asyncio.gather(
        run_one("thread-A", "token-A"),
        run_one("thread-B", "token-B"),
    )

    assert sorted(results) == ["thread-A", "thread-B"]
    by_thread = dict(fake_call_results)
    assert by_thread["thread-A"] == "token-A"
    assert by_thread["thread-B"] == "token-B"
