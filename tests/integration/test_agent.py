import time
from urllib.parse import parse_qs, urlparse

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

AGENT_TEST_AUDIENCE = "test-client-id"


@pytest.fixture
def service_auth(monkeypatch):
    """Mint valid `X-Service-Authorization` headers for the agent service.

    The agent's endpoints are no longer gated on a shared secret: they verify
    an OAuth 2.0 client-credentials token against JWKS, so tests present real
    signed tokens.
    """
    from agent.config import settings as agent_settings
    from tests.helpers.tokens import issuer_for, patch_agent_jwks, service_auth_header

    monkeypatch.setattr(agent_settings, "CLIENT_ID", AGENT_TEST_AUDIENCE)
    issuer = issuer_for(agent_settings.IS_BASE_URL, agent_settings.TENANT_PATH)

    def _headers(**overrides):
        overrides.setdefault("audience", AGENT_TEST_AUDIENCE)
        overrides.setdefault("issuer", issuer)
        return service_auth_header(**overrides)

    with patch_agent_jwks():
        yield _headers


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

def test_agent_chat_idle_state(agent_client, mock_gemini_run_agent, mock_auth_manager, service_auth):
    # Test chatting when the state is IDLE
    payload = {
        "thread_id": "thread-123",
        "message": "Hi, who are you?",
        "org_name": "numbainfinite"
    }
    resp = agent_client.post("/chat", json=payload, headers=service_auth())
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
        mock_settings.state_jwt_signing_secret.return_value = "mock-internal-secret"

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


