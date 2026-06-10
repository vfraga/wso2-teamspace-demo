import json
import logging

import jwt as pyjwt
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app

from webapp.utils.decorators import login_required
from webapp.api_proxy import get_meetings, create_meeting, update_meeting, delete_meeting, get_personalization, upsert_personalization
from webapp.is_operations import fetch_branding_from_is

logger = logging.getLogger(__name__)

bp = Blueprint("dashboard", __name__)


def _ensure_branding_loaded():
    """Load org branding into session if not already cached.

    Implements lazy-sync (Gap #5): if the local DB has no branding but
    WSO2 IS does (e.g. set via IS console), pull it down and backfill.
    """
    if "org_branding" in session:
        return

    org_id = session.get("user", {}).get("org_id", "")
    if not org_id:
        return

    branding = get_personalization(org_id)
    if branding:
        session["org_branding"] = branding
        return

    # No local branding — try lazy sync from IS if user is admin
    is_admin = session.get("is_admin", False)
    if not is_admin:
        session["org_branding"] = None
        return

    try:
        token = session.get("access_token", "")
        tenant_path = current_app.config.get("TENANT_PATH", "")
        is_branding = fetch_branding_from_is(org_id, token, tenant_path)
        if is_branding:
            logger.info("Lazy-syncing branding from IS for org=%s", org_id)
            upsert_res = upsert_personalization(is_branding)
            if upsert_res.status_code in (200, 201):
                session["org_branding"] = is_branding
                return
    except Exception:
        logger.exception("Lazy branding sync from IS failed")

    session["org_branding"] = None


@bp.route("/")
@login_required
def home(org_handle):
    _ensure_branding_loaded()
    user = session.get("user", {})
    id_token_debug = ""
    id_raw = session.get("id_token_raw", "")
    if id_raw:
        try:
            claims = pyjwt.decode(id_raw, options={"verify_signature": False})
            debug_keys = {k: v for k, v in claims.items() if k in ("roles", "groups", "application_roles", "scope")}
            id_token_debug = json.dumps(debug_keys, indent=2) if debug_keys else "No roles/groups claims found in id_token"
        except Exception:
            id_token_debug = "Failed to decode id_token"
    return render_template("dashboard/home.html", org_handle=org_handle, user=user, id_token_debug=id_token_debug)


@bp.route("/meetings")
@login_required
def meetings(org_handle):
    meeting_list = get_meetings()
    return render_template("dashboard/meetings.html", org_handle=org_handle, meetings=meeting_list)


@bp.route("/meetings/new", methods=["GET"])
@login_required
def meeting_form(org_handle):
    return render_template("dashboard/meeting_form.html", org_handle=org_handle, meeting=None)


@bp.route("/meetings", methods=["POST"])
@login_required
def create(org_handle):
    data = {
        "topic": request.form["topic"],
        "date": request.form["date"],
        "start_time": request.form["start_time"],
        "duration": request.form.get("duration", "60"),
        "time_zone": request.form.get("time_zone", "America/Sao_Paulo"),
    }
    create_res = create_meeting(data)
    if create_res.status_code == 201:
        flash("Meeting scheduled successfully.", "success")
    else:
        flash("Failed to create meeting.", "error")
    return redirect(url_for("dashboard.meetings", org_handle=org_handle))


@bp.route("/meetings/<meeting_id>/edit", methods=["GET"])
@login_required
def edit_form(org_handle, meeting_id):
    meeting_list = get_meetings()
    meeting = next((m for m in meeting_list if m["id"] == meeting_id), None)
    return render_template("dashboard/meeting_form.html", org_handle=org_handle, meeting=meeting)


@bp.route("/meetings/<meeting_id>", methods=["POST"])
@login_required
def update(org_handle, meeting_id):
    data = {
        "topic": request.form["topic"],
        "date": request.form["date"],
        "start_time": request.form["start_time"],
        "duration": request.form.get("duration", "60"),
        "time_zone": request.form.get("time_zone", "America/Sao_Paulo"),
    }
    update_res = update_meeting(meeting_id, data)
    if update_res.status_code == 200:
        flash("Meeting updated.", "success")
    else:
        flash("Failed to update meeting.", "error")
    return redirect(url_for("dashboard.meetings", org_handle=org_handle))


@bp.route("/meetings/<meeting_id>/delete", methods=["POST"])
@login_required
def delete(org_handle, meeting_id):
    delete_res = delete_meeting(meeting_id)
    if delete_res.status_code == 204:
        flash("Meeting deleted.", "success")
    else:
        flash("Failed to delete meeting.", "error")
    return redirect(url_for("dashboard.meetings", org_handle=org_handle))
