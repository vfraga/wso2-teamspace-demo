import logging
from werkzeug.wrappers import Response
from flask import Blueprint, render_template, request, session

from webapp.auth import start_login, handle_callback, logout as auth_logout
from webapp.is_operations import resolve_org_id
from webapp.api_proxy import get_personalization

logger = logging.getLogger(__name__)

bp = Blueprint("main", __name__)


@bp.route("/")
def landing() -> str:
    org_handle = request.args.get("org_handle")
    branding = None
    if org_handle:
        org_id = resolve_org_id(org_handle)
        if org_id:
            branding = get_personalization(org_id)
    return render_template("landing.html", branding=branding, org_handle=org_handle)


@bp.route("/login")
def login() -> Response | str:
    org_handle = request.args.get("org_handle")
    if org_handle:
        org_id = resolve_org_id(org_handle)
        if org_id:
            return start_login(org_id=org_id)
        return render_template("login.html", error=f"Organization '{org_handle}' not found.")
    return render_template("login.html")


@bp.route("/login", methods=["POST"])
def login_post() -> Response | str:
    org_handle = request.form.get("org_handle", "").strip()
    email = request.form.get("email", "").strip()
    if email and "@" in email:
        domain = email.split("@")[-1]
        handle = domain.split(".")[0]
        org_id = resolve_org_id(handle)
        if org_id:
            logger.info("Organization Discovery: mapped email %s to organization %s (id=%s)", email, handle, org_id)
            return start_login(org_id=org_id)
        return start_login(org_id=None)
    if org_handle:
        org_id = resolve_org_id(org_handle)
        if org_id:
            return start_login(org_id=org_id)
        return render_template("login.html", error=f"Organization '{org_handle}' not found.")
    return render_template("login.html", error="Please enter an organization handle or email.")


@bp.route("/callback")
def callback() -> Response:
    logger.debug("Received OAuth callback")
    return handle_callback()


@bp.route("/logout")
def logout() -> Response:
    logger.info("User logging out: %s", session.get("user", {}).get("email", "unknown"))
    return auth_logout()

