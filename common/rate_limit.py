"""Shared rate-limit configuration.

Applied to the chat and OAuth endpoints, which are the ones that cost money
(Gemini calls) or drive an external identity provider. Storage is in-memory by
default so the demo needs no extra infrastructure, and switches to ``REDIS_URL``
when set — which is also what makes the limits meaningful across workers,
since an in-memory limiter counts per process.

Limits are deliberately generous: a demo walkthrough, including someone
clicking through the OBO consent flow several times, must never trip them.
"""

import logging
import os
import threading

logger = logging.getLogger(__name__)

#: Chat endpoints — these reach Gemini, so they are the expensive ones.
CHAT_LIMIT = os.getenv("RATE_LIMIT_CHAT", "60/minute")

#: OAuth authorize/callback. Higher, because a single booking flow issues
#: several requests and a stuck popup can retry.
AUTH_LIMIT = os.getenv("RATE_LIMIT_AUTH", "120/minute")

#: Global backstop applied to every request, empty to disable.
#
#: This layer matters because FastAPI resolves a route's `dependencies` — and
#: therefore `require_service_auth` — *before* calling the handler, so a
#: per-route limit on /chat only ever counts requests that already passed
#: authentication. Without a default, an unauthenticated flood of garbage
#: tokens would be unthrottled while still costing signature verification.
#: The middleware runs ahead of routing, so this covers the 401 path too.
#:
#: Set well above any real demo usage — including container health probes,
#: which it also applies to.
DEFAULT_LIMIT = os.getenv("RATE_LIMIT_DEFAULT", "600/minute")

#: Set to "false" to turn rate limiting off entirely.
ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").strip().lower() not in ("0", "false", "no")


def storage_uri() -> str:
    """Limiter storage backend: Redis when configured, else in-process memory."""
    redis_url = os.getenv("REDIS_URL", "").strip()
    if redis_url:
        return redis_url
    return "memory://"


def describe() -> str:
    backend = "redis" if storage_uri() != "memory://" else "in-memory (per-process)"
    if not ENABLED:
        return "rate limiting disabled (RATE_LIMIT_ENABLED=false)"
    return (
        f"rate limiting active, storage={backend}, chat={CHAT_LIMIT}, "
        f"auth={AUTH_LIMIT}, default={DEFAULT_LIMIT or 'none'}"
    )


# ---------------------------------------------------------------------------
# FastAPI enforcement
#
# Built directly on `limits` (the library slowapi wraps) rather than on slowapi
# itself, for one reason: ordering. slowapi enforces a limit by decorating the
# endpoint function, which FastAPI calls only *after* resolving the route's
# `dependencies` — so a limit on /chat would never see a request rejected by
# `require_service_auth`, leaving an unauthenticated flood unthrottled. Its
# middleware doesn't cover the gap either, because it skips routes that carry an
# explicit decorator.
#
# A dependency can be ordered ahead of the auth dependency, which is what the
# endpoints in agent/main.py do.
# ---------------------------------------------------------------------------

_MEMORY_URI = "memory://"


class _LimiterRegistry:
    """Lazily-built limiter and per-spec parsed rate items."""

    def __init__(self):
        self._lock = threading.Lock()
        self._limiter = None
        self._storage_uri = None
        self._items: dict[str, object] = {}

    def _ensure(self):
        from limits.storage import storage_from_string
        from limits.strategies import FixedWindowRateLimiter

        uri = storage_uri()
        with self._lock:
            if self._limiter is None or self._storage_uri != uri:
                self._limiter = FixedWindowRateLimiter(storage_from_string(uri))
                self._storage_uri = uri
            return self._limiter

    def item(self, spec: str):
        with self._lock:
            if spec not in self._items:
                from limits import parse

                self._items[spec] = parse(spec)
            return self._items[spec]

    def hit(self, spec: str, identity: str) -> bool:
        """Consume one unit. False means the limit is exhausted."""
        return self._ensure().hit(self.item(spec), identity)

    def reset(self) -> None:
        with self._lock:
            self._limiter = None
            self._storage_uri = None
            self._items.clear()


_registry = _LimiterRegistry()


def reset_limiter_state() -> None:
    """Drop cached limiter state. For tests that change the configuration."""
    _registry.reset()


def client_identity(request) -> str:
    """Best-effort client key: the first X-Forwarded-For hop, else the peer IP.

    Behind a proxy the peer is the proxy, so every caller would share one
    bucket. Only trust the header when a proxy is actually in front of the
    service, which is what RATE_LIMIT_TRUST_FORWARDED_FOR declares.
    """
    if os.getenv("RATE_LIMIT_TRUST_FORWARDED_FOR", "").strip().lower() in ("1", "true", "yes"):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"


def rate_limit_dependency(spec_name: str):
    """Build a FastAPI dependency enforcing one of this module's limits.

    Takes the *name* of the module-level constant rather than its value, so a
    test (or a reload) that changes the limit takes effect without rebuilding
    the route.
    """
    from fastapi import HTTPException, Request

    async def _check(request: Request) -> None:
        if not ENABLED:
            return
        spec = globals().get(spec_name) or ""
        if not spec:
            return
        identity = f"{spec_name}:{client_identity(request)}"
        if not _registry.hit(spec, identity):
            logger.warning(
                "Rate limit %s (%s) hit by %s on %s",
                spec_name, spec, identity, request.url.path,
            )
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded: {spec}",
                headers={"Retry-After": "60"},
            )

    return _check
