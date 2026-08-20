"""Tests that the rate limits actually enforce.

Wiring a limiter is easy to get subtly wrong — a decorator on the wrong object,
a limit that never attaches, storage that resets per request. These drive the
real endpoints until they 429, so a silently inert limiter fails the build.

The configured limits are deliberately generous, so each test overrides them to
something tiny via the `common.rate_limit` module values the factories read.
"""
import importlib

import pytest
from fastapi.testclient import TestClient

from common import rate_limit


@pytest.fixture
def tiny_limits(monkeypatch):
    """Shrink the limits, then rebuild the apps so the factories pick them up."""
    monkeypatch.setattr(rate_limit, "CHAT_LIMIT", "2/minute")
    monkeypatch.setattr(rate_limit, "AUTH_LIMIT", "2/minute")
    monkeypatch.setattr(rate_limit, "ENABLED", True)
    monkeypatch.setattr(rate_limit, "DEFAULT_LIMIT", "3/minute")
    rate_limit.reset_limiter_state()
    yield
    rate_limit.reset_limiter_state()


# --- agent service ---------------------------------------------------------


def test_agent_authorize_is_rate_limited(tiny_limits):
    import agent.main

    agent_main = importlib.reload(agent.main)
    client = TestClient(agent_main.app, raise_server_exceptions=False)

    # /authorize needs no service token, so the limiter is the only gate.
    codes = [
        client.get("/authorize?thread_id=t1&action=book").status_code
        for _ in range(4)
    ]
    assert 429 in codes, f"expected a 429 within 4 requests, got {codes}"
    assert codes.count(429) >= 2, codes
    # The un-limited requests must be handled errors, not 500s: this endpoint is
    # public, and an unknown thread_id used to raise ValueError.
    assert all(c in (400, 429) for c in codes), codes

    # The 429 body is JSON with a `detail`, matching this service's other errors.
    limited = client.get("/authorize?thread_id=t1&action=book")
    assert limited.status_code == 429
    assert "Rate limit exceeded" in limited.json()["detail"]


def test_unauthenticated_chat_flood_is_throttled(tiny_limits):
    """An unauthenticated flood must be throttled before any token is verified.

    This is why the limits are FastAPI dependencies ordered ahead of
    `require_service_auth` rather than decorators on the handler: a decorator
    runs only after the auth dependency has already rejected the request, so
    the limiter would never see it. Regression guard for that ordering.
    """
    import agent.main

    agent_main = importlib.reload(agent.main)
    client = TestClient(agent_main.app, raise_server_exceptions=False)

    payload = {"thread_id": "t1", "message": "hi", "org_name": "acme"}
    codes = [client.post("/chat", json=payload).status_code for _ in range(6)]
    # Requests below the limit are rejected 401 by require_service_auth; past
    # it the middleware answers 429 without ever verifying a token.
    assert set(codes) <= {401, 429}, codes
    assert 429 in codes, f"chat endpoint never rate limited: {codes}"


def test_health_endpoint_is_never_rate_limited(tiny_limits):
    # /health carries no limit dependency at all, so container liveness probes
    # can never be throttled — even with the limits set absurdly low.
    import agent.main

    agent_main = importlib.reload(agent.main)
    client = TestClient(agent_main.app, raise_server_exceptions=False)

    codes = [client.get("/health").status_code for _ in range(10)]
    assert codes == [200] * 10, codes


def test_service_endpoints_carry_the_default_backstop(tiny_limits):
    import agent.main

    agent_main = importlib.reload(agent.main)
    client = TestClient(agent_main.app, raise_server_exceptions=False)

    codes = [client.get("/state/t1").status_code for _ in range(6)]
    assert set(codes) <= {401, 429}, codes
    assert 429 in codes, codes


def test_limits_are_tracked_per_client(tiny_limits):
    # One noisy client must not exhaust another's budget.
    import agent.main

    agent_main = importlib.reload(agent.main)
    client = TestClient(agent_main.app, raise_server_exceptions=False)

    noisy = {"x-forwarded-for": "10.0.0.1"}
    quiet = {"x-forwarded-for": "10.0.0.2"}
    # Trusted only when declared, so enable it for this test.
    import os
    os.environ["RATE_LIMIT_TRUST_FORWARDED_FOR"] = "true"
    try:
        for _ in range(6):
            client.get("/authorize?thread_id=t1&action=book", headers=noisy)
        assert client.get("/authorize?thread_id=t1&action=book", headers=noisy).status_code == 429
        assert client.get("/authorize?thread_id=t1&action=book", headers=quiet).status_code != 429
    finally:
        os.environ.pop("RATE_LIMIT_TRUST_FORWARDED_FOR", None)


