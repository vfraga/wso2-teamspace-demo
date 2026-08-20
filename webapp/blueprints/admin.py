import logging
import json
from typing import Any

from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, abort


from webapp.utils.decorators import login_required, admin_required, role_required
from webapp.utils.roles import TEAMSPACE_ADMIN, IDP_MANAGER, TEAMSPACE_USER
from webapp.is_client import ISClient
from webapp.api_proxy import resolve_plan_for_gating
from webapp.is_operations import assign_roles_to_user
from common.constants import OIDC_AUTHENTICATOR_ID, OIDC_AUTHENTICATOR_NAME

logger = logging.getLogger(__name__)

bp = Blueprint("admin", __name__)


def scim_resources(result) -> list[dict[str, Any]]:
    data = result.get("data") if isinstance(result, dict) else None
    if isinstance(data, dict):
        return data.get("Resources", []) or []
    return []


def _verify_org_handle(org_handle: str):
    session_org_handle = session.get("user", {}).get("org_handle", "")
    if session_org_handle and org_handle != session_org_handle:
        abort(403)


def _resolve_teamspace_app_id(is_client, token, tenant_path, api_debug_list):
    apps_res = is_client.call("GET", f"{tenant_path}/o/api/server/v1/applications", token)
    if isinstance(api_debug_list, list):
        api_debug_list.append(apps_res["debug"])
    if apps_res["status_code"] != 200 or not isinstance(apps_res.get("data"), dict):
        return None
    apps = apps_res["data"].get("applications", [])
    teamspace_app = next((a for a in apps if a.get("name") == "Teamspace"), None)
    return teamspace_app.get("id") if teamspace_app else None


def _patch_auth_sequence(is_client, token, tenant_path, app_id, auth_sequence, api_debug_list=None):
    patch_body = {"authenticationSequence": auth_sequence}
    patch_res = is_client.call(
        "PATCH", f"{tenant_path}/o/api/server/v1/applications/{app_id}", token, json=patch_body
    )
    if isinstance(api_debug_list, list):
        api_debug_list.append(patch_res["debug"])
    return patch_res


def _build_oidc_authenticator_properties(form, org_id, is_base_url, token_endpoint):
    client_id = form["client_id"]
    client_secret = form["client_secret"]
    auth_endpoint = form["auth_endpoint"]
    jwks_uri = (form.get("jwks_uri", "") or "").strip()
    userinfo_endpoint = (form.get("userinfo_endpoint", "") or "").strip()
    logout_endpoint = (form.get("logout_endpoint", "") or "").strip()
    scopes = (form.get("scopes", "openid email profile groups") or "openid email profile groups").strip() or "openid email profile groups"

    callback_url = f"{is_base_url}/o/{org_id}/commonauth"

    return [
        {"key": "ClientId", "value": client_id},
        {"key": "ClientSecret", "value": client_secret},
        {"key": "OAuth2AuthzEPUrl", "value": auth_endpoint},
        {"key": "OAuth2TokenEPUrl", "value": token_endpoint},
        {"key": "JwksUri", "value": jwks_uri or token_endpoint.replace("/oauth2/token", "/oauth2/jwks")},
        {"key": "UserInfoUrl", "value": userinfo_endpoint or token_endpoint.replace("/oauth2/token", "/oauth2/userinfo")},
        {"key": "OIDCLogoutEPUrl", "value": logout_endpoint or token_endpoint.replace("/oauth2/token", "/oidc/logout")},
        {"key": "Scopes", "value": scopes},
        {"key": "IsPKCEEnabled", "value": "false"},
        {"key": "IsBasicAuthEnabled", "value": "false"},
        {"key": "callbackUrl", "value": callback_url},
        {"key": "AdditionalQueryParameters", "value": f"scope={scopes}"},
        {"key": "commonAuthEPUrl", "value": f"{is_base_url}/commonauth"},
    ]


def _build_jit_put_body(jit_enabled: bool) -> dict:
    """Build the JIT (Just-In-Time provisioning) PUT body for an IdP.

    All seven fields must be present: the WSO2 IS API fills in defaults
    for any omitted field, and the defaults don't match what the form
    was just configured to do (e.g. isEnabled would fall back to false).
    """
    return {
        "accountLookupAttributeMappings": [],
        "associateLocalUser": True,
        "attributeSyncMethod": "PRESERVE_LOCAL",
        "isEnabled": bool(jit_enabled),
        "scheme": "PROVISION_SILENTLY",
        "skipJITForLookupFailure": False,
        "userstore": "PRIMARY",
    }


