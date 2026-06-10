import os
import time
import threading
import pytest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, Request as FastApiRequest, Response as FastApiResponse
from fastapi.responses import RedirectResponse, JSONResponse
import uvicorn
import jwt
from playwright.sync_api import Page, expect

import base64
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

# Generate RS256 key pair once for mock IS
private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
private_pem = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.TraditionalOpenSSL,
    encryption_algorithm=serialization.NoEncryption()
)

def int_to_base64url(val: int) -> str:
    val_bytes = val.to_bytes((val.bit_length() + 7) // 8, byteorder='big')
    return base64.urlsafe_b64encode(val_bytes).rstrip(b'=').decode()

public_key = private_key.public_key()
numbers = public_key.public_numbers()
n_str = int_to_base64url(numbers.n)
e_str = int_to_base64url(numbers.e)

# Import our actual apps and settings
from api.main import app as api_app
from api.config import settings as api_settings
from api.database import engine as api_engine
from api.models import Base as ApiBase

from agent.main import app as agent_app
from agent.config import settings as agent_settings
from agent.state_manager import StateManager, FlowState

from webapp.app import create_app
from webapp.config import Config
from webapp.plans import PLANS

# Create the Mock WSO2 Identity Server
mock_is = FastAPI()


class MockCodesDB:
    def __init__(self) -> None:
        self._data: dict[str, dict] = {}

    def __setitem__(self, code: str, payload: dict) -> None:
        self._data[code] = payload

    def get(self, code: str):
        return self._data.get(code)


mock_codes_db = MockCodesDB()

@mock_is.get("/oauth2/token/.well-known/openid-configuration")
@mock_is.get("/t/{tenant}/oauth2/token/.well-known/openid-configuration")
def openid_config(tenant: str = None):
    issuer = "http://127.0.0.1:9444"
    if tenant:
        issuer = f"{issuer}/t/{tenant}"
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oauth2/authorize",
        "token_endpoint": f"{issuer}/oauth2/token",
        "userinfo_endpoint": f"{issuer}/oauth2/userinfo",
        "jwks_uri": f"{issuer}/oauth2/jwks",
        "response_types_supported": ["code", "token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"]
    }

@mock_is.get("/oauth2/jwks")
@mock_is.get("/t/{tenant}/oauth2/jwks")
def jwks(tenant: str = None):
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "kid": "default",
                "alg": "RS256",
                "n": n_str,
                "e": e_str
            }
        ]
    }

@mock_is.get("/oauth2/authorize")
@mock_is.get("/t/{tenant}/oauth2/authorize")
def authorize(client_id: str, redirect_uri: str, response_type: str, scope: str, state: str, fidp: str = None, orgId: str = None, tenant: str = None, nonce: str = None):
    code = "mock_code"
    mock_codes_db[code] = {"nonce": nonce, "orgId": orgId}
    # Auto-redirect to redirect_uri with mock authorization code
    return RedirectResponse(url=f"{redirect_uri}?code={code}&state={state}")

@mock_is.post("/oauth2/authorize")
@mock_is.post("/t/{tenant}/oauth2/authorize")
async def authorize_post(request: FastApiRequest, tenant: str = None):
    body_bytes = await request.body()
    import urllib.parse
    params = dict(urllib.parse.parse_qsl(body_bytes.decode("utf-8")))
    response_mode = params.get("response_mode")
    if response_mode == "direct":
        return {
            "flowId": "mock_flow_12345",
            "nextStep": {
                "authenticators": [
                    {
                        "authenticator": "Username & Password",
                        "authenticatorId": "mock_authenticator_id"
                    }
                ]
            }
        }
    return JSONResponse(content={"flowId": "mock_flow_12345"})

@mock_is.post("/oauth2/authn")
@mock_is.post("/t/{tenant}/oauth2/authn")
async def authn(request: FastApiRequest, tenant: str = None):
    try:
        await request.json()
    except Exception:
        pass
    return {
        "authData": {
            "code": "mock_agent_code"
        }
    }