def test_authenticated_chat_still_has_its_own_tighter_limit():
    # The per-route chat limit must be at least as tight as the global default,
    # or it would never be the binding constraint on Gemini spend.
    def _per_minute(spec: str) -> int:
        count, _, window = spec.partition("/")
        assert window == "minute", spec
        return int(count)

    assert _per_minute(rate_limit.CHAT_LIMIT) <= _per_minute(rate_limit.DEFAULT_LIMIT)


def test_disabling_rate_limiting_lets_everything_through(monkeypatch):
    monkeypatch.setattr(rate_limit, "ENABLED", False)
    monkeypatch.setattr(rate_limit, "AUTH_LIMIT", "1/minute")
    monkeypatch.setattr(rate_limit, "DEFAULT_LIMIT", "1/minute")

    import agent.main

    agent_main = importlib.reload(agent.main)
    client = TestClient(agent_main.app, raise_server_exceptions=False)

    codes = [
        client.get("/authorize?thread_id=t1&action=book").status_code
        for _ in range(5)
    ]
    assert 429 not in codes, codes


# --- web portal ------------------------------------------------------------


def test_webapp_chat_blueprint_is_rate_limited(tiny_limits):
    from webapp.app import create_app

    app = create_app()
    app.config.update(TESTING=True, SECRET_KEY="test")
    client = app.test_client()

    # Unauthenticated, so @login_required redirects — but the limiter runs
    # first, so the 429 still surfaces.
    codes = [
        client.post("/o/acme/chat/clear").status_code
        for _ in range(5)
    ]
    assert 429 in codes, f"webapp chat routes never rate limited: {codes}"


def test_webapp_non_chat_routes_use_the_generous_default(monkeypatch):
    # The portal applies DEFAULT_LIMIT globally (Flask-Limiter's default_limits),
    # so /health is covered — but at a ceiling far above any probe cadence.
    monkeypatch.setattr(rate_limit, "ENABLED", True)
    monkeypatch.setattr(rate_limit, "CHAT_LIMIT", "60/minute")
    monkeypatch.setattr(rate_limit, "DEFAULT_LIMIT", "600/minute")

    from webapp.app import create_app

    app = create_app()
    app.config.update(TESTING=True, SECRET_KEY="test")
    client = app.test_client()

    codes = [client.get("/health").status_code for _ in range(10)]
    assert codes == [200] * 10, codes


# --- configuration ---------------------------------------------------------


def test_storage_uri_follows_redis_url(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert rate_limit.storage_uri() == "memory://"

    monkeypatch.setenv("REDIS_URL", "redis://example:6379/2")
    assert rate_limit.storage_uri() == "redis://example:6379/2"


def test_describe_reports_the_active_backend(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr(rate_limit, "ENABLED", True)
    assert "in-memory" in rate_limit.describe()

    monkeypatch.setattr(rate_limit, "ENABLED", False)
    assert "disabled" in rate_limit.describe()


# --- MCP endpoints ---------------------------------------------------------
#
# /mcp/sse opens a persistent stream per request and had neither a rate limit
# nor a cap on concurrency, so it was an unauthenticated way to tie up workers.


def test_mcp_endpoints_are_rate_limited(tiny_limits):
    import agent.main

    agent_main = importlib.reload(agent.main)
    client = TestClient(agent_main.app, raise_server_exceptions=False)

    codes = [client.post("/mcp/messages/").status_code for _ in range(6)]
    assert 429 in codes, f"/mcp/messages/ never rate limited: {codes}"


def test_mcp_sse_stream_count_is_capped(monkeypatch):
    import agent.main

    agent_main = importlib.reload(agent.main)
    monkeypatch.setattr(agent_main, "MAX_MCP_SSE_STREAMS", 2)

    # Occupy every slot, then confirm the next reservation is refused.
    from fastapi import HTTPException

    with agent_main._mcp_sse_slot(), agent_main._mcp_sse_slot():
        with pytest.raises(HTTPException) as excinfo:
            with agent_main._mcp_sse_slot():
                pass
        assert excinfo.value.status_code == 503

    # Slots are returned on exit, so the endpoint recovers.
    with agent_main._mcp_sse_slot():
        pass


def test_mcp_sse_slot_is_released_when_the_stream_raises(monkeypatch):
    import agent.main

    agent_main = importlib.reload(agent.main)
    monkeypatch.setattr(agent_main, "MAX_MCP_SSE_STREAMS", 1)

    with pytest.raises(RuntimeError):
        with agent_main._mcp_sse_slot():
            raise RuntimeError("stream died")

    # A crashed stream must not permanently consume its slot.
    with agent_main._mcp_sse_slot():
        pass
