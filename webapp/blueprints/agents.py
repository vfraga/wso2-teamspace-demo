import logging

from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, abort

from webapp.utils.decorators import login_required, admin_required
from webapp.utils.roles import TEAMSPACE_ADMIN
from webapp.is_client import ISClient
from webapp.auth import get_agent_management_token
from webapp.api_proxy import get_agent_config, save_agent_config, delete_agent_config, resolve_plan_for_gating
from webapp.is_operations import assign_roles_to_user
from webapp.utils.roles import TEAMSPACE_USER
from webapp.auth import get_root_role_management_token

logger = logging.getLogger(__name__)

bp = Blueprint("agents", __name__)


def _check_plan_and_role(required_plan: str, required_role: str, title: str, description: str, upgrade_template: str):
    """Gate a feature on the org's plan and the user's role.

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
    
    Returns a response to short-circuit with, or None to allow.
    """
    org_handle = request.view_args.get("org_handle") if request.view_args else None
    org_id = session.get("user", {}).get("org_id", "")
    user_roles = session.get("user_roles", [])

    org_plan = resolve_plan_for_gating(org_id)
    if org_plan is not None and org_plan != required_plan:
        return render_template(upgrade_template, org_handle=org_handle)

    if required_role not in user_roles:
        if org_plan == required_plan:
            return render_template(
                "admin/access_denied.html",
                org_handle=org_handle,
                title=title,
                description=description,
                required_role=required_role,
            )
        logger.warning(
            "Plan unknown for org=%s and role %s absent; showing the upgrade prompt for %s",
            org_id, required_role, title,
        )
        return render_template(upgrade_template, org_handle=org_handle)

    if org_plan is None:
        logger.warning(
            "Plan unknown for org=%s; allowing %s on the strength of the %s role alone",
            org_id, title, required_role,
        )
    return None


@bp.before_request
def gate_agents_by_plan():
    if not session.get("is_admin"):
        abort(403)

    denied = _check_plan_and_role(
        required_plan="enterprise",
        required_role=TEAMSPACE_ADMIN,
        title="AI Agents",
        description="Deploy autonomous AI agents to interact with your team.",
        upgrade_template="admin/agents_upgrade.html",
    )
    if denied:
        return denied




@bp.route("/")
@login_required
@admin_required
def list_agents(org_handle):
    user = session.get("user", {})
    org_id = user.get("org_id", "")
    agent_cfg = get_agent_config(org_id) if org_id else None
    return render_template("admin/agents.html", org_handle=org_handle, agent_config=agent_cfg)


@bp.route("/add", methods=["GET"])
@login_required
@admin_required
def agent_form(org_handle):
    return render_template("admin/agent_form.html", org_handle=org_handle)


@bp.route("/add", methods=["POST"])
@login_required
@admin_required
def add_agent(org_handle):
    is_client = ISClient(current_app.config["IS_BASE_URL"])
    tenant_path = current_app.config.get("TENANT_PATH", "")
    user = session.get("user", {})

    display_name = request.form["display_name"]
    description = request.form.get("description", "")
    gemini_api_key = request.form.get("gemini_api_key", "")
    custom_prompt = request.form.get("custom_prompt", "")

    # Step 1: Create agent identity in IS using client_credentials token
    agent_token = get_agent_management_token()
    if not agent_token:
        flash({"type": "error", "message": "Failed to get agent management token. Check IS configuration."})
        return redirect(url_for("agents.list_agents", org_handle=org_handle))

    body = {
        "urn:scim:wso2:agent:schema": {
            "DisplayName": display_name,
            "Description": description,
            "Owner": f"{user.get('sub', '')}@{user.get('org_handle', '')}",
        }
    }
    result = is_client.call("POST", f"{tenant_path}/o/scim2/Agents", agent_token, json=body)
    if result["status_code"] not in (200, 201):
        flash({"type": "error", "message": "Failed to create agent in IS.", "api_debug": result["debug"]})
        return redirect(url_for("agents.list_agents", org_handle=org_handle))

    agent_data = result["data"]
    agent_id = agent_data.get("id", "")
    agent_secret = agent_data.get("password", "")

    api_debug_list = [result["debug"]]

    # Assign TEAMSPACE_USER role to the agent so that it has the required scopes.
    # We must do this at the root tenant level using root client credentials with role management scopes,
    # because agents reside in the AGENT userstore which is only accessible at the root level.
    root_token = get_root_role_management_token()
    if root_token and agent_id:
        logger.info("Assigning TEAMSPACE_USER role to agent at root level: agent_id=%s", agent_id)
        assign_roles_to_user(
            is_client, root_token, "/t/teamspace", agent_id,
            [TEAMSPACE_USER], api_debug_list, use_org_endpoint=False
        )

    # Step 2: Store agent config in Business API
    org_id = user.get("org_id", "")
    save_result = save_agent_config({
        "org": org_id,
        "agent_id": agent_id,
        "agent_secret": agent_secret,
        "display_name": display_name,
        "description": description,
        "gemini_api_key": gemini_api_key,
        "custom_prompt": custom_prompt,
    })
    if save_result.status_code in (200, 201):
        flash({"type": "success", "message": f"Agent '{display_name}' deployed successfully.", "api_debug_list": api_debug_list})
        session["has_agent_config"] = True
    else:
        flash({"type": "warning", "message": "Agent created in IS but config save failed.", "api_debug_list": api_debug_list})
    return redirect(url_for("agents.list_agents", org_handle=org_handle))


@bp.route("/delete", methods=["POST"])
@login_required
@admin_required
def delete_agent(org_handle):
    is_client = ISClient(current_app.config["IS_BASE_URL"])
    tenant_path = current_app.config.get("TENANT_PATH", "")
    user = session.get("user", {})
    org_id = user.get("org_id", "")

    agent_cfg = get_agent_config(org_id)
    if not agent_cfg:
        flash({"type": "error", "message": "No agent configured."})
        return redirect(url_for("agents.list_agents", org_handle=org_handle))

    agent_id = agent_cfg.get("agent_id", "")
    agent_token = get_agent_management_token()
    if agent_token and agent_id:
        result = is_client.call("DELETE", f"{tenant_path}/o/scim2/Agents/{agent_id}", agent_token)
        if result["status_code"] not in (204, 404):
            flash({"type": "warning", "message": "Failed to delete agent from IS.", "api_debug": result["debug"]})

    delete_agent_config(org_id)
    session["has_agent_config"] = False
    flash({"type": "success", "message": "Agent removed."})
    return redirect(url_for("agents.list_agents", org_handle=org_handle))
