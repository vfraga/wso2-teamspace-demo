"""Per-thread chat history.

Public API unchanged; storage moved behind `agent/store.py` so a conversation
survives a restart and is visible to every agent worker.
"""

from agent.store import DEFAULT_TTL_SECONDS, get_store

_NS_HISTORY = "chat_history"


class ChatHistoryManager:
    _instance = None

    @classmethod
    def get_instance(cls, max_messages: int = 50):
        if cls._instance is None:
            cls._instance = cls(max_messages)
        return cls._instance

    def __init__(self, max_messages: int = 50):
        self._max = max_messages

    @property
    def _store(self):
        return get_store()

    def get_history(self, thread_id: str) -> list[dict]:
        stored = self._store.get(_NS_HISTORY, thread_id)
        return list(stored) if stored else []

    def add_message(self, thread_id: str, role: str, content: str):
        # Read-modify-write. Two concurrent messages on one thread could lose
        # an entry, which is acceptable for a transcript and avoids needing a
        # distributed lock; the OBO state that must not be lost lives in
        # StateManager and is written whole.
        history = self.get_history(thread_id)
        history.append({"role": role, "content": content})
        if len(history) > self._max:
            history = history[-self._max:]
        self._store.set(_NS_HISTORY, thread_id, history, ttl=DEFAULT_TTL_SECONDS)

    def clear(self, thread_id: str):
        self._store.delete(_NS_HISTORY, thread_id)

    @classmethod
    def reset(cls):
        """Clear chat history only, not the OBO state sharing the store."""
        get_store().clear(_NS_HISTORY)