@mock_is.post("/oauth2/token")
@mock_is.post("/t/{tenant}/oauth2/token")
async def token(request: FastApiRequest, tenant: str = None):
    body_bytes = await request.body()
    import urllib.parse
    params = dict(urllib.parse.parse_qsl(body_bytes.decode("utf-8")))
    
    code = params.get("code")
    actor_token = params.get("actor_token")
    
    now = int(time.time())
    iss = f"http://127.0.0.1:9444/t/{tenant}" if tenant else "http://127.0.0.1:9444/t/teamspace"
    client_id = Config.CLIENT_ID or "fLHf61QVhvGKJwOhM3NvekepG1Aa"
    
    # Check 3 mock token modes:
    if actor_token:
        # OBO Token Exchange
        payload = {
            "iss": iss,
            "aud": client_id,
            "azp": client_id,
            "client_id": client_id,
            "sub": "user-12345",
            "aut": "APPLICATION_USER",
            "act": {
                "sub": "agent-81488"
            },
            "email": "testuser@numbainfinite.com",
            "org_id": "org-infinite-id",
            "org_name": "Numba Infinite",
            "org_handle": "numbainfinite",
            "roles": ["Teamspace Admin"],
            "groups": ["admin"],
            "scope": "openid email create_meeting list_meetings delete_meeting update_meeting view_meeting view_agent_config manage_agent_config create_meeting_agent list_meetings_agent",
            "exp": now + 3600,
            "iat": now,
        }
    elif code == "mock_agent_code":
        # Agent Auth
        payload = {
            "iss": iss,
            "aud": client_id,
            "sub": "agent-81488",
            "aut": "AGENT",
            "org_id": "org-infinite-id",
            "org_name": "Teamspace",
            "org_handle": "teamspace",
            "scope": "openid email create_meeting list_meetings delete_meeting update_meeting view_meeting view_agent_config manage_agent_config create_meeting_agent list_meetings_agent",
            "exp": now + 3600,
            "iat": now,
        }
    else:
        # User Login / Default
        code_data = mock_codes_db.get("mock_code")
        nonce = None
        org_id = None
        if isinstance(code_data, dict):
            nonce = code_data.get("nonce")
            org_id = code_data.get("orgId")
        else:
            nonce = code_data

        org_handle = "numbainfinite"
        org_name = "Numba Infinite"
        org_id_val = "org-infinite-id"
        roles = ["teamspace-admin", "Teamspace Admin"]

        if org_id == "org-enterprise-id":
            org_handle = "enterprise-test"
            org_name = "Enterprise Test"
            org_id_val = "org-enterprise-id"
            roles = ["teamspace-admin", "Teamspace Admin", "idp-manager", "basic-branding-editor", "advanced-branding-editor"]

        payload = {
            "iss": iss,
            "aud": client_id,
            "azp": client_id,
            "client_id": client_id,
            "sub": "user-12345",
            "email": "testuser@numbainfinite.com",
            "name": "Test User",
            "org_id": org_id_val,
            "org_name": org_name,
            "org_handle": org_handle,
            "roles": roles,
            "groups": ["admin"],
            "scope": "openid email create_meeting list_meetings delete_meeting update_meeting view_meeting view_agent_config manage_agent_config create_meeting_agent list_meetings_agent",
            "exp": now + 3600,
            "iat": now,
        }
        if nonce:
            payload["nonce"] = nonce

    encoded_jwt = jwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": "default"})
    return {
        "access_token": encoded_jwt,
        "id_token": encoded_jwt,
        "token_type": "Bearer",
        "expires_in": 3600,
    }

@mock_is.get("/api/server/v1/organizations")
@mock_is.get("/t/{tenant}/api/server/v1/organizations")
def list_organizations(tenant: str = None):
    return {
        "organizations": [
            {
                "id": "org-infinite-id",
                "orgHandle": "numbainfinite",
                "name": "Numba Infinite"
            },
            {
                "id": "org-enterprise-id",
                "orgHandle": "enterprise-test",
                "name": "Enterprise Test"
            }
        ]
    }

@mock_is.post("/api/server/v1/organizations")
@mock_is.post("/t/{tenant}/api/server/v1/organizations")
def create_organization(tenant: str = None):
    return {
        "id": "org-infinite-id",
        "orgHandle": "enterprise-test",
        "name": "Enterprise Test"
    }

@mock_is.post("/api/server/v1/applications/share-with-all")
@mock_is.post("/t/{tenant}/api/server/v1/applications/share-with-all")
def share_application(tenant: str = None):
    return FastApiResponse(status_code=204)

