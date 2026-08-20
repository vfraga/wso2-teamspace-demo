"""Tests for the agent's token cache.

Storage moved from instance dicts into `agent/store.py`, so these seed through
the store rather than patching private attributes — which also means they now
exercise the same code path production uses.
"""
import time

import pytest

from agent.auth_manager import _NS_AGENT_TOKEN, _NS_OBO_TOKEN, _NS_PKCE, AuthManager
from agent.store import InMemoryStore, get_store, set_store


@pytest.fixture(autouse=True)
def _fresh_store():
    set_store(InMemoryStore())
    yield
    set_store(None)


def _seed_obo(thread_id: str, token: str, *, expires_in: int = 3600):
    get_store().set(
        _NS_OBO_TOKEN, thread_id,
        {"token": token, "expires_at": time.time() + expires_in},
    )


def test_clear_obo_tokens_removes_cached_entry():
    auth_mgr = AuthManager.get_instance()
    thread_id = "test-thread-clear-001"
    _seed_obo(thread_id, "old-obo-token")

    assert auth_mgr.get_obo_token(thread_id) == "old-obo-token"
    assert auth_mgr.get_obo_jwt_raw(thread_id) == "old-obo-token"

    auth_mgr.clear_obo_tokens(thread_id)

    assert auth_mgr.get_obo_token(thread_id) is None
    assert auth_mgr.get_obo_jwt_raw(thread_id) is None


def test_get_real_wso2_authorization_url_clears_cached_tokens():
    auth_mgr = AuthManager.get_instance()
    thread_id = "test-thread-clear-002"
    _seed_obo(thread_id, "stale-obo-token")

    assert auth_mgr.get_obo_token(thread_id) == "stale-obo-token"

    url = auth_mgr.get_real_wso2_authorization_url(
        thread_id=thread_id,
        scopes=["create_meeting"],
        state_token="mock-state-token",
        agent_id="agent-id-1",
    )

    assert url.startswith("https://")
    # Starting a new authorization must not leave a previous OBO token usable.
    assert auth_mgr.get_obo_token(thread_id) is None
    assert auth_mgr.get_obo_jwt_raw(thread_id) is None


def test_clear_obo_tokens_unknown_thread_is_noop():
    auth_mgr = AuthManager.get_instance()
    auth_mgr.clear_obo_tokens("never-existed-thread")
    assert auth_mgr.get_obo_token("never-existed-thread") is None
    assert auth_mgr.get_obo_jwt_raw("never-existed-thread") is None


def test_expired_token_is_not_returned():
    auth_mgr = AuthManager.get_instance()
    _seed_obo("expired-thread", "long-gone", expires_in=-1)
    assert auth_mgr.get_obo_token("expired-thread") is None


def test_agent_jwt_falls_back_to_the_thread_less_token():
    # fetch_agent_token may run before a thread exists, storing under
    # "_default"; a later thread-scoped read should still find it.
    auth_mgr = AuthManager.get_instance()
    get_store().set(
        _NS_AGENT_TOKEN, "_default",
        {"token": "default-agent-token", "expires_at": time.time() + 3600},
    )
    assert auth_mgr.get_agent_jwt_raw("some-thread") == "default-agent-token"
    assert auth_mgr.get_agent_jwt_raw() == "default-agent-token"


def test_thread_scoped_agent_token_wins_over_the_default():
    auth_mgr = AuthManager.get_instance()
    for key, token in (("_default", "default-token"), ("t1", "thread-token")):
        get_store().set(
            _NS_AGENT_TOKEN, key,
            {"token": token, "expires_at": time.time() + 3600},
        )
    assert auth_mgr.get_agent_jwt_raw("t1") == "thread-token"


def test_pkce_verifier_survives_in_the_shared_store():
    """The multi-instance blocker, in miniature.

    /authorize writes the verifier and /callback reads it. With the state in
    the store rather than a process dict, a second AuthManager instance — the
    stand-in for another worker — can complete the exchange.
    """
    thread_id = "pkce-thread"
    AuthManager.get_instance().get_real_wso2_authorization_url(
        thread_id=thread_id,
        scopes=["create_meeting"],
        state_token="state",
        agent_id="agent-1",
    )

    other_worker = AuthManager()
    assert other_worker._store.get(_NS_PKCE, thread_id)


def test_reset_clears_everything():
    auth_mgr = AuthManager.get_instance()
    _seed_obo("t1", "tok")
    AuthManager.reset()
    assert auth_mgr.get_obo_token("t1") is None
