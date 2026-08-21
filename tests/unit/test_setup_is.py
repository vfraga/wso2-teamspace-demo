"""Behaviour and configuration surface of the primary IS bootstrap script.

The tenant identity is env-driven so the demo can be provisioned under another
handle, but every existing instance — and the whole README quickstart — was
built against the hardcoded values. So these pin two things at once: that the
defaults still reproduce exactly what the literals produced, and that one
variable really does move every derived value with it.

Nothing here talks to WSO2. The assertions are on values computed at import
time, before any call is made.
"""

import contextlib
import importlib
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import setup_is

_TENANT_VARS = (
    "IS_ORG_HANDLE",
    "IS_TENANT_ADMIN_USERNAME",
    "IS_TENANT_ADMIN_EMAIL",
    "IS_SUPER_ADMIN_USERNAME",
    "APP_NAME",
    "IS_BASE_URL",
)


@contextlib.contextmanager
def _reloaded(**env):
    """Re-import the module under a given environment, then put it back.

    Mirrors tests/unit/test_setup_idp_server.py: the derived endpoints and
    admin login are computed at import time, so overriding one means reloading.
    The developer's own .env is restored afterwards so test ordering cannot
    matter.
    """
    saved = {k: os.environ.get(k) for k in _TENANT_VARS}
    for key in _TENANT_VARS:
        os.environ.pop(key, None)
    os.environ.update(env)
    try:
        yield importlib.reload(setup_is)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(setup_is)


# ─── Behaviour ───────────────────────────────────────────────────────────────

def test_ok():
    mock_resp_200 = MagicMock()
    mock_resp_200.status_code = 200
    mock_resp_404 = MagicMock()
    mock_resp_404.status_code = 404

    assert setup_is._ok(mock_resp_200) is True
    assert setup_is._ok(mock_resp_404) is False
    assert setup_is._ok(mock_resp_404, accept_codes=(200, 404)) is True


def test_create_tenant_already_exists():
    mock_client = MagicMock()

    # Mock response for get tenants returning an existing tenant
    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 200
    mock_get_resp.json.return_value = {
        "tenants": [
            {"domainName": "teamspace", "id": "mock-tenant-uuid-123"}
        ]
    }
    mock_client.get.return_value = mock_get_resp

    with patch("setup_is.step") as mock_step, \
         patch("setup_is.info") as mock_info:

        tenant_id = setup_is.create_tenant(mock_client)

        assert tenant_id == "mock-tenant-uuid-123"
        mock_step.assert_called_once()
        mock_info.assert_called_once_with("Tenant already exists (id=mock-tenant-uuid-123)")


# ─── Configuration surface ───────────────────────────────────────────────────

def test_defaults_reproduce_the_hardcoded_values_they_replaced():
    # Every instance provisioned before these became env-driven was built from
    # these literals, and the README quickstart still tells you to expect them.
    with _reloaded(IS_BASE_URL="https://localhost:9443") as m:
        assert m.TENANT_DOMAIN == "teamspace"
        assert m.TENANT_ADMIN_USERNAME == "teamspaceadmin"
        assert m.TENANT_ADMIN_AUTH[0] == "teamspaceadmin@teamspace"
        assert m.TENANT_ADMIN_EMAIL == "teamspaceadmin@mail.com"
        assert m.APP_NAME == "Teamspace"
        assert m.SUPER_ADMIN_USERNAME == "admin"
        assert m.TENANT_API == "https://localhost:9443/t/teamspace/api/server/v1"
        assert m.TENANT_SCIM == "https://localhost:9443/t/teamspace/scim2/v2"


def test_is_org_handle_moves_every_derived_value_together():
    # The whole point of reusing IS_ORG_HANDLE: the tenant this script creates
    # and the tenant the three services look under cannot disagree.
    with _reloaded(IS_ORG_HANDLE="acme", IS_BASE_URL="https://localhost:9443") as m:
        assert m.TENANT_DOMAIN == "acme"
        assert m.TENANT_ADMIN_AUTH[0] == "teamspaceadmin@acme"
        assert m.TENANT_API == "https://localhost:9443/t/acme/api/server/v1"
        assert m.TENANT_SCIM == "https://localhost:9443/t/acme/scim2/v2"
        assert m.BRANDING_PAYLOAD["name"] == "acme"


def test_empty_org_handle_still_provisions_a_tenant():
    # README documents an empty handle as carbon.super mode for the services.
    # Setup still needs a domain to create the tenant with, so it falls back
    # rather than building "/t//api/server/v1".
    with _reloaded(IS_ORG_HANDLE="", IS_BASE_URL="https://localhost:9443") as m:
        assert m.TENANT_DOMAIN == "teamspace"
        assert "/t/teamspace/" in m.TENANT_API


def test_tenant_admin_email_is_not_derived_from_the_domain():
    # Pinned so it does not get "tidied up" into an @{domain} address later:
    # this is the owner's email ATTRIBUTE. The domain belongs in the login
    # name, which TENANT_ADMIN_AUTH builds. Same split as setup_idp_server.py.
    with _reloaded(IS_ORG_HANDLE="acme") as m:
        assert m.TENANT_ADMIN_EMAIL == "teamspaceadmin@mail.com"
        assert m.TENANT_ADMIN_AUTH[0] == "teamspaceadmin@acme"


def test_app_name_and_super_admin_are_honoured():
    # APP_NAME is read by the portal too, which looks the application up by
    # name (webapp/blueprints/admin.py); IS_SUPER_ADMIN_USERNAME was the only
    # one of the three setup scripts that ignored it.
    with _reloaded(APP_NAME="Workbench", IS_SUPER_ADMIN_USERNAME="root") as m:
        assert m.APP_NAME == "Workbench"
        assert m.SUPER_ADMIN_USERNAME == "root"
        assert m.SUPER_ADMIN_AUTH[0] == "root"


def test_api_resource_identifiers_are_urns_not_phantom_ports():
    # These are opaque WSO2 keys, not endpoints. They used to be
    # http://localhost:9091 and :9093 — the latter a port nothing in the repo
    # ever listened on. Pinned because changing an identifier makes WSO2
    # register a SECOND resource rather than reuse the existing one.
    source = Path(setup_is.__file__).read_text()
    assert '"urn:teamspace:meetings"' in source
    assert '"urn:teamspace:personalization"' in source
    # The quoted literal, not any mention of it: the comment above the block
    # names the old identifiers on purpose so the migration note stays readable.
    assert '"http://localhost:9091"' not in source
    assert '"http://localhost:9093"' not in source
