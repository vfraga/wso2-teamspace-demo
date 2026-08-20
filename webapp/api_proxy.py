import logging
from typing import Any, NamedTuple

import requests
from flask import session, current_app

from common.constants import DEFAULT_PLAN
from webapp.service_auth import service_token_client

logger = logging.getLogger(__name__)


class ApiResult(NamedTuple):
    status_code: int
    data: Any


def api_request(method: str, path: str, token: str = None, **kwargs) -> requests.Response:
    base = current_app.config["BUSINESS_API_URL"]
    if not token:
        token = session.get("access_token", "")
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    headers.setdefault("Content-Type", "application/json")
    url = f"{base}{path}"
    try:
        timeout = kwargs.pop("timeout", (3, 10))
        resp = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
        if resp.status_code >= 400:
            logger.error("Business API error: %s %s -> %s: %s", method.upper(), path, resp.status_code, resp.text[:200])
        else:
            logger.debug("Business API response: %s %s -> %s", method.upper(), path, resp.status_code)
        return resp
    except requests.exceptions.RequestException as e:
        logger.error("Business API connection failed: %s %s -> %s", method.upper(), path, e)
        mock_resp = requests.Response()
        mock_resp.status_code = 503
        mock_resp._content = b'{"detail": "Business API offline"}'
        return mock_resp



def _parse_response(resp: requests.Response) -> ApiResult:
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    return ApiResult(status_code=resp.status_code, data=data)


def get_meetings() -> list[dict[str, Any]]:
    resp = api_request("GET", "/meetings")
    if resp.status_code == 200:
        return resp.json()
    return []


def create_meeting(data: dict) -> ApiResult:
    return _parse_response(api_request("POST", "/meetings", json=data))


def update_meeting(meeting_id: str, data: dict) -> ApiResult:
    return _parse_response(api_request("PUT", f"/meetings/{meeting_id}", json=data))


def delete_meeting(meeting_id: str) -> ApiResult:
    return _parse_response(api_request("DELETE", f"/meetings/{meeting_id}"))


def get_personalization(org_id: str) -> dict | None:
    resp = api_request("GET", f"/personalization/org/{org_id}")
    if resp.status_code == 200:
        return resp.json()
    return None


def upsert_personalization(data: dict) -> ApiResult:
    return _parse_response(api_request("POST", "/personalization", json=data))


def delete_personalization(org_id: str) -> ApiResult:
    return _parse_response(api_request("DELETE", f"/personalization/org/{org_id}"))


def get_agent_config(org_id: str) -> dict | None:
    resp = api_request("GET", f"/agent-config/org/{org_id}")
    if resp.status_code == 200:
        return resp.json()
    return None


def get_agent_config_via_service_token(org_id: str) -> dict | None:
    """Fetch the agent config from the Business API using an M2M service token.

    The user's access token (if present in the session) is still forwarded in
    `Authorization` so the API can record who triggered the request for audit;
    the service token in `X-Service-Authorization` is the auth gate. That split
    is why the two credentials use separate headers — see `common/m2m_auth.py`.
    """
    headers = service_token_client().auth_headers()
    if not headers:
        logger.debug(
            "No service token available; skipping M2M agent-config fetch for org=%s",
            org_id,
        )
        return None
    resp = api_request("GET", f"/agent-config/org/{org_id}", headers=headers)
    if resp.status_code == 401:
        # Token revoked or IS keys rotated — mint a fresh one and retry once.
        logger.info("Service token rejected by the Business API; retrying with a fresh token")
        retry_headers = service_token_client().auth_headers(force_refresh=True)
        if not retry_headers:
            return None
        resp = api_request("GET", f"/agent-config/org/{org_id}", headers=retry_headers)
    if resp.status_code == 200:
        return resp.json()
    return None


def save_agent_config(data: dict) -> ApiResult:
    return _parse_response(api_request("POST", "/agent-config", json=data))


def delete_agent_config(org_id: str) -> ApiResult:
    return _parse_response(api_request("DELETE", f"/agent-config/org/{org_id}"))


def get_organization_plan(org_id: str, token: str = None) -> dict[str, Any]:
    """The org's plan, defaulting to basic. Safe for display and feature hints.

    Do NOT use this for authorization decisions: it cannot tell "this org has
    no subscription" from "the Business API is unreachable", and reports both as
    basic. Use `resolve_plan_for_gating` where the answer gates access.
    """
    resp = api_request("GET", f"/plans/org/{org_id}", token=token)
    if resp.status_code == 200:
        return resp.json()
    return {"org": org_id, "plan": DEFAULT_PLAN}


def resolve_plan_for_gating(org_id: str, token: str = None) -> str | None:
    """The org's plan for an access decision, or None if it cannot be determined.

    The distinction `get_organization_plan` throws away:

    * ``200`` — the stored plan. Authoritative.
    * ``404`` — no plan row, so no subscription. Authoritative: signup always
      writes one (`webapp/blueprints/signup.py`), so its absence means the org
      never selected a paid plan.
    * anything else (503, 5xx, connection refused) — unknown. Returning
      ``DEFAULT_PLAN`` here would silently revoke paid features during a
      Business API outage, so callers are told they don't know instead.
    """
    if not org_id:
        return None
    resp = api_request("GET", f"/plans/org/{org_id}", token=token)
    if resp.status_code == 200:
        try:
            return resp.json().get("plan") or DEFAULT_PLAN
        except ValueError:
            logger.warning("Plan lookup for org=%s returned non-JSON", org_id)
            return None
    if resp.status_code == 404:
        return DEFAULT_PLAN
    logger.warning(
        "Plan lookup for org=%s failed with %s; treating the plan as unknown",
        org_id, resp.status_code,
    )
    return None


def save_organization_plan(org_id: str, plan_id: str, token: str = None) -> ApiResult:
    return _parse_response(
        api_request(
            "POST",
            "/plans",
            token=token,
            json={"org": org_id, "plan": plan_id},
        )
    )

