import time
from unittest.mock import patch

from agent.auth_manager import AuthManager


def _seeded_cache(auth_mgr, thread_id, token, jwt_raw):
    return (
        patch.object(
            auth_mgr,
            "_obo_tokens",
            {thread_id: {"token": token, "expires_at": time.time() + 3600}},
        ),
        patch.object(
            auth_mgr,
            "_obo_jwt_raw",
            {thread_id: {"token": jwt_raw, "expires_at": time.time() + 3600}},
        ),
    )


def test_clear_obo_tokens_removes_cached_entry():
    auth_mgr = AuthManager.get_instance()
    thread_id = "test-thread-clear-001"

    token_patch, jwt_patch = _seeded_cache(
        auth_mgr, thread_id, "old-obo-token", "old-obo-jwt-raw"
    )
    with token_patch, jwt_patch:
        assert auth_mgr.get_obo_token(thread_id) == "old-obo-token"
        assert auth_mgr.get_obo_jwt_raw(thread_id) == "old-obo-jwt-raw"

        auth_mgr.clear_obo_tokens(thread_id)

        assert auth_mgr.get_obo_token(thread_id) is None
        assert auth_mgr.get_obo_jwt_raw(thread_id) is None


def test_get_real_wso2_authorization_url_clears_cached_tokens():
    auth_mgr = AuthManager.get_instance()
    thread_id = "test-thread-clear-002"

    token_patch, jwt_patch = _seeded_cache(
        auth_mgr, thread_id, "stale-obo-token", "stale-obo-jwt-raw"
    )
    with token_patch, jwt_patch:
        assert auth_mgr.get_obo_token(thread_id) == "stale-obo-token"
        assert auth_mgr.get_obo_jwt_raw(thread_id) == "stale-obo-jwt-raw"

        url = auth_mgr.get_real_wso2_authorization_url(
            thread_id=thread_id,
            scopes=["create_meeting"],
            state_token="mock-state-token",
            agent_id="agent-id-1",
        )

        assert url.startswith("https://")
        assert auth_mgr.get_obo_token(thread_id) is None
        assert auth_mgr.get_obo_jwt_raw(thread_id) is None


def test_clear_obo_tokens_unknown_thread_is_noop():
    auth_mgr = AuthManager.get_instance()
    auth_mgr.clear_obo_tokens("never-existed-thread")
    assert auth_mgr.get_obo_token("never-existed-thread") is None
    assert auth_mgr.get_obo_jwt_raw("never-existed-thread") is None