@mock_is.post("/o/scim2/Users")
@mock_is.post("/t/{tenant}/o/scim2/Users")
def create_user(tenant: str = None):
    return {
        "id": "new-user-id",
        "userName": "admin@enterprise.com",
        "emails": [{"value": "admin@enterprise.com"}]
    }

@mock_is.get("/o/scim2/v2/Roles")
@mock_is.get("/t/{tenant}/o/scim2/v2/Roles")
def get_roles(filter: str = None, tenant: str = None):
    role_name = "TEAMSPACE_ADMIN"
    if filter and "eq" in filter:
        role_name = filter.split("eq")[1].strip().strip('"').strip("'")
    return {
        "Resources": [
            {
                "id": f"role-{role_name.lower()}",
                "displayName": role_name
            }
        ]
    }

@mock_is.patch("/o/scim2/v2/Roles/{role_id}")
@mock_is.patch("/t/{tenant}/o/scim2/v2/Roles/{role_id}")
def update_role(role_id: str, tenant: str = None):
    return {
        "id": role_id,
        "displayName": role_id.replace("role-", "").upper()
    }

@mock_is.post("/o/scim2/Agents")
@mock_is.post("/t/{tenant}/o/scim2/Agents")
def create_agent(tenant: str = None):
    return {
        "id": "agent-81488",
        "password": "my-secret-123",
        "displayName": "Numba Agent",
        "meta": {"resourceType": "Agent"}
    }

@mock_is.delete("/o/scim2/Agents/{agent_id}")
@mock_is.delete("/t/{tenant}/o/scim2/Agents/{agent_id}")
def delete_agent(agent_id: str, tenant: str = None):
    return FastApiResponse(status_code=204)


# Server running threads
class UvicornServer(threading.Thread):
    def __init__(self, app, port):
        super().__init__()
        config = uvicorn.Config(app, host="127.0.0.1", port=port, ws="none", log_level="warning")
        self.server = uvicorn.Server(config)
        self.daemon = True

    def run(self):
        self.server.run()

    def shutdown(self):
        self.server.should_exit = True


class WerkzeugServer(threading.Thread):
    def __init__(self, app, port):
        super().__init__()
        from werkzeug.serving import make_server
        self.server = make_server("127.0.0.1", port, app, threaded=True)
        self.daemon = True

    def run(self):
        self.server.serve_forever()

    def shutdown(self):
        self.server.shutdown()