def _apply_idp_role_group_mappings(is_client, token, tenant_path, idp_id, request_form, api_debug_list):
    raw_groups_input = request_form.get("groups", "")
    external_group_names = [g.strip() for g in raw_groups_input.split(",") if g.strip()]

    idp_groups = [{"name": gname, "id": ""} for gname in external_group_names]
    groups_res = is_client.call(
        "PUT", f"{tenant_path}/o/api/server/v1/identity-providers/{idp_id}/groups", token, json=idp_groups
    )
    api_debug_list.append(groups_res["debug"])

    resolved_groups = []
    if groups_res["status_code"] in (200, 201) and isinstance(groups_res.get("data"), list):
        resolved_groups = groups_res["data"]
    else:
        detail_res = is_client.call("GET", f"{tenant_path}/o/api/server/v1/identity-providers/{idp_id}", token)
        api_debug_list.append(detail_res["debug"])
        if detail_res["status_code"] == 200:
            resolved_groups = detail_res.get("data", {}).get("groups", [])

    roles_res = is_client.call("GET", f"{tenant_path}/o/scim2/v2/Roles", token)
    api_debug_list.append(roles_res["debug"])
    scim_roles = scim_resources(roles_res) if roles_res["status_code"] == 200 else []

    idp_group_ids = {ig["id"] for ig in resolved_groups if ig.get("id")}
    for role in scim_roles:
        role_id = role["id"]
        selected_gnames = request_form.getlist(f"role_groups[{role_id}]")
        selected_gids = [
            ig["id"] for ig in resolved_groups
            if ig.get("name") in selected_gnames and ig.get("id")
        ]

        role_detail = is_client.call("GET", f"{tenant_path}/o/scim2/v2/Roles/{role_id}", token)
        api_debug_list.append(role_detail["debug"])
        if role_detail["status_code"] != 200:
            continue
        current_groups = role_detail.get("data", {}).get("groups", [])
        other_groups = [g for g in current_groups if g.get("value") not in idp_group_ids]
        new_groups = [{"value": gid} for gid in selected_gids]
        combined_groups = other_groups + new_groups

        patch_body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "path": "groups", "value": combined_groups}],
        }
        patch_res = is_client.call("PATCH", f"{tenant_path}/o/scim2/v2/Roles/{role_id}", token, json=patch_body)
        api_debug_list.append(patch_res["debug"])


def check_idp_access(org_handle, title="Identity Providers", description="Connect external identity providers for federated SSO.", upgrade_template="admin/idp_upgrade.html"):
    """Gate the IdP features on the enterprise plan and the idp-manager role.

    Both the plan and the role must hold, and the plan is checked first. It used
    to be consulted only *after* the role check had failed, so `required_plan`
    was never compared at all: any user holding `required_role` reached the
    feature whatever their org's plan.

    The role alone is not a safe proxy for the plan. `PLAN_ROLES` grants
    `idp-manager` and the branding-editor roles on upgrade
    (`webapp/blueprints/subscription.py`), but that handler never *revokes* them
    and happily accepts a downgrade, so an org that drops from enterprise to
    basic keeps every role its old plan granted.

    The plan can also be *unknown* — `resolve_plan_for_gating` returns None when
    the Business API is unreachable, rather than reporting a default that would
    silently revoke paid features during an outage. The three-way outcome:

    * plan says no        -> upgrade prompt (this closes the stale-role bypass)
    * plan says yes       -> role decides; a missing role is an honest
                             "you need this role" rather than "upgrade"
    * plan unknown        -> don't punish a paying customer for an outage: allow
                             if the role is present, otherwise fall back to the
                             upgrade prompt, which is what this case showed
                             before and remains the likelier explanation
    
    Kept as its own function rather than shared with
    `agents.py:_check_plan_and_role` because the two differ in how they source
    `org_handle` and which role they need; the decision table above is the part
    that matters and is identical in both.
    """
    org_id = session.get("user", {}).get("org_id", "")
    org_plan = resolve_plan_for_gating(org_id)
    if org_plan is not None and org_plan != "enterprise":
        return render_template(upgrade_template, org_handle=org_handle)

    if IDP_MANAGER not in session.get("user_roles", []):
        if org_plan == "enterprise":
            return render_template(
                "admin/access_denied.html",
                org_handle=org_handle,
                title=title,
                description=description,
                required_role="idp-manager"
            )
        logger.warning(
            "Plan unknown for org=%s and idp-manager absent; showing the upgrade prompt",
            org_id,
        )
        return render_template(upgrade_template, org_handle=org_handle)

    if org_plan is None:
        logger.warning(
            "Plan unknown for org=%s; allowing IdP access on the strength of the "
            "idp-manager role alone", org_id,
        )
    return None


@bp.route("/users")
@login_required
@admin_required
def users(org_handle):
    _verify_org_handle(org_handle)
    is_client = ISClient(current_app.config["IS_BASE_URL"])
    token = session["access_token"]
    tenant_path = current_app.config.get("TENANT_PATH", "")
    users_resp = is_client.call("GET", f"{tenant_path}/o/scim2/Users", token)
    user_list = scim_resources(users_resp)
    return render_template(
        "admin/users.html", org_handle=org_handle, users=user_list, api_debug=users_resp["debug"]
    )


@bp.route("/users/add", methods=["GET"])
@login_required
@admin_required
def user_form(org_handle):
    return render_template("admin/user_form.html", org_handle=org_handle, user=None)


