import pytest
import requests
from unittest.mock import patch, MagicMock

@pytest.fixture
def logged_in_session(flask_app):
    # Setup standard logged in user session
    return {
        "user": {
            "sub": "user-12345",
            "email": "testuser@numbainfinite.com",
            "name": "Test User",
            "org_id": "org-infinite-id",
            "org_name": "Numba Infinite",
            "org_handle": "numbainfinite",
            "roles": [],
            "groups": []
        },
        "is_admin": False,
        "user_roles": [],
        "user_scopes": ["openid", "email"],
        "chat_thread_id": "mock-thread-xyz",
        "access_token": "mock-access-token",
        "access_token_claims": {"sub": "user-12345", "scope": "openid email"}
    }

@pytest.fixture
def admin_session(flask_app):
    # Setup admin logged in session
    return {
        "user": {
            "sub": "admin-12345",
            "email": "admin@numbainfinite.com",
            "name": "Admin User",
            "org_id": "org-infinite-id",
            "org_name": "Numba Infinite",
            "org_handle": "numbainfinite",
            "roles": ["teamspace-admin"],
            "groups": ["admin"]
        },
        "is_admin": True,
        "user_roles": ["teamspace-admin"],
        "user_scopes": ["openid", "email", "create_meeting", "list_meetings", "internal_org_user_mgt_create", "view_agent_config", "manage_agent_config"],
        "chat_thread_id": "mock-thread-xyz",
        "access_token": "mock-access-token",
        "access_token_claims": {"sub": "admin-12345", "scope": "openid email create_meeting list_meetings view_agent_config manage_agent_config"}
    }

def test_landing_page(flask_client):
    resp = flask_client.get("/")
    assert resp.status_code == 200
    assert b"Welcome to Teamspace" in resp.data or b"Teamspace" in resp.data

def test_login_page(flask_client):
    resp = flask_client.get("/login")
    assert resp.status_code == 200
    assert b"Sign In" in resp.data or b"Organization" in resp.data

