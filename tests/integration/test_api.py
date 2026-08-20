import pytest
from api.auth import get_current_user, UserInfo
from api.main import app as api_app
from common.m2m_auth import SERVICE_SCOPE
from tests.helpers.tokens import issuer_for, patch_api_jwks, service_auth_header

# Define a standard mock user with full scopes
mock_user_claims = {
    "sub": "user-12345",
    "user_org": "numbainfinite",
    "email": "admin@numbainfinite.com",
    "scope": "create_meeting list_meetings view_meeting update_meeting delete_meeting create_basic_branding delete_branding view_agent_config manage_agent_config",
    "groups": ["admin"],
    "act": {"sub": "agent-8888"},
    "aut": "AGENT"
}

@pytest.fixture(autouse=True)
def mock_auth_dependency():
    def override_get_current_user():
        return UserInfo(mock_user_claims)
    
    api_app.dependency_overrides[get_current_user] = override_get_current_user
    yield
    api_app.dependency_overrides.pop(get_current_user, None)

def test_create_and_list_meetings(api_client):
    # Step 1: Create a meeting
    meeting_data = {
        "topic": "Integration Test Meeting",
        "date": "2026-05-25",
        "start_time": "15:00",
        "duration": "30",
        "time_zone": "America/Sao_Paulo"
    }
    resp = api_client.post("/meetings/", json=meeting_data)
    assert resp.status_code == 201
    created = resp.json()
    assert created["topic"] == "Integration Test Meeting"
    assert created["org"] == "numbainfinite"
    assert created["user_id"] == "user-12345"
    assert created["actor_user_id"] == "agent-8888"
    assert "id" in created

    # Step 2: List meetings and assert created meeting is present
    list_resp = api_client.get("/meetings/")
    assert list_resp.status_code == 200
    meetings = list_resp.json()
    assert len(meetings) >= 1
    assert any(m["id"] == created["id"] for m in meetings)

