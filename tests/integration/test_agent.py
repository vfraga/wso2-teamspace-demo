import pytest
import jwt
from unittest.mock import AsyncMock, patch, MagicMock
from agent.state_manager import StateManager, FlowState, FrontendState
from agent.tool_schemas import MEETING_BASE_ARGS

@pytest.fixture(autouse=True)
def mock_validate_mcp_token():
    with patch("agent.mcp_server.validate_mcp_token") as mock_val:
        mock_val.return_value = {
            "scope": "openid email create_meeting list_meetings delete_meeting update_meeting create_meeting_agent list_meetings_agent delete_meeting_agent update_meeting_agent"
        }
        yield mock_val

@pytest.fixture
def mock_gemini_run_agent():
    # Mock the LLM agent call to return a fixed mock message
    with patch("agent.main.run_agent", new_callable=AsyncMock) as mock:
        mock.return_value = "This is a mock assistant response for booking a meeting."
        yield mock

@pytest.fixture
def mock_auth_manager():
    # Mock the AuthManager singleton to prevent outbound HTTP requests to WSO2
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

def test_agent_health(agent_client):
    resp = agent_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

def test_agent_chat_idle_state(agent_client, mock_gemini_run_agent, mock_auth_manager):
    # Test chatting when the state is IDLE
    payload = {
        "thread_id": "thread-123",
        "message": "Hi, who are you?",
        "org_name": "numbainfinite"
    }
    headers = {"X-Internal-Secret": "mock-internal-secret"}
    with patch("agent.main.settings") as mock_settings:
        mock_settings.INTERNAL_SECRET = "mock-internal-secret"
        resp = agent_client.post("/chat", json=payload, headers=headers)
    assert resp.status_code == 200
    
    data = resp.json()
    assert data["message"] == "This is a mock assistant response for booking a meeting."
    assert data["state"] == "IDLE"
    assert data["obo_jwt"] == "mock-obo-jwt-payload"
    assert data["agent_jwt"] == "mock-agent-jwt-payload"

def test_agent_callback_exchange(agent_client, mock_auth_manager):
    # Setup state manager to simulate pre-existing credentials
    state_mgr = StateManager.get_instance()
    state_mgr.set_agent_credentials("thread-555", "agent-id-123", "agent-secret-456")

    # §2.1.4: callback CSRF is fail-closed — requires MOCK_LLM for plain state strings
    with patch("agent.main.settings") as mock_settings:
        mock_settings.MOCK_LLM = True
        mock_settings.INTERNAL_SECRET = "mock-internal-secret"

        # Trigger callback endpoint
        resp = agent_client.get("/callback?code=authcode123&state=thread-555")
        assert resp.status_code == 200

        # Assert successful callback HTML scripts to close window and postMessage
        assert "window.opener.postMessage('authorized', '*')" in resp.text
        assert "window.close()" in resp.text

    # Assert AuthManager exchange was invoked correctly
    mock_auth_manager.exchange_obo_code.assert_awaited_once_with(
        "thread-555", "authcode123", "agent-id-123", "agent-secret-456"
    )

    # Assert StateManager transitioned to BOOKING_AUTHORIZED
    assert state_mgr.get_state("thread-555") == FlowState.BOOKING_AUTHORIZED


def _build_callback_state_jwt(thread_id: str, action: str, csrf: str, secret: str) -> str:
    payload = {"thread_id": thread_id, "action": action, "state": csrf}
    return jwt.encode(payload, secret, algorithm="HS256")