# AsyncMock for the LLM runner to simulate user-assistant interactions
async def mock_run_agent(message: str, thread_id: str, org_name: str, history: list[dict], gemini_api_key: str = "", custom_prompt: str = "", **kwargs) -> str:
    state_mgr = StateManager.get_instance()
    state = state_mgr.get_state(thread_id)
    
    if "authorized" in message.lower() or "check" in message.lower():
        if state in (FlowState.BOOKING_AUTHORIZED, FlowState.LIST_AUTHORIZED, FlowState.UPDATE_AUTHORIZED, FlowState.DELETE_AUTHORIZED):
            from agent.tools import dispatch_tool
            pending = state_mgr.get_pending_meeting(thread_id)
            if pending and pending.get("action") == "delete":
                res = await dispatch_tool("delete_meeting", {"meeting_id": pending["meeting_id"]}, thread_id)
                if res.get("status") == "deleted":
                    return "I have successfully deleted the meeting."
                else:
                    return f"Deletion failed: {res.get('message', 'Unknown error')}"
            elif pending and "meeting_id" in pending:
                res = await dispatch_tool("update_meeting", pending, thread_id)
                if res.get("status") == "updated":
                    return f"I have updated the meeting successfully! Topic: {res['meeting']['topic']}."
                else:
                    return f"Update failed: {res.get('message', 'Unknown error')}"
            elif pending and "topic" in pending:
                res = await dispatch_tool("schedule_meeting", pending, thread_id)
                if res.get("status") == "booked":
                    return f"I have booked the meeting successfully! Topic: {pending['topic']}."
                else:
                    return f"Booking failed: {res.get('message', 'Unknown error')}"
            else:
                res = await dispatch_tool("list_meetings", {}, thread_id)
                if res.get("status") == "success":
                    meetings = res.get("meetings", [])
                    if not meetings:
                        return "You have no scheduled meetings."
                    met_list = "\n".join(f"- {m['topic']} ({m['date']} at {m['start_time']}, ID: {m['id']})" for m in meetings)
                    return f"Here are your scheduled meetings:\n{met_list}"
                return "I couldn't find your authorization. Please authorize me using the link first."
        return "I couldn't find your authorization. Please authorize me using the link first."

    elif "list" in message.lower() or "show" in message.lower():
        from agent.tools import dispatch_tool
        res = await dispatch_tool("list_meetings", {}, thread_id)
        if res.get("status") == "preview_ready":
            return f"""Please authorize me to view your meetings by clicking the link below:
<a href="{res['authorization_url']}" target="_blank">Authorize List</a>
"""
        else:
            meetings = res.get("meetings", [])
            if not meetings:
                return "You have no scheduled meetings."
            met_list = "\n".join(f"- {m['topic']} ({m['date']} at {m['start_time']}, ID: {m['id']})" for m in meetings)
            return f"Here are your scheduled meetings:\n{met_list}"

    elif "delete" in message.lower() or "remove" in message.lower():
        meeting_id = "test-meeting-id"
        words = message.split()
        for w in words:
            if len(w) > 10 and "-" in w:
                meeting_id = w
                break
        from agent.tools import dispatch_tool
        args = {"meeting_id": meeting_id, "topic": "Test E2E Meeting", "action": "delete"}
        res = await dispatch_tool("delete_meeting_preview", args, thread_id)
        return f"""I have prepared a preview to delete your meeting:
Meeting ID: {meeting_id}

To finalize this deletion, please authorize me by clicking the link below:
<a href="{res['authorization_url']}" target="_blank">Authorize Delete</a>
"""

    elif "update" in message.lower() or "change" in message.lower() or "reschedule" in message.lower():
        meeting_id = "test-meeting-id"
        words = message.split()
        for w in words:
            if len(w) > 10 and "-" in w:
                meeting_id = w
                break
        
        topic = "Updated E2E Meeting"
        if "Topic:" in message:
            parts = message.split("Topic:")
            if len(parts) > 1:
                topic = parts[1].strip().rstrip(".")
        elif "topic:" in message.lower():
            parts = message.lower().split("topic:")
            if len(parts) > 1:
                topic = parts[1].strip().rstrip(".")
        
        from agent.tools import dispatch_tool
        from agent.tool_schemas import MEETING_BASE_ARGS
        args = {
            **MEETING_BASE_ARGS,
            "meeting_id": meeting_id,
            "topic": topic,
            "start_time": "15:00",
        }
        res = await dispatch_tool("update_meeting_preview", args, thread_id)
        return f"""I have prepared a preview to update your meeting:
Meeting ID: {meeting_id}
New Topic: {args['topic']}
New Time: {args['start_time']}

To finalize this update, please authorize me by clicking the link below:
<a href="{res['authorization_url']}" target="_blank">Authorize Update</a>
"""

    elif "schedule" in message.lower() or "meeting" in message.lower():
        # Dispatch schedule_meeting_preview tool to initiate OBO flow
        from agent.tools import dispatch_tool
        args = {
            "topic": "Test E2E Meeting",
            "date": "2026-05-22",
            "start_time": "14:00",
            "duration": "60",
            "time_zone": "America/Sao_Paulo"
        }
        res = await dispatch_tool("schedule_meeting_preview", args, thread_id)
        
        return f"""I have prepared a preview for your meeting:
Topic: {args['topic']}
Date: {args['date']}
Time: {args['start_time']}
Duration: {args['duration']} minutes

To finalize and book this meeting, please authorize me by clicking the link below:
<a href="{res['authorization_url']}" target="_blank">Authorize Meeting Booking</a>
"""
    
    return "Hi there! I am the Teamspace Meeting Assistant. Try saying 'Schedule a meeting for tomorrow at 2 PM'."


async def mock_run_agent_stream(message: str, thread_id: str, org_name: str, history: list[dict], gemini_api_key: str = "", custom_prompt: str = "", **kwargs):
    res = await mock_run_agent(message, thread_id, org_name, history, gemini_api_key, custom_prompt, **kwargs)
    chunk_size = 5
    for i in range(0, len(res), chunk_size):
        yield res[i:i+chunk_size]


