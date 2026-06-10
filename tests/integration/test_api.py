import pytest
from api.auth import get_current_user, UserInfo
from api.main import app as api_app

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


def test_agent_config_get_via_internal_secret_m2m(api_client, db_session, monkeypatch):
    from api.models import AgentConfig
    from api.config import settings

    db_session.add(
        AgentConfig(
            org="numbainfinite",
            agent_id="agent-m2m-001",
            agent_secret="m2m-secret",
            display_name="M2M Agent",
            gemini_api_key="k",
            org_client_id="c",
        )
    )
    db_session.commit()

    monkeypatch.setattr(settings, "INTERNAL_SECRET", "test-shared-secret")

    resp = api_client.get(
        "/agent-config/org/numbainfinite",
        headers={"X-Internal-Secret": "test-shared-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["agent_id"] == "agent-m2m-001"


def test_agent_config_get_with_wrong_internal_secret_returns_401(api_client, monkeypatch):
    from api.config import settings

    api_app.dependency_overrides.pop(get_current_user, None)
    monkeypatch.setattr(settings, "INTERNAL_SECRET", "test-shared-secret")
    try:
        resp = api_client.get(
            "/agent-config/org/numbainfinite",
            headers={"X-Internal-Secret": "wrong-secret"},
        )
        assert resp.status_code == 401
    finally:
        def restore():
            return UserInfo(mock_user_claims)
        api_app.dependency_overrides[get_current_user] = restore


def test_agent_config_get_with_no_auth_returns_401(api_client, monkeypatch):
    from api.config import settings

    api_app.dependency_overrides.pop(get_current_user, None)
    monkeypatch.setattr(settings, "INTERNAL_SECRET", "test-shared-secret")
    try:
        resp = api_client.get("/agent-config/org/numbainfinite")
        assert resp.status_code == 401
    finally:
        def restore():
            return UserInfo(mock_user_claims)
        api_app.dependency_overrides[get_current_user] = restore


def test_agent_config_get_internal_secret_with_user_jwt_audit(api_client, db_session, monkeypatch):
    from api.config import settings
    from api.models import AgentConfig

    db_session.add(
        AgentConfig(
            org="numbainfinite",
            agent_id="agent-m2m-audit",
            agent_secret="audit-secret",
            display_name="Audit Agent",
            gemini_api_key="k",
            org_client_id="c",
        )
    )
    db_session.commit()

    monkeypatch.setattr(settings, "INTERNAL_SECRET", "test-shared-secret")

    no_scope_claims = {
        **mock_user_claims,
        "scope": "create_meeting list_meetings view_meeting",
    }

    def override_user():
        return UserInfo(no_scope_claims)
    api_app.dependency_overrides[get_current_user] = override_user
    try:
        resp = api_client.get(
            "/agent-config/org/numbainfinite",
            headers={"X-Internal-Secret": "test-shared-secret"},
        )
        assert resp.status_code == 200
        assert resp.json()["agent_id"] == "agent-m2m-audit"
    finally:
        def restore():
            return UserInfo(mock_user_claims)
        api_app.dependency_overrides[get_current_user] = restore


def test_agent_config_get_internal_secret_to_different_org_allowed(api_client, db_session, monkeypatch):
    from api.config import settings
    from api.models import AgentConfig

    db_session.add(
        AgentConfig(
            org="other-org",
            agent_id="agent-other",
            agent_secret="other-secret",
            display_name="Other Agent",
            gemini_api_key="k",
            org_client_id="c",
        )
    )
    db_session.commit()

    monkeypatch.setattr(settings, "INTERNAL_SECRET", "test-shared-secret")

    api_app.dependency_overrides.pop(get_current_user, None)
    try:
        resp = api_client.get(
            "/agent-config/org/other-org",
            headers={"X-Internal-Secret": "test-shared-secret"},
        )
        assert resp.status_code == 200
        assert resp.json()["agent_id"] == "agent-other"
    finally:
        def restore():
            return UserInfo(mock_user_claims)
        api_app.dependency_overrides[get_current_user] = restore

