"""Tests for the OAuth 2.0 client-credentials M2M layer.

This replaced the `X-Internal-Secret` shared secret, so the tests pin the
properties the shared secret could not offer: tokens expire, they are scoped,
and a caller cannot be authenticated by simply echoing a static string.
"""
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.m2m_auth import (
    SERVICE_AUTH_HEADER,
    SERVICE_SCOPE,
    M2MConfig,
    ServiceAuthError,
    ServiceTokenClient,
    parse_bearer,
    verify_service_claims,
)


def _config(**overrides) -> M2MConfig:
    base = dict(
        is_base_url="https://localhost:9443",
        tenant_path="/t/teamspace",
        client_id="client-1",
        client_secret="secret-1",
        verify_tls=False,
    )
    base.update(overrides)
    return M2MConfig(**base)


# --- M2MConfig -------------------------------------------------------------


def test_token_url_is_tenant_aware():
    assert _config().token_url == "https://localhost:9443/t/teamspace/oauth2/token"
    assert _config(tenant_path="").token_url == "https://localhost:9443/oauth2/token"


@pytest.mark.parametrize(
    ("overrides", "usable"),
    [
        ({}, True),
        ({"client_id": ""}, False),
        ({"client_secret": ""}, False),
        ({"is_base_url": ""}, False),
    ],
)
def test_is_usable_requires_full_credentials(overrides, usable):
    assert _config(**overrides).is_usable is usable


def test_cache_key_separates_tenants_and_clients():
    # Two apps in one process must never read each other's token.
    assert _config().cache_key != _config(client_id="client-2").cache_key
    assert _config().cache_key != _config(tenant_path="/t/other").cache_key


# --- parse_bearer ----------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer abc.def", "abc.def"),
        ("bearer abc.def", "abc.def"),
        ("  Bearer   abc.def  ", "abc.def"),
        ("abc.def", "abc.def"),      # bare token, for hand-rolled curl in a demo
        ("Bearer ", None),
        ("Bearer", None),       # regression: was returning the literal "Bearer"
        ("Basic xyz", None),    # a non-Bearer scheme is not a token
        ("", None),
        (None, None),
    ],
)
def test_parse_bearer(header, expected):
    assert parse_bearer(header) == expected


# --- verify_service_claims -------------------------------------------------


def test_accepts_an_application_token_with_the_service_scope():
    verify_service_claims({"scope": f"openid {SERVICE_SCOPE}", "aut": "APPLICATION"})


def test_rejects_a_token_without_the_service_scope():
    with pytest.raises(ServiceAuthError, match=SERVICE_SCOPE):
        verify_service_claims({"scope": "list_meetings", "aut": "APPLICATION"})


def test_rejects_a_user_token_even_when_it_carries_the_scope():
    with pytest.raises(ServiceAuthError, match="aut="):
        verify_service_claims({"scope": SERVICE_SCOPE, "aut": "APPLICATION_USER"})


def test_accepts_when_aut_is_absent():
    # Defence in depth, not the gate: a WSO2 build that omits `aut` must not
    # break every M2M call, because the scope is application-authorized anyway.
    verify_service_claims({"scope": SERVICE_SCOPE})


def test_handles_a_missing_scope_claim():
    with pytest.raises(ServiceAuthError):
        verify_service_claims({"aut": "APPLICATION"})


# --- ServiceTokenClient ----------------------------------------------------