@bp.route("/users/add", methods=["POST"])
@login_required
@admin_required
def add_user(org_handle):
    _verify_org_handle(org_handle)
    is_client = ISClient(current_app.config["IS_BASE_URL"])
    token = session["access_token"]
    tenant_path = current_app.config.get("TENANT_PATH", "")
    body = {
        "schemas": [],
        "userName": request.form["email"],
        "password": request.form["password"],
        "name": {
            "givenName": request.form["first_name"],
            "familyName": request.form["last_name"],
        },
        "emails": [{"value": request.form["email"], "primary": True}],
    }
    create_resp = is_client.call("POST", f"{tenant_path}/o/scim2/Users", token, json=body)
    if create_resp["status_code"] in (200, 201):
        user_id = create_resp["data"].get("id", "")
        if user_id:
            api_debug_list = [create_resp["debug"]]
            assign_roles_to_user(is_client, token, tenant_path, user_id, [TEAMSPACE_USER], api_debug_list)
            flash({"type": "success", "message": "User created.", "api_debug_list": api_debug_list})
        else:
            flash({"type": "success", "message": "User created.", "api_debug": create_resp["debug"]})
    else:
        flash({"type": "error", "message": "Failed to create user.", "api_debug": create_resp["debug"]})
    return redirect(url_for("admin.users", org_handle=org_handle))


@bp.route("/users/<user_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_user(org_handle, user_id):
    _verify_org_handle(org_handle)
    current_user_id = session.get("user", {}).get("sub")
    if current_user_id == user_id:
        flash({"type": "error", "message": "You cannot delete your own user account."})
        return redirect(url_for("admin.users", org_handle=org_handle))

    is_client = ISClient(current_app.config["IS_BASE_URL"])
    token = session["access_token"]
    tenant_path = current_app.config.get("TENANT_PATH", "")
    delete_resp = is_client.call("DELETE", f"{tenant_path}/o/scim2/Users/{user_id}", token)
    if delete_resp["status_code"] == 204:
        flash({"type": "success", "message": "User deleted.", "api_debug": delete_resp["debug"]})
    else:
        flash({"type": "error", "message": "Failed to delete user.", "api_debug": delete_resp["debug"]})
    return redirect(url_for("admin.users", org_handle=org_handle))


@bp.route("/roles")
@login_required
@admin_required
def roles(org_handle):
    _verify_org_handle(org_handle)
    is_client = ISClient(current_app.config["IS_BASE_URL"])
    token = session["access_token"]
    tenant_path = current_app.config.get("TENANT_PATH", "")

    api_debug_list = []

    roles_resp = is_client.call("GET", f"{tenant_path}/o/scim2/v2/Roles", token)
    api_debug_list.append(roles_resp["debug"])
    role_list = scim_resources(roles_resp)

    filtered_roles = []
    for role in role_list:
        audience = role.get("audience", {})
        audience_type = audience.get("type", "")
        audience_display = audience.get("display", "")
        if audience_type == "application" and audience_display == "Console":
            continue
        filtered_roles.append(role)

    roles_with_users = []
    for role in filtered_roles:
        role_id = role["id"]
        role_detail_resp = is_client.call("GET", f"{tenant_path}/o/scim2/v2/Roles/{role_id}", token)
        api_debug_list.append(role_detail_resp["debug"])
        assigned_users = []
        if role_detail_resp["status_code"] == 200:
            assigned_users = role_detail_resp.get("data", {}).get("users", [])
        roles_with_users.append({
            "id": role_id,
            "displayName": role.get("displayName", ""),
            "audience": role.get("audience", {}),
            "users": assigned_users,
        })

    users_resp = is_client.call("GET", f"{tenant_path}/o/scim2/Users", token)
    api_debug_list.append(users_resp["debug"])
    all_users = scim_resources(users_resp)

    return render_template(
        "admin/roles.html",
        org_handle=org_handle,
        roles=roles_with_users,
        all_users=all_users,
        api_debug_list=api_debug_list,
    )


@bp.route("/roles/<role_id>/members", methods=["POST"])
@login_required
@admin_required
def update_role_members(org_handle, role_id):
    _verify_org_handle(org_handle)
    is_client = ISClient(current_app.config["IS_BASE_URL"])
    token = session["access_token"]
    tenant_path = current_app.config.get("TENANT_PATH", "")

    api_debug_list = []

    selected_user_ids = request.form.getlist("user_ids")

    patch_body = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [
            {
                "op": "replace",
                "path": "users",
                "value": [{"value": uid} for uid in selected_user_ids]
            }
        ]
    }

    patch_resp = is_client.call("PATCH", f"{tenant_path}/o/scim2/v2/Roles/{role_id}", token, json=patch_body)
    api_debug_list.append(patch_resp["debug"])

    if patch_resp["status_code"] in (200, 204):
        flash({"type": "success", "message": "Role members updated successfully.", "api_debug_list": api_debug_list})
    else:
        err_msg = "Failed to update role members."
        if isinstance(patch_resp.get("data"), dict) and "detail" in patch_resp["data"]:
            err_msg = patch_resp["data"]["detail"]
        flash({"type": "error", "message": err_msg, "api_debug_list": api_debug_list})

    return redirect(url_for("admin.roles", org_handle=org_handle))


