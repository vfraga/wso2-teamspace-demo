"""End-to-end test of the M2M cutover with nothing mocked.

Every other M2M test stubs either the token endpoint or the JWKS fetch. This
one stands up a minimal WSO2-IS-shaped server on a real loopback port and runs
the whole loop through it:

    ServiceTokenClient  --client_credentials-->  mock IS  -->  signed RS256 token
    Business API  --JWKS over HTTP-->  mock IS  -->  verify  -->  200

That covers the parts unit tests cannot: the real HTTP form encoding, the real
JWKS fetch and cache, and the exact issuer/audience strings the services build.
It is hermetic — no WSO2 install, no browser — so it runs in CI.
"""
import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from common.m2m_auth import SERVICE_AUTH_HEADER, SERVICE_SCOPE, M2MConfig, ServiceTokenClient
from tests.helpers.tokens import jwks, sign

CLIENT_ID = "m2m-e2e-client"
CLIENT_SECRET = "m2m-e2e-secret"
TENANT_PATH = "/t/teamspace"

#: A client the mock IS issues a valid token for, but with an empty scope.
EMPTY_SCOPE_CLIENT_ID = "empty-scope-client"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_mock_is(base_url: str) -> FastAPI:
    """A WSO2-IS-shaped token + JWKS endpoint pair."""
    app = FastAPI()
    issuer = f"{base_url}{TENANT_PATH}/oauth2/token"

    @app.get("/t/{tenant}/oauth2/jwks")
    def _jwks(tenant: str):
        return jwks()

    @app.post("/t/{tenant}/oauth2/token")
    async def _token(tenant: str, request: Request):
        form = await request.form()
        if form.get("grant_type") != "client_credentials":
            return {"error": "unsupported_grant_type"}
        client_id = form.get("client_id")
        if client_id not in (CLIENT_ID, EMPTY_SCOPE_CLIENT_ID):
            return {"error": "invalid_client"}
        if form.get("client_secret") != CLIENT_SECRET:
            return {"error": "invalid_client"}
        scope = "" if client_id == EMPTY_SCOPE_CLIENT_ID else form.get("scope", "")
        now = int(time.time())
        token = sign({
            "sub": client_id,
            "aud": CLIENT_ID,
            "iss": issuer,
            "iat": now,
            "exp": now + 3600,
            "aut": "APPLICATION",
            "scope": scope,
        })
        return {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": scope,
        }

    return app


class _Server(threading.Thread):
    def __init__(self, app, port):
        super().__init__(daemon=True)
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, ws="none", log_level="warning")
        )

    def run(self):
        self.server.run()

    def stop(self):
        self.server.should_exit = True


@pytest.fixture(scope="module")
def mock_is():
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    server = _Server(_make_mock_is(base_url), port)
    server.start()

    deadline = time.time() + 15
    while time.time() < deadline:
        if getattr(server.server, "started", False):
            break
        time.sleep(0.05)
    else:
        server.stop()
        pytest.fail("mock IS did not start")

    yield base_url
    server.stop()
    server.join(timeout=5)


@pytest.fixture
def api_pointed_at_mock_is(mock_is, monkeypatch):
    """Point the Business API's verification at the mock IS."""
    from api.auth import JWKSCache
    from api.config import settings

    monkeypatch.setattr(settings, "IS_BASE_URL", mock_is)
    monkeypatch.setattr(settings, "CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(settings, "IS_VERIFY_TLS", False)
    settings.TENANT_PATH = TENANT_PATH
    # The JWKS cache is a class-level singleton keyed on (base_url, tenant);
    # clear it so a previous test's keys can't satisfy this one.
    JWKSCache._data = None
    JWKSCache._key = None
    yield
    JWKSCache._data = None
    JWKSCache._key = None


@pytest.fixture
def token_client(mock_is):
    return ServiceTokenClient(
        lambda: M2MConfig(
            is_base_url=mock_is,
            tenant_path=TENANT_PATH,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            verify_tls=False,
        ),
        label="test caller",
    )


def test_client_credentials_token_is_issued_over_real_http(token_client):
    token = token_client.get_token()
    assert token is not None
    # Three dot-separated segments: this is a real signed JWT, not a secret.
    assert token.count(".") == 2


def test_bad_client_secret_yields_no_token(mock_is):
    client = ServiceTokenClient(
        lambda: M2MConfig(
            is_base_url=mock_is,
            tenant_path=TENANT_PATH,
            client_id=CLIENT_ID,
            client_secret="wrong-secret",
            verify_tls=False,
        ),
        label="test caller",
    )
    assert client.get_token() is None


def test_full_loop_service_token_is_accepted_by_the_business_api(
    token_client, api_pointed_at_mock_is, db_session
):
    """The whole cutover, unmocked: mint at the IS, verify at the API."""
    from api.database import get_db
    from api.main import app as api_app
    from api.models import AgentConfig

    db_session.add(
        AgentConfig(
            org="acme", agent_id="agent-e2e", agent_secret="s",
            display_name="E2E Agent", gemini_api_key="k", org_client_id="c",
        )
    )
    db_session.commit()

    api_app.dependency_overrides[get_db] = lambda: db_session
    try:
        with TestClient(api_app) as client:
            resp = client.get(
                "/agent-config/org/acme",
                headers=token_client.auth_headers(),
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["agent_id"] == "agent-e2e"
    finally:
        api_app.dependency_overrides.pop(get_db, None)


def test_application_token_without_requested_scope_is_refused(
    mock_is, api_pointed_at_mock_is, caplog
):
    client = ServiceTokenClient(
        lambda: M2MConfig(
            is_base_url=mock_is,
            tenant_path=TENANT_PATH,
            client_id=EMPTY_SCOPE_CLIENT_ID,
            client_secret=CLIENT_SECRET,
            verify_tls=False,
        ),
        label="test caller",
    )
    with caplog.at_level("ERROR"):
        assert client.get_token() is None
    assert "WITHOUT" in caplog.text
    assert SERVICE_SCOPE in caplog.text


def test_scope_mismatch_between_request_and_requirement_is_rejected(
    mock_is, api_pointed_at_mock_is, db_session
):
    """A token minted for a different scope is rejected by the API with 403."""
    from api.database import get_db
    from api.main import app as api_app

    issuer = f"{mock_is}{TENANT_PATH}/oauth2/token"
    now = int(time.time())
    wrong_scope_token = sign({
        "sub": CLIENT_ID, "aud": CLIENT_ID, "iss": issuer,
        "iat": now, "exp": now + 3600,
        "aut": "APPLICATION", "scope": "list_meetings",
    })

    api_app.dependency_overrides[get_db] = lambda: db_session
    try:
        with TestClient(api_app) as client:
            resp = client.get(
                "/agent-config/org/acme",
                headers={SERVICE_AUTH_HEADER: f"Bearer {wrong_scope_token}"},
            )
        assert resp.status_code == 403
        assert SERVICE_SCOPE in resp.json()["detail"]
    finally:
        api_app.dependency_overrides.pop(get_db, None)