def test_get_update_delete_meeting(api_client):
    # Create
    meeting_data = {
        "topic": "To Be Deleted",
        "date": "2026-05-26",
        "start_time": "09:00",
        "duration": "60",
        "time_zone": "UTC"
    }
    created = api_client.post("/meetings/", json=meeting_data).json()
    meeting_id = created["id"]

    # Fetch
    get_resp = api_client.get(f"/meetings/{meeting_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["topic"] == "To Be Deleted"

    # Update
    updated_data = meeting_data.copy()
    updated_data["topic"] = "Updated Topic"
    update_resp = api_client.put(f"/meetings/{meeting_id}", json=updated_data)
    assert update_resp.status_code == 200
    assert update_resp.json()["topic"] == "Updated Topic"

    # Delete
    del_resp = api_client.delete(f"/meetings/{meeting_id}")
    assert del_resp.status_code == 204

    # Assert Not Found
    get_gone_resp = api_client.get(f"/meetings/{meeting_id}")
    assert get_gone_resp.status_code == 404

def test_personalization_endpoints(api_client):
    # Upsert personalization
    payload = {
        "org": "numbainfinite",
        "logo_url": "https://example.com/logo.png",
        "logo_alt_text": "Numba Infinite Logo",
        "primary_color": "#123456",
        "secondary_color": "#abcdef"
    }
    resp = api_client.post("/personalization/", json=payload)
    assert resp.status_code == 200
    assert resp.json()["org"] == "numbainfinite"
    assert resp.json()["primary_color"] == "#123456"

    # Fetch personalization
    get_resp = api_client.get("/personalization/org/numbainfinite")
    assert get_resp.status_code == 200
    assert get_resp.json()["logo_alt_text"] == "Numba Infinite Logo"

def test_agent_config_endpoints(api_client):
    # Create agent configuration
    payload = {
        "org": "numbainfinite",
        "agent_id": "agent-81488",
        "agent_secret": "mySecret123!",
        "display_name": "Demo Agent",
        "gemini_api_key": "dummy-key",
        "org_client_id": "clientId123",
        "custom_prompt": "Friendly assistant"
    }
    resp = api_client.post("/agent-config/", json=payload)
    assert resp.status_code == 201
    assert resp.json()["org"] == "numbainfinite"
    assert resp.json()["display_name"] == "Demo Agent"

    # Fetch agent configuration
    get_resp = api_client.get("/agent-config/org/numbainfinite")
    assert get_resp.status_code == 200
    assert get_resp.json()["agent_id"] == "agent-81488"


def test_plans_endpoints(api_client):
    # §2.3.4: unknown orgs now return 404 instead of synthesizing "basic"
    resp = api_client.get("/plans/org/numbainfinite")
    assert resp.status_code == 404

    # Upsert plan
    payload = {
        "org": "numbainfinite",
        "plan": "enterprise"
    }
    resp = api_client.post("/plans/", json=payload)
    assert resp.status_code == 200
    assert resp.json()["plan"] == "enterprise"

    # Fetch again to verify persistence
    resp = api_client.get("/plans/org/numbainfinite")
    assert resp.status_code == 200
    assert resp.json()["plan"] == "enterprise"

    # Mismatched org should raise 403
    mismatched_payload = {
        "org": "otherorg",
        "plan": "business"
    }
    resp = api_client.post("/plans/", json=mismatched_payload)
    assert resp.status_code == 403


def _set_user_scope(scope_string: str):
    def override():
        return UserInfo({
            **mock_user_claims,
            "scope": scope_string,
        })
    api_app.dependency_overrides[get_current_user] = override


def test_meetings_list_requires_list_meetings_scope(api_client):
    _set_user_scope("create_meeting view_meeting")
    resp = api_client.get("/meetings/")
    assert resp.status_code == 403
    assert "list_meetings" in resp.json()["detail"]


def test_agent_config_get_requires_view_agent_config_scope(api_client):
    _set_user_scope("create_meeting list_meetings")
    resp = api_client.get("/agent-config/org/numbainfinite")
    assert resp.status_code == 403
    assert "view_agent_config" in resp.json()["detail"]


def test_personalization_get_unauthenticated_returns_401_or_403(api_client):
    api_app.dependency_overrides.pop(get_current_user, None)
    try:
        resp = api_client.get("/personalization/org/numbainfinite")
        assert resp.status_code in (401, 403)
    finally:
        def restore():
            return UserInfo(mock_user_claims)
        api_app.dependency_overrides[get_current_user] = restore


def test_plans_get_unknown_org_returns_404(api_client):
    resp = api_client.get("/plans/org/this-org-does-not-exist-anywhere")
    assert resp.status_code == 404


def test_plans_get_known_org_returns_plan(api_client, db_session):
    from api.models import OrganizationPlan
    db_session.add(OrganizationPlan(org="known-org", plan="enterprise"))
    db_session.commit()
    resp = api_client.get("/plans/org/known-org")
    assert resp.status_code == 200
    assert resp.json()["plan"] == "enterprise"


def test_cross_tenant_isolation(api_client, db_session):
    assert 'from api.models import Meeting, AgentConfig, Personalization' != None
    from api.models import Meeting, AgentConfig, Personalization

    org_a_claims = {
        "sub": "user-org-a",
        "user_org": "org-a",
        "email": "a@org-a.com",
        "scope": "create_meeting list_meetings view_meeting update_meeting delete_meeting view_agent_config manage_agent_config",
        "groups": ["admin"],
        "act": {},
        "aut": "APPLICATION_USER",
    }
    org_b_claims = {
        "sub": "user-org-b",
        "user_org": "org-b",
        "email": "b@org-b.com",
        "scope": "create_meeting list_meetings view_meeting update_meeting delete_meeting view_agent_config manage_agent_config",
        "groups": ["admin"],
        "act": {},
        "aut": "APPLICATION_USER",
    }

    # Seed data as org_a
    api_app.dependency_overrides[get_current_user] = lambda: UserInfo(org_a_claims)
    db_session.add(Meeting(id="m-a1", org="org-a", topic="A Meeting", date="2024-01-01",
                           start_time="09:00", duration="30", time_zone="UTC", user_id="user-org-a"))
    db_session.add(AgentConfig(org="org-a", agent_id="ag-a1", agent_secret="s1",
                                display_name="Agent A"))
    db_session.add(Personalization(org="org-a", primary_color="#111111"))
    db_session.commit()

    # Switch to org_b
    api_app.dependency_overrides[get_current_user] = lambda: UserInfo(org_b_claims)

    # org_b GET /meetings returns empty (filtered by org, not 403)
    resp = api_client.get("/meetings/")
    assert resp.status_code == 200
    assert resp.json() == []

    # org_b GET /agent-config/org/org_a returns 403 (cross-org access)
    resp = api_client.get("/agent-config/org/org-a")
    assert resp.status_code == 403

    # org_b GET /personalization/org/org_a returns 403 (authenticated endpoint, cross-org)
    resp = api_client.get("/personalization/org/org-a")
    assert resp.status_code in (401, 403)

    # org_b DELETE org_a's meeting returns 404 (not found for org_b's scope)
    resp = api_client.delete("/meetings/m-a1")
    assert resp.status_code == 404

    # Restore
    api_app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Service-to-service auth on GET /agent-config/org/{org_id}
#
# The X-Internal-Secret shared secret was replaced by OAuth 2.0
# client-credentials tokens, so these exercise real RS256 tokens verified
# against a stubbed JWKS — the same path a live WSO2 IS token takes.
# ---------------------------------------------------------------------------

TEST_AUDIENCE = "test-client-id"


@pytest.fixture
def service_auth(monkeypatch):
    """Mint valid `X-Service-Authorization` headers for the Business API."""
    from api.config import settings

    monkeypatch.setattr(settings, "CLIENT_ID", TEST_AUDIENCE)
    issuer = issuer_for(settings.IS_BASE_URL, settings.TENANT_PATH)

    def _headers(**overrides):
        overrides.setdefault("audience", TEST_AUDIENCE)
        overrides.setdefault("issuer", issuer)
        return service_auth_header(**overrides)

    with patch_api_jwks():
        yield _headers


@pytest.fixture
def no_user_override():
    """Drop the autouse user override so only M2M auth is in play."""
    api_app.dependency_overrides.pop(get_current_user, None)
    yield
    api_app.dependency_overrides[get_current_user] = lambda: UserInfo(mock_user_claims)


def _seed_agent_config(db_session, org: str, agent_id: str):
    from api.models import AgentConfig

    db_session.add(
        AgentConfig(
            org=org,
            agent_id=agent_id,
            agent_secret="m2m-secret",
            display_name="M2M Agent",
            gemini_api_key="k",
            org_client_id="c",
        )
    )
    db_session.commit()


def test_agent_config_get_via_service_token(api_client, db_session, service_auth):
    _seed_agent_config(db_session, "numbainfinite", "agent-m2m-001")

    resp = api_client.get("/agent-config/org/numbainfinite", headers=service_auth())
    assert resp.status_code == 200
    assert resp.json()["agent_id"] == "agent-m2m-001"


def test_agent_config_get_with_no_auth_returns_401(api_client, no_user_override):
    resp = api_client.get("/agent-config/org/numbainfinite")
    assert resp.status_code == 401
    assert "X-Service-Authorization" in resp.json()["detail"]


def test_agent_config_get_with_unverifiable_token_returns_401(
    api_client, service_auth, no_user_override
):
    # A syntactically plausible token that this JWKS cannot verify. Under the
    # old scheme any attacker-chosen string was compared to a static secret;
    # now the signature itself is the gate.
    resp = api_client.get(
        "/agent-config/org/numbainfinite",
        headers={"X-Service-Authorization": "Bearer not-a-real-jwt"},
    )
    assert resp.status_code == 401


def test_agent_config_get_with_expired_service_token_returns_401(
    api_client, service_auth, no_user_override
):
    # The central improvement over the shared secret: these credentials expire.
    resp = api_client.get(
        "/agent-config/org/numbainfinite",
        headers=service_auth(expires_in=-60),
    )
    assert resp.status_code == 401
    assert "expired" in resp.json()["detail"].lower()


def test_agent_config_get_with_wrong_audience_returns_401(
    api_client, service_auth, no_user_override
):
    resp = api_client.get(
        "/agent-config/org/numbainfinite",
        headers=service_auth(audience="some-other-client"),
    )
    assert resp.status_code == 401


def test_agent_config_get_with_wrong_issuer_returns_401(
    api_client, service_auth, no_user_override
):
    resp = api_client.get(
        "/agent-config/org/numbainfinite",
        headers=service_auth(issuer="https://evil.example.com/oauth2/token"),
    )
    assert resp.status_code == 401


def test_agent_config_get_without_service_scope_returns_403(
    api_client, service_auth, no_user_override
):
    resp = api_client.get(
        "/agent-config/org/numbainfinite",
        headers=service_auth(scope="list_meetings create_meeting"),
    )
    assert resp.status_code == 403
    assert SERVICE_SCOPE in resp.json()["detail"]


def test_agent_config_get_rejects_user_token_in_service_header(
    api_client, service_auth, no_user_override
):
    # Defence in depth: even if a user token somehow carried the scope, its
    # `aut=APPLICATION_USER` marks it as not an application credential.
    resp = api_client.get(
        "/agent-config/org/numbainfinite",
        headers=service_auth(aut="APPLICATION_USER"),
    )
    assert resp.status_code == 403
    assert "aut=" in resp.json()["detail"]


def test_agent_config_get_service_token_with_user_jwt_audit(
    api_client, db_session, service_auth
):
    """A service token is the gate; the user JWT is only recorded for audit.

    This is the chat path: a non-admin without `view_agent_config` must still
    be able to start an OBO flow.
    """
    _seed_agent_config(db_session, "numbainfinite", "agent-m2m-audit")

    no_scope_claims = {
        **mock_user_claims,
        "scope": "create_meeting list_meetings view_meeting",
    }
    api_app.dependency_overrides[get_current_user] = lambda: UserInfo(no_scope_claims)
    try:
        resp = api_client.get("/agent-config/org/numbainfinite", headers=service_auth())
        assert resp.status_code == 200
        assert resp.json()["agent_id"] == "agent-m2m-audit"
    finally:
        api_app.dependency_overrides[get_current_user] = lambda: UserInfo(mock_user_claims)


def test_agent_config_get_service_token_to_different_org_allowed(
    api_client, db_session, service_auth, no_user_override
):
    # A trusted service is trusted to name the org it is serving — the same
    # trust model as before, now on a short-lived verifiable credential.
    _seed_agent_config(db_session, "other-org", "agent-other")

    resp = api_client.get("/agent-config/org/other-org", headers=service_auth())
    assert resp.status_code == 200
    assert resp.json()["agent_id"] == "agent-other"
