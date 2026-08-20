"""Tests for the agent's pluggable state store.

Both backends run against one shared suite, so `RedisStore` cannot silently
diverge from the `InMemoryStore` the demo defaults to. The Redis cases skip
when no server is reachable — the same pattern `tests/conftest.py` uses for the
live WSO2 fixture.
"""
import os

import pytest

from agent.chat_history import ChatHistoryManager
from agent.state_manager import FlowState, FrontendState, StateManager
from agent.store import (
    DEFAULT_TTL_SECONDS,
    InMemoryStore,
    build_store,
    get_store,
    set_store,
)

REDIS_TEST_URL = os.getenv("REDIS_TEST_URL", "redis://127.0.0.1:6379/15")


def _redis_store_or_skip():
    try:
        import redis  # noqa: F401
    except ImportError:
        pytest.skip("redis package not installed (optional `[redis]` extra)")
    from agent.store import RedisStore

    try:
        store = RedisStore(REDIS_TEST_URL, key_prefix="pytest:")
        store._redis.ping()
    except Exception:
        pytest.skip(f"no Redis reachable at {REDIS_TEST_URL}")
    store.clear()
    return store


@pytest.fixture(params=["memory", "redis"])
def store(request):
    if request.param == "memory":
        impl = InMemoryStore()
    else:
        impl = _redis_store_or_skip()
    set_store(impl)
    yield impl
    impl.clear()
    set_store(None)


# --- store contract --------------------------------------------------------


def test_missing_key_returns_none(store):
    assert store.get("ns", "nope") is None


def test_roundtrip_preserves_json_types(store):
    payload = {"a": 1, "b": [1, 2, 3], "c": None, "d": "text", "e": True}
    store.set("ns", "k", payload)
    assert store.get("ns", "k") == payload


def test_namespaces_are_isolated(store):
    store.set("ns1", "k", "one")
    store.set("ns2", "k", "two")
    assert store.get("ns1", "k") == "one"
    assert store.get("ns2", "k") == "two"


def test_overwrite_replaces_the_value(store):
    store.set("ns", "k", "first")
    store.set("ns", "k", "second")
    assert store.get("ns", "k") == "second"


def test_delete_removes_the_value(store):
    store.set("ns", "k", "v")
    store.delete("ns", "k")
    assert store.get("ns", "k") is None


def test_delete_of_a_missing_key_is_a_noop(store):
    store.delete("ns", "never-set")


def test_clear_removes_everything(store):
    store.set("ns1", "a", 1)
    store.set("ns2", "b", 2)
    store.clear()
    assert store.get("ns1", "a") is None
    assert store.get("ns2", "b") is None


def test_an_already_expired_entry_reads_as_missing(store):
    # A negative TTL is meaningless to Redis, so assert on the in-memory
    # backend where lazy expiry is observable; Redis expiry is its own concern.
    if not isinstance(store, InMemoryStore):
        pytest.skip("Redis rejects a non-positive TTL")
    store.set("ns", "k", "v", ttl=-1)
    assert store.get("ns", "k") is None


# --- backend selection -----------------------------------------------------


