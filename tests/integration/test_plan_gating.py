"""Tests for plan-and-role gating on the enterprise-only admin features.

The bug these guard against: both gates evaluated the org's plan only *inside*
the role-failure branch, so `required_plan` was never actually compared. Any
user holding the required role reached the feature whatever their org's plan.

That is reachable through the app's own UI, not just in theory:
`subscription.py:upgrade` grants a plan's roles but never revokes them, and it
accepts a downgrade. So an org that goes enterprise -> basic keeps
`idp-manager`, `advanced-branding-editor` and `basic-branding-editor`.

The gate is a three-way decision because the plan lookup can fail, and
reporting a default in that case would revoke paid features during a Business
API outage. Each row of that table is pinned below.
"""
from unittest.mock import patch

import pytest

from webapp.blueprints import admin as admin_bp
from webapp.blueprints import agents as agents_bp

ADMIN_ROLES = ["teamspace-admin"]
IDP_ROLES = ["teamspace-admin", "idp-manager"]


@pytest.fixture
def session_for(flask_client):
    """Log a session in with the given roles."""

    def _apply(roles):
        with flask_client.session_transaction() as sess:
            sess.update({
                "user": {
                    "sub": "u1", "email": "a@acme.com", "org_id": "org-1",
                    "org_name": "Acme", "org_handle": "numbainfinite",
                },
                "is_admin": True,
                "user_roles": roles,
                "user_scopes": ["openid", "view_agent_config", "manage_agent_config"],
                "access_token": "tok",
                "access_token_claims": {"sub": "u1", "scope": "openid"},
            })
        return flask_client

    return _apply


def _patch_plan(module, value):
    """Patch the gating plan lookup as imported by a blueprint module."""
    return patch.object(module, "resolve_plan_for_gating", return_value=value)


# --- Identity Providers (enterprise + idp-manager) -------------------------


def test_enterprise_plan_with_role_is_allowed(session_for):
    client = session_for(IDP_ROLES)
    with _patch_plan(admin_bp, "enterprise"), patch.object(admin_bp.ISClient, "call") as call:
        call.return_value = {"status_code": 200, "data": {"identityProviders": []}, "debug": []}
        resp = client.get("/o/numbainfinite/admin/idp")
    assert resp.status_code == 200
    assert b"Upgrade Required" not in resp.data


def test_basic_plan_with_stale_role_is_denied(session_for):
    """The bypass, closed.

    This is the downgraded-org case: the plan says basic but the user still
    holds idp-manager from when the org was on enterprise. Before the fix the
    role alone granted access.
    """
    client = session_for(IDP_ROLES)
    with _patch_plan(admin_bp, "basic"):
        resp = client.get("/o/numbainfinite/admin/idp")
    assert resp.status_code == 200
    assert b"Upgrade Required" in resp.data


def test_business_plan_with_stale_role_is_also_denied(session_for):
    # Any non-enterprise plan, not just basic.
    client = session_for(IDP_ROLES)
    with _patch_plan(admin_bp, "business"):
        resp = client.get("/o/numbainfinite/admin/idp")
    assert b"Upgrade Required" in resp.data


def test_enterprise_plan_without_role_says_role_not_upgrade(session_for):
    # The org IS entitled, so "upgrade" would be a lie; the honest answer is
    # that this user lacks the role.
    client = session_for(ADMIN_ROLES)
    with _patch_plan(admin_bp, "enterprise"):
        resp = client.get("/o/numbainfinite/admin/idp")
    assert resp.status_code == 200
    assert b"Upgrade Required" not in resp.data
    assert b"idp-manager" in resp.data


def test_unknown_plan_with_role_is_allowed(session_for):
    # A Business API outage must not lock an enterprise customer out.
    client = session_for(IDP_ROLES)
    with _patch_plan(admin_bp, None), patch.object(admin_bp.ISClient, "call") as call:
        call.return_value = {"status_code": 200, "data": {"identityProviders": []}, "debug": []}
        resp = client.get("/o/numbainfinite/admin/idp")
    assert resp.status_code == 200
    assert b"Upgrade Required" not in resp.data


def test_unknown_plan_without_role_shows_the_upgrade_prompt(session_for):
    client = session_for(ADMIN_ROLES)
    with _patch_plan(admin_bp, None):
        resp = client.get("/o/numbainfinite/admin/idp")
    assert b"Upgrade Required" in resp.data


# --- AI Agents (enterprise + teamspace-admin) ------------------------------
#
# This gate was fully bypassed rather than merely fragile: its required role is
# Teamspace Admin, which every org admin holds, so the plan never mattered.


def test_agents_page_denied_on_basic_plan_even_for_an_admin(session_for):
    client = session_for(ADMIN_ROLES)
    with _patch_plan(agents_bp, "basic"):
        resp = client.get("/o/numbainfinite/admin/agents/")
    assert resp.status_code == 200
    assert b"Upgrade Required" in resp.data


def test_agents_page_allowed_on_enterprise_plan(session_for):
    client = session_for(ADMIN_ROLES)
    with _patch_plan(agents_bp, "enterprise"), \
         patch.object(agents_bp, "get_agent_config", return_value=None):
        resp = client.get("/o/numbainfinite/admin/agents/")
    assert resp.status_code == 200
    assert b"Upgrade Required" not in resp.data


def test_agents_page_allowed_when_plan_unknown_but_admin(session_for):
    client = session_for(ADMIN_ROLES)
    with _patch_plan(agents_bp, None), \
         patch.object(agents_bp, "get_agent_config", return_value=None):
        resp = client.get("/o/numbainfinite/admin/agents/")
    assert b"Upgrade Required" not in resp.data


# --- resolve_plan_for_gating ----------------------------------------------


def _fake_response(status_code, payload=None):
    class _R:
        def __init__(self):
            self.status_code = status_code

        def json(self):
            if payload is None:
                raise ValueError("no json")
            return payload

    return _R()


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (200, {"plan": "enterprise"}, "enterprise"),
        (200, {"plan": "basic"}, "basic"),
        # A row with no plan value still means "subscribed to nothing".
        (200, {}, "basic"),
        # No row: signup always writes one, so its absence is authoritative.
        (404, None, "basic"),
        # Unknown — must NOT be reported as basic.
        (503, None, None),
        (500, None, None),
    ],
)
def test_resolve_plan_for_gating_distinguishes_absent_from_unknown(
    flask_app, status, payload, expected
):
    from webapp import api_proxy

    with flask_app.test_request_context("/"):
        with patch.object(api_proxy, "api_request", return_value=_fake_response(status, payload)):
            assert api_proxy.resolve_plan_for_gating("org-1") == expected


def test_resolve_plan_for_gating_without_an_org_is_unknown(flask_app):
    from webapp import api_proxy

    with flask_app.test_request_context("/"):
        assert api_proxy.resolve_plan_for_gating("") is None


def test_get_organization_plan_still_defaults_for_display(flask_app):
    # The display helper keeps its forgiving default; only the gating helper
    # distinguishes unknown.
    from webapp import api_proxy

    with flask_app.test_request_context("/"):
        with patch.object(api_proxy, "api_request", return_value=_fake_response(503)):
            assert api_proxy.get_organization_plan("org-1")["plan"] == "basic"
