from enum import Enum
import threading
import time


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


class StateManager:
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self._states: dict[str, FlowState] = {}
        self._pending_meetings: dict[str, dict] = {}
        self._auth_urls: dict[str, str] = {}
        self._agent_credentials: dict[str, tuple[str, str]] = {}
        self._agent_names: dict[str, str] = {}
        self._org_names: dict[str, str] = {}
        self._last_access: dict[str, float] = {}

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls):
        inst = cls.get_instance()
        with inst._lock:
            inst._states.clear()
            inst._pending_meetings.clear()
            inst._auth_urls.clear()
            inst._agent_credentials.clear()
            inst._agent_names.clear()
            inst._org_names.clear()
            inst._last_access.clear()

    def _touch(self, thread_id: str):
        self._last_access[thread_id] = time.time()

    def get_state(self, thread_id: str) -> FlowState:
        self._touch(thread_id)
        return self._states.get(thread_id, FlowState.INITIAL)

    def set_state(self, thread_id: str, state: FlowState):
        with self._lock:
            self._states[thread_id] = state
            self._touch(thread_id)

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

    def set_pending_meeting(self, thread_id: str, meeting: dict):
        with self._lock:
            self._pending_meetings[thread_id] = meeting
            self._touch(thread_id)

    def get_pending_meeting(self, thread_id: str) -> dict | None:
        self._touch(thread_id)
        return self._pending_meetings.get(thread_id)

    def clear_pending_meeting(self, thread_id: str):
        with self._lock:
            self._pending_meetings.pop(thread_id, None)

    def set_auth_url(self, thread_id: str, url: str):
        with self._lock:
            self._auth_urls[thread_id] = url
            self._touch(thread_id)

    def get_auth_url(self, thread_id: str) -> str | None:
        self._touch(thread_id)
        return self._auth_urls.get(thread_id)

    def set_agent_credentials(self, thread_id: str, agent_id: str, agent_secret: str):
        with self._lock:
            self._agent_credentials[thread_id] = (agent_id, agent_secret)
            self._touch(thread_id)

    def get_agent_credentials(self, thread_id: str) -> tuple[str, str]:
        self._touch(thread_id)
        return self._agent_credentials.get(thread_id, ("", ""))

    def set_org_name(self, thread_id: str, org_name: str):
        with self._lock:
            self._org_names[thread_id] = org_name
            self._touch(thread_id)

    def get_org_name(self, thread_id: str) -> str:
        self._touch(thread_id)
        return self._org_names.get(thread_id, "")

    def set_agent_name(self, thread_id: str, agent_name: str):
        with self._lock:
            self._agent_names[thread_id] = agent_name
            self._touch(thread_id)

    def get_agent_name(self, thread_id: str) -> str:
        self._touch(thread_id)
        return self._agent_names.get(thread_id, "Worklink Assistant")

    def clear_state(self, thread_id: str):
        with self._lock:
            self._states.pop(thread_id, None)
            self._pending_meetings.pop(thread_id, None)
            self._auth_urls.pop(thread_id, None)
            self._agent_credentials.pop(thread_id, None)
            self._agent_names.pop(thread_id, None)
            self._org_names.pop(thread_id, None)
            self._last_access.pop(thread_id, None)