def test_agent_callback_signed_state_with_matching_cookie(agent_client, mock_auth_manager):
    state_mgr = StateManager.get_instance()
    state_mgr.set_agent_credentials("thread-csrf-happy", "agent-csrf", "secret-csrf")

    with patch("agent.main.settings") as mock_settings:
        mock_settings.INTERNAL_SECRET = "mock-internal-secret"
        secret = "mock-internal-secret"
        signed_state = _build_callback_state_jwt(
            "thread-csrf-happy", "booking", "csrf-token-happy", secret
        )
        cookie_jwt = jwt.encode(
            {"thread_id": "thread-csrf-happy", "action": "booking", "state": "csrf-token-happy"},
            secret,
            algorithm="HS256",
        )
        resp = agent_client.get(
            f"/callback?code=auth-csrf-happy&state={signed_state}",
            cookies={"oauth_session": cookie_jwt},
        )

    assert resp.status_code == 200
    assert "window.opener.postMessage('authorized', '*')" in resp.text
    mock_auth_manager.exchange_obo_code.assert_awaited_once_with(
        "thread-csrf-happy", "auth-csrf-happy", "agent-csrf", "secret-csrf"
    )
    assert state_mgr.get_state("thread-csrf-happy") == FlowState.BOOKING_AUTHORIZED


def test_agent_callback_mismatched_cookie(agent_client, mock_auth_manager):
    state_mgr = StateManager.get_instance()
    state_mgr.set_agent_credentials("thread-csrf-mismatch", "agent-mismatch", "secret-mismatch")

    with patch("agent.main.settings") as mock_settings:
        mock_settings.INTERNAL_SECRET = "mock-internal-secret"
        secret = "mock-internal-secret"
        signed_state = _build_callback_state_jwt(
            "thread-csrf-mismatch", "booking", "csrf-expected", secret
        )
        cookie_jwt = jwt.encode(
            {"thread_id": "thread-csrf-mismatch", "action": "booking", "state": "csrf-different"},
            secret,
            algorithm="HS256",
        )
        resp = agent_client.get(
            f"/callback?code=auth-csrf-bad&state={signed_state}",
            cookies={"oauth_session": cookie_jwt},
        )

    # §2.1.4: CSRF mismatch must return 400 (fail-closed)
    assert resp.status_code == 400
    mock_auth_manager.exchange_obo_code.assert_not_awaited()


def test_agent_callback_state_split_fallback_is_test_only(agent_client, mock_auth_manager):
    # Documents that the bare `state.split(":")` fallback in /callback exists
    # only for integration tests that pre-date the signed-state production flow.
    # Production callers always emit a signed JWT state, so the fallback should
    # never be exercised in live environments. §2.1.4: this is gated behind MOCK_LLM.
    state_mgr = StateManager.get_instance()
    state_mgr.set_agent_credentials("thread-legacy", "agent-legacy", "secret-legacy")

    with patch("agent.main.settings") as mock_settings:
        mock_settings.MOCK_LLM = True
        mock_settings.INTERNAL_SECRET = "mock-internal-secret"

        resp = agent_client.get("/callback?code=auth-legacy&state=thread-legacy:booking")
        assert resp.status_code == 200
    mock_auth_manager.exchange_obo_code.assert_awaited_once_with(
        "thread-legacy", "auth-legacy", "agent-legacy", "secret-legacy"
    )
    assert state_mgr.get_state("thread-legacy") == FlowState.BOOKING_AUTHORIZED


def test_agent_token_generation(agent_client, mock_auth_manager):
    # Set headers with correct internal secret
    headers = {"X-Internal-Secret": "mock-internal-secret"}
    
    # We patch settings to match our headers
    with patch("agent.main.settings") as mock_settings:
        mock_settings.INTERNAL_SECRET = "mock-internal-secret"
        
        resp = agent_client.post("/agent-token", headers=headers)
        assert resp.status_code == 200
        assert resp.json() == {"access_token": "mock-agent-token-123"}
        mock_auth_manager.fetch_agent_token.assert_awaited_once()

