import base64
import logging
import os
import time
from typing import NamedTuple, Any

from flask import current_app

from common.constants import DEFAULT_PRIMARY_COLOR, DEFAULT_SECONDARY_COLOR
from webapp.is_client import ISClient
from webapp.api_proxy import upsert_personalization, delete_personalization
from webapp.auth import get_client_credentials_token

logger = logging.getLogger(__name__)


def _get_auth_params(token: str, use_org_endpoint: bool = True):
    if use_org_endpoint:
        return token, {}
    admin_user = os.getenv("IS_ADMIN_USERNAME") or "teamspaceadmin@teamspace"
    admin_pass = os.getenv("IS_ADMIN_PASSWORD") or "Admin123"
    credentials = f"{admin_user}:{admin_pass}"
    encoded = base64.b64encode(credentials.encode()).decode()
    return None, {"headers": {"Authorization": f"Basic {encoded}"}}


def poll_for_role_id(is_client: ISClient, token: str, tenant_path: str,
                     role_name: str, max_attempts: int = 5,
                     poll_interval: int = 2, use_org_endpoint: bool = True) -> str | None:
    segment = "/o" if use_org_endpoint else ""
    t_token, kwargs = _get_auth_params(token, use_org_endpoint)
    filter_str = f"displayName+eq+{role_name}"
    if not use_org_endpoint:
        app_id = current_app.config.get("APP_ID", "")
        if app_id:
            filter_str += f"+and+audience.value+eq+{app_id}"
            
    for attempt in range(max_attempts):
        result = is_client.call(
            "GET",
            f"{tenant_path}{segment}/scim2/v2/Roles?filter={filter_str}",
            t_token,
            **kwargs
        )
        resources = (
            result["data"].get("Resources", [])
            if isinstance(result["data"], dict)
            else []
        )
        if resources:
            return resources[0]["id"]
        logger.info(
            "Waiting for role %s provisioning (attempt %d/%d)",
            role_name, attempt + 1, max_attempts,
        )
        time.sleep(poll_interval)
    logger.warning(
        "Role %s not found after %d attempts (%ds)",
        role_name, max_attempts, max_attempts * poll_interval,
    )
    return None


def assign_roles_to_user(is_client: ISClient, token: str, tenant_path: str,
                          user_id: str, role_names: list[str],
                          debug_list: list[dict], use_org_endpoint: bool = True) -> None:
    segment = "/o" if use_org_endpoint else ""
    t_token, kwargs = _get_auth_params(token, use_org_endpoint)
    for role_name in role_names:
        role_id = poll_for_role_id(is_client, token, tenant_path, role_name, use_org_endpoint=use_org_endpoint)
        if not role_id:
            logger.warning("Role %s not found after polling, skipping assignment", role_name)
            continue
        result = is_client.call(
            "PATCH",
            f"{tenant_path}{segment}/scim2/v2/Roles/{role_id}",
            t_token,
            json={
                "Operations": [{
                    "op": "add",
                    "path": "users",
                    "value": [{"value": user_id}],
                }]
            },
            **kwargs
        )
        debug_list.append(result["debug"])
        if result["status_code"] == 200:
            logger.info("Assigned role %s to user %s", role_name, user_id)
        else:
            logger.warning(
                "Role assignment %s returned %s: %s",
                role_name, result["status_code"], result["data"],
            )




def share_roles_with_org(is_client: ISClient, root_token: str,
                         tenant_path: str, org_id: str, role_names: list[str],
                         debug_list: list[dict]) -> None:
    """Share specific application roles with a SINGLE sub-organization.

    Uses the per-org ``applications/share`` PATCH API so only the target org's
    shared roles are modified. The ``share-with-all`` API (used solely by the
    setup scripts to establish the baseline) re-applies role sharing to ALL
    existing and future orgs, which would clobber the roles of pre-existing
    organizations — that is the bug this function avoids for signups/upgrades.

    The base ``teamspace-admin`` / ``teamspace-user`` roles are already shared
    with every org by the setup script, so callers pass only the plan-specific
    extra roles here. Role sharing is asynchronous: callers must poll for the
    role to appear in the sub-org (see :func:`poll_for_role_id`) before assigning
    it to a user.
    """
    if not role_names:
        return
    app_name = current_app.config.get("APP_NAME", "Teamspace")
    app_id = current_app.config.get("APP_ID", "")

    roles_value = [
        {"displayName": name, "audience": {"display": app_name, "type": "application"}}
        for name in role_names
    ]
    result = is_client.call(
        "PATCH",
        f"{tenant_path}/api/server/v1/applications/share",
        root_token,
        json={
            "applicationId": app_id,
            "Operations": [{
                "op": "add",
                "path": f'organizations[orgId eq "{org_id}"].roles',
                "value": roles_value,
            }],
        },
    )
    if result["status_code"] in (200, 201, 202, 204):
        logger.info("Shared roles %s with org %s", role_names, org_id)
    else:
        logger.warning("share (org=%s) returned %s: %s", org_id, result["status_code"], result["data"])
    debug_list.append(result["debug"])


