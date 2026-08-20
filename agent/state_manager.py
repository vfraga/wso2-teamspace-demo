"""Per-thread OBO flow state.

The public API is unchanged from the original in-process version; only the
backing storage moved behind `agent/store.py`, so the agent can run more than
one worker without `/callback` landing on a process that never saw `/authorize`.
"""

from enum import Enum

from agent.store import DEFAULT_TTL_SECONDS, get_store
from common.constants import DEFAULT_AGENT_NAME


class FlowState(Enum):
    INITIAL = "INITIAL"
    BOOKING_PREVIEW_INITIATED = "BOOKING_PREVIEW_INITIATED"
    BOOKING_AUTHORIZED = "BOOKING_AUTHORIZED"
    LIST_PREVIEW_INITIATED = "LIST_PREVIEW_INITIATED"
    LIST_AUTHORIZED = "LIST_AUTHORIZED"
    UPDATE_PREVIEW_INITIATED = "UPDATE_PREVIEW_INITIATED"
    UPDATE_AUTHORIZED = "UPDATE_AUTHORIZED"
    DELETE_PREVIEW_INITIATED = "DELETE_PREVIEW_INITIATED"
    DELETE_AUTHORIZED = "DELETE_AUTHORIZED"


class FrontendState(Enum):
    IDLE = "IDLE"
    AWAITING_AUTHORIZATION = "AWAITING_AUTHORIZATION"
    BOOKING_COMPLETE = "BOOKING_COMPLETE"


# Store namespaces, one per logical map the old implementation kept as a dict.
_NS_STATE = "flow_state"
_NS_PENDING = "pending_meeting"
_NS_AUTH_URL = "auth_url"
_NS_CREDS = "agent_credentials"
_NS_AGENT_NAME = "agent_name"
_NS_ORG_NAME = "org_name"

_ALL_NAMESPACES = (
    _NS_STATE, _NS_PENDING, _NS_AUTH_URL, _NS_CREDS, _NS_AGENT_NAME, _NS_ORG_NAME,
)


class StateManager:
    _instance = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls):
        """Clear this manager's data only.

        Scoped per namespace rather than wiping the whole store, so resetting
        flow state does not also discard OBO tokens or chat history.
        """
        store = get_store()
        for namespace in _ALL_NAMESPACES:
            store.clear(namespace)

    # -- internals ---------------------------------------------------------
    #
    # Every write refreshes the entry's TTL, which is what the old
    # `_last_access` bookkeeping approximated by hand.

    @property
    def _store(self):
        return get_store()

    def _put(self, namespace: str, thread_id: str, value):
        self._store.set(namespace, thread_id, value, ttl=DEFAULT_TTL_SECONDS)

    def _fetch(self, namespace: str, thread_id: str, default=None):
        value = self._store.get(namespace, thread_id)
        return default if value is None else value

    # -- flow state --------------------------------------------------------

    def get_state(self, thread_id: str) -> FlowState:
        raw = self._fetch(_NS_STATE, thread_id)
        if raw is None:
            return FlowState.INITIAL
        try:
            return FlowState(raw)
        except ValueError:
            # An unknown value means a newer/older agent wrote it. Treat the
            # flow as fresh rather than crashing mid-conversation.
            return FlowState.INITIAL

    def set_state(self, thread_id: str, state: FlowState):
        self._put(_NS_STATE, thread_id, state.value)

    def get_frontend_state(self, thread_id: str) -> FrontendState:
        state = self.get_state(thread_id)
        if state in (
            FlowState.BOOKING_PREVIEW_INITIATED,
            FlowState.LIST_PREVIEW_INITIATED,
            FlowState.UPDATE_PREVIEW_INITIATED,
            FlowState.DELETE_PREVIEW_INITIATED,
        ):
            return FrontendState.AWAITING_AUTHORIZATION
        if state in (
            FlowState.BOOKING_AUTHORIZED,
            FlowState.LIST_AUTHORIZED,
            FlowState.UPDATE_AUTHORIZED,
            FlowState.DELETE_AUTHORIZED,
        ):
            return FrontendState.BOOKING_COMPLETE
        return FrontendState.IDLE

    # -- pending meeting ---------------------------------------------------

    def set_pending_meeting(self, thread_id: str, meeting: dict):
        self._put(_NS_PENDING, thread_id, meeting)

    def get_pending_meeting(self, thread_id: str) -> dict | None:
        return self._fetch(_NS_PENDING, thread_id)

    def clear_pending_meeting(self, thread_id: str):
        self._store.delete(_NS_PENDING, thread_id)

    # -- authorization URL -------------------------------------------------

    def set_auth_url(self, thread_id: str, url: str):
        self._put(_NS_AUTH_URL, thread_id, url)

    def get_auth_url(self, thread_id: str) -> str | None:
        return self._fetch(_NS_AUTH_URL, thread_id)

    # -- agent credentials -------------------------------------------------

    def set_agent_credentials(self, thread_id: str, agent_id: str, agent_secret: str):
        self._put(_NS_CREDS, thread_id, [agent_id, agent_secret])

    def get_agent_credentials(self, thread_id: str) -> tuple[str, str]:
        stored = self._fetch(_NS_CREDS, thread_id)
        if not stored:
            return ("", "")
        # JSON round-trips the tuple as a list.
        return (stored[0], stored[1])

    # -- org / agent names -------------------------------------------------

    def set_org_name(self, thread_id: str, org_name: str):
        self._put(_NS_ORG_NAME, thread_id, org_name)

    def get_org_name(self, thread_id: str) -> str:
        return self._fetch(_NS_ORG_NAME, thread_id, "")

    def set_agent_name(self, thread_id: str, agent_name: str):
        self._put(_NS_AGENT_NAME, thread_id, agent_name)

    def get_agent_name(self, thread_id: str) -> str:
        return self._fetch(_NS_AGENT_NAME, thread_id, DEFAULT_AGENT_NAME)

    # -- teardown ----------------------------------------------------------

    def clear_state(self, thread_id: str):
        for namespace in _ALL_NAMESPACES:
            self._store.delete(namespace, thread_id)