def test_get_state_internal(agent_client, mock_auth_manager):
    headers = {"X-Internal-Secret": "mock-internal-secret"}
    
    with patch("agent.main.settings") as mock_settings:
        mock_settings.INTERNAL_SECRET = "mock-internal-secret"
        
        # Preset state to BOOKING_AUTHORIZED (maps to FrontendState.BOOKING_COMPLETE)
        state_mgr = StateManager.get_instance()
        state_mgr.set_state("thread-999", FlowState.BOOKING_AUTHORIZED)
        
        resp = agent_client.get("/state/thread-999", headers=headers)
        assert resp.status_code == 200
        
        data = resp.json()
        assert data["state"] == "BOOKING_COMPLETE"
        assert data["obo_jwt"] == "mock-obo-jwt-payload"
        assert data["agent_jwt"] == "mock-agent-jwt-payload"


def test_agent_chat_missing_internal_secret(agent_client):
    # Test chat endpoint with missing X-Internal-Secret header when internal secret is set
    # (should return 403 Forbidden since secret check is active)
    payload = {
        "thread_id": "thread-123",
        "message": "Hi",
        "org_name": "test-org"
    }
    with patch("agent.main.settings") as mock_settings:
        mock_settings.INTERNAL_SECRET = "some-internal-secret"
        resp = agent_client.post("/chat", json=payload)
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Forbidden"}


def test_agent_chat_missing_internal_secret_when_secret_unset(agent_client, mock_gemini_run_agent, mock_auth_manager):
    # Test chat endpoint with missing X-Internal-Secret header when internal secret is unset/empty
    # (should pass the header/secret check and return 200)
    payload = {
        "thread_id": "thread-123",
        "message": "Hi",
        "org_name": "test-org"
    }
    with patch("agent.main.settings") as mock_settings:
        mock_settings.INTERNAL_SECRET = ""
        resp = agent_client.post("/chat", json=payload)
    assert resp.status_code == 200


def test_agent_chat_invalid_internal_secret(agent_client):
    # Test chat endpoint with invalid X-Internal-Secret header (should return 403 Forbidden)
    payload = {
        "thread_id": "thread-123",
        "message": "Hi",
        "org_name": "test-org"
    }
    headers = {"X-Internal-Secret": "wrong-secret"}
    with patch("agent.main.settings") as mock_settings:
        mock_settings.INTERNAL_SECRET = "correct-secret"
        resp = agent_client.post("/chat", json=payload, headers=headers)
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Forbidden"}


def test_agent_chat_missing_required_payload_fields(agent_client):
    # Test chat endpoint with correct secret but missing message in payload (should return 422)
    payload = {
        "thread_id": "thread-123"
        # message is missing
    }
    headers = {"X-Internal-Secret": "mock-internal-secret"}
    with patch("agent.main.settings") as mock_settings:
        mock_settings.INTERNAL_SECRET = "mock-internal-secret"
        resp = agent_client.post("/chat", json=payload, headers=headers)
    assert resp.status_code == 422
    data = resp.json()
    assert "detail" in data
    assert "body" in data


def run_async_isolated(coro):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(asyncio.run, coro)
        return future.result()


def test_run_agent_mock_mode_no_scoping_error():
    # Test run_agent directly under MOCK_LLM = True to ensure no scoping/local variable unbound issues
    from agent.gemini_agent import run_agent
    from agent.state_manager import StateManager, FlowState
    
    state_mgr = StateManager.get_instance()
    state_mgr.set_state("test-thread-scoping-1", FlowState.INITIAL)
    
    with patch("agent.gemini_agent.settings") as mock_settings:
        mock_settings.MOCK_LLM = True
        # Mock dispatch_tool to return a dummy authorization_url
        with patch("agent.gemini_agent.dispatch_tool", new_callable=AsyncMock) as mock_dispatch:
            mock_dispatch.return_value = {"authorization_url": "http://mock-auth-url"}
            
            response = run_async_isolated(run_agent(
                message="schedule a meeting Topic: Test Scoping",
                thread_id="test-thread-scoping-1",
                org_name="test-org",
                history=[]
            ))
            assert "Authorize Meeting" in response
            assert "http://mock-auth-url" in response
            mock_dispatch.assert_awaited_once()