@pytest.fixture(scope="module", autouse=True)
def run_isolated_servers():
    # Configure Business API
    test_db = "test_e2e.db"
    if os.path.exists(test_db):
        try:
            os.remove(test_db)
        except Exception:
            pass
            
    api_settings.DATABASE_URL = f"sqlite:///{test_db}"
    api_settings.ALLOWED_ORIGINS = ["http://127.0.0.1:5002"]
    api_settings.IS_BASE_URL = "http://127.0.0.1:9444"
    api_settings.IS_ORG_HANDLE = "teamspace"
    api_settings.TENANT_PATH = "/t/teamspace"
    
    # Configure Agent Service
    agent_settings.BUSINESS_API_URL = "http://127.0.0.1:9092"
    agent_settings.IS_BASE_URL = "http://127.0.0.1:9444"
    agent_settings.AGENT_REDIRECT_URI = "http://127.0.0.1:8002/callback"
    agent_settings.AGENT_SERVICE_URL = "http://127.0.0.1:8002"
    agent_settings.ALLOWED_ORIGINS = ["http://127.0.0.1:5002"]
    agent_settings.INTERNAL_SECRET = "test-internal-secret"
    agent_settings.IS_ORG_HANDLE = "teamspace"
    agent_settings.TENANT_PATH = "/t/teamspace"
    
    # Configure Flask App
    Config.BUSINESS_API_URL = "http://127.0.0.1:9092"
    Config.AGENT_SERVICE_URL = "http://127.0.0.1:8002"
    Config.IS_BASE_URL = "http://127.0.0.1:9444"
    Config.IS_ORG_HANDLE = "teamspace"
    Config.TENANT_PATH = "/t/teamspace"
    Config.OIDC_REDIRECT_URI = "http://127.0.0.1:5002/callback"
    Config.OIDC_POST_LOGOUT_URI = "http://127.0.0.1:5002"
    
    flask_app = create_app()
    flask_app.config.update({
        "BUSINESS_API_URL": "http://127.0.0.1:9092",
        "AGENT_SERVICE_URL": "http://127.0.0.1:8002",
        "AGENT_INTERNAL_SECRET": "test-internal-secret",
        "IS_BASE_URL": "http://127.0.0.1:9444",
        "IS_ORG_HANDLE": "teamspace",
        "TENANT_PATH": "/t/teamspace",
        "OIDC_REDIRECT_URI": "http://127.0.0.1:5002/callback",
        "OIDC_POST_LOGOUT_URI": "http://127.0.0.1:5002",
        "TESTING": True,
        "SECRET_KEY": "test-e2e-secret",
        "WTF_CSRF_ENABLED": False
    })
    
    # Initialize Business API tables
    ApiBase.metadata.create_all(bind=api_engine)

    # Start all 4 servers
    mock_is_thread = UvicornServer(mock_is, 9444)
    api_thread = UvicornServer(api_app, 9092)
    agent_thread = UvicornServer(agent_app, 8002)
    flask_thread = WerkzeugServer(flask_app, 5002)

    mock_is_thread.start()
    api_thread.start()
    agent_thread.start()
    flask_thread.start()

    # Wait for startup
    time.sleep(1.5)

    # Mock the Agent LLM runner
    patcher = patch("agent.main.run_agent", side_effect=mock_run_agent)
    patcher.start()
    patcher_stream = patch("agent.main.run_agent_stream", side_effect=mock_run_agent_stream)
    patcher_stream.start()

    yield

    # Stop LLM mock patcher
    patcher.stop()
    patcher_stream.stop()

    # Stop all servers
    mock_is_thread.shutdown()
    api_thread.shutdown()
    agent_thread.shutdown()
    flask_thread.shutdown()

    # Clean up DB
    if os.path.exists(test_db):
        try:
            os.remove(test_db)
        except Exception:
            pass


