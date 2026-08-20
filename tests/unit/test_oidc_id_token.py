"""Tests for OIDC id_token verification in the portal's login flow.

Regression guard for a bug that made every login skip signature verification:
Authlib's `parse_id_token(self, token, nonce, ...)` takes `nonce` as a required
positional argument, so calling it as `parse_id_token(token)` raised TypeError
on every request. The except branch caught it and fell back to
`decode_jwt_unverified`, so the failure was invisible — logins kept working, on
an unverified identity.

These tests pin the two halves of the fix: a nonce is generated and sent, and it
is passed to the verified parse.
"""
import inspect
from unittest.mock import MagicMock, patch

import pytest
from authlib.integrations.base_client.sync_openid import OpenIDMixin

from webapp import auth as webapp_auth


def test_authlib_still_requires_nonce_positionally():
    """If Authlib ever makes `nonce` optional, this test explains the history.

    It is not testing our code — it documents the upstream contract the bug
    came from, so a future upgrade that changes it is noticed deliberately.
    """
    params = inspect.signature(OpenIDMixin.parse_id_token).parameters
    assert "nonce" in params
    assert params["nonce"].default is inspect.Parameter.empty, (
        "Authlib made `nonce` optional; the call in webapp/auth.py can be simplified"
    )


def test_start_login_generates_and_sends_a_nonce(flask_app):
    with flask_app.test_request_context("/login"):
        from flask import session

        with patch.object(webapp_auth.oauth, "wso2is") as client:
            client.authorize_redirect.return_value = "redirected"
            webapp_auth.start_login()

        assert "oidc_nonce" in session, "no nonce stored for the callback to check"
        nonce = session["oidc_nonce"]
        assert len(nonce) >= 20, "nonce is too short to be unguessable"

        # The same value must reach the IdP, or it can't come back in the id_token.
        kwargs = client.authorize_redirect.call_args.kwargs
        assert kwargs["nonce"] == nonce
        # PKCE must survive the change.
        assert kwargs["code_challenge_method"] == "S256"


def test_start_login_nonce_differs_per_request(flask_app):
    seen = set()
    for _ in range(3):
        with flask_app.test_request_context("/login"):
            from flask import session

            with patch.object(webapp_auth.oauth, "wso2is") as client:
                client.authorize_redirect.return_value = "r"
                webapp_auth.start_login()
            seen.add(session["oidc_nonce"])
    assert len(seen) == 3, "nonce must not be reused across logins"


def _token() -> dict:
    return {"access_token": "at", "id_token": "it", "refresh_token": "rt"}


def test_callback_passes_the_stored_nonce_to_the_verified_parse(flask_app):
    with flask_app.test_request_context("/callback"):
        from flask import session

        session["oidc_nonce"] = "the-stored-nonce"
        session["code_verifier"] = "cv"

        with patch.object(webapp_auth.oauth, "wso2is") as client:
            client.authorize_access_token.return_value = _token()
            client.parse_id_token.return_value = {
                "sub": "u1", "email": "u@acme.com", "org_id": "o1",
                "org_name": "Acme", "org_handle": "acme",
            }
            with patch.object(webapp_auth, "decode_jwt_unverified", return_value={"scope": "openid"}):
                webapp_auth.handle_callback()

        # The whole point: nonce is supplied, so verification can actually run.
        assert client.parse_id_token.call_args.kwargs["nonce"] == "the-stored-nonce"
        # And it is single-use.
        assert "oidc_nonce" not in session


def test_callback_does_not_fall_back_silently_when_verification_succeeds(flask_app):
    with flask_app.test_request_context("/callback"):
        from flask import session

        session["oidc_nonce"] = "n"
        with patch.object(webapp_auth.oauth, "wso2is") as client:
            client.authorize_access_token.return_value = _token()
            client.parse_id_token.return_value = {"sub": "verified-user", "email": "v@acme.com"}
            with patch.object(webapp_auth, "decode_jwt_unverified", return_value={"scope": ""}) as unverified:
                webapp_auth.handle_callback()

        # decode_jwt_unverified is still used for the access token (which the
        # Business API verifies itself), but must NOT be used for the id_token.
        decoded_names = [c.args[1] for c in unverified.call_args_list]
        assert "id_token" not in decoded_names, decoded_names
        assert session["user"]["sub"] == "verified-user"


def test_production_refuses_login_when_verification_fails(flask_app, monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "production")
    with flask_app.test_request_context("/callback"):
        from flask import session

        session["oidc_nonce"] = "n"
        with patch.object(webapp_auth.oauth, "wso2is") as client:
            client.authorize_access_token.return_value = _token()
            client.parse_id_token.side_effect = ValueError("bad signature")
            webapp_auth.handle_callback()

        # No identity may be established from an unverified token in production.
        assert "user" not in session
        assert "access_token" not in session


def test_non_production_falls_back_but_says_so(flask_app, monkeypatch, caplog):
    monkeypatch.delenv("FLASK_ENV", raising=False)
    with flask_app.test_request_context("/callback"):
        from flask import session

        session["oidc_nonce"] = "n"
        with patch.object(webapp_auth.oauth, "wso2is") as client:
            client.authorize_access_token.return_value = _token()
            client.parse_id_token.side_effect = ValueError("bad signature")
            with patch.object(
                webapp_auth, "decode_jwt_unverified",
                return_value={"sub": "fallback-user", "scope": ""},
            ):
                with caplog.at_level("WARNING"):
                    webapp_auth.handle_callback()

        # The demo keeps working, but the downgrade is loud rather than silent.
        assert "UNVERIFIED" in caplog.text
        assert session["user"]["sub"] == "fallback-user"
