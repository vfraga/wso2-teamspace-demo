import logging
import re

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app

from webapp.auth import get_client_credentials_token, switch_org_token
from webapp.is_client import ISClient
from webapp.is_operations import share_roles_with_org, assign_roles_to_user
from webapp.utils.helpers import slugify
from webapp.utils.roles import TEAMSPACE_ADMIN, TEAMSPACE_USER
from webapp.plans import PLAN_ROLES
from webapp.api_proxy import save_organization_plan

logger = logging.getLogger(__name__)

bp = Blueprint("signup", __name__)

_EMAIL_RE = re.compile(r"^[^@\s\u0000-\u001f]+@[^@\s\u0000-\u001f]+\.[^@\s\u0000-\u001f]+$")
_FORBIDDEN_ORG_CHARS = re.compile(r"[<>&]|\x00")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

MAX_ORG_NAME = 100
MAX_NAME = 100
MAX_PASSWORD = 128


def _invalid_field(field, message):
    flash({"type": "error", "message": f"{field}: {message}"})
    return redirect(url_for("signup.signup_form"))


def _check_org_name(value):
    if not value:
        return _invalid_field("Organization name", "is required")
    if len(value) > MAX_ORG_NAME:
        return _invalid_field("Organization name", f"must be at most {MAX_ORG_NAME} characters")
    if _FORBIDDEN_ORG_CHARS.search(value) or _CONTROL_CHARS_RE.search(value):
        return _invalid_field("Organization name", "contains forbidden characters")
    return None


def _check_org_handle(value):
    if value and _FORBIDDEN_ORG_CHARS.search(value):
        return _invalid_field("Organization handle", "contains forbidden characters")
    return None


def _check_admin_email(value):
    if not value or "@" not in value or _CONTROL_CHARS_RE.search(value):
        return _invalid_field("Email", "must be a valid address")
    if not _EMAIL_RE.match(value):
        return _invalid_field("Email", "must be a valid address")
    return None


def _check_admin_password(value):
    if not value or len(value) > MAX_PASSWORD:
        return _invalid_field("Password", "is required and must be reasonable length")
    return None


def _check_name_field(label, value):
    if len(value) > MAX_NAME or _CONTROL_CHARS_RE.search(value):
        return _invalid_field(label, f"must be at most {MAX_NAME} characters")
    return None


def _validate_signup_form(form):
    org_name = (form.get("org_name") or "").strip()
    org_handle_input = (form.get("org_handle") or "").strip()
    admin_email = (form.get("email") or "").strip()
    admin_password = form.get("password") or ""
    first_name = (form.get("first_name") or "").strip()
    last_name = (form.get("last_name") or "").strip()
    selected_plan = (form.get("plan") or "basic").strip()

    for check in (
        _check_org_name(org_name),
        _check_org_handle(org_handle_input),
        _check_admin_email(admin_email),
        _check_admin_password(admin_password),
        _check_name_field("First name", first_name),
        _check_name_field("Last name", last_name),
    ):
        if check is not None:
            return None, check

    return {
        "org_name": org_name,
        "org_handle": org_handle_input or slugify(org_name),
        "admin_email": admin_email,
        "admin_password": admin_password,
        "first_name": first_name,
        "last_name": last_name,
        "selected_plan": selected_plan,
    }, None


@bp.route("/")
def signup_form():
    return render_template("signup.html")




@bp.route("/", methods=["POST"])
def register():
    fields, error = _validate_signup_form(request.form)
    if error is not None:
        return error

    org_name = fields["org_name"]
    org_handle = fields["org_handle"]
    admin_email = fields["admin_email"]
    admin_password = fields["admin_password"]
    first_name = fields["first_name"]
    last_name = fields["last_name"]
    selected_plan = fields["selected_plan"]

    logger.info("Starting org registration: org_name=%s, admin=%s, plan=%s", org_name, admin_email, selected_plan)
    is_client = ISClient(current_app.config["IS_BASE_URL"])

    root_token = get_client_credentials_token()
    if not root_token:
        logger.error("Failed to get client_credentials token")
        flash("Failed to authenticate with Identity Server.", "error")
        return redirect(url_for("signup.signup_form"))

    tenant_path = current_app.config.get("TENANT_PATH", "")
    result = is_client.call(
        "POST",
        f"{tenant_path}/api/server/v1/organizations",
        root_token,
        json={"name": org_name, "type": "TENANT", "orgHandle": org_handle},
    )
    if result["status_code"] not in (200, 201):
        logger.error("Failed to create org: status=%s, body=%s", result["status_code"], result["data"])
        flash({
            "type": "error",
            "message": f"Failed to create organization: {result['data']}",
            "api_debug": result["debug"],
        })
        return redirect(url_for("signup.signup_form"))

    new_org_id = result["data"]["id"]
    org_handle = result["data"].get("orgHandle", new_org_id)
    logger.info("Organization created: id=%s, handle=%s, name=%s", new_org_id, org_handle, org_name)
    api_debug_list = [result["debug"]]

    extra_roles = PLAN_ROLES.get(selected_plan, [])
    # Base teamspace-admin/teamspace-user roles are shared with every org by the
    # setup script's share-with-all. Here we share only this plan's extra roles,
    # and only with the new org, so pre-existing organizations are never touched.
    share_roles_with_org(is_client, root_token, tenant_path, new_org_id, extra_roles, api_debug_list)

    sub_org_token = switch_org_token(new_org_id, root_token)
    if not sub_org_token:
        logger.error("Failed to switch org context for org_id=%s", new_org_id)
        flash("Failed to switch to new organization context.", "error")
        return redirect(url_for("signup.signup_form"))

    logger.info("Creating admin user in org=%s", new_org_id)
    user_result = is_client.call(
        "POST",
        f"{tenant_path}/o/scim2/Users",
        sub_org_token,
        json={
            "schemas": [],
            "userName": admin_email,
            "password": admin_password,
            "name": {"givenName": first_name, "familyName": last_name},
            "emails": [{"value": admin_email, "primary": True}],
        },
    )
    api_debug_list.append(user_result["debug"])

    if user_result["status_code"] not in (200, 201):
        logger.error("Failed to create admin user: status=%s, body=%s", user_result["status_code"], user_result["data"])
        flash({
            "type": "error",
            "message": f"Failed to create admin user: {user_result['data']}",
            "api_debug_list": api_debug_list,
        })
        return redirect(url_for("signup.signup_form"))

    user_id = user_result["data"].get("id", "")
    logger.info("Admin user created: id=%s, assigning roles", user_id)
    assign_roles_to_user(
        is_client, sub_org_token, tenant_path, user_id,
        [TEAMSPACE_ADMIN, TEAMSPACE_USER] + extra_roles, api_debug_list,
    )

    # Save the selected plan to the SQLite DB
    plan_save_res = save_organization_plan(new_org_id, selected_plan, token=sub_org_token)
    if plan_save_res.status_code not in (200, 201):
        logger.error("Failed to save organization plan to DB: status=%s, body=%s", plan_save_res.status_code, plan_save_res.data)


    flash({
        "type": "success",
        "message": f"Organization '{org_name}' created! You can now sign in.",
        "api_debug_list": api_debug_list,
    })
    return redirect(url_for("main.landing", org_handle=org_handle))
