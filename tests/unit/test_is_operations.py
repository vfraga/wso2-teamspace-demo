"""Root-tenant admin credentials for the one call with no `/o` equivalent.

Assigning the teamspace-user role to a freshly provisioned SCIM agent has to go
through the root tenant, because the agent lives in the root-level AGENT
userstore. That is the only path in the portal that falls back to basic auth.

It used to hardcode `teamspaceadmin@teamspace` / `Admin123`, which meant the
assignment silently 401'd on every deployment that chose a different password —
everything else kept working, so the failure was invisible. These pin the
derivation so the literals cannot come back.
"""

import base64

import pytest

from webapp.is_operations import _get_auth_params, _root_admin_credentials

_CRED_VARS = ("IS_ADMIN_USERNAME", "IS_ADMIN_PASSWORD",
              "IS_TENANT_ADMIN_USERNAME", "IS_TENANT_ADMIN_PASSWORD")


@pytest.fixture
def clean_creds(monkeypatch):
    """Drop any credential the developer's own .env put in the environment."""
    for var in _CRED_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _decode(kwargs):
    header = kwargs["headers"]["Authorization"]
    assert header.startswith("Basic ")
    return base64.b64decode(header.removeprefix("Basic ")).decode().split(":", 1)


def test_org_endpoint_uses_the_bearer_token_untouched(flask_app):
    # The overwhelmingly common path: no basic auth anywhere near it.
    with flask_app.app_context():
        assert _get_auth_params("tok-123", use_org_endpoint=True) == ("tok-123", {})


def test_username_is_derived_from_the_org_handle(flask_app, clean_creds):
    clean_creds.setenv("IS_TENANT_ADMIN_PASSWORD", "s3cret")
    flask_app.config["IS_ORG_HANDLE"] = "acme"
    with flask_app.app_context():
        username, password = _root_admin_credentials()
    assert username == "teamspaceadmin@acme"
    assert password == "s3cret"


def test_password_falls_back_to_the_one_setup_provisions(flask_app, clean_creds):
    # The fix for the live bug: IS_TENANT_ADMIN_PASSWORD is already required to
    # run setup_is.py at all, and is the password that account actually has.
    clean_creds.setenv("IS_TENANT_ADMIN_PASSWORD", "not-admin123")
    flask_app.config["IS_ORG_HANDLE"] = "teamspace"
    with flask_app.app_context():
        _, kwargs = _get_auth_params(None, use_org_endpoint=False)
    assert _decode(kwargs) == ["teamspaceadmin@teamspace", "not-admin123"]


def test_explicit_overrides_win(flask_app, clean_creds):
    clean_creds.setenv("IS_ADMIN_USERNAME", "someone@else")
    clean_creds.setenv("IS_ADMIN_PASSWORD", "override")
    clean_creds.setenv("IS_TENANT_ADMIN_PASSWORD", "ignored")
    flask_app.config["IS_ORG_HANDLE"] = "teamspace"
    with flask_app.app_context():
        _, kwargs = _get_auth_params(None, use_org_endpoint=False)
    assert _decode(kwargs) == ["someone@else", "override"]


def test_tenant_admin_username_is_configurable(flask_app, clean_creds):
    # setup_is.py reads the same variable, so renaming that account moves both.
    clean_creds.setenv("IS_TENANT_ADMIN_USERNAME", "root-admin")
    clean_creds.setenv("IS_TENANT_ADMIN_PASSWORD", "s3cret")
    flask_app.config["IS_ORG_HANDLE"] = "acme"
    with flask_app.app_context():
        username, _ = _root_admin_credentials()
    assert username == "root-admin@acme"


def test_empty_org_handle_yields_a_bare_username(flask_app, clean_creds):
    # carbon.super mode: the login name carries no tenant suffix.
    clean_creds.setenv("IS_TENANT_ADMIN_PASSWORD", "s3cret")
    flask_app.config["IS_ORG_HANDLE"] = ""
    with flask_app.app_context():
        username, _ = _root_admin_credentials()
    assert username == "teamspaceadmin"


def test_missing_password_sends_nothing_and_says_why(flask_app, clean_creds, caplog):
    # Fail as "no credentials" rather than as a rejected guess: a bare 401 plus
    # this log names the variable to set. Sending Admin123 named nothing.
    flask_app.config["IS_ORG_HANDLE"] = "teamspace"
    with flask_app.app_context(), caplog.at_level("ERROR"):
        token, kwargs = _get_auth_params(None, use_org_endpoint=False)

    assert (token, kwargs) == (None, {})
    assert "IS_ADMIN_PASSWORD" in caplog.text
    assert "IS_TENANT_ADMIN_PASSWORD" in caplog.text


def test_the_old_hardcoded_password_is_gone(flask_app, clean_creds):
    # Belt and braces: no combination of unset variables can produce it.
    flask_app.config["IS_ORG_HANDLE"] = "teamspace"
    with flask_app.app_context():
        assert _root_admin_credentials()[1] != "Admin123"