def test_complete_e2e_meeting_booking_flow(page: Page):
    from sqlalchemy.orm import Session
    from api.models import AgentConfig, Meeting
    
    with Session(api_engine) as db_session:
        db_session.query(Meeting).delete()
        db_session.commit()
        config = db_session.query(AgentConfig).filter_by(org="org-infinite-id").first()
        if not config:
            config = AgentConfig(
                org="org-infinite-id",
                agent_id="agent-81488",
                agent_secret="my-secret-123",
                display_name="Numba Agent",
                description="E2E Mocked Agent",
                gemini_api_key="mock-api-key",
                org_client_id="mock-client-id",
                org_client_secret="mock-client-secret"
            )
            db_session.add(config)
            db_session.commit()

    # Step 1: Visit the landing page
    page.goto("http://127.0.0.1:5002/")
    expect(page).to_have_title("Teamspace")
    
    # Click Login
    page.click("text=Sign In")
    
    # Step 2: Login via organization SSO flow
    expect(page.locator("h1")).to_contain_text("Sign in to Teamspace")
    page.fill("input[name='org_handle']", "numbainfinite")
    
    # Click Submit to initiate authorization redirect which auto-signs in through mock IS
    page.click("button[type='submit']")
    
    # Step 3: Verify we are on the dashboard
    expect(page).to_have_url("http://127.0.0.1:5002/o/numbainfinite/")
    expect(page.locator("h1")).to_contain_text("Welcome")
    expect(page.locator(".subtitle")).to_contain_text("Numba Infinite")
    expect(page.locator("body")).to_contain_text("Sign Out")
    
    # Step 4: Open the AI Assistant side panel
    expect(page.locator(".chat-toggle")).to_be_visible()
    page.click("text=AI Assistant")
    expect(page.locator(".chat-window")).to_be_visible()
    
    # Step 5: Ask to schedule a meeting
    page.fill(".chat-input input[name='message']", "Schedule a meeting for tomorrow")
    page.click(".chat-input button[type='submit']")
    
    # Wait for the AI preview response and authorize link
    expect(page.locator(".chat-messages")).to_contain_text("Authorize Meeting Booking")
    
    # Step 6: Simulate OBO authorization flow by clicking "Authorize Meeting Booking"
    # Wait for the popup window to open, complete the code exchange, and close itself
    with page.expect_popup() as popup_info:
        page.click("text=Authorize Meeting Booking")
    popup = popup_info.value
    try:
        popup.wait_for_load_state("networkidle", timeout=3000)
    except Exception:
        # The popup may close itself programmatically on successful authorization
        pass
    
    # Step 7: The popup's postMessage auto-sends "Authorized. Please check." via app.js
    # Wait for the confirmation response from the auto-callback
    expect(page.locator(".chat-messages")).to_contain_text("booked the meeting successfully")
    
    # Step 8: Verify meeting is saved in DB and rendered on Meetings page
    page.goto("http://127.0.0.1:5002/o/numbainfinite/meetings")
    expect(page.locator(".meeting-list")).to_contain_text("Test E2E Meeting")
    expect(page.locator(".meeting-list")).to_contain_text("14:00")


