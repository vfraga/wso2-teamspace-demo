import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from agent.tools import TOOL_DEFINITIONS, dispatch_tool
from agent.state_manager import StateManager, FlowState

def test_tool_definitions():
    assert len(TOOL_DEFINITIONS) == 1
    funcs = TOOL_DEFINITIONS[0].function_declarations
    func_names = [f.name for f in funcs]
    
    assert "schedule_meeting_preview" in func_names
    assert "schedule_meeting" in func_names
    assert "list_meetings" in func_names
    assert "delete_meeting_preview" in func_names
    assert "delete_meeting" in func_names
    assert "update_meeting_preview" in func_names
    assert "update_meeting" in func_names

def test_dispatch_schedule_meeting_preview():
    def run_async_isolated(coro):
        import asyncio
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(asyncio.run, coro)
            return future.result()

    thread_id = "test-thread-tools"
    args = {
        "topic": "Lunch Meeting",
        "date": "2026-05-27",
        "start_time": "12:00",
        "duration": "45",
        "time_zone": "America/Sao_Paulo"
    }

    # Mock AuthManager and StateManager
    mock_auth_mgr = MagicMock()
    mock_auth_mgr.get_obo_token.return_value = "mock-obo-token"

    mock_state_mgr = MagicMock()

    # Mock local MCP Server tool invocation response
    import json
    mock_content = MagicMock()
    mock_content.text = json.dumps({
        "status": "preview_ready",
        "meeting": args,
        "authorization_url": "https://mock-is.com/auth",
        "message": "Please authorize me to book this meeting on your behalf."
    })
    
    mock_mcp_call = AsyncMock(return_value=[mock_content])

    with patch("agent.tools.AuthManager.get_instance", return_value=mock_auth_mgr), \
         patch("agent.tools.StateManager.get_instance", return_value=mock_state_mgr), \
         patch("agent.mcp_server.call_tool", mock_mcp_call):
         
        result = run_async_isolated(dispatch_tool("schedule_meeting_preview", args, thread_id))
        
        assert result["status"] == "preview_ready"
        assert result["meeting"] == args
        assert result["authorization_url"] == "https://mock-is.com/auth"
        
        # Verify the client passed the correct parameters to the local MCP handler
        expected_args = dict(args)
        expected_args["thread_id"] = thread_id
        mock_mcp_call.assert_awaited_once_with("schedule_meeting_preview", expected_args)


