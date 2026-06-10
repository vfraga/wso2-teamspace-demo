import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent import agent_config_cache
from agent.agent_config_cache import (
    _cache,
    fetch_and_cache_agent_config,
    invalidate_agent_config_cache,
)
from agent.config import settings
from agent.state_manager import StateManager
from agent.tools import dispatch_tool


def _run(coro):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


@pytest.fixture(autouse=True)
def _clear_cache():
    _cache.clear()
    yield
    _cache.clear()


def test_fetch_returns_none_when_secret_unset(monkeypatch):
    monkeypatch.setattr(settings, "BUSINESS_API_INTERNAL_SECRET", "")
    result = _run(fetch_and_cache_agent_config("acme"))
    assert result is None
    assert "acme" not in _cache


def test_fetch_returns_config_and_caches(monkeypatch):
    monkeypatch.setattr(settings, "BUSINESS_API_INTERNAL_SECRET", "secret")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "org": "acme", "agent_id": "a1", "agent_secret": "s1",
    }

    with patch.object(agent_config_cache.httpx, "AsyncClient") as mock_client_class:
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = _run(fetch_and_cache_agent_config("acme"))
        assert result == {"org": "acme", "agent_id": "a1", "agent_secret": "s1"}
        assert "acme" in _cache
        mock_client.get.assert_awaited_once()
        url = mock_client.get.await_args.args[0]
        assert url.endswith("/agent-config/org/acme")
        assert mock_client.get.await_args.kwargs["headers"] == {
            "X-Internal-Secret": "secret",
        }


def test_fetch_returns_none_on_404_and_does_not_cache(monkeypatch):
    monkeypatch.setattr(settings, "BUSINESS_API_INTERNAL_SECRET", "secret")
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch.object(agent_config_cache.httpx, "AsyncClient") as mock_client_class:
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = _run(fetch_and_cache_agent_config("acme"))
        assert result is None
        assert "acme" not in _cache


def test_fetch_returns_none_on_network_error(monkeypatch):
    import httpx
    monkeypatch.setattr(settings, "BUSINESS_API_INTERNAL_SECRET", "secret")
    with patch.object(agent_config_cache.httpx, "AsyncClient") as mock_client_class:
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
        mock_client_class.return_value.__aenter__.return_value = mock_client
        result = _run(fetch_and_cache_agent_config("acme"))
        assert result is None


def test_second_fetch_uses_cache(monkeypatch):
    monkeypatch.setattr(settings, "BUSINESS_API_INTERNAL_SECRET", "secret")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"org": "acme", "agent_id": "a1", "agent_secret": "s1"}

    with patch.object(agent_config_cache.httpx, "AsyncClient") as mock_client_class:
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_class.return_value.__aenter__.return_value = mock_client
        first = _run(fetch_and_cache_agent_config("acme"))
        second = _run(fetch_and_cache_agent_config("acme"))
        assert first == second
        assert mock_client.get.await_count == 1


def test_force_refresh_bypasses_cache(monkeypatch):
    monkeypatch.setattr(settings, "BUSINESS_API_INTERNAL_SECRET", "secret")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"org": "acme", "agent_id": "a1", "agent_secret": "s1"}

    with patch.object(agent_config_cache.httpx, "AsyncClient") as mock_client_class:
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_class.return_value.__aenter__.return_value = mock_client
        _run(fetch_and_cache_agent_config("acme"))
        _run(fetch_and_cache_agent_config("acme", force_refresh=True))
        assert mock_client.get.await_count == 2


def test_invalidate_clears_entry(monkeypatch):
    monkeypatch.setattr(settings, "BUSINESS_API_INTERNAL_SECRET", "secret")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"org": "acme", "agent_id": "a1", "agent_secret": "s1"}
    with patch.object(agent_config_cache.httpx, "AsyncClient") as mock_client_class:
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_class.return_value.__aenter__.return_value = mock_client
        _run(fetch_and_cache_agent_config("acme"))
        assert "acme" in _cache
        invalidate_agent_config_cache("acme")
        assert "acme" not in _cache


def test_dispatch_tool_populates_creds_via_m2m(monkeypatch):
    from agent.state_manager import StateManager
    state_mgr = StateManager.get_instance()
    state_mgr.reset()
    state_mgr.set_org_name("thread-m2m-1", "acme")

    monkeypatch.setattr(settings, "BUSINESS_API_INTERNAL_SECRET", "secret")

    mock_config = {"org": "acme", "agent_id": "a-from-m2m", "agent_secret": "s-from-m2m"}
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_config

    mock_auth = MagicMock()
    mock_auth.get_obo_token.return_value = None
    mock_auth.fetch_agent_token = AsyncMock(return_value="m2m-derived-token")

    mock_mcp_content = MagicMock()
    mock_mcp_content.text = json.dumps({"status": "success", "meetings": []})

    with patch.object(agent_config_cache.httpx, "AsyncClient") as mock_client_class, \
         patch("agent.tools.AuthManager.get_instance", return_value=mock_auth), \
         patch("agent.mcp_server.call_tool", new_callable=AsyncMock) as mock_mcp_call:
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_mcp_call.return_value = [mock_mcp_content]

        result = _run(dispatch_tool("list_meetings", {}, "thread-m2m-1"))

    assert result["status"] == "success"
    mock_auth.fetch_agent_token.assert_awaited_once_with(
        "a-from-m2m", "s-from-m2m", thread_id="thread-m2m-1",
    )
    assert state_mgr.get_agent_credentials("thread-m2m-1") == ("a-from-m2m", "s-from-m2m")
    assert mock_client.get.await_count == 1