@bp.route("/idp")
@login_required
@role_required(TEAMSPACE_ADMIN, IDP_MANAGER)
def idp(org_handle):
    _verify_org_handle(org_handle)
    denied = check_idp_access(org_handle)
    if denied:
        return denied

    is_client = ISClient(current_app.config["IS_BASE_URL"])
    token = session["access_token"]
    tenant_path = current_app.config.get("TENANT_PATH", "")
    idp_resp = is_client.call("GET", f"{tenant_path}/o/api/server/v1/identity-providers", token)
    idp_list = []
    if isinstance(idp_resp.get("data"), dict):
        idp_list = idp_resp["data"].get("identityProviders", [])
    return render_template(
        "admin/idp.html", org_handle=org_handle, idps=idp_list, api_debug=idp_resp["debug"]
    )


@bp.route("/idp/add", methods=["GET"])
@login_required
@role_required(TEAMSPACE_ADMIN, IDP_MANAGER)
def idp_form(org_handle):
    _verify_org_handle(org_handle)
    denied = check_idp_access(org_handle)
    if denied:
        return denied

    is_client = ISClient(current_app.config["IS_BASE_URL"])
    token = session["access_token"]
    tenant_path = current_app.config.get("TENANT_PATH", "")

    roles_res = is_client.call("GET", f"{tenant_path}/o/scim2/v2/Roles", token)
    scim_roles = scim_resources(roles_res) if roles_res["status_code"] == 200 else []

    return render_template(
        "admin/idp_form.html",
        org_handle=org_handle,
        roles=scim_roles,
        api_debug=roles_res["debug"]
    )


@bp.route("/idp/add", methods=["POST"])
@login_required
@role_required(TEAMSPACE_ADMIN, IDP_MANAGER)
def add_idp(org_handle):
    _verify_org_handle(org_handle)
    denied = check_idp_access(org_handle)
    if denied:
        return denied

    is_client = ISClient(current_app.config["IS_BASE_URL"])
    token = session["access_token"]
    tenant_path = current_app.config.get("TENANT_PATH", "")
    is_base_url = current_app.config["IS_BASE_URL"]

    api_debug_list = []

    org_id = session.get("user", {}).get("org_id", "")
    for role_name in ("teamspace-admin", "teamspace-user"):
        check_res = is_client.call(
            "GET",
            f"{tenant_path}/o/scim2/v2/Roles?filter=displayName+eq+{role_name}",
            token
        )
        api_debug_list.append(check_res["debug"])
        exists = False
        if check_res["status_code"] == 200:
            for r in scim_resources(check_res):
                if r.get("audience", {}).get("type") in ("organization", "application"):
                    exists = True
                    break

        if not exists:
            logger.info("Creating organization-audience role %s in sub-org %s (org_id=%s)", role_name, org_handle, org_id)
            role_create_res = is_client.call(
                "POST",
                f"{tenant_path}/o/scim2/v2/Roles",
                token,
                json={
                    "displayName": role_name,
                    "audience": {
                        "type": "organization",
                        "value": org_id
                    },
                    "schemas": []
                }
            )
            api_debug_list.append(role_create_res["debug"])

    name = request.form["name"]
    jit_enabled = request.form.get("jit_enabled") == "true"
    group_attribute = request.form.get("group_attribute", "groups")
    token_endpoint = request.form["token_endpoint"]

    properties = _build_oidc_authenticator_properties(request.form, org_id, is_base_url, token_endpoint)

    body = {
        "name": name,
        "federatedAuthenticators": {
            "defaultAuthenticatorId": OIDC_AUTHENTICATOR_ID,
            "authenticators": [{
                "authenticatorId": OIDC_AUTHENTICATOR_ID,
                "isEnabled": True,
                "properties": properties,
            }],
        },
        "claims": {
            "userIdClaim": {
                "uri": "http://wso2.org/claims/emailaddress"
            },
            "roleClaim": {
                "uri": "http://wso2.org/claims/roles"
            },
            "mappings": [
                {
                    "idpClaim": "sub",
                    "localClaim": {
                        "uri": "http://wso2.org/claims/username"
                    }
                },
                {
                    "idpClaim": "email",
                    "localClaim": {
                        "uri": "http://wso2.org/claims/emailaddress"
                    }
                },
                {
                    "idpClaim": group_attribute,
                    "localClaim": {
                        "uri": "http://wso2.org/claims/roles"
                    }
                },
                {
                    "idpClaim": "roles",
                    "localClaim": {
                        "uri": "http://wso2.org/claims/roles"
                    }
                }
            ]
        },
        "roles": {
            "mappings": [
                {"idpRole": "admin", "localRole": "teamspace-admin"},
                {"idpRole": "user", "localRole": "teamspace-user"},
                {"idpRole": "PRIMARY/admin", "localRole": "teamspace-admin"},
                {"idpRole": "PRIMARY/user", "localRole": "teamspace-user"},
                {"idpRole": f"{org_handle}/admin", "localRole": "teamspace-admin"},
                {"idpRole": f"{org_handle}/user", "localRole": "teamspace-user"},
            ]
        },
        "provisioning": {
            "jit": _build_jit_put_body(jit_enabled),
        },
    }
    create_resp = is_client.call("POST", f"{tenant_path}/o/api/server/v1/identity-providers", token, json=body)
    api_debug_list.append(create_resp["debug"])

    if create_resp["status_code"] in (200, 201):
        idp_id = create_resp.get("data", {}).get("id")
        if idp_id:
            jit_res = is_client.call(
                "PUT",
                f"{tenant_path}/o/api/server/v1/identity-providers/{idp_id}/provisioning/jit",
                token,
                json=_build_jit_put_body(jit_enabled),
            )
            api_debug_list.append(jit_res["debug"])

            _apply_idp_role_group_mappings(is_client, token, tenant_path, idp_id, request.form, api_debug_list)

        app_id = _resolve_teamspace_app_id(is_client, token, tenant_path, api_debug_list)
        if app_id:
            _patch_auth_sequence(
                is_client, token, tenant_path, app_id,
                {
                    "type": "USER_DEFINED",
                    "steps": [
                        {
                            "id": 1,
                            "options": [
                                {"idp": "LOCAL", "authenticator": "BasicAuthenticator"},
                                {"idp": name, "authenticator": "OpenIDConnectAuthenticator"}
                            ]
                        }
                    ]
                },
                api_debug_list,
            )

        flash({"type": "success", "message": "Identity Provider created.", "api_debug_list": api_debug_list})
    else:
        flash({"type": "error", "message": "Failed to create IdP.", "api_debug_list": api_debug_list})
    return redirect(url_for("admin.idp", org_handle=org_handle))