def test_run_agent_real_mode_dispatch_tool_no_unbound_error():
    # Test run_agent directly under MOCK_LLM = False to ensure dispatch_tool resolves in real mode
    from agent.gemini_agent import run_agent
    from google.genai import types
    
    with patch("agent.gemini_agent.settings") as mock_settings:
        mock_settings.MOCK_LLM = False
        
        # Mock genai.Client
        mock_client_instance = MagicMock()
        
        # We need response candidates where candidate 1 has a function call, and candidate 2 has a text response
        # Iteration 1: Gemini returns a function call to schedule_meeting_preview
        args_data = {
            "topic": "Scoping Test",
            "date": "2026-05-22",
            "start_time": "14:00",
            "duration": "60",
            "time_zone": "America/Sao_Paulo"
        }
        part_fn = types.Part.from_function_call(
            name="schedule_meeting_preview",
            args=args_data
        )
        
        mock_candidate_fn = MagicMock()
        mock_candidate_fn.content.parts = [part_fn]
        
        # Iteration 2: Gemini returns a final text response
        part_text = types.Part.from_text(text="I have scheduled the meeting.")
        
        mock_candidate_text = MagicMock()
        mock_candidate_text.content.parts = [part_text]
        
        # Set up generate_content side effect
        mock_response_fn = MagicMock()
        mock_response_fn.candidates = [mock_candidate_fn]
        
        mock_response_text = MagicMock()
        mock_response_text.candidates = [mock_candidate_text]
        
        mock_client_instance.models.generate_content.side_effect = [
            mock_response_fn,
            mock_response_text
        ]
        
        with patch("agent.gemini_agent.genai.Client", return_value=mock_client_instance):
            with patch("agent.gemini_agent.dispatch_tool", new_callable=AsyncMock) as mock_dispatch:
                mock_dispatch.return_value = {"authorization_url": "http://mock-auth-url"}
                
                response = run_async_isolated(run_agent(
                    message="schedule a meeting",
                    thread_id="test-thread-scoping-2",
                    org_name="test-org",
                    history=[],
                    gemini_api_key="fake-key"
                ))
                
                assert response == "I have scheduled the meeting."
                mock_dispatch.assert_awaited_once_with(
                    "schedule_meeting_preview",
                    args_data,
                    thread_id="test-thread-scoping-2"
                )


def test_list_meetings_tool_flow(mock_auth_manager):
    from agent.tools import dispatch_tool
    from agent.state_manager import StateManager, FlowState
    
    state_mgr = StateManager.get_instance()
    state_mgr.set_state("thread-list-1", FlowState.INITIAL)
    
    # 1. First call: No token -> should return preview with authorization url
    mock_auth_manager.get_obo_token.return_value = None
    mock_auth_manager.get_obo_authorization_url.return_value = "http://mock-list-auth-url"
    
    res = run_async_isolated(dispatch_tool("list_meetings", {}, "thread-list-1"))
    assert res["status"] == "preview_ready"
    assert res["authorization_url"] == "http://mock-list-auth-url"
    mock_auth_manager.get_obo_authorization_url.assert_called_with(
        thread_id="thread-list-1",
        scopes=["list_meetings_agent"],
        agent_id="",
        action="list"
    )
    
    # 2. Second call: Token is present -> should fetch from Business API
    mock_auth_manager.get_obo_token.return_value = "mock-obo-token-123"
    
    with patch("agent.tools.httpx.AsyncClient") as mock_client_class:
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"id": "m1", "topic": "List Test"}]
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_class.return_value.__aenter__.return_value = mock_client
        
        res = run_async_isolated(dispatch_tool("list_meetings", {}, "thread-list-1"))
        assert res["status"] == "success"
        assert res["meetings"] == [{"id": "m1", "topic": "List Test"}]

        # Verify single-use token paradigm cleared the token
        mock_auth_manager.clear_obo_tokens.assert_called_with("thread-list-1")


