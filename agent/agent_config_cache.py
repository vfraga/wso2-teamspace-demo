import logging
import threading
import time
from typing import Optional

import httpx

from agent.config import service_token_client, settings

logger = logging.getLogger(__name__)

_AGENT_CONFIG_TTL_SECONDS = 300
_CACHE_LOCK = threading.Lock()
_cache: dict[str, tuple[float, dict | None]] = {}


def _cache_key(org_id: str) -> str:
    return org_id


def _is_fresh(entry: tuple[float, dict | None]) -> bool:
    fetched_at, _ = entry
    return (time.monotonic() - fetched_at) < _AGENT_CONFIG_TTL_SECONDS


def invalidate_agent_config_cache(org_id: str) -> None:
    with _CACHE_LOCK:
        _cache.pop(_cache_key(org_id), None)
    logger.debug("Invalidated agent config cache for org=%s", org_id)


async def fetch_and_cache_agent_config(
    org_id: str,
    *,
    force_refresh: bool = False,
) -> Optional[dict]:
    """Fetch the agent configuration for ``org_id`` from the Business API.

    Uses the M2M service-token channel so the agent service can read its own
    configuration without requiring the calling user to hold the
    ``view_agent_config`` scope. Results are cached for
    ``_AGENT_CONFIG_TTL_SECONDS`` per ``org_id``.

    Returns the parsed config dict on success, ``None`` if the API rejects
    the request (404, 401, 503) or returns a non-200 response. Cached
    ``None`` results are NOT served — a subsequent call retries.
    """
    key = _cache_key(org_id)
    if not force_refresh:
        with _CACHE_LOCK:
            entry = _cache.get(key)
            if entry is not None and _is_fresh(entry):
                _, cached = entry
                if cached is not None:
                    logger.debug("Agent config cache hit for org=%s", org_id)
                    return cached

    headers = await service_token_client.aauth_headers()
    if not headers:
        logger.warning(
            "No service token available on the agent; cannot fetch agent config via M2M"
        )
        return None

    url = f"{settings.BUSINESS_API_URL.rstrip('/')}/agent-config/org/{org_id}"

    async def _get(request_headers: dict) -> Optional[httpx.Response]:
        try:
            async with httpx.AsyncClient(verify=settings.SERVICE_VERIFY_TLS, timeout=10.0) as client:
                return await client.get(url, headers=request_headers)
        except httpx.RequestError:
            logger.exception("M2M fetch of agent config failed for org=%s", org_id)
            return None

    resp = await _get(headers)
    if resp is None:
        return None

    if resp.status_code == 401:
        # The cached token may have been revoked or the IS keys rotated. Mint a
        # fresh one and retry exactly once before giving up.
        logger.info("Service token rejected by the Business API; retrying with a fresh token")
        service_token_client.invalidate()
        retry_headers = await service_token_client.aauth_headers(force_refresh=True)
        if not retry_headers:
            return None
        resp = await _get(retry_headers)
        if resp is None:
            return None

    if resp.status_code != 200:
        logger.warning(
            "M2M fetch of agent config returned %s for org=%s",
            resp.status_code, org_id,
        )
        return None

    try:
        data = resp.json()
    except ValueError:
        logger.exception("M2M fetch of agent config returned non-JSON for org=%s", org_id)
        return None

    with _CACHE_LOCK:
        _cache[key] = (time.monotonic(), data)
    logger.info("Fetched and cached agent config for org=%s via M2M", org_id)
    return data