@bp.route("/security")
@login_required
@role_required(TEAMSPACE_ADMIN, IDP_MANAGER)
def security(org_handle):
    return render_template("admin/security.html", org_handle=org_handle)


SUPPORTED_LOCAL_AUTH_NAMES = {
    "BasicAuthenticator", "FIDOAuthenticator", "MagicLinkAuthenticator",
    "email-otp-authenticator", "sms-otp-authenticator", "totp", "IdentifierExecutor",
}

DEFAULT_LOCAL_AUTHENTICATORS = [
    {"name": "BasicAuthenticator", "displayName": "Username & Password"},
    {"name": "FIDOAuthenticator", "displayName": "Passkey"},
    {"name": "MagicLinkAuthenticator", "displayName": "Magic Link"},
    {"name": "email-otp-authenticator", "displayName": "Email OTP"},
    {"name": "sms-otp-authenticator", "displayName": "SMS OTP"},
    {"name": "totp", "displayName": "TOTP"},
]

DEFAULT_AUTH_SEQUENCE = {
    "type": "USER_DEFINED",
    "steps": [
        {
            "id": 1,
            "options": [
                {"idp": "LOCAL", "authenticator": "BasicAuthenticator"}
            ]
        }
    ],
    "subjectStepId": 1,
    "attributeStepId": 1,
}


def _fetch_local_authenticators(is_client, token: str, tenant_path: str) -> list[dict]:
    auths_res = is_client.call("GET", f"{tenant_path}/o/api/server/v1/configs/authenticators", token)
    if auths_res["status_code"] == 200 and isinstance(auths_res["data"], list):
        return [
            a for a in auths_res["data"]
            if a.get("isEnabled") and a.get("name") in SUPPORTED_LOCAL_AUTH_NAMES
        ]
    return list(DEFAULT_LOCAL_AUTHENTICATORS)


def _fetch_idps(is_client, token: str, tenant_path: str) -> list[dict]:
    idp_res = is_client.call("GET", f"{tenant_path}/o/api/server/v1/identity-providers", token)
    if idp_res["status_code"] == 200 and isinstance(idp_res["data"], dict):
        return idp_res["data"].get("identityProviders", [])
    return []


def _fetch_teamspace_login_flow(is_client, token: str, tenant_path: str) -> tuple[str | None, dict | None, dict | None]:
    apps_res = is_client.call("GET", f"{tenant_path}/o/api/server/v1/applications", token)
    if not (apps_res["status_code"] == 200 and isinstance(apps_res["data"], dict)):
        return None, None, None

    apps = apps_res["data"].get("applications", [])
    teamspace_app = next((a for a in apps if a.get("name") == "Teamspace"), None)
    if not teamspace_app:
        return None, None, None

    app_id = teamspace_app["id"]
    app_detail_res = is_client.call("GET", f"{tenant_path}/o/api/server/v1/applications/{app_id}", token)
    current_sequence = None
    if app_detail_res["status_code"] == 200 and isinstance(app_detail_res["data"], dict):
        current_sequence = app_detail_res["data"].get("authenticationSequence")

    login_flow_debug = dict(app_detail_res["debug"])
    if isinstance(login_flow_debug.get("response_body"), dict):
        login_flow_debug["response_body"] = {
            "authenticationSequence": login_flow_debug["response_body"].get("authenticationSequence")
        }
    return app_id, current_sequence, login_flow_debug


