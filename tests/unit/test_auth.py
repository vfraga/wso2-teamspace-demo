from unittest.mock import ANY, MagicMock, patch

import pytest

from webapp.auth import init_oauth, oauth


def _app_config(**overrides):
    config = {
        "IS_BASE_URL": "https://localhost:9443",
        "TENANT_PATH": "/t/teamspace",
        "CLIENT_ID": "mock-client-id",
        "CLIENT_SECRET": "mock-client-secret",
        "OIDC_SCOPES": "openid email profile",
    }
    config.update(overrides)
    app = MagicMock()
    app.config = config
    return app


def _register_kwargs(app):
    with patch.object(oauth, "init_app") as mock_init_app, \
         patch.object(oauth, "register") as mock_register:
        init_oauth(app)
        mock_init_app.assert_called_once_with(app)
        mock_register.assert_called_once()
        return mock_register.call_args.kwargs


def test_init_oauth_registers_the_wso2_client():
    kwargs = _register_kwargs(_app_config(IS_VERIFY_TLS=False))
    assert kwargs == {
        "name": "wso2is",
        "client_id": "mock-client-id",
        "client_secret": "mock-client-secret",
        "server_metadata_url": (
            "https://localhost:9443/t/teamspace/oauth2/token/"
            ".well-known/openid-configuration"
        ),
        "client_kwargs": {
            "scope": "openid email profile",
            "token_endpoint_auth_method": "client_secret_post",
            "verify": False,
        },
        "fetch_token": ANY,
    }


# --- TLS verification ------------------------------------------------------
#
# `verify` used to be hardcoded to False, so IS_VERIFY_TLS had no effect on the
# OIDC path at all — including the JWKS fetch that the id_token signature is
# checked against, and including in production. A real TLS failure isn't
# unit-testable, so these assert on what reaches the Authlib client.


@pytest.mark.parametrize("configured", [True, False])
def test_tls_verification_follows_is_verify_tls(configured):
    kwargs = _register_kwargs(_app_config(IS_VERIFY_TLS=configured))
    assert kwargs["client_kwargs"]["verify"] is configured


def test_tls_verification_defaults_to_on_when_unconfigured():
    # Fail closed: an absent setting must not silently disable verification.
    kwargs = _register_kwargs(_app_config())
    assert kwargs["client_kwargs"]["verify"] is True


def test_disabling_tls_verification_is_logged_as_a_warning(caplog):
    with caplog.at_level("WARNING"):
        _register_kwargs(_app_config(IS_VERIFY_TLS=False))
    assert "TLS verification is DISABLED" in caplog.text
    assert "IS_VERIFY_TLS" in caplog.text


def test_enabling_tls_verification_is_not_warned_about(caplog):
    with caplog.at_level("WARNING"):
        _register_kwargs(_app_config(IS_VERIFY_TLS=True))
    assert "TLS verification is DISABLED" not in caplog.text


# --- TLS failure diagnostics ----------------------------------------------


@pytest.mark.parametrize("message", [
    "SSLCertVerificationError: certificate verify failed",
    "[SSL: CERTIFICATE_VERIFY_FAILED] self signed certificate",
    "SSLError: bad handshake",
])
def test_tls_failures_name_the_setting_to_change(caplog, message):
    from webapp.auth import _explain_tls_failure

    with caplog.at_level("ERROR"):
        assert _explain_tls_failure(Exception(message), "the token exchange") is True
    # A bare SSLCertVerificationError from inside httpx is useless to a
    # developer; the log must point at the knob.
    assert "IS_VERIFY_TLS=false" in caplog.text


def test_non_tls_failures_are_left_alone(caplog):
    from webapp.auth import _explain_tls_failure

    with caplog.at_level("ERROR"):
        assert _explain_tls_failure(ValueError("something else"), "the token exchange") is False
    assert "IS_VERIFY_TLS" not in caplog.text
