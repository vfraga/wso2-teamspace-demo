import logging
import os
import sys

import requests
from cachelib.file import FileSystemCache
from flask import Flask, Response, request, redirect, session, render_template
from typing import Any

from webapp.config import Config
from webapp.auth import init_oauth
from webapp.blueprints import main, dashboard, admin, personalization, signup, subscription, chat, agents
from webapp.utils.helpers import get_sidebar_items
from webapp.utils.helpers import has_role, has_scope, contrast_text
from webapp.utils.i18n import init_translations, translate, get_locale

logger = logging.getLogger(__name__)


def _populate_org_plan_in_session(org_id: str) -> None:
    if not org_id or "org_plan" in session:
        return
    from webapp.api_proxy import get_organization_plan
    try:
        plan_info = get_organization_plan(org_id)
        session["org_plan"] = plan_info.get("plan", "basic")
    except requests.RequestException:
        logger.warning("org_plan lookup failed (transient), will retry")
        logger.debug("org_plan transient failure trace", exc_info=True)
    except Exception:
        logger.exception("org_plan lookup failed unexpectedly")
        session["org_plan"] = "basic"


def _populate_agent_availability_in_session(org_id: str) -> None:
    """Cache whether the org has an AI agent configured, so the chat panel can
    show its 'Assistant Offline' state instead of a live chat that can't act.

    Mirrors the org_plan lazy-cache. The deploy/remove handlers in the agents
    blueprint keep this fresh for the acting admin; this populates it for
    everyone else (and on fresh logins) via the M2M agent-config lookup, which
    does not depend on the user's view_agent_config scope.
    """
    if not org_id or "has_agent_config" in session:
        return
    from webapp.api_proxy import get_agent_config_via_internal_secret
    try:
        session["has_agent_config"] = bool(get_agent_config_via_internal_secret(org_id))
    except requests.RequestException:
        logger.warning("agent-config lookup failed (transient), will retry")
        logger.debug("agent-config transient failure trace", exc_info=True)
    except Exception:
        logger.exception("agent-config lookup failed unexpectedly")
        session["has_agent_config"] = False


def create_app() -> Flask:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("authlib").setLevel(logging.INFO)
    logging.getLogger("werkzeug").setLevel(logging.INFO)

    app = Flask(__name__)
    app.config.from_object(Config)
    
    # Lazily initialize FileSystemCache and compute TENANT_PATH at runtime
    app.config["SESSION_CACHELIB"] = FileSystemCache(
        cache_dir=os.path.join(os.path.dirname(__file__), "..", "flask_session"),
        threshold=500
    )
    is_org_handle = app.config.get("IS_ORG_HANDLE", "")
    app.config["TENANT_PATH"] = f"/t/{is_org_handle}" if is_org_handle else ""
    app.config["OIDC_REDIRECT_URI"] = f"http://{Config.FLASK_HOST}:{Config.FLASK_PORT}/callback"
    app.config["OIDC_POST_LOGOUT_URI"] = f"http://{Config.FLASK_HOST}:{Config.FLASK_PORT}"

    logger.info("Creating Flask app, port=%s", Config.FLASK_PORT)

    from flask_session import Session
    Session(app)
    init_translations(app)
    init_oauth(app)

    @app.route("/set_lang")
    def set_lang():
        lang = request.args.get("lang", "en")
        if lang in ("en", "pt"):
            session["lang"] = lang
        return redirect(request.referrer or "/")

    @app.route("/health")
    def health():
        return {"status": "healthy"}

    @app.after_request
    def add_security_headers(response: Response) -> Response:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        is_prod = os.getenv("FLASK_ENV") == "production"
        if is_prod:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
                "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net; "
                "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net; "
                "img-src 'self' data: https:; "
                "connect-src 'self'"
            )
        if request.is_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    app.register_blueprint(main.bp)
    app.register_blueprint(dashboard.bp, url_prefix="/o/<org_handle>")
    app.register_blueprint(admin.bp, url_prefix="/o/<org_handle>/admin")
    app.register_blueprint(personalization.bp, url_prefix="/o/<org_handle>/personalization")
    app.register_blueprint(signup.bp, url_prefix="/signup")
    app.register_blueprint(subscription.bp, url_prefix="/o/<org_handle>/subscription")
    app.register_blueprint(chat.bp, url_prefix="/o/<org_handle>/chat")
    app.register_blueprint(agents.bp, url_prefix="/o/<org_handle>/admin/agents")

    @app.before_request
    def auto_ensure_branding() -> None:
        if "org_branding" not in session:
            try:
                from webapp.blueprints.dashboard import _ensure_branding_loaded
                _ensure_branding_loaded()
            except requests.RequestException:
                logger.warning("Branding pre-load failed (transient), will retry next request")
                logger.debug("Branding pre-load transient failure trace", exc_info=True)
            except Exception:
                logger.exception("Branding pre-load raised an unexpected error")


    @app.context_processor
    def inject_globals() -> dict[str, Any]:
        user = session.get("user", {})
        org_handle = user.get("org_handle", "")
        org_id = user.get("org_id", "")
        user_roles = session.get("user_roles", [])
        org_branding = session.get("org_branding")

        _populate_org_plan_in_session(org_id)
        _populate_agent_availability_in_session(org_id)

        from webapp.blueprints.chat import decode_jwt
        return {
            "current_user": user,
            "org_handle": org_handle,
            "org_id": org_id,
            "sidebar_items": get_sidebar_items(org_handle, user_roles, request.path) if org_handle else [],
            "has_role": has_role,
            "has_scope": has_scope,
            "contrast_text": contrast_text,
            "is_admin": session.get("is_admin", False),
            "org_branding": org_branding,
            "org_plan": session.get("org_plan", "basic"),
            "has_agent_config": session.get("has_agent_config", False),
            "_t": translate,
            "current_locale": get_locale(),
            "decode_jwt": decode_jwt,
        }

    @app.errorhandler(404)
    def not_found(e):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(e):
        return render_template("errors/500.html"), 500

    return app