@bp.route("/security/login-flow")
@login_required
@role_required(TEAMSPACE_ADMIN, IDP_MANAGER)
def login_flow(org_handle):
    _verify_org_handle(org_handle)
    denied = check_idp_access(org_handle, title="Login Flow Configuration", description="Customize the login flow for your organization.", upgrade_template="admin/login_flow_upgrade.html")
    if denied:
        return denied

    is_client = ISClient(current_app.config["IS_BASE_URL"])
    token = session["access_token"]
    tenant_path = current_app.config.get("TENANT_PATH", "")

    local_authenticators = _fetch_local_authenticators(is_client, token, tenant_path)
    idps = _fetch_idps(is_client, token, tenant_path)
    app_id, current_sequence, login_flow_debug = _fetch_teamspace_login_flow(is_client, token, tenant_path)

    if not current_sequence:
        current_sequence = DEFAULT_AUTH_SEQUENCE

    return render_template(
        "admin/login_flow.html",
        org_handle=org_handle,
        local_authenticators=local_authenticators,
        idps=idps,
        current_sequence=current_sequence,
        app_id=app_id,
        api_debug=login_flow_debug
    )


def _parse_login_flow_steps(steps_json: str) -> list[dict]:
    steps = json.loads(steps_json)
    if not steps or not isinstance(steps, list):
        raise ValueError("Steps must be a non-empty list.")

    for step in steps:
        if "id" not in step or "options" not in step:
            raise ValueError("Each step must have 'id' and 'options'.")
        if not step["options"] or not isinstance(step["options"], list):
            raise ValueError("Each step must have at least one option.")
        for opt in step["options"]:
            if "idp" not in opt or "authenticator" not in opt:
                raise ValueError("Each option must have 'idp' and 'authenticator'.")
    return steps


def _build_login_flow_patch_body(steps: list[dict]) -> dict:
    return {
        "authenticationSequence": {
            "type": "USER_DEFINED",
            "steps": steps,
            "subjectStepId": 1,
            "attributeStepId": 1,
        }
    }


def _extract_login_flow_error_message(patch_resp) -> str:
    if isinstance(patch_resp.get("data"), dict) and "description" in patch_resp["data"]:
        return patch_resp["data"]["description"]
    if patch_resp.get("data"):
        return str(patch_resp["data"])
    return "Failed to update login flow."


@bp.route("/security/login-flow", methods=["POST"])
@login_required
@role_required(TEAMSPACE_ADMIN, IDP_MANAGER)
def update_login_flow(org_handle):
    _verify_org_handle(org_handle)
    denied = check_idp_access(org_handle, title="Login Flow Configuration", description="Customize the login flow for your organization.", upgrade_template="admin/login_flow_upgrade.html")
    if denied:
        return denied

    is_client = ISClient(current_app.config["IS_BASE_URL"])
    token = session["access_token"]
    tenant_path = current_app.config.get("TENANT_PATH", "")

    app_id = request.form.get("app_id")
    if not app_id:
        flash({"type": "error", "message": "Application ID is required."})
        return redirect(url_for("admin.login_flow", org_handle=org_handle))

    steps_json = request.form.get("steps_json")
    try:
        steps = _parse_login_flow_steps(steps_json)
    except Exception as e:
        flash({"type": "error", "message": f"Invalid login flow structure: {str(e)}"})
        return redirect(url_for("admin.login_flow", org_handle=org_handle))

    patch_body = _build_login_flow_patch_body(steps)
    patch_resp = is_client.call("PATCH", f"{tenant_path}/o/api/server/v1/applications/{app_id}", token, json=patch_body)

    if patch_resp["status_code"] in (200, 204):
        flash({"type": "success", "message": "Login flow updated successfully.", "api_debug": patch_resp["debug"]})
    else:
        err_msg = _extract_login_flow_error_message(patch_resp)
        flash({"type": "error", "message": err_msg, "api_debug": patch_resp["debug"]})

    return redirect(url_for("admin.login_flow", org_handle=org_handle))