def resolve_org_id(handle: str) -> str | None:
    tenant_path = current_app.config.get("TENANT_PATH", "")
    root_token = get_client_credentials_token()
    if not root_token:
        return None
    is_client = ISClient(current_app.config["IS_BASE_URL"])
    result = is_client.call(
        "GET",
        f"{tenant_path}/api/server/v1/organizations?limit=100&recursive=false",
        root_token,
    )
    if result["status_code"] == 200 and result["data"]:
        for org in result["data"].get("organizations", []):
            if org.get("orgHandle") == handle:
                return org["id"]
    return None


class DualWriteResult(NamedTuple):
    db_success: bool
    is_success: bool
    db_status: int
    is_status: int
    error_message: str = ""
    api_debug: Any = None


def build_is_branding_payload(data: dict, org_name: str = "") -> dict:
    primary = data.get("primary_color", DEFAULT_PRIMARY_COLOR)
    secondary = data.get("secondary_color", DEFAULT_SECONDARY_COLOR)
    logo_url = data.get("logo_url", "")
    logo_alt = data.get("logo_alt_text", "")
    favicon_url = data.get("favicon_url", "")

    return {
        "type": "ORG",
        "locale": "en-US",
        "preference": {
            "configs": {
                "isBrandingEnabled": True,
                "removeDefaultBranding": True,
            },
            "layout": {"activeLayout": "centered"},
            "organizationDetails": {
                "displayName": org_name,
                "supportEmail": "",
            },
            "theme": {
                "activeTheme": "LIGHT",
                "LIGHT": {
                    "buttons": {
                        "externalConnection": {
                            "base": {
                                "background": {"backgroundColor": "#FFFFFF"},
                                "border": {"borderRadius": "22px"},
                                "font": {"color": "#000000de"},
                            }
                        },
                        "primary": {
                            "base": {
                                "border": {"borderRadius": "22px"},
                                "font": {"color": "#ffffffe6"},
                            }
                        },
                        "secondary": {
                            "base": {
                                "border": {"borderRadius": "22px"},
                                "font": {"color": "#000000de"},
                            }
                        },
                    },
                    "colors": {
                        "alerts": {
                            "error": {"main": "#ffd8d8"},
                            "info": {"main": "#eff7fd"},
                            "neutral": {"main": "#f8f8f9"},
                            "warning": {"main": "#fff6e7"},
                        },
                        "background": {
                            "body": {"main": "#fbfbfb"},
                            "surface": {
                                "main": "#ffffff",
                                "dark": "#F6F4F2",
                                "light": "#f9fafb",
                                "inverted": "#212A32",
                            },
                        },
                        "illustrations": {
                            "accent1": {"main": "#3865B5"},
                            "accent2": {"main": "#19BECE"},
                            "accent3": {"main": "#FFFFFF"},
                            "primary": {"main": primary},
                            "secondary": {"main": "#E0E1E2"},
                        },
                        "outlined": {"default": "#dadce0"},
                        "primary": {"main": primary},
                        "secondary": {"main": secondary},
                        "text": {"primary": "#000000de", "secondary": "#00000066"},
                    },
                    "images": {
                        "favicon": {"imgURL": favicon_url},
                        "logo": {"altText": logo_alt, "imgURL": logo_url},
                        "myAccountLogo": {"title": "Account"},
                    },
                    "inputs": {
                        "base": {
                            "background": {"backgroundColor": "#FFFFFF"},
                            "border": {"borderRadius": "8px"},
                        }
                    },
                    "loginBox": {
                        "border": {"borderRadius": "12px", "borderWidth": "1px"}
                    },
                    "typography": {"font": {"fontFamily": "Gilmer"}},
                },
            },
            "urls": {
                "cookiePolicyURL": "",
                "privacyPolicyURL": "",
                "termsOfUseURL": "",
            },
        },
    }


