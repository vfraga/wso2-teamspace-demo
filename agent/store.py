"""Pluggable key-value storage for the agent's per-thread state.

`StateManager`, `AuthManager` and `ChatHistoryManager` were in-process
singletons, which made the agent single-instance by construction: `/authorize`
signs an OBO state on one worker and `/callback` arrives on another, which has
no record of the flow. Their public APIs are unchanged; only the backing
storage moved behind this interface.

Two implementations:

* `InMemoryStore` — **the default**, so the demo needs no extra
  infrastructure and behaves exactly as it did before.
* `RedisStore` — selected when ``REDIS_URL`` is set, so the agent can run
  multiple workers or replicas.

Values are JSON, which keeps what lands in Redis inspectable — useful for a
demo — and forces the managers to store plain data rather than live objects.

.. warning::
   With ``RedisStore``, OBO access tokens and per-org agent credentials are at
   rest in Redis. Require AUTH and TLS on the connection, and keep the TTL
   short. See the README's *Agent State Store* section.
"""

import json
import logging
import os
import threading
import time
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)

#: Per-thread state is scoped to a chat session, not kept forever.
DEFAULT_TTL_SECONDS = 24 * 60 * 60


class KeyValueStore(Protocol):
    """Namespaced key-value storage with per-entry expiry."""

    def get(self, namespace: str, key: str) -> Optional[Any]: ...

    def set(self, namespace: str, key: str, value: Any, ttl: int = DEFAULT_TTL_SECONDS) -> None: ...

    def delete(self, namespace: str, key: str) -> None: ...

    def clear(self, namespace: Optional[str] = None) -> None:
        """Drop one namespace, or everything this store owns when None.

        Namespace-scoped so a manager's `reset()` clears only its own data —
        `AuthManager.reset()` must not wipe chat history.
        """
        ...


def _compose(namespace: str, key: str) -> str:
    return f"teamspace:agent:{namespace}:{key}"


class InMemoryStore:
    """Process-local store. The default, and what the demo runs on.

    Expiry is checked lazily on read, which is enough for a single process and
    avoids a background sweeper thread.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict[str, tuple[Any, float]] = {}

    def get(self, namespace: str, key: str) -> Optional[Any]:
        composed = _compose(namespace, key)
        with self._lock:
            entry = self._data.get(composed)
            if entry is None:
                return None
            value, expires_at = entry
            if expires_at and time.monotonic() > expires_at:
                del self._data[composed]
                return None
            return value

    def set(self, namespace: str, key: str, value: Any, ttl: int = DEFAULT_TTL_SECONDS) -> None:
        with self._lock:
            expires_at = time.monotonic() + ttl if ttl else 0.0
            self._data[_compose(namespace, key)] = (value, expires_at)

    def delete(self, namespace: str, key: str) -> None:
        with self._lock:
            self._data.pop(_compose(namespace, key), None)

    def clear(self, namespace: Optional[str] = None) -> None:
        with self._lock:
            if namespace is None:
                self._data.clear()
                return
            prefix = _compose(namespace, "")
            for key in [k for k in self._data if k.startswith(prefix)]:
                del self._data[key]


class RedisStore:
    """Redis-backed store, so agent state survives restarts and is shared.

    Values are JSON-serialised. A Redis failure is logged and degrades to a
    miss rather than raising: losing a chat thread's state is recoverable,
    while a 500 in the middle of an OBO consent popup is not.
    """

    def __init__(self, url: str, key_prefix: str = ""):
        import redis  # imported here: an optional dependency (`[redis]` extra)

        self._redis = redis.Redis.from_url(url, decode_responses=True)
        self._prefix = key_prefix

    def _key(self, namespace: str, key: str) -> str:
        return f"{self._prefix}{_compose(namespace, key)}"

    def get(self, namespace: str, key: str) -> Optional[Any]:
        try:
            raw = self._redis.get(self._key(namespace, key))
        except Exception:
            logger.exception("Redis read failed for %s/%s; treating as a miss", namespace, key)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("Discarding non-JSON value at %s/%s", namespace, key)
            return None

    def set(self, namespace: str, key: str, value: Any, ttl: int = DEFAULT_TTL_SECONDS) -> None:
        try:
            payload = json.dumps(value)
        except (TypeError, ValueError):
            logger.exception("Refusing to store non-JSON value at %s/%s", namespace, key)
            return
        try:
            # `ex=` rather than setex(), which redis-py deprecated in 2.6.12.
            self._redis.set(self._key(namespace, key), payload, ex=ttl or None)
        except Exception:
            logger.exception("Redis write failed for %s/%s", namespace, key)

    def delete(self, namespace: str, key: str) -> None:
        try:
            self._redis.delete(self._key(namespace, key))
        except Exception:
            logger.exception("Redis delete failed for %s/%s", namespace, key)

    def ping(self) -> bool:
        """Round-trip to Redis, so a bad URL fails at startup not mid-request."""
        return bool(self._redis.ping())

    def clear(self, namespace: Optional[str] = None) -> None:
        """Delete this agent's keys — never `FLUSHDB`, the server may be shared."""
        scope = f"{namespace}:" if namespace else ""
        pattern = f"{self._prefix}teamspace:agent:{scope}*"
        try:
            for chunk in _batched(self._redis.scan_iter(match=pattern, count=500), 500):
                if chunk:
                    self._redis.delete(*chunk)
        except Exception:
            logger.exception("Redis clear failed for pattern %s", pattern)


def _batched(iterable, size: int):
    """Yield lists of at most `size` items. (itertools.batched is 3.12+.)"""
    batch = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


_store: Optional[KeyValueStore] = None
_store_lock = threading.Lock()


def build_store() -> KeyValueStore:
    """Construct the store the environment asks for.

    Falls back to `InMemoryStore` when ``REDIS_URL`` is unset, and also when
    Redis is configured but unusable — the demo should still run, loudly
    degraded, rather than refuse to boot.
    """
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        logger.info("Agent state store: in-memory (set REDIS_URL to share state across instances)")
        return InMemoryStore()
    try:
        store = RedisStore(url, key_prefix=os.getenv("REDIS_KEY_PREFIX", ""))
        store.ping()
    except ImportError:
        logger.error(
            "REDIS_URL is set but the redis package is not installed. "
            "Install the extra (`uv sync --extra redis`) or unset REDIS_URL. "
            "Falling back to in-memory state — the agent cannot scale past one instance."
        )
        return InMemoryStore()
    except Exception:
        logger.exception(
            "REDIS_URL is set but Redis is unreachable. Falling back to in-memory "
            "state — the agent cannot scale past one instance."
        )
        return InMemoryStore()
    logger.info("Agent state store: Redis (state shared across instances)")
    return store


def get_store() -> KeyValueStore:
    """The process-wide store, built on first use."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = build_store()
    return _store


def set_store(store: Optional[KeyValueStore]) -> None:
    """Replace the store. For tests, which parametrise over both backends."""
    global _store
    with _store_lock:
        _store = store
