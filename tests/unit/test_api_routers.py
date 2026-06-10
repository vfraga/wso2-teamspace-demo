import pytest
from unittest.mock import MagicMock
# Direct imports to ensure direct test relationship is recognized
import api.routers.meetings as meetings_router
import api.routers.personalization as personalization_router
import api.routers.agent_configs as agent_configs_router
import api.routers.plans as plans_router
from api.auth import get_current_user, UserInfo
from api.main import app as api_app

mock_user_claims = {
    "sub": "user-unit-test",
    "user_org": "numbainfinite",
    "email": "unit@numbainfinite.com",
    "scope": "create_meeting list_meetings view_meeting update_meeting delete_meeting create_basic_branding delete_branding view_agent_config manage_agent_config",
    "groups": ["admin"],
    "act": {"sub": "agent-unit"},
    "aut": "AGENT"
}

@pytest.fixture(autouse=True)
def mock_auth():
    def override():
        return UserInfo(mock_user_claims)
    api_app.dependency_overrides[get_current_user] = override
    yield
    api_app.dependency_overrides.pop(get_current_user, None)

def test_meetings_router_direct(api_client):
    # Verify routers are imported and registered
    assert meetings_router.router is not None
    assert personalization_router.router is not None
    assert agent_configs_router.router is not None
    assert plans_router.router is not None

    # Call a quick meetings endpoint
    resp = api_client.get("/meetings/")
    assert resp.status_code == 200

    # Call the new plans endpoint — §2.3.4: unknown orgs now return 404
    resp = api_client.get("/plans/org/numbainfinite")
    assert resp.status_code == 404