def test_dispatch_authorized_state_list_authorized_contract():
    """Regression: _handle_list_authorized must accept the dispatch contract
    and route through `_dispatch_authorized_state` without raising. Prior
    to the fix, this raised TypeError at runtime when the user completed
    the OBO flow for a list request. The 3-arg signature (thread_id,
    pending, language) is the contract after the i18n cleanup; the unused
    `pending` arg is still accepted for handler-uniformity.
    """
    from agent.gemini_agent import _dispatch_authorized_state
    from agent.state_manager import StateManager, FlowState
    from unittest.mock import AsyncMock, patch

    state_mgr = StateManager.get_instance()
    state_mgr.set_state("thread-list-dispatch-1", FlowState.LIST_AUTHORIZED)

    with patch("agent.gemini_agent.dispatch_tool", new=AsyncMock(return_value={"status": "success", "meetings": []})) as mock_dispatch:
        result = run_async_isolated(
            _dispatch_authorized_state(
                FlowState.LIST_AUTHORIZED, "thread-list-dispatch-1", {}, "en"
            )
        )
    assert result is not None
    assert result[0] is True
    mock_dispatch.assert_awaited_once_with("list_meetings", {}, "thread-list-dispatch-1")


def test_delete_meeting_tool_flow(mock_auth_manager):
    from agent.tools import dispatch_tool
    from agent.state_manager import StateManager, FlowState

    state_mgr = StateManager.get_instance()
    state_mgr.set_state("thread-delete-1", FlowState.INITIAL)

    # 1. Preview call -> should return preview with delete_meeting scope authorization url
    mock_auth_manager.get_obo_authorization_url.return_value = "http://mock-delete-auth-url"

    res = run_async_isolated(dispatch_tool("delete_meeting_preview", {"meeting_id": "m1", "topic": "Delete Test"}, "thread-delete-1"))
    assert res["status"] == "preview_ready"
    assert res["meeting_id"] == "m1"
    assert "Delete Test" in res["message"]

    # 2. Finalize call: Not authorized state -> should return waiting
    res = run_async_isolated(dispatch_tool("delete_meeting", {"meeting_id": "m1"}, "thread-delete-1"))
    assert res["status"] == "waiting_for_authorization"

    # 3. Finalize call: Authorized state -> should call DELETE
    state_mgr.set_state("thread-delete-1", FlowState.DELETE_AUTHORIZED)
    mock_auth_manager.get_obo_token.return_value = "mock-obo-token-123"

    with patch("agent.tools.httpx.AsyncClient") as mock_client_class:
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        mock_client.delete = AsyncMock(return_value=mock_resp)
        mock_client_class.return_value.__aenter__.return_value = mock_client

        res = run_async_isolated(dispatch_tool("delete_meeting", {"meeting_id": "m1"}, "thread-delete-1"))
        assert res["status"] == "deleted"
        assert res["meeting_id"] == "m1"
        mock_auth_manager.clear_obo_tokens.assert_called_with("thread-delete-1")


def test_delete_meeting_tool_flow_booking_state_yields_waiting(mock_auth_manager):
    from agent.tools import dispatch_tool
    from agent.state_manager import StateManager, FlowState

    state_mgr = StateManager.get_instance()
    state_mgr.set_state("thread-delete-bad-state", FlowState.BOOKING_AUTHORIZED)
    mock_auth_manager.get_obo_token.return_value = "mock-obo-token-123"

    res = run_async_isolated(
        dispatch_tool("delete_meeting", {"meeting_id": "m1"}, "thread-delete-bad-state")
    )
    assert res["status"] == "waiting_for_authorization", (
        "BOOKING_AUTHORIZED must not unlock delete_meeting — only DELETE_AUTHORIZED allowed (Batch C)"
    )