def update_branding(data: dict, token: str, tenant_path: str, org_name: str) -> DualWriteResult:
    """Updates branding in both DB (via business API) and WSO2 IS in sequence."""
    db_result = upsert_personalization(data)
    db_success = db_result.status_code in (200, 201)

    if not db_success:
        return DualWriteResult(
            db_success=False,
            is_success=False,
            db_status=db_result.status_code,
            is_status=0,
            error_message="Failed to update branding in DB"
        )

    if not token:
        logger.warning("No access_token available, skipping IS branding sync")
        return DualWriteResult(
            db_success=True,
            is_success=False,
            db_status=db_result.status_code,
            is_status=0,
            error_message="No access token available for IS sync"
        )

    is_client = ISClient(current_app.config["IS_BASE_URL"])
    body = build_is_branding_payload(data, org_name=org_name)
    endpoint = f"{tenant_path}/o/api/server/v1/branding-preference"

    # Try PUT first (update existing)
    result = is_client.call("PUT", endpoint, token, json=body)
    if result["status_code"] in (200, 201):
        logger.info("IS branding synced via PUT")
        return DualWriteResult(
            db_success=True,
            is_success=True,
            db_status=db_result.status_code,
            is_status=result["status_code"],
            api_debug=result["debug"]
        )

    # If 404, branding doesn't exist yet — create with POST
    if result["status_code"] == 404:
        logger.info("IS branding not found (PUT 404), creating via POST")
        result = is_client.call("POST", endpoint, token, json=body)
        if result["status_code"] in (200, 201):
            logger.info("IS branding created via POST")
            return DualWriteResult(
                db_success=True,
                is_success=True,
                db_status=db_result.status_code,
                is_status=result["status_code"],
                api_debug=result["debug"]
            )

    logger.warning(
        "IS branding sync failed: status=%s, body=%s",
        result["status_code"],
        result["data"],
    )
    return DualWriteResult(
        db_success=True,
        is_success=False,
        db_status=db_result.status_code,
        is_status=result["status_code"],
        error_message=f"IS branding sync failed with status {result['status_code']}",
        api_debug=result["debug"]
    )


def delete_branding(org_id: str, token: str, tenant_path: str) -> DualWriteResult:
    """Deletes branding from both DB (via business API) and WSO2 IS."""
    db_result = delete_personalization(org_id)
    db_success = db_result.status_code in (200, 201, 204)

    if not token:
        logger.warning("No access_token available, skipping IS branding delete")
        return DualWriteResult(
            db_success=db_success,
            is_success=False,
            db_status=db_result.status_code,
            is_status=0,
            error_message="No access token available for IS delete"
        )

    is_client = ISClient(current_app.config["IS_BASE_URL"])
    endpoint = (
        f"{tenant_path}/o/api/server/v1/branding-preference"
        f"?locale=en-US&name={org_id}&type=ORG"
    )
    result = is_client.call("DELETE", endpoint, token)
    is_success = result["status_code"] in (200, 204)

    if is_success:
        logger.info("IS branding reverted to defaults")
    else:
        logger.warning("IS branding delete failed: %s", result["status_code"])

    return DualWriteResult(
        db_success=db_success,
        is_success=is_success,
        db_status=db_result.status_code,
        is_status=result["status_code"],
        api_debug=result["debug"]
    )


def fetch_branding_from_is(org_id: str, token: str, tenant_path: str) -> dict | None:
    """Fetch org branding from WSO2 IS and extract personalization fields."""
    if not token:
        return None
    is_client = ISClient(current_app.config["IS_BASE_URL"])
    endpoint = (
        f"{tenant_path}/o/api/server/v1/branding-preference/resolve"
        f"?locale=en-US&name={org_id}&type=ORG"
    )
    result = is_client.call("GET", endpoint, token)
    if result["status_code"] != 200 or not result["data"]:
        return None

    try:
        pref = result["data"].get("preference", {})
        theme = pref.get("theme", {})
        active = theme.get("activeTheme", "LIGHT")
        t = theme.get(active, {})
        return {
            "org": org_id,
            "logo_url": t.get("images", {}).get("logo", {}).get("imgURL", ""),
            "logo_alt_text": t.get("images", {}).get("logo", {}).get("altText", ""),
            "favicon_url": t.get("images", {}).get("favicon", {}).get("imgURL", ""),
            "primary_color": t.get("colors", {}).get("primary", {}).get("main", DEFAULT_PRIMARY_COLOR),
            "secondary_color": t.get("colors", {}).get("secondary", {}).get("main", DEFAULT_SECONDARY_COLOR),
        }
    except Exception:
        logger.exception("Failed to parse IS branding response")
        return None

