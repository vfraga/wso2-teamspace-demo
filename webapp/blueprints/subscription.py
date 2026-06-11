import logging

from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app

from webapp.utils.decorators import login_required, admin_required
from webapp.auth import get_client_credentials_token, switch_org_token
from webapp.is_client import ISClient
from webapp.is_operations import share_roles_with_org, assign_roles_to_user
from webapp.api_proxy import get_organization_plan, save_organization_plan
from webapp.plans import PLANS

logger = logging.getLogger(__name__)

bp = Blueprint("subscription", __name__)


@bp.route("/")
@login_required
def plans(org_handle):
    org_id = session.get("user", {}).get("org_id", "")
    plan_info = get_organization_plan(org_id)
    current_plan = plan_info.get("plan", "basic")
    return render_template(
        "subscription/plans.html",
        org_handle=org_handle,
        plans=PLANS,
        current_plan=current_plan,
    )



@bp.route("/upgrade", methods=["POST"])
@login_required
@admin_required
def upgrade(org_handle):
    target_plan = request.form.get("plan")
    plan = next((p for p in PLANS if p["id"] == target_plan), None)
    if not plan:
        flash("Invalid plan.", "error")
        return redirect(url_for("subscription.plans", org_handle=org_handle))

    is_client = ISClient(current_app.config["IS_BASE_URL"])
    root_token = get_client_credentials_token()
    if not root_token:
        flash("Failed to authenticate with Identity Server.", "error")
        return redirect(url_for("subscription.plans", org_handle=org_handle))

    org_id = session.get("user", {}).get("org_id", "")
    user_id = session.get("user", {}).get("sub", "")
    tenant_path = current_app.config.get("TENANT_PATH", "")
    api_debug_list = []

    # Share only this plan's extra roles, and only with THIS org — never
    # share-with-all, which would re-apply role sharing to every org. The base
    # teamspace-admin/teamspace-user roles are already shared org-wide by setup.
    share_roles_with_org(is_client, root_token, tenant_path, org_id, plan["upgrade_roles"], api_debug_list)

    sub_org_token = switch_org_token(org_id, root_token)
    if sub_org_token:
        if plan["upgrade_roles"] and user_id:
            assign_roles_to_user(
                is_client, sub_org_token, tenant_path, user_id,
                plan["upgrade_roles"], api_debug_list,
            )
        # Save the new plan to the SQLite DB
        plan_save_res = save_organization_plan(org_id, target_plan, token=sub_org_token)
        if plan_save_res.status_code not in (200, 201):
            logger.error("Failed to update organization plan in DB: status=%s, body=%s", plan_save_res.status_code, plan_save_res.data)
        else:
            # Update the cached plan in session so UI reflects immediately
            session["org_plan"] = target_plan
    else:
        logger.error("Failed to switch org context for plan update")
        flash("Failed to update organization plan. Please try again.", "error")
        return redirect(url_for("subscription.plans", org_handle=org_handle))


    flash({
        "type": "success",
        "message": f"Upgraded to {plan['name']} plan! Please log out and back in to see new features.",
        "api_debug_list": api_debug_list,
    })
    return redirect(url_for("subscription.plans", org_handle=org_handle))
