import logging
import re

from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app

from webapp.utils.decorators import login_required, role_required
from webapp.utils.roles import BASIC_BRANDING_EDITOR, ADVANCED_BRANDING_EDITOR
from webapp.api_proxy import get_personalization, get_organization_plan
import webapp.is_operations as is_ops
from common.constants import DEFAULT_PRIMARY_COLOR, DEFAULT_SECONDARY_COLOR

logger = logging.getLogger(__name__)

bp = Blueprint("personalization", __name__)


_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{3,8}$")
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _valid_http_url(value: str) -> bool:
    return bool(value) and bool(_URL_RE.match(value.strip()))


# ── Routes ──────────────────────────────────────────────────────────────


@bp.route("/")
@login_required
def branding(org_handle):
    user_roles = session.get("user_roles", [])
    can_edit = any(
        r in user_roles
        for r in [BASIC_BRANDING_EDITOR, ADVANCED_BRANDING_EDITOR]
    )
    if not can_edit:
        org_id = session.get("user", {}).get("org_id", "")
        plan_info = get_organization_plan(org_id)
        org_plan = plan_info.get("plan", "basic")
        if org_plan in ("business", "enterprise"):
            required_role = "basic-branding-editor" if org_plan == "business" else "advanced-branding-editor"
            return render_template(
                "admin/access_denied.html",
                org_handle=org_handle,
                title="Personalization",
                description="Customize your organization's branding, logo, and colors.",
                required_role=required_role
            )
        return render_template("personalization/upgrade_prompt.html", org_handle=org_handle)


    is_advanced = ADVANCED_BRANDING_EDITOR in user_roles
    org_id = session.get("user", {}).get("org_id", "")
    current = get_personalization(org_id)
    return render_template(
        "personalization/branding.html",
        org_handle=org_handle,
        current=current,
        is_advanced=is_advanced,
    )


@bp.route("/", methods=["POST"])
@login_required
@role_required(BASIC_BRANDING_EDITOR, ADVANCED_BRANDING_EDITOR)
def update_branding(org_handle):
    org_id = session.get("user", {}).get("org_id", "")
    primary_color = request.form.get("primary_color", DEFAULT_PRIMARY_COLOR)
    secondary_color = request.form.get("secondary_color", DEFAULT_SECONDARY_COLOR)
    logo_url = request.form.get("logo_url", "")
    favicon_url = request.form.get("favicon_url", "")

    if not _COLOR_RE.match(primary_color or ""):
        flash({"message": "Invalid primary color. Use a hex value like #4F46E5.", "type": "error"})
        return redirect(url_for("personalization.branding", org_handle=org_handle))
    if not _COLOR_RE.match(secondary_color or ""):
        flash({"message": "Invalid secondary color. Use a hex value like #E0E7FF.", "type": "error"})
        return redirect(url_for("personalization.branding", org_handle=org_handle))
    if logo_url and not _valid_http_url(logo_url):
        flash({"message": "Invalid logo URL. Must start with http:// or https://.", "type": "error"})
        return redirect(url_for("personalization.branding", org_handle=org_handle))
    if favicon_url and not _valid_http_url(favicon_url):
        flash({"message": "Invalid favicon URL. Must start with http:// or https://.", "type": "error"})
        return redirect(url_for("personalization.branding", org_handle=org_handle))

    data = {
        "org": org_id,
        "logo_url": logo_url,
        "logo_alt_text": request.form.get("logo_alt_text", ""),
        "favicon_url": favicon_url,
        "primary_color": primary_color,
        "secondary_color": secondary_color,
    }

    token = session.get("access_token", "")
    tenant_path = current_app.config.get("TENANT_PATH", "")
    org_name = session.get("user", {}).get("org_name", "")

    res = is_ops.update_branding(data, token, tenant_path, org_name)

    if res.db_success:
        # Update session cache so sidebar/header reflect changes immediately
        session["org_branding"] = data
        if res.is_success:
            flash({"message": "Branding updated and synced to login portal.", "type": "success", "api_debug": res.api_debug})
        else:
            flash({"message": f"Branding updated locally, but failed to sync to login portal: {res.error_message}", "type": "warning", "api_debug": res.api_debug})
    else:
        flash({"message": f"Failed to update branding: {res.error_message}", "type": "error", "api_debug": res.api_debug})

    return redirect(url_for("personalization.branding", org_handle=org_handle))


@bp.route("/reset", methods=["POST"])
@login_required
@role_required(BASIC_BRANDING_EDITOR, ADVANCED_BRANDING_EDITOR)
def reset_branding(org_handle):
    org_id = session.get("user", {}).get("org_id", "")
    token = session.get("access_token", "")
    tenant_path = current_app.config.get("TENANT_PATH", "")

    res = is_ops.delete_branding(org_id, token, tenant_path)

    if res.db_success:
        session.pop("org_branding", None)
        if res.is_success:
            flash({"message": "Branding reverted to defaults.", "type": "success", "api_debug": res.api_debug})
        else:
            flash({"message": "Branding reverted locally, but failed to sync removal to login portal.", "type": "warning", "api_debug": res.api_debug})
    else:
        flash({"message": "Failed to revert branding.", "type": "error"})

    return redirect(url_for("personalization.branding", org_handle=org_handle))