def _token_response(status=200, payload=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload if payload is not None else {
        "access_token": "tok-1", "expires_in": 3600, "scope": SERVICE_SCOPE,
    }
    return resp


def test_get_token_requests_client_credentials_with_the_service_scope():
    client = ServiceTokenClient(_config, label="test")
    with patch("common.m2m_auth.requests.post", return_value=_token_response()) as post:
        assert client.get_token() == "tok-1"

    url, kwargs = post.call_args.args[0], post.call_args.kwargs
    assert url == "https://localhost:9443/t/teamspace/oauth2/token"
    assert kwargs["data"] == {
        "grant_type": "client_credentials",
        "client_id": "client-1",
        "client_secret": "secret-1",
        "scope": SERVICE_SCOPE,
    }
    assert kwargs["verify"] is False


def test_token_is_cached_between_calls():
    client = ServiceTokenClient(_config, label="test")
    with patch("common.m2m_auth.requests.post", return_value=_token_response()) as post:
        client.get_token()
        client.get_token()
    assert post.call_count == 1


def test_force_refresh_bypasses_the_cache():
    client = ServiceTokenClient(_config, label="test")
    with patch("common.m2m_auth.requests.post", return_value=_token_response()) as post:
        client.get_token()
        client.get_token(force_refresh=True)
    assert post.call_count == 2


def test_invalidate_drops_the_cache():
    client = ServiceTokenClient(_config, label="test")
    with patch("common.m2m_auth.requests.post", return_value=_token_response()) as post:
        client.get_token()
        client.invalidate()
        client.get_token()
    assert post.call_count == 2


def test_token_is_refreshed_before_it_expires():
    # A 40s lifetime minus the 30s skew leaves ~10s of usable cache, so a
    # token can never be presented after it has already expired.
    client = ServiceTokenClient(_config, label="test")
    payload = {"access_token": "tok-1", "expires_in": 40, "scope": SERVICE_SCOPE}
    with patch("common.m2m_auth.requests.post", return_value=_token_response(payload=payload)) as post:
        client.get_token()
        with patch("common.m2m_auth.time.monotonic", return_value=time.monotonic() + 20):
            client.get_token()
    assert post.call_count == 2


def test_a_short_lifetime_never_produces_a_negative_ttl():
    client = ServiceTokenClient(_config, label="test")
    payload = {"access_token": "tok-1", "expires_in": 5, "scope": SERVICE_SCOPE}
    with patch("common.m2m_auth.requests.post", return_value=_token_response(payload=payload)):
        assert client.get_token() == "tok-1"


def test_token_without_the_requested_scope_is_refused(caplog):
    client = ServiceTokenClient(_config, label="test")
    payload = {"access_token": "tok-1", "expires_in": 3600, "scope": ""}
    with patch("common.m2m_auth.requests.post", return_value=_token_response(payload=payload)):
        with caplog.at_level("ERROR"):
            assert client.get_token() is None
    assert "WITHOUT" in caplog.text
    assert SERVICE_SCOPE in caplog.text


def test_missing_credentials_returns_none_without_a_request():
    client = ServiceTokenClient(lambda: _config(client_id=""), label="test")
    with patch("common.m2m_auth.requests.post") as post:
        assert client.get_token() is None
    post.assert_not_called()


def test_non_200_from_the_token_endpoint_returns_none():
    client = ServiceTokenClient(_config, label="test")
    resp = _token_response(status=401, payload={"error": "invalid_client"})
    resp.text = '{"error": "invalid_client"}'
    with patch("common.m2m_auth.requests.post", return_value=resp):
        assert client.get_token() is None


def test_network_error_returns_none():
    import requests as _requests

    client = ServiceTokenClient(_config, label="test")
    with patch("common.m2m_auth.requests.post", side_effect=_requests.ConnectionError("boom")):
        assert client.get_token() is None


def test_auth_headers_are_empty_when_no_token_is_available():
    client = ServiceTokenClient(lambda: _config(client_secret=""), label="test")
    assert client.auth_headers() == {}


def test_auth_headers_carry_a_bearer_token():
    client = ServiceTokenClient(_config, label="test")
    with patch("common.m2m_auth.requests.post", return_value=_token_response()):
        assert client.auth_headers() == {SERVICE_AUTH_HEADER: "Bearer tok-1"}


# --- async path ------------------------------------------------------------


def _run(coro):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


def test_async_token_acquisition_and_caching():
    client = ServiceTokenClient(_config, label="test")
    post = AsyncMock(return_value=_token_response())
    with patch("common.m2m_auth.httpx.AsyncClient") as client_class:
        client_class.return_value.__aenter__.return_value = MagicMock(post=post)
        assert _run(client.aget_token()) == "tok-1"
        assert _run(client.aget_token()) == "tok-1"
    assert post.await_count == 1


def test_async_auth_headers_are_empty_without_credentials():
    client = ServiceTokenClient(lambda: _config(client_id=""), label="test")
    assert _run(client.aauth_headers()) == {}