def test_list_update_delete_meeting_flow(page: Page):
    from sqlalchemy.orm import Session
    from api.models import AgentConfig, Meeting
    
    with Session(api_engine) as db_session:
        db_session.query(Meeting).delete()
        db_session.commit()
        config = db_session.query(AgentConfig).filter_by(org="org-infinite-id").first()
        if not config:
            config = AgentConfig(
                org="org-infinite-id",
                agent_id="agent-81488",
                agent_secret="my-secret-123",
                display_name="Numba Agent",
                description="E2E Mocked Agent",
                gemini_api_key="mock-api-key",
                org_client_id="mock-client-id",
                org_client_secret="mock-client-secret"
            )
            db_session.add(config)
            db_session.commit()

    # Step 1: Visit the landing page
    page.goto("http://127.0.0.1:5002/")
    expect(page).to_have_title("Teamspace")
    
    # Click Login
    page.click("text=Sign In")
    
    # Step 2: Login via organization SSO flow
    expect(page.locator("h1")).to_contain_text("Sign in to Teamspace")
    page.fill("input[name='org_handle']", "numbainfinite")
    
    # Click Submit to initiate authorization redirect which auto-signs in through mock IS
    page.click("button[type='submit']")
    
    # Step 3: Verify we are on the dashboard
    expect(page).to_have_url("http://127.0.0.1:5002/o/numbainfinite/")
    expect(page.locator("h1")).to_contain_text("Welcome")
    
    # Step 4: Open the AI Assistant side panel
    expect(page.locator(".chat-toggle")).to_be_visible()
    page.click("text=AI Assistant")
    expect(page.locator(".chat-window")).to_be_visible()
    
    # Step 5: Book a meeting first so we have one to list, update, and delete
    page.fill(".chat-input input[name='message']", "Schedule a meeting for tomorrow. Topic: Test E2E Meeting")
    page.click(".chat-input button[type='submit']")
    
    # Wait for the AI preview response and authorize link
    expect(page.locator(".chat-messages")).to_contain_text("Authorize Meeting Booking")
    
    # Click "Authorize Meeting Booking" in popup
    with page.expect_popup() as popup_info:
        page.click("text=Authorize Meeting Booking")
    popup = popup_info.value
    try:
        popup.wait_for_load_state("networkidle", timeout=3000)
    except Exception:
        pass
    
    # The popup's postMessage auto-sends "Authorized. Please check." via app.js
    # Wait for the confirmation response from the auto-callback
    expect(page.locator(".chat-messages")).to_contain_text("booked the meeting successfully")

    # Step 6: Ask the assistant to list the meetings
    page.fill(".chat-input input[name='message']", "Show my meetings")
    page.click(".chat-input button[type='submit']")
    expect(page.locator(".chat-messages")).to_contain_text("Authorize List")
    
    # Click "Authorize List" in popup
    with page.expect_popup() as popup_info:
        page.click("text=Authorize List")
    popup = popup_info.value
    try:
        popup.wait_for_load_state("networkidle", timeout=3000)
    except Exception:
        pass

    # The popup's postMessage auto-sends "Authorized. Please check." via app.js
    # Wait for the meeting list
    expect(page.locator(".chat-messages")).to_contain_text("Here are your scheduled meetings:")
    expect(page.locator(".chat-messages")).to_contain_text("Test E2E Meeting")
    
    # Parse the meeting ID from the messages
    messages_text = page.locator(".chat-messages").inner_text()
    import re
    match = re.search(r"ID:\s*([a-fA-F0-9\-]+)", messages_text)
    assert match is not None, "Failed to parse meeting ID from list response"
    meeting_id = match.group(1)
    
    # Step 7: Reschedule (update) this meeting
    page.fill(".chat-input input[name='message']", f"Reschedule meeting {meeting_id} to tomorrow at 15:00 Topic: Updated E2E Meeting")
    page.click(".chat-input button[type='submit']")
    
    # Wait for update preview
    expect(page.locator(".chat-messages")).to_contain_text("Authorize Update")
    
    # Click "Authorize Update" in popup
    with page.expect_popup() as popup_info:
        page.click("text=Authorize Update")
    popup = popup_info.value
    try:
        popup.wait_for_load_state("networkidle", timeout=3000)
    except Exception:
        pass

    # The popup's postMessage auto-sends "Authorized. Please check." via app.js
    # Wait for update confirmation
    expect(page.locator(".chat-messages")).to_contain_text("updated the meeting successfully")
    
    # Go to Meetings page to verify details and ensure NO DUPLICATES
    page.goto("http://127.0.0.1:5002/o/numbainfinite/meetings")
    # Verify ONLY one meeting card exists
    cards = page.locator(".meeting-card")
    expect(cards).to_have_count(1)
    expect(page.locator(".meeting-list")).to_contain_text("Updated E2E Meeting")
    expect(page.locator(".meeting-list")).to_contain_text("15:00")
    expect(page.locator(".meeting-list")).not_to_contain_text("14:00")
    
    # Step 8: Delete the meeting
    # Chat panel persists open via localStorage after navigation; ensure it's visible
    if not page.locator(".chat-window").is_visible():
        page.locator(".chat-toggle").click()
    expect(page.locator(".chat-window")).to_be_visible()
    
    page.fill(".chat-input input[name='message']", f"Delete meeting {meeting_id}")
    page.click(".chat-input button[type='submit']")
    
    # Wait for delete preview
    expect(page.locator(".chat-messages")).to_contain_text("Authorize Delete")
    
    # Click "Authorize Delete" in popup
    with page.expect_popup() as popup_info:
        page.click("text=Authorize Delete")
    popup = popup_info.value
    try:
        popup.wait_for_load_state("networkidle", timeout=3000)
    except Exception:
        pass

    # The popup's postMessage auto-sends "Authorized. Please check." via app.js
    # Wait for delete confirmation
    expect(page.locator(".chat-messages")).to_contain_text("successfully deleted")
    
    # Go to Meetings page to verify it's gone
    page.goto("http://127.0.0.1:5002/o/numbainfinite/meetings")
    expect(page.locator("body")).to_contain_text("No meetings scheduled yet")