def test_defaults_to_in_memory_without_redis_url(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert isinstance(build_store(), InMemoryStore)


def test_unreachable_redis_degrades_to_in_memory(monkeypatch, caplog):
    # The demo must still boot with a bad REDIS_URL, loudly degraded.
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    with caplog.at_level("ERROR"):
        store = build_store()
    assert isinstance(store, InMemoryStore)
    assert "in-memory" in caplog.text.lower()


def test_get_store_is_a_singleton(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    set_store(None)
    try:
        assert get_store() is get_store()
    finally:
        set_store(None)


# --- the managers, over both backends -------------------------------------


def test_state_manager_roundtrips_the_flow_state(store):
    mgr = StateManager.get_instance()
    assert mgr.get_state("t1") == FlowState.INITIAL

    mgr.set_state("t1", FlowState.BOOKING_PREVIEW_INITIATED)
    assert mgr.get_state("t1") == FlowState.BOOKING_PREVIEW_INITIATED
    assert mgr.get_frontend_state("t1") == FrontendState.AWAITING_AUTHORIZATION

    mgr.set_state("t1", FlowState.BOOKING_AUTHORIZED)
    assert mgr.get_frontend_state("t1") == FrontendState.BOOKING_COMPLETE


def test_state_manager_survives_an_unknown_stored_state(store):
    # A rolling deploy can leave a value written by another agent version.
    store.set("flow_state", "t1", "SOMETHING_FROM_THE_FUTURE")
    assert StateManager.get_instance().get_state("t1") == FlowState.INITIAL


def test_agent_credentials_roundtrip_as_a_tuple(store):
    mgr = StateManager.get_instance()
    assert mgr.get_agent_credentials("t1") == ("", "")

    mgr.set_agent_credentials("t1", "agent-1", "secret-1")
    # JSON stores the pair as a list; callers must still get a tuple.
    assert mgr.get_agent_credentials("t1") == ("agent-1", "secret-1")


def test_agent_name_defaults_and_overrides(store):
    mgr = StateManager.get_instance()
    assert mgr.get_agent_name("t1") == "Worklink Assistant"
    mgr.set_agent_name("t1", "Custom Bot")
    assert mgr.get_agent_name("t1") == "Custom Bot"


def test_pending_meeting_roundtrip_and_clear(store):
    mgr = StateManager.get_instance()
    meeting = {"topic": "Standup", "date": "2026-01-01", "duration": "30"}
    mgr.set_pending_meeting("t1", meeting)
    assert mgr.get_pending_meeting("t1") == meeting
    mgr.clear_pending_meeting("t1")
    assert mgr.get_pending_meeting("t1") is None


def test_clear_state_removes_every_namespace_for_the_thread(store):
    mgr = StateManager.get_instance()
    mgr.set_state("t1", FlowState.BOOKING_AUTHORIZED)
    mgr.set_pending_meeting("t1", {"topic": "x"})
    mgr.set_auth_url("t1", "https://example.com/authorize")
    mgr.set_agent_credentials("t1", "a", "s")
    mgr.set_agent_name("t1", "Bot")
    mgr.set_org_name("t1", "acme")

    mgr.clear_state("t1")

    assert mgr.get_state("t1") == FlowState.INITIAL
    assert mgr.get_pending_meeting("t1") is None
    assert mgr.get_auth_url("t1") is None
    assert mgr.get_agent_credentials("t1") == ("", "")
    assert mgr.get_org_name("t1") == ""


def test_clear_state_leaves_other_threads_alone(store):
    mgr = StateManager.get_instance()
    mgr.set_org_name("t1", "acme")
    mgr.set_org_name("t2", "other")
    mgr.clear_state("t1")
    assert mgr.get_org_name("t2") == "other"


def test_chat_history_appends_and_truncates(store):
    history = ChatHistoryManager(max_messages=3)
    for i in range(5):
        history.add_message("t1", "user", f"msg-{i}")
    stored = history.get_history("t1")
    assert [m["content"] for m in stored] == ["msg-2", "msg-3", "msg-4"]


def test_chat_history_is_per_thread_and_clearable(store):
    history = ChatHistoryManager(max_messages=10)
    history.add_message("t1", "user", "hello")
    history.add_message("t2", "user", "other")
    assert len(history.get_history("t1")) == 1

    history.clear("t1")
    assert history.get_history("t1") == []
    assert len(history.get_history("t2")) == 1


def test_state_is_visible_to_a_second_manager_instance(store):
    """The point of the whole change: state is not process-local.

    Two manager instances stand in for two agent workers. With the old
    in-process dicts the second would see nothing.
    """
    StateManager().set_state("shared-thread", FlowState.UPDATE_PREVIEW_INITIATED)
    assert StateManager().get_state("shared-thread") == FlowState.UPDATE_PREVIEW_INITIATED

    ChatHistoryManager(max_messages=10).add_message("shared-thread", "user", "hi")
    assert len(ChatHistoryManager(max_messages=10).get_history("shared-thread")) == 1


def test_default_ttl_is_a_sane_session_length():
    # Long enough for a working day's chat thread, short enough that abandoned
    # OBO tokens don't linger in Redis indefinitely.
    assert 3600 <= DEFAULT_TTL_SECONDS <= 7 * 24 * 3600


# --- reset() isolation -----------------------------------------------------
#
# Each manager's reset() clears only its own namespaces. Wiping the whole store
# would mean AuthManager.reset() silently discarded chat history and flow state
# — invisible in the test suite, which resets all three together, but a trap for
# any other caller.


def _seed_all(store):
    from agent.auth_manager import _NS_OBO_TOKEN

    StateManager.get_instance().set_state("t1", FlowState.BOOKING_AUTHORIZED)
    ChatHistoryManager(max_messages=10).add_message("t1", "user", "hello")
    store.set(_NS_OBO_TOKEN, "t1", {"token": "tok", "expires_at": 9_999_999_999})


def test_auth_manager_reset_leaves_state_and_history(store):
    from agent.auth_manager import AuthManager, _NS_OBO_TOKEN

    _seed_all(store)
    AuthManager.reset()

    assert store.get(_NS_OBO_TOKEN, "t1") is None
    assert StateManager.get_instance().get_state("t1") == FlowState.BOOKING_AUTHORIZED
    assert len(ChatHistoryManager(max_messages=10).get_history("t1")) == 1


def test_state_manager_reset_leaves_tokens_and_history(store):
    from agent.auth_manager import _NS_OBO_TOKEN

    _seed_all(store)
    StateManager.reset()

    assert StateManager.get_instance().get_state("t1") == FlowState.INITIAL
    assert store.get(_NS_OBO_TOKEN, "t1") is not None
    assert len(ChatHistoryManager(max_messages=10).get_history("t1")) == 1


def test_chat_history_reset_leaves_tokens_and_state(store):
    from agent.auth_manager import _NS_OBO_TOKEN

    _seed_all(store)
    ChatHistoryManager.reset()

    assert ChatHistoryManager(max_messages=10).get_history("t1") == []
    assert StateManager.get_instance().get_state("t1") == FlowState.BOOKING_AUTHORIZED
    assert store.get(_NS_OBO_TOKEN, "t1") is not None


def test_clear_with_no_namespace_still_wipes_everything(store):
    _seed_all(store)
    store.clear()
    assert StateManager.get_instance().get_state("t1") == FlowState.INITIAL
    assert ChatHistoryManager(max_messages=10).get_history("t1") == []


def test_namespace_clear_does_not_touch_a_similarly_named_namespace(store):
    # "org_name" and "org_name_extra" must not clear each other.
    store.set("org_name", "t1", "acme")
    store.set("agent_name", "t1", "bot")
    store.clear("agent_name")
    assert store.get("org_name", "t1") == "acme"
    assert store.get("agent_name", "t1") is None