def _build_callback_state_jwt(
    thread_id: str, action: str, csrf: str, secret: str, *, expires_in: int = 300
) -> str:
    now = int(time.time())
    payload = {
        "thread_id": thread_id, "action": action, "state": csrf,
        "iat": now, "exp": now + expires_in,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def _build_oauth_cookie(
    thread_id: str, action: str, csrf: str, secret: str, *, expires_in: int = 300
) -> str:
    now = int(time.time())
    return jwt.encode(
        {"thread_id": thread_id, "action": action, "state": csrf,
         "iat": now, "exp": now + expires_in},
        secret, algorithm="HS256",
    )


# ---------------------------------------------------------------------------
# CSRF and replay protection on /callback
#
# The OBO callback has two factors: the HMAC-signed `state` parameter and the
# paired `oauth_session` cookie. Both were weaker than they looked — the state
# carried no `exp`, so a captured one was a permanent replay credential, and the
# cookie was only checked `if cookie_val`, so omitting it skipped the second
# factor entirely. Together that allowed authorization-code injection.
# ---------------------------------------------------------------------------


def test_callback_without_the_csrf_cookie_is_refused(agent_client, mock_auth_manager):
    secret = "mock-internal-secret"
    with patch("agent.main.settings") as mock_settings:
        mock_settings.MOCK_LLM = False
        mock_settings.state_jwt_signing_secret.return_value = secret
        signed_state = _build_callback_state_jwt("t-nocookie", "booking", "csrf-1", secret)
        # Valid, unexpired, correctly signed state — but no cookie.
        resp = agent_client.get(f"/callback?code=abc&state={signed_state}")

    assert resp.status_code == 400
    assert "Invalid OAuth session" in resp.text
    mock_auth_manager.exchange_obo_code.assert_not_awaited()


def test_callback_with_an_expired_state_is_refused(agent_client, mock_auth_manager):
    secret = "mock-internal-secret"
    with patch("agent.main.settings") as mock_settings:
        mock_settings.MOCK_LLM = False
        mock_settings.state_jwt_signing_secret.return_value = secret
        stale = _build_callback_state_jwt(
            "t-stale", "booking", "csrf-2", secret, expires_in=-60
        )
        cookie = _build_oauth_cookie("t-stale", "booking", "csrf-2", secret)
        resp = agent_client.get(
            f"/callback?code=abc&state={stale}", cookies={"oauth_session": cookie}
        )

    assert resp.status_code == 400
    # A distinct message: an expired state is what happens when someone leaves
    # the consent screen open, not an attack.
    assert "expired" in resp.text.lower()
    mock_auth_manager.exchange_obo_code.assert_not_awaited()


def test_callback_with_an_expired_cookie_is_refused(agent_client, mock_auth_manager):
    secret = "mock-internal-secret"
    with patch("agent.main.settings") as mock_settings:
        mock_settings.MOCK_LLM = False
        mock_settings.state_jwt_signing_secret.return_value = secret
        signed_state = _build_callback_state_jwt("t-cexp", "booking", "csrf-3", secret)
        stale_cookie = _build_oauth_cookie(
            "t-cexp", "booking", "csrf-3", secret, expires_in=-60
        )
        resp = agent_client.get(
            f"/callback?code=abc&state={signed_state}",
            cookies={"oauth_session": stale_cookie},
        )

    assert resp.status_code == 400
    assert "expired" in resp.text.lower()
    mock_auth_manager.exchange_obo_code.assert_not_awaited()


def test_authorize_issues_an_expiring_state_and_cookie(agent_client):
    """/authorize must stamp exp on both halves, or the replay window is forever."""
    from agent.state_manager import StateManager
    from agent.store import InMemoryStore, set_store

    secret = "mock-internal-secret"
    set_store(InMemoryStore())
    try:
        StateManager.get_instance().set_agent_credentials("t-exp", "agent-1", "s-1")
        with patch("agent.main.settings") as mock_settings:
            mock_settings.state_jwt_signing_secret.return_value = secret
            mock_settings.CLIENT_ID = "cid"
            mock_settings.AGENT_REDIRECT_URI = "http://localhost:8000/callback"
            mock_settings.IS_BASE_URL = "https://localhost:9443"
            mock_settings.TENANT_PATH = "/t/teamspace"
            resp = agent_client.get(
                "/authorize?thread_id=t-exp&action=book", follow_redirects=False
            )
    finally:
        set_store(None)

    assert resp.status_code in (302, 307)
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    claims = jwt.decode(state, secret, algorithms=["HS256"])
    assert "exp" in claims and "iat" in claims
    assert 0 < claims["exp"] - claims["iat"] <= 300

    cookie_jwt = resp.cookies["oauth_session"]
    cookie_claims = jwt.decode(cookie_jwt, secret, algorithms=["HS256"])
    # Both halves must expire together, or the state could outlive its cookie.
    assert cookie_claims["exp"] == claims["exp"]
    assert cookie_claims["state"] == claims["state"]


def test_agent_callback_signed_state_with_matching_cookie(agent_client, mock_auth_manager):
    state_mgr = StateManager.get_instance()
    state_mgr.set_agent_credentials("thread-csrf-happy", "agent-csrf", "secret-csrf")

    with patch("agent.main.settings") as mock_settings:
        secret = "mock-internal-secret"
        mock_settings.state_jwt_signing_secret.return_value = secret
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
        secret = "mock-internal-secret"
        mock_settings.state_jwt_signing_secret.return_value = secret
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
        mock_settings.state_jwt_signing_secret.return_value = "mock-internal-secret"

        resp = agent_client.get("/callback?code=auth-legacy&state=thread-legacy:booking")
        assert resp.status_code == 200
    mock_auth_manager.exchange_obo_code.assert_awaited_once_with(
        "thread-legacy", "auth-legacy", "agent-legacy", "secret-legacy"
    )
    assert state_mgr.get_state("thread-legacy") == FlowState.BOOKING_AUTHORIZED


def test_agent_token_generation(agent_client, mock_auth_manager, service_auth):
    resp = agent_client.post("/agent-token", headers=service_auth())
    assert resp.status_code == 200
    assert resp.json() == {"access_token": "mock-agent-token-123"}
    mock_auth_manager.fetch_agent_token.assert_awaited_once()


def test_get_state_internal(agent_client, mock_auth_manager, service_auth):
    # Preset state to BOOKING_AUTHORIZED (maps to FrontendState.BOOKING_COMPLETE)
    state_mgr = StateManager.get_instance()
    state_mgr.set_state("thread-999", FlowState.BOOKING_AUTHORIZED)

    resp = agent_client.get("/state/thread-999", headers=service_auth())
    assert resp.status_code == 200

    if True:
        data = resp.json()
        assert data["state"] == "BOOKING_COMPLETE"
        assert data["obo_jwt"] == "mock-obo-jwt-payload"
        assert data["agent_jwt"] == "mock-agent-jwt-payload"


_CHAT_PAYLOAD = {"thread_id": "thread-123", "message": "Hi", "org_name": "test-org"}


def test_agent_chat_without_service_token_is_rejected(agent_client):
    resp = agent_client.post("/chat", json=_CHAT_PAYLOAD)
    assert resp.status_code == 401
    assert "X-Service-Authorization" in resp.json()["detail"]


def test_agent_chat_with_no_credentials_configured_still_fails_closed(
    agent_client, mock_gemini_run_agent, mock_auth_manager, monkeypatch
):
    """The old shared-secret check failed OPEN when the secret was unset.

    `agent/main.py` used to wrap the comparison in `if settings.INTERNAL_SECRET:`
    while `agent/config.py` generated a random secret to keep that from
    happening. With the secret gone, an unconfigured deployment must reject the
    call rather than serve an unauthenticated one.
    """
    from agent.config import settings as agent_settings

    monkeypatch.setattr(agent_settings, "CLIENT_ID", "")
    monkeypatch.setattr(agent_settings, "CLIENT_SECRET", "")

    resp = agent_client.post("/chat", json=_CHAT_PAYLOAD)
    assert resp.status_code == 401


def test_agent_chat_with_unverifiable_token_is_rejected(agent_client, service_auth):
    resp = agent_client.post(
        "/chat", json=_CHAT_PAYLOAD,
        headers={"X-Service-Authorization": "Bearer not-a-real-jwt"},
    )
    assert resp.status_code == 401


def test_agent_chat_with_expired_service_token_is_rejected(agent_client, service_auth):
    resp = agent_client.post("/chat", json=_CHAT_PAYLOAD, headers=service_auth(expires_in=-60))
    assert resp.status_code == 401
    assert "expired" in resp.json()["detail"].lower()


def test_agent_chat_without_internal_service_scope_is_rejected(agent_client, service_auth):
    resp = agent_client.post(
        "/chat", json=_CHAT_PAYLOAD, headers=service_auth(scope="list_meetings"),
    )
    assert resp.status_code == 403
    assert "internal_service" in resp.json()["detail"]


def test_agent_chat_missing_required_payload_fields(agent_client, service_auth):
    # Valid service token, but the body is missing `message` (should be 422).
    payload = {
        "thread_id": "thread-123"
        # message is missing
    }
    resp = agent_client.post("/chat", json=payload, headers=service_auth())
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
def test_agent_chat_httpx_timeout_graceful(mock_run, agent_client, mock_auth_manager, service_auth):
    import httpx
    mock_run.side_effect = httpx.ReadTimeout("Read timed out")
    
    resp = agent_client.post(
        "/chat",
        json={"thread_id": "test-t1", "message": "hello", "org_name": "Batata"},
        headers=service_auth(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "timed out" in data["message"]


@patch("agent.main.run_agent")
def test_agent_chat_httpx_error_graceful(mock_run, agent_client, mock_auth_manager, service_auth):
    import httpx
    mock_run.side_effect = httpx.ReadError("Connection reset by peer")
    
    resp = agent_client.post(
        "/chat",
        json={"thread_id": "test-t1", "message": "hello", "org_name": "Batata"},
        headers=service_auth(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "failed to communicate" in data["message"]


@patch("agent.main.run_agent")
def test_agent_chat_google_api_error_graceful(mock_run, agent_client, mock_auth_manager, service_auth):
    from google.genai.errors import APIError
    mock_run.side_effect = APIError(429, {"error": "Resource exhausted"})
    
    resp = agent_client.post(
        "/chat",
        json={"thread_id": "test-t1", "message": "hello", "org_name": "Batata"},
        headers=service_auth(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "rate limit" in data["message"]



# --- /state token exposure -------------------------------------------------
#
# The raw OBO and agent JWTs feed the portal's JWT inspector, which is the point
# of the demo. They are still bearer tokens, and any service-token holder can
# ask for any thread, so production withholds them.


def _seed_tokens(thread_id: str):
    import time as _time

    from agent.auth_manager import _NS_AGENT_TOKEN, _NS_OBO_TOKEN
    from agent.store import get_store

    store = get_store()
    for ns, tok in ((_NS_OBO_TOKEN, "obo-token-value"), (_NS_AGENT_TOKEN, "agent-token-value")):
        store.set(ns, thread_id, {"token": tok, "expires_at": _time.time() + 3600})


def test_state_exposes_raw_tokens_outside_production(agent_client, service_auth, monkeypatch):
    from agent.store import InMemoryStore, set_store

    monkeypatch.delenv("FLASK_ENV", raising=False)
    set_store(InMemoryStore())
    try:
        _seed_tokens("t-inspect")
        resp = agent_client.get("/state/t-inspect", headers=service_auth())
    finally:
        set_store(None)

    assert resp.status_code == 200
    data = resp.json()
    assert data["obo_jwt"] == "obo-token-value"
    assert data["agent_jwt"] == "agent-token-value"


def test_state_withholds_raw_tokens_in_production(agent_client, service_auth, monkeypatch):
    from agent.store import InMemoryStore, set_store

    monkeypatch.setenv("FLASK_ENV", "production")
    set_store(InMemoryStore())
    try:
        _seed_tokens("t-inspect")
        resp = agent_client.get("/state/t-inspect", headers=service_auth())
    finally:
        set_store(None)

    assert resp.status_code == 200
    data = resp.json()
    # The state itself is still served — only the bearer tokens are held back.
    assert data["state"] == "IDLE"
    assert data["obo_jwt"] is None
    assert data["agent_jwt"] is None
    assert data["tokens_withheld"] is True