@bp.route("/idp/<idp_id>/edit", methods=["GET"])
@login_required
@role_required(TEAMSPACE_ADMIN, IDP_MANAGER)
def edit_idp_form(org_handle, idp_id):
    _verify_org_handle(org_handle)
    denied = check_idp_access(org_handle)
    if denied:
        return denied

    is_client = ISClient(current_app.config["IS_BASE_URL"])
    token = session["access_token"]
    tenant_path = current_app.config.get("TENANT_PATH", "")

    idp_res = is_client.call("GET", f"{tenant_path}/o/api/server/v1/identity-providers/{idp_id}", token)
    if idp_res["status_code"] != 200:
        flash({"type": "error", "message": "Failed to fetch Identity Provider details.", "api_debug": idp_res["debug"]})
        return redirect(url_for("admin.idp", org_handle=org_handle))

    idp_data = idp_res.get("data", {})

    auth_res = is_client.call("GET", f"{tenant_path}/o/api/server/v1/identity-providers/{idp_id}/federated-authenticators/{OIDC_AUTHENTICATOR_ID}", token)
    props = []
    if auth_res["status_code"] == 200 and isinstance(auth_res.get("data"), dict):
        props = auth_res["data"].get("properties", [])

    props_dict = {p.get("key"): p.get("value") for p in props if p.get("key")}

    jit_enabled = idp_data.get("provisioning", {}).get("jit", {}).get("isEnabled", False)

    claims_res = is_client.call("GET", f"{tenant_path}/o/api/server/v1/identity-providers/{idp_id}/claims", token)
    claims_data = claims_res.get("data", {})
    mappings = claims_data.get("mappings", [])
    group_mapping = next((m for m in mappings if m.get("localClaim", {}).get("uri") in ("http://wso2.org/claims/groups", "http://wso2.org/claims/roles")), None)
    group_attribute = group_mapping.get("idpClaim") if group_mapping else "groups"

    idp_groups = idp_data.get("groups", [])
    external_groups = [g["name"] for g in idp_groups if g.get("name")]
    external_groups_str = ", ".join(external_groups)

    roles_res = is_client.call("GET", f"{tenant_path}/o/scim2/v2/Roles", token)
    scim_roles = scim_resources(roles_res) if roles_res["status_code"] == 200 else []

    roles_with_mappings = []
    for role in scim_roles:
        role_id = role["id"]
        role_detail = is_client.call("GET", f"{tenant_path}/o/scim2/v2/Roles/{role_id}", token)
        mapped_groups_for_role = []
        if role_detail["status_code"] == 200:
            assigned_groups = role_detail.get("data", {}).get("groups", [])
            mapped_groups_for_role = [
                ig["name"] for ig in idp_groups
                if ig.get("id") and ig["id"] in [g.get("value") for g in assigned_groups]
            ]
        roles_with_mappings.append({
            "id": role_id,
            "displayName": role.get("displayName"),
            "mapped_groups": mapped_groups_for_role
        })

    return render_template(
        "admin/idp_edit.html",
        org_handle=org_handle,
        idp_id=idp_id,
        idp=idp_data,
        client_id=props_dict.get("ClientId", ""),
        auth_endpoint=props_dict.get("OAuth2AuthzEPUrl", ""),
        token_endpoint=props_dict.get("OAuth2TokenEPUrl", ""),
        jwks_uri=props_dict.get("JwksUri", ""),
        userinfo_endpoint=props_dict.get("UserInfoUrl", ""),
        logout_endpoint=props_dict.get("OIDCLogoutEPUrl", ""),
        scopes=props_dict.get("Scopes", "openid email profile groups"),
        jit_enabled=jit_enabled,
        group_attribute=group_attribute,
        external_groups=external_groups,
        external_groups_str=external_groups_str,
        roles=roles_with_mappings,
        api_debug=idp_res["debug"]
    )


DEFAULT_LOCAL_CLAIMS = {
    "http://wso2.org/claims/username": {
        "id": "aHR0cDovL3dzbzIub3JnL2NsYWltcy91c2VybmFtZQ",
        "displayName": "Username",
    },
    "http://wso2.org/claims/emailaddress": {
        "id": "aHR0cDovL3dzbzIub3JnL2NsYWltcy9lbWFpbGFkZHJlc3M",
        "displayName": "Email",
    },
    "http://wso2.org/claims/roles": {
        "id": "aHR0cDovL3dzbzIub3JnL2NsYWltcy9yb2xlcw",
        "displayName": "Roles",
    },
}


def _fetch_idp_props_dict(is_client, token: str, tenant_path: str, idp_id: str, api_debug_list) -> dict:
    auth_res = is_client.call("GET", f"{tenant_path}/o/api/server/v1/identity-providers/{idp_id}/federated-authenticators/{OIDC_AUTHENTICATOR_ID}", token)
    api_debug_list.append(auth_res["debug"])
    props = []
    if auth_res["status_code"] == 200 and isinstance(auth_res.get("data"), dict):
        props = auth_res["data"].get("properties", [])
    return {p.get("key"): p.get("value") for p in props if p.get("key")}


def _build_local_claims_index(current_idp: dict) -> dict:
    local_claims_by_uri = {}
    current_claims = current_idp.get("claims", {})
    for key in ("userIdClaim", "roleClaim"):
        c = current_claims.get(key, {})
        if c.get("uri"):
            local_claims_by_uri[c["uri"]] = {
                "id": c.get("id"),
                "displayName": c.get("displayName"),
            }
    for m in current_claims.get("mappings", []):
        lc = m.get("localClaim", {})
        if lc.get("uri"):
            local_claims_by_uri[lc["uri"]] = {
                "id": lc.get("id"),
                "displayName": lc.get("displayName"),
            }
    for uri, val in DEFAULT_LOCAL_CLAIMS.items():
        if uri not in local_claims_by_uri or not local_claims_by_uri[uri].get("id"):
            local_claims_by_uri[uri] = val
    return local_claims_by_uri


def _build_claims_put_body(local_claims_by_uri: dict, group_attribute: str) -> dict:
    def make_local_claim(uri: str) -> dict:
        info = local_claims_by_uri.get(uri, {})
        return {
            "uri": uri,
            "id": info.get("id", ""),
            "displayName": info.get("displayName", ""),
        }

    return {
        "userIdClaim": make_local_claim("http://wso2.org/claims/emailaddress"),
        "roleClaim": make_local_claim("http://wso2.org/claims/roles"),
        "mappings": [
            {"idpClaim": "sub", "localClaim": make_local_claim("http://wso2.org/claims/username")},
            {"idpClaim": "email", "localClaim": make_local_claim("http://wso2.org/claims/emailaddress")},
            {"idpClaim": group_attribute, "localClaim": make_local_claim("http://wso2.org/claims/roles")},
            {"idpClaim": "roles", "localClaim": make_local_claim("http://wso2.org/claims/roles")},
        ],
    }