def test_dashboard_unauthenticated(flask_client):
    # Unauthenticated users should be redirected to login
    resp = flask_client.get("/o/numbainfinite/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]

@patch("webapp.blueprints.dashboard.get_personalization")
def test_dashboard_authenticated(mock_get_personalization, flask_client, logged_in_session):
    mock_get_personalization.return_value = None
    with flask_client.session_transaction() as sess:
        sess.update(logged_in_session)
        
    resp = flask_client.get("/o/numbainfinite/")
    assert resp.status_code == 200
    assert b"Numba Infinite" in resp.data
    assert b"Sign Out" in resp.data

@patch("webapp.blueprints.dashboard.get_personalization")
def test_chat_page_authenticated(mock_get_personalization, flask_client, logged_in_session):
    mock_get_personalization.return_value = None
    with flask_client.session_transaction() as sess:
        sess.update(logged_in_session)
        
    resp = flask_client.get("/o/numbainfinite/")
    assert resp.status_code == 200
    assert b"AI Assistant" in resp.data
    assert b"Teamspace Assistant" in resp.data

def test_admin_agents_unauthorized(flask_client, logged_in_session):
    unauthorized_session = logged_in_session.copy()
    unauthorized_session["user_roles"] = ["idp-manager"]
    with flask_client.session_transaction() as sess:
        sess.update(unauthorized_session)
        
    # Standard user has no Teamspace Admin role
    resp = flask_client.get("/o/numbainfinite/admin/agents/")
    # Returns 403 Forbidden or redirects
    assert resp.status_code in (403, 302)


@patch("webapp.blueprints.agents._check_plan_and_role", return_value=None)
@patch("webapp.blueprints.agents.get_agent_config")
def test_admin_agents_authorized(mock_get_cfg, mock_check_plan, flask_client, admin_session):
    mock_get_cfg.return_value = {
        "org": "numbainfinite",
        "agent_id": "agent-81488",
        "agent_secret": "my-secret-123",
        "display_name": "Numba Agent",
        "gemini_api_key": "some-key",
        "org_client_id": "org-client-id-xyz",
        "custom_prompt": "",
        "created_at": "2026-05-21T01:51:49Z"
    }

    with flask_client.session_transaction() as sess:
        sess.update(admin_session)

    resp = flask_client.get("/o/numbainfinite/admin/agents/")
    assert resp.status_code == 200
    assert b"Numba Agent" in resp.data
    assert b"agent-81488" in resp.data



def test_dashboard_unauthenticated_with_org_handle(flask_client):
    resp = flask_client.get("/o/numbainfinite/")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/login?org_handle=numbainfinite"


def test_idp_unauthorized_standard_user(flask_client, logged_in_session):
    with flask_client.session_transaction() as sess:
        sess.update(logged_in_session)
    resp = flask_client.get("/o/numbainfinite/admin/idp")
    assert resp.status_code == 403


def test_idp_upgrade_prompt_for_admin_without_idp_manager(flask_client, admin_session):
    with flask_client.session_transaction() as sess:
        sess.update(admin_session)
    resp = flask_client.get("/o/numbainfinite/admin/idp")
    assert resp.status_code == 200
    assert b"Upgrade Required" in resp.data
    assert b"To bring your identity provider, upgrade your plan" in resp.data


@patch("webapp.blueprints.admin.ISClient.call")
def test_idp_allowed_for_enterprise_admin(mock_call, flask_client, admin_session):
    enterprise_session = admin_session.copy()
    enterprise_session["user_roles"] = admin_session["user_roles"] + ["idp-manager"]
    with flask_client.session_transaction() as sess:
        sess.update(enterprise_session)
    
    mock_call.return_value = {
        "status_code": 200,
        "data": {
            "identityProviders": [
                {"id": "idp-1", "name": "Mock WSO2IS", "isEnabled": True}
            ]
        },
        "debug": []
    }
    
    resp = flask_client.get("/o/numbainfinite/admin/idp")
    assert resp.status_code == 200
    assert b"Upgrade Required" not in resp.data
    assert b"Mock WSO2IS" in resp.data


def test_idp_edit_unauthorized_standard_user(flask_client, logged_in_session):
    with flask_client.session_transaction() as sess:
        sess.update(logged_in_session)
    resp = flask_client.get("/o/numbainfinite/admin/idp/idp-123/edit")
    assert resp.status_code == 403


def test_idp_edit_upgrade_prompt_for_admin_without_idp_manager(flask_client, admin_session):
    with flask_client.session_transaction() as sess:
        sess.update(admin_session)
    resp = flask_client.get("/o/numbainfinite/admin/idp/idp-123/edit")
    assert resp.status_code == 200
    assert b"Upgrade Required" in resp.data


@patch("webapp.blueprints.admin.ISClient.call")
def test_idp_edit_form_render_authorized(mock_call, flask_client, admin_session):
    enterprise_session = admin_session.copy()
    enterprise_session["user_roles"] = admin_session["user_roles"] + ["idp-manager"]
    with flask_client.session_transaction() as sess:
        sess.update(enterprise_session)

    def mock_call_side_effect(method, path, token, json=None):
        if method == "GET" and path.endswith("/identity-providers/idp-123"):
            return {
                "status_code": 200,
                "data": {
                    "id": "idp-123",
                    "name": "Mock Secondary IS",
                    "federatedAuthenticators": {
                        "authenticators": [{
                            "authenticatorId": "T3BlbklEQ29ubmVjdEF1dGhlbnRpY2F0b3I",
                            "isEnabled": True
                        }]
                    },
                    "provisioning": {
                        "jit": {
                            "isEnabled": True
                        }
                    },
                    "groups": [
                        {"id": "group-id-admin", "name": "admin"},
                        {"id": "group-id-user", "name": "user"}
                    ]
                },
                "debug": []
            }
        elif method == "GET" and path.endswith("/identity-providers/idp-123/federated-authenticators/T3BlbklEQ29ubmVjdEF1dGhlbnRpY2F0b3I"):
            return {
                "status_code": 200,
                "data": {
                    "properties": [
                        {"key": "ClientId", "value": "client-id-abc"},
                        {"key": "OAuth2AuthzEPUrl", "value": "https://localhost:9444/oauth2/authorize"},
                        {"key": "OAuth2TokenEPUrl", "value": "https://localhost:9444/oauth2/token"},
                    ]
                },
                "debug": []
            }
        elif method == "GET" and path.endswith("/identity-providers/idp-123/claims"):
            return {
                "status_code": 200,
                "data": {
                    "mappings": [
                        {
                            "idpClaim": "groups",
                            "localClaim": {"uri": "http://wso2.org/claims/groups"}
                        }
                    ]
                },
                "debug": []
            }
        elif method == "GET" and path.endswith("/scim2/v2/Roles"):
            return {
                "status_code": 200,
                "data": {
                    "Resources": [
                        {"id": "role-admin-id", "displayName": "teamspace-admin"},
                        {"id": "role-user-id", "displayName": "teamspace-user"}
                    ]
                },
                "debug": []
            }
        elif method == "GET" and path.endswith("/scim2/v2/Roles/role-admin-id"):
            return {
                "status_code": 200,
                "data": {
                    "id": "role-admin-id",
                    "displayName": "teamspace-admin",
                    "groups": [
                        {"value": "group-id-admin"}
                    ]
                },
                "debug": []
            }
        elif method == "GET" and path.endswith("/scim2/v2/Roles/role-user-id"):
            return {
                "status_code": 200,
                "data": {
                    "id": "role-user-id",
                    "displayName": "teamspace-user",
                    "groups": [
                        {"value": "group-id-user"}
                    ]
                },
                "debug": []
            }
        return {"status_code": 404, "data": None, "debug": []}

    mock_call.side_effect = mock_call_side_effect

    resp = flask_client.get("/o/numbainfinite/admin/idp/idp-123/edit")
    assert resp.status_code == 200
    assert b"Edit Identity Provider" in resp.data
    assert b"client-id-abc" in resp.data
    assert b"Mock Secondary IS" in resp.data


@patch("webapp.blueprints.admin.ISClient.call")
def test_idp_edit_submit_authorized(mock_call, flask_client, admin_session):
    enterprise_session = admin_session.copy()
    enterprise_session["user_roles"] = admin_session["user_roles"] + ["idp-manager"]
    with flask_client.session_transaction() as sess:
        sess.update(enterprise_session)

    calls = []
    def mock_call_side_effect(method, path, token, json=None):
        calls.append((method, path, json))
        if method == "GET" and path.endswith("/identity-providers/idp-123"):
            return {
                "status_code": 200,
                "data": {
                    "id": "idp-123",
                    "name": "Mock Secondary IS",
                    "federatedAuthenticators": {
                        "authenticators": [{
                            "authenticatorId": "T3BlbklEQ29ubmVjdEF1dGhlbnRpY2F0b3I",
                            "isEnabled": True
                        }]
                    },
                    "groups": [
                        {"id": "group-id-admin", "name": "admin"},
                        {"id": "group-id-user", "name": "user"}
                    ]
                },
                "debug": []
            }
        elif method == "GET" and path.endswith("/identity-providers/idp-123/federated-authenticators/T3BlbklEQ29ubmVjdEF1dGhlbnRpY2F0b3I"):
            return {
                "status_code": 200,
                "data": {
                    "properties": [
                        {"key": "ClientId", "value": "client-id-abc"},
                        {"key": "ClientSecret", "value": "old-secret"},
                        {"key": "OAuth2AuthzEPUrl", "value": "https://localhost:9444/oauth2/authorize"},
                        {"key": "OAuth2TokenEPUrl", "value": "https://localhost:9444/oauth2/token"},
                    ]
                },
                "debug": []
            }
        elif method == "PATCH" and path.endswith("/identity-providers/idp-123"):
            return {"status_code": 204, "data": None, "debug": []}
        elif method == "PUT" and "/federated-authenticators/" in path:
            return {"status_code": 204, "data": None, "debug": []}
        elif method == "PUT" and "/provisioning/jit" in path:
            return {"status_code": 204, "data": None, "debug": []}
        elif method == "PUT" and path.endswith("/claims"):
            return {"status_code": 204, "data": None, "debug": []}
        elif method == "PUT" and path.endswith("/groups"):
            return {
                "status_code": 200,
                "data": [
                    {"id": "group-id-admin", "name": "admin"},
                    {"id": "group-id-user", "name": "user"},
                    {"id": "group-id-manager", "name": "manager"}
                ],
                "debug": []
            }
        elif method == "GET" and path.endswith("/scim2/v2/Roles"):
            return {
                "status_code": 200,
                "data": {
                    "Resources": [
                        {"id": "role-admin-id", "displayName": "teamspace-admin"},
                        {"id": "role-user-id", "displayName": "teamspace-user"}
                    ]
                },
                "debug": []
            }
        elif method == "GET" and path.endswith("/scim2/v2/Roles/role-admin-id"):
            return {
                "status_code": 200,
                "data": {
                    "id": "role-admin-id",
                    "displayName": "teamspace-admin",
                    "groups": [
                        {"value": "group-id-admin"},
                        {"value": "some-other-non-idp-group"}
                    ]
                },
                "debug": []
            }
        elif method == "GET" and path.endswith("/scim2/v2/Roles/role-user-id"):
            return {
                "status_code": 200,
                "data": {
                    "id": "role-user-id",
                    "displayName": "teamspace-user",
                    "groups": []
                },
                "debug": []
            }
        elif method == "PATCH" and "/scim2/v2/Roles/" in path:
            return {"status_code": 204, "data": None, "debug": []}
        return {"status_code": 404, "data": None, "debug": []}

    mock_call.side_effect = mock_call_side_effect

    form_data = {
        "name": "Updated Mock IdP",
        "client_id": "new-client-id",
        "client_secret": "new-client-secret",
        "auth_endpoint": "https://localhost:9444/oauth2/authorize",
        "token_endpoint": "https://localhost:9444/oauth2/token",
        "jit_enabled": "true",
        "group_attribute": "groups",
        "groups": "admin, user, manager",
        "role_groups[role-admin-id]": ["admin", "manager"],
        "role_groups[role-user-id]": ["user"]
    }

    resp = flask_client.post(
        "/o/numbainfinite/admin/idp/idp-123/edit",
        data=form_data
    )

    assert resp.status_code == 302
    assert "/admin/idp" in resp.headers["Location"]

    patch_paths = [
        c[1] for c in calls
        if c[0] == "PATCH" and "/scim2/v2/Roles/" in c[1]
    ]
    assert any(p.endswith("/scim2/v2/Roles/role-admin-id") for p in patch_paths)
    assert any(p.endswith("/scim2/v2/Roles/role-user-id") for p in patch_paths)

    federated_put = next(
        c for c in calls
        if c[0] == "PUT" and "/federated-authenticators/" in c[1]
    )
    assert federated_put[2] == {
        "name": "OpenIDConnectAuthenticator",
        "isEnabled": True,
        "definedBy": "SYSTEM",
        "isDefault": True,
        "properties": federated_put[2]["properties"],
    }

    jit_put = next(
        c for c in calls
        if c[0] == "PUT" and c[1].endswith("/provisioning/jit")
    )
    assert jit_put[2] == {
        "accountLookupAttributeMappings": [],
        "associateLocalUser": True,
        "attributeSyncMethod": "PRESERVE_LOCAL",
        "isEnabled": True,
        "scheme": "PROVISION_SILENTLY",
        "skipJITForLookupFailure": False,
        "userstore": "PRIMARY",
    }


@patch("webapp.blueprints.admin.ISClient.call")
def test_idp_delete_authorized(mock_call, flask_client, admin_session):
    enterprise_session = admin_session.copy()
    enterprise_session["user_roles"] = admin_session["user_roles"] + ["idp-manager"]
    with flask_client.session_transaction() as sess:
        sess.update(enterprise_session)

    calls = []
    def mock_call_side_effect(method, path, token, json=None):
        calls.append((method, path, json))
        if method == "GET" and path.endswith("/identity-providers/idp-123"):
            return {
                "status_code": 200,
                "data": {"id": "idp-123", "name": "Mock Secondary IS"},
                "debug": []
            }
        elif method == "DELETE" and path.endswith("/identity-providers/idp-123"):
            return {"status_code": 204, "data": None, "debug": []}
        elif method == "GET" and path.endswith("/applications"):
            return {
                "status_code": 200,
                "data": {
                    "applications": [
                        {"id": "app-xyz", "name": "Teamspace"}
                    ]
                },
                "debug": []
            }
        elif method == "GET" and path.endswith("/applications/app-xyz"):
            return {
                "status_code": 200,
                "data": {
                    "id": "app-xyz",
                    "name": "Teamspace",
                    "authenticationSequence": {
                        "type": "USER_DEFINED",
                        "steps": [
                            {
                                "id": 1,
                                "options": [
                                    {"idp": "LOCAL", "authenticator": "BasicAuthenticator"},
                                    {"idp": "Mock Secondary IS", "authenticator": "OpenIDConnectAuthenticator"}
                                ]
                            }
                        ]
                    }
                },
                "debug": []
            }
        elif method == "PATCH" and path.endswith("/applications/app-xyz"):
            return {"status_code": 204, "data": None, "debug": []}
        return {"status_code": 404, "data": None, "debug": []}

    mock_call.side_effect = mock_call_side_effect

    resp = flask_client.post("/o/numbainfinite/admin/idp/idp-123/delete")
    assert resp.status_code == 302
    assert "/admin/idp" in resp.headers["Location"]

    delete_call = next((c for c in calls if c[0] == "DELETE" and c[1].endswith("/identity-providers/idp-123")), None)
    assert delete_call is not None

    patch_app_call = next((c for c in calls if c[0] == "PATCH" and c[1].endswith("/applications/app-xyz")), None)
    assert patch_app_call is not None
    assert patch_app_call[2]["authenticationSequence"]["type"] == "DEFAULT"


@patch("webapp.blueprints.admin.ISClient.call")
def test_idp_add_form_render_authorized(mock_call, flask_client, admin_session):
    enterprise_session = admin_session.copy()
    enterprise_session["user_roles"] = admin_session["user_roles"] + ["idp-manager"]
    with flask_client.session_transaction() as sess:
        sess.update(enterprise_session)

    def mock_call_side_effect(method, path, token, json=None):
        if method == "GET" and path.endswith("/scim2/v2/Roles"):
            return {
                "status_code": 200,
                "data": {
                    "Resources": [
                        {"id": "role-admin-id", "displayName": "teamspace-admin"},
                        {"id": "role-user-id", "displayName": "teamspace-user"}
                    ]
                },
                "debug": {}
            }
        return {"status_code": 404, "data": None, "debug": {}}

    mock_call.side_effect = mock_call_side_effect

    resp = flask_client.get("/o/numbainfinite/admin/idp/add")
    assert resp.status_code == 200
    assert b"Add Identity Provider" in resp.data
    assert b"teamspace-admin" in resp.data
    assert b"teamspace-user" in resp.data


@patch("webapp.blueprints.admin.ISClient.call")
def test_idp_add_submit_authorized(mock_call, flask_client, admin_session):
    enterprise_session = admin_session.copy()
    enterprise_session["user_roles"] = admin_session["user_roles"] + ["idp-manager"]
    with flask_client.session_transaction() as sess:
        sess.update(enterprise_session)

    calls = []
    def mock_call_side_effect(method, path, token, json=None):
        calls.append((method, path, json))
        if method == "GET" and "/scim2/v2/Roles?filter=" in path:
            # Simulate role checks returning application-audience roles
            role_name = "teamspace-admin" if "teamspace-admin" in path else "teamspace-user"
            return {
                "status_code": 200,
                "data": {
                    "Resources": [
                        {
                            "id": f"role-{role_name}-id",
                            "displayName": role_name,
                            "audience": {"type": "application", "value": "app-xyz"}
                        }
                    ]
                },
                "debug": {}
            }
        elif method == "POST" and path.endswith("/identity-providers"):
            return {
                "status_code": 201,
                "data": {"id": "new-idp-id"},
                "debug": {}
            }
        elif method == "PUT" and "/provisioning/jit" in path:
            return {"status_code": 204, "data": None, "debug": {}}
        elif method == "PUT" and path.endswith("/groups"):
            return {
                "status_code": 200,
                "data": [
                    {"id": "new-group-id-admin", "name": "admin"},
                    {"id": "new-group-id-user", "name": "user"}
                ],
                "debug": {}
            }
        elif method == "GET" and path.endswith("/scim2/v2/Roles"):
            return {
                "status_code": 200,
                "data": {
                    "Resources": [
                        {"id": "role-admin-id", "displayName": "teamspace-admin"},
                        {"id": "role-user-id", "displayName": "teamspace-user"}
                    ]
                },
                "debug": {}
            }
        elif method == "GET" and path.endswith("/scim2/v2/Roles/role-admin-id"):
            return {
                "status_code": 200,
                "data": {
                    "id": "role-admin-id",
                    "displayName": "teamspace-admin",
                    "groups": []
                },
                "debug": {}
            }
        elif method == "GET" and path.endswith("/scim2/v2/Roles/role-user-id"):
            return {
                "status_code": 200,
                "data": {
                    "id": "role-user-id",
                    "displayName": "teamspace-user",
                    "groups": []
                },
                "debug": {}
            }
        elif method == "PATCH" and "/scim2/v2/Roles/" in path:
            return {"status_code": 204, "data": None, "debug": {}}
        elif method == "GET" and path.endswith("/applications"):
            return {
                "status_code": 200,
                "data": {
                    "applications": [
                        {"id": "app-xyz", "name": "Teamspace"}
                    ]
                },
                "debug": {}
            }
        elif method == "PATCH" and path.endswith("/applications/app-xyz"):
            return {"status_code": 204, "data": None, "debug": {}}
        return {"status_code": 404, "data": None, "debug": {}}

    mock_call.side_effect = mock_call_side_effect

    form_data = {
        "name": "Corporate-IDP",
        "client_id": "client-id-123",
        "client_secret": "client-secret-123",
        "auth_endpoint": "https://localhost:9444/oauth2/authorize",
        "token_endpoint": "https://localhost:9444/oauth2/token",
        "jit_enabled": "true",
        "group_attribute": "groups",
        "groups": "admin, user",
        "role_groups[role-admin-id]": ["admin"],
        "role_groups[role-user-id]": ["user"]
    }

    resp = flask_client.post(
        "/o/numbainfinite/admin/idp/add",
        data=form_data
    )
    assert resp.status_code == 302
    assert "/admin/idp" in resp.headers["Location"]

    # Verify we did NOT call POST on SCIM2 Roles to create local roles because we matched the shared application ones
    role_post_calls = [c for c in calls if c[0] == "POST" and c[1].endswith("/scim2/v2/Roles")]
    assert len(role_post_calls) == 0

    # Verify group PUT registration call
    group_put_call = next((c for c in calls if c[0] == "PUT" and c[1].endswith("/identity-providers/new-idp-id/groups")), None)
    assert group_put_call is not None
    assert group_put_call[2] == [{"name": "admin", "id": ""}, {"name": "user", "id": ""}]

    jit_put_call = next((c for c in calls if c[0] == "PUT" and c[1].endswith("/identity-providers/new-idp-id/provisioning/jit")), None)
    assert jit_put_call is not None
    assert jit_put_call[2] == {
        "accountLookupAttributeMappings": [],
        "associateLocalUser": True,
        "attributeSyncMethod": "PRESERVE_LOCAL",
        "isEnabled": True,
        "scheme": "PROVISION_SILENTLY",
        "skipJITForLookupFailure": False,
        "userstore": "PRIMARY",
    }

    # Verify role mappings PATCH calls
    admin_patch_call = next((c for c in calls if c[0] == "PATCH" and c[1].endswith("/scim2/v2/Roles/role-admin-id")), None)
    assert admin_patch_call is not None
    assert admin_patch_call[2]["Operations"][0]["value"] == [{"value": "new-group-id-admin"}]

    user_patch_call = next((c for c in calls if c[0] == "PATCH" and c[1].endswith("/scim2/v2/Roles/role-user-id")), None)
    assert user_patch_call is not None
    assert user_patch_call[2]["Operations"][0]["value"] == [{"value": "new-group-id-user"}]

    # Verify application PATCH login sequence call
    app_patch_call = next((c for c in calls if c[0] == "PATCH" and c[1].endswith("/applications/app-xyz")), None)
    assert app_patch_call is not None
    options = app_patch_call[2]["authenticationSequence"]["steps"][0]["options"]
    assert {"idp": "LOCAL", "authenticator": "BasicAuthenticator"} in options
    assert {"idp": "Corporate-IDP", "authenticator": "OpenIDConnectAuthenticator"} in options


@patch("webapp.blueprints.admin.ISClient.call")
def test_delete_self_user_fails(mock_call, flask_client, admin_session):
    with flask_client.session_transaction() as sess:
        sess.update(admin_session)
    
    # Try deleting self ("admin-12345")
    resp = flask_client.post("/o/numbainfinite/admin/users/admin-12345/delete")
    assert resp.status_code == 302
    assert "/admin/users" in resp.headers["Location"]
    
    # Filter for DELETE calls
    delete_calls = [c for c in mock_call.call_args_list if c[0] and c[0][0] == "DELETE"]
    assert len(delete_calls) == 0


@patch("webapp.blueprints.admin.ISClient.call")
def test_delete_other_user_succeeds(mock_call, flask_client, admin_session):
    with flask_client.session_transaction() as sess:
        sess.update(admin_session)
    
    mock_call.return_value = {
        "status_code": 204,
        "data": None,
        "debug": {"url": "delete-url"}
    }
    
    resp = flask_client.post("/o/numbainfinite/admin/users/other-user-456/delete")
    assert resp.status_code == 302
    
    # Filter for DELETE calls
    delete_calls = [c for c in mock_call.call_args_list if c[0] and c[0][0] == "DELETE"]
    assert len(delete_calls) == 1
    assert "other-user-456" in delete_calls[0][0][1]


@patch("webapp.blueprints.admin.ISClient.call")
def test_add_user_assigns_role(mock_call, flask_client, admin_session):
    with flask_client.session_transaction() as sess:
        sess.update(admin_session)

    calls = []
    def mock_call_side_effect(method, path, token, json=None):
        calls.append((method, path, json))
        if method == "POST" and "/scim2/Users" in path:
            return {
                "status_code": 201,
                "data": {"id": "new-user-999"},
                "debug": {"url": "post-user"}
            }
        elif method == "GET" and "/scim2/v2/Roles" in path:
            return {
                "status_code": 200,
                "data": {
                    "Resources": [
                        {"id": "role-user-id", "displayName": "teamspace-user"}
                    ]
                },
                "debug": {"url": "get-roles"}
            }
        elif method == "PATCH" and "/scim2/v2/Roles/" in path:
            return {
                "status_code": 200,
                "data": {},
                "debug": {"url": "patch-role"}
            }
        return {"status_code": 404, "data": None, "debug": {}}

    mock_call.side_effect = mock_call_side_effect

    form_data = {
        "email": "newuser@test.com",
        "password": "Password123!",
        "first_name": "New",
        "last_name": "User"
    }

    resp = flask_client.post("/o/numbainfinite/admin/users/add", data=form_data)
    assert resp.status_code == 302
    assert "/admin/users" in resp.headers["Location"]

    # Verify user creation call
    user_creation = next((c for c in calls if c[0] == "POST" and "/scim2/Users" in c[1]), None)
    assert user_creation is not None

    # Verify role assignment call
    role_assignment = next((c for c in calls if c[0] == "PATCH" and "/scim2/v2/Roles/role-user-id" in c[1]), None)
    assert role_assignment is not None
    assert role_assignment[2]["Operations"][0]["value"] == [{"value": "new-user-999"}]