def test_update_meeting_tool_flow(mock_auth_manager):
    from agent.tools import dispatch_tool
    from agent.state_manager import StateManager, FlowState

    state_mgr = StateManager.get_instance()
    state_mgr.set_state("thread-update-1", FlowState.INITIAL)

    # 1. Preview call -> should return preview with update_meeting scope authorization url
    mock_auth_manager.get_obo_authorization_url.return_value = "http://mock-update-auth-url"
    args = {
        **MEETING_BASE_ARGS,
        "meeting_id": "m1",
        "topic": "Updated Topic",
        "start_time": "15:00",
    }
    res = run_async_isolated(dispatch_tool("update_meeting_preview", args, "thread-update-1"))
    assert res["status"] == "preview_ready"
    assert res["meeting"] == args

    # 2. Finalize call: Authorized state -> should call PUT on business API
    state_mgr.set_state("thread-update-1", FlowState.UPDATE_AUTHORIZED)
    mock_auth_manager.get_obo_token.return_value = "mock-obo-token-123"

    with patch("agent.tools.httpx.AsyncClient") as mock_client_class:
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "m1", "topic": "Updated Topic"}
        mock_client.put = AsyncMock(return_value=mock_resp)
        mock_client_class.return_value.__aenter__.return_value = mock_client

        res = run_async_isolated(dispatch_tool("update_meeting", args, "thread-update-1"))
        assert res["status"] == "updated"
        assert res["meeting"]["topic"] == "Updated Topic"
        mock_auth_manager.clear_obo_tokens.assert_called_with("thread-update-1")


def test_update_meeting_tool_flow_booking_state_yields_waiting(mock_auth_manager):
    from agent.tools import dispatch_tool
    from agent.state_manager import StateManager, FlowState

    state_mgr = StateManager.get_instance()
    state_mgr.set_state("thread-update-bad-state", FlowState.BOOKING_AUTHORIZED)
    mock_auth_manager.get_obo_token.return_value = "mock-obo-token-123"

    args = {
        **MEETING_BASE_ARGS,
        "meeting_id": "m1",
        "topic": "Updated Topic",
        "start_time": "15:00",
    }
    res = run_async_isolated(
        dispatch_tool("update_meeting", args, "thread-update-bad-state")
    )
    assert res["status"] == "waiting_for_authorization", (
        "BOOKING_AUTHORIZED must not unlock update_meeting — only UPDATE_AUTHORIZED allowed (Batch C)"
    )


@patch("agent.main.run_agent")
def test_agent_chat_httpx_timeout_graceful(mock_run, agent_client, mock_auth_manager):
    import httpx
    mock_run.side_effect = httpx.ReadTimeout("Read timed out")
    
    with patch("agent.main.settings") as mock_settings:
        mock_settings.INTERNAL_SECRET = "test-secret"
        resp = agent_client.post(
            "/chat",
            json={"thread_id": "test-t1", "message": "hello", "org_name": "Batata"},
            headers={"X-Internal-Secret": "test-secret"}
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "timed out" in data["message"]


@patch("agent.main.run_agent")
def test_agent_chat_httpx_error_graceful(mock_run, agent_client, mock_auth_manager):
    import httpx
    mock_run.side_effect = httpx.ReadError("Connection reset by peer")
    
    with patch("agent.main.settings") as mock_settings:
        mock_settings.INTERNAL_SECRET = "test-secret"
        resp = agent_client.post(
            "/chat",
            json={"thread_id": "test-t1", "message": "hello", "org_name": "Batata"},
            headers={"X-Internal-Secret": "test-secret"}
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "failed to communicate" in data["message"]


@patch("agent.main.run_agent")
def test_agent_chat_google_api_error_graceful(mock_run, agent_client, mock_auth_manager):
    from google.genai.errors import APIError
    mock_run.side_effect = APIError(429, {"error": "Resource exhausted"})
    
    with patch("agent.main.settings") as mock_settings:
        mock_settings.INTERNAL_SECRET = "test-secret"
        resp = agent_client.post(
            "/chat",
            json={"thread_id": "test-t1", "message": "hello", "org_name": "Batata"},
            headers={"X-Internal-Secret": "test-secret"}
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "rate limit" in data["message"]