@bp.route("/idp/<idp_id>/edit", methods=["POST"])
@login_required
@role_required(TEAMSPACE_ADMIN, IDP_MANAGER)
def edit_idp(org_handle, idp_id):
    _verify_org_handle(org_handle)
    denied = check_idp_access(org_handle)
    if denied:
        return denied

    is_client = ISClient(current_app.config["IS_BASE_URL"])
    token = session["access_token"]
    tenant_path = current_app.config.get("TENANT_PATH", "")
    is_base_url = current_app.config["IS_BASE_URL"]

    api_debug_list = []

    idp_res = is_client.call("GET", f"{tenant_path}/o/api/server/v1/identity-providers/{idp_id}", token)
    api_debug_list.append(idp_res["debug"])
    if idp_res["status_code"] != 200:
        flash({"type": "error", "message": "Failed to update Identity Provider details.", "api_debug_list": api_debug_list})
        return redirect(url_for("admin.idp", org_handle=org_handle))

    current_idp = idp_res.get("data", {})
    props_dict = _fetch_idp_props_dict(is_client, token, tenant_path, idp_id, api_debug_list)

    name = request.form["name"]
    client_secret = request.form.get("client_secret") or props_dict.get("ClientSecret")
    token_endpoint = request.form["token_endpoint"]
    jit_enabled = request.form.get("jit_enabled") == "true"

    org_id = session.get("user", {}).get("org_id", "")

    patch_body = [
        {"operation": "REPLACE", "path": "/name", "value": name},
    ]
    patch_res = is_client.call("PATCH", f"{tenant_path}/o/api/server/v1/identity-providers/{idp_id}", token, json=patch_body)
    api_debug_list.append(patch_res["debug"])

    form_for_props = request.form.copy()
    form_for_props["client_secret"] = client_secret
    properties = _build_oidc_authenticator_properties(form_for_props, org_id, is_base_url, token_endpoint)
    authenticator_body = {
        "name": OIDC_AUTHENTICATOR_NAME,
        "isEnabled": True,
        "definedBy": "SYSTEM",
        "isDefault": True,
        "properties": properties,
    }
    put_auth_res = is_client.call("PUT", f"{tenant_path}/o/api/server/v1/identity-providers/{idp_id}/federated-authenticators/{OIDC_AUTHENTICATOR_ID}", token, json=authenticator_body)
    api_debug_list.append(put_auth_res["debug"])

    put_jit_res = is_client.call("PUT", f"{tenant_path}/o/api/server/v1/identity-providers/{idp_id}/provisioning/jit", token, json=_build_jit_put_body(jit_enabled))
    api_debug_list.append(put_jit_res["debug"])

    group_attribute = request.form["group_attribute"]
    local_claims_by_uri = _build_local_claims_index(current_idp)
    new_claims_body = _build_claims_put_body(local_claims_by_uri, group_attribute)
    put_claims_res = is_client.call("PUT", f"{tenant_path}/o/api/server/v1/identity-providers/{idp_id}/claims", token, json=new_claims_body)
    api_debug_list.append(put_claims_res["debug"])

    _apply_idp_role_group_mappings(is_client, token, tenant_path, idp_id, request.form, api_debug_list)

    flash({"type": "success", "message": "Identity Provider updated successfully.", "api_debug_list": api_debug_list})
    return redirect(url_for("admin.idp", org_handle=org_handle))


@bp.route("/idp/<idp_id>/delete", methods=["POST"])
@login_required
@role_required(TEAMSPACE_ADMIN, IDP_MANAGER)
def delete_idp(org_handle, idp_id):
    _verify_org_handle(org_handle)
    denied = check_idp_access(org_handle)
    if denied:
        return denied

    is_client = ISClient(current_app.config["IS_BASE_URL"])
    token = session["access_token"]
    tenant_path = current_app.config.get("TENANT_PATH", "")

    api_debug_list = []

    idp_res = is_client.call("GET", f"{tenant_path}/o/api/server/v1/identity-providers/{idp_id}", token)
    api_debug_list.append(idp_res["debug"])

    app_id = _resolve_teamspace_app_id(is_client, token, tenant_path, api_debug_list)
    if app_id:
        _patch_auth_sequence(
            is_client, token, tenant_path, app_id,
            {
                "type": "DEFAULT",
                "steps": [
                    {
                        "id": 1,
                        "options": [
                            {"idp": "LOCAL", "authenticator": "BasicAuthenticator"}
                        ]
                    }
                ]
            },
            api_debug_list,
        )

    delete_res = is_client.call("DELETE", f"{tenant_path}/o/api/server/v1/identity-providers/{idp_id}", token)
    api_debug_list.append(delete_res["debug"])
    if delete_res["status_code"] in (200, 204):
        flash({"type": "success", "message": "Identity Provider deleted successfully.", "api_debug_list": api_debug_list})
    else:
        flash({"type": "error", "message": "Failed to delete Identity Provider.", "api_debug_list": api_debug_list})

    return redirect(url_for("admin.idp", org_handle=org_handle))
