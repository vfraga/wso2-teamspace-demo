import threading


class ChatHistoryManager:
    _instance = None
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(cls, max_messages: int = 50):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls(max_messages)
        return cls._instance

    def __init__(self, max_messages: int = 50):
        self._histories: dict[str, list[dict]] = {}
        self._lock = threading.Lock()
        self._max = max_messages

    def get_history(self, thread_id: str) -> list[dict]:
        with self._lock:
            return list(self._histories.get(thread_id, []))

    def add_message(self, thread_id: str, role: str, content: str):
        with self._lock:
            if thread_id not in self._histories:
                self._histories[thread_id] = []
            self._histories[thread_id].append({"role": role, "content": content})
            if len(self._histories[thread_id]) > self._max:
                self._histories[thread_id] = self._histories[thread_id][-self._max:]

    def clear(self, thread_id: str):
        with self._lock:
            self._histories.pop(thread_id, None)

    @classmethod
    def reset(cls):
        inst = cls.get_instance()
        with inst._lock:
            inst._histories.clear()