@pytest.mark.parametrize("plan_id", [p["id"] for p in PLANS])
def test_signup_flow_for_all_plans(page: Page, plan_id: str):
    # Step 1: Visit the landing page
    page.goto("http://127.0.0.1:5002/")
    expect(page).to_have_title("Teamspace")
    
    # Click Get Started
    page.click("text=Get Started")
    
    # Step 2: Fill out organization details
    page.wait_for_selector("#org_name", timeout=10000)
    page.fill("#org_name", f"{plan_id.capitalize()} Test")
    page.fill("#org_handle", f"{plan_id}-test")
    page.fill("#first_name", "Test")
    page.fill("#last_name", "Admin")
    page.fill("#email", f"admin@{plan_id}.com")
    page.fill("#password", "Password123!")
    
    # Click Next
    page.click("button:has-text('Next')")
    
    # Step 3: Select the specific plan
    plan_name = next(p["name"] for p in PLANS if p["id"] == plan_id)
    page.click(f"label.plan-card:has-text('{plan_name}')")
    
    # Click Create Organization and intercept the POST request
    with page.expect_request("**/signup/") as request_info:
        page.click("button:has-text('Create Organization')")
    
    req = request_info.value
    assert req.method == "POST"
    post_data = req.post_data
    # Assert that the correct plan is actually present in the post data
    assert f"plan={plan_id}" in post_data


def test_unauthenticated_org_redirect(page: Page):
    # Accessing /o/numbainfinite/ when not logged in should redirect to /login?org_handle=numbainfinite,
    # which immediately logs the user in via SSO
    page.goto("http://127.0.0.1:5002/o/numbainfinite/")
    
    # It should auto-login and end up on the dashboard
    expect(page).to_have_url("http://127.0.0.1:5002/o/numbainfinite/")
    expect(page.locator("h1")).to_contain_text("Welcome")
    expect(page.locator(".subtitle")).to_contain_text("Numba Infinite")


def test_idp_upgrade_prompt_for_non_enterprise_user(page: Page):
    # Log in as default/Business user under numbainfinite
    page.goto("http://127.0.0.1:5002/")
    page.click("text=Sign In")
    
    expect(page.locator("h1")).to_contain_text("Sign in to Teamspace")
    page.fill("input[name='org_handle']", "numbainfinite")
    page.click("button[type='submit']")
    
    expect(page).to_have_url("http://127.0.0.1:5002/o/numbainfinite/")
    
    # Go directly to Identity Providers
    page.goto("http://127.0.0.1:5002/o/numbainfinite/admin/idp")
    
    # Verify the upgrade prompt is visible
    expect(page.locator(".upgrade-lock")).to_be_visible()
    expect(page.locator(".upgrade-lock h2")).to_contain_text("Upgrade Required")
    expect(page.locator("body")).to_contain_text("To bring your identity provider, upgrade your plan")

    # Go directly to Login Flow
    page.goto("http://127.0.0.1:5002/o/numbainfinite/admin/security/login-flow")
    
    # Verify the upgrade prompt is visible
    expect(page.locator(".upgrade-lock")).to_be_visible()
    expect(page.locator(".upgrade-lock h2")).to_contain_text("Upgrade Required")
    expect(page.locator("body")).to_contain_text("To configure custom authentication sequence steps")

    # Go directly to AI Agents
    page.goto("http://127.0.0.1:5002/o/numbainfinite/admin/agents/")
    
    # Verify the upgrade prompt is visible
    expect(page.locator(".upgrade-lock")).to_be_visible()
    expect(page.locator(".upgrade-lock h2")).to_contain_text("Upgrade Required")
    expect(page.locator("body")).to_contain_text("To deploy corporate AI agents")


def test_idp_allowed_for_enterprise_user(page: Page):
    # Log in as Enterprise user under enterprise-test
    page.goto("http://127.0.0.1:5002/")
    page.click("text=Sign In")
    
    expect(page.locator("h1")).to_contain_text("Sign in to Teamspace")
    page.fill("input[name='org_handle']", "enterprise-test")
    page.click("button[type='submit']")
    
    expect(page).to_have_url("http://127.0.0.1:5002/o/enterprise-test/")
    
    # Go to Identity Providers page via Security menu
    page.click("text=Security")
    page.click("text=Manage Identity Providers")
    
    # Verify we are on the Identity Providers page and the upgrade prompt is NOT visible
    expect(page).to_have_url("http://127.0.0.1:5002/o/enterprise-test/admin/idp")
    expect(page.locator(".upgrade-lock")).not_to_be_visible()
    expect(page.locator("h1")).to_contain_text("Identity Providers")

