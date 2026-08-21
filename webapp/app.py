import logging
import os

import requests
from cachelib.file import FileSystemCache
from flask import Flask, Response, request, redirect, session, render_template
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from typing import Any

from common import rate_limit
from common.logging_setup import configure_logging
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
    from webapp.api_proxy import get_agent_config_via_service_token
    try:
        session["has_agent_config"] = bool(get_agent_config_via_service_token(org_id))
    except requests.RequestException:
        logger.warning("agent-config lookup failed (transient), will retry")
        logger.debug("agent-config transient failure trace", exc_info=True)
    except Exception:
        logger.exception("agent-config lookup failed unexpectedly")
        session["has_agent_config"] = False


def _configure_rate_limiting(app: Flask) -> Limiter:
    """Attach the portal's rate limiter and limit the chat blueprint.

    Only the chat routes carry an explicit limit — they proxy to the agent and
    therefore to Gemini. Everything else is left unlimited so browsing the demo
    cannot trip a 429. Storage matches the rest of the stack: Redis when
    REDIS_URL is set, in-process memory otherwise.

    The limit is applied to the blueprint object (which Flask-Limiter supports)
    before it is registered, so it covers every route the blueprint adds
    without each one needing a decorator.
    """
    limiter = Limiter(
        get_remote_address,
        app=app,
        storage_uri=rate_limit.storage_uri(),
        enabled=rate_limit.ENABLED,
        default_limits=[rate_limit.DEFAULT_LIMIT] if rate_limit.DEFAULT_LIMIT else [],
    )
    limiter.limit(rate_limit.CHAT_LIMIT)(chat.bp)
    logger.info("Web Portal %s", rate_limit.describe())
    return limiter


def _configure_session_backend(app: Flask) -> None:
    """Select the session store: Redis when configured, filesystem otherwise.

    Filesystem sessions (`flask_session/` via cachelib) are per-instance, so a
    user bounced between gunicorn workers behind a load balancer loses their
    login. Reuses the same REDIS_URL as the agent's state store, so one setting
    makes the whole stack multi-instance capable.
    """
    redis_url = os.getenv("REDIS_URL", "").strip()
    if redis_url:
        try:
            import redis

            client = redis.Redis.from_url(redis_url)
            client.ping()
        except ImportError:
            logger.error(
                "REDIS_URL is set but the redis package is not installed; falling back "
                "to filesystem sessions. Install the extra with `uv sync --extra redis`."
            )
        except Exception:
            logger.exception(
                "REDIS_URL is set but Redis is unreachable; falling back to filesystem "
                "sessions. Sessions will not be shared across instances."
            )
        else:
            app.config["SESSION_TYPE"] = "redis"
            app.config["SESSION_REDIS"] = client
            app.config["SESSION_KEY_PREFIX"] = os.getenv("REDIS_KEY_PREFIX", "") + "teamspace:session:"
            logger.info("Session store: Redis (shared across instances)")
            return

    app.config["SESSION_TYPE"] = "cachelib"
    app.config["SESSION_CACHELIB"] = FileSystemCache(
        cache_dir=os.path.join(os.path.dirname(__file__), "..", "flask_session"),
        threshold=500,
    )
    logger.info("Session store: filesystem (set REDIS_URL to share across instances)")


def create_app() -> Flask:
    configure_logging("Web Portal")

    app = Flask(__name__)
    app.config.from_object(Config)
    
    _configure_session_backend(app)
    is_org_handle = app.config.get("IS_ORG_HANDLE", "")
    app.config["TENANT_PATH"] = f"/t/{is_org_handle}" if is_org_handle else ""
    app.config["OIDC_REDIRECT_URI"] = f"http://{Config.FLASK_HOST}:{Config.FLASK_PORT}/callback"
    app.config["OIDC_POST_LOGOUT_URI"] = f"http://{Config.FLASK_HOST}:{Config.FLASK_PORT}"

    logger.info("Creating Flask app, port=%s", Config.FLASK_PORT)

    from flask_session import Session
    Session(app)
    _configure_rate_limiting(app)
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
            # Alpine's default CDN build compiles x-data / @click with `new Function`,
            # which CSP treats as 'unsafe-eval'. 'unsafe-inline' is already required
            # for those attributes; the Alpine CSP build would mean rewriting every
            # template onto Alpine.data(). connect-src includes the script CDNs so
            # DevTools can fetch .map files (DOMPurify's is the noisy one).
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
                "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net; "
                "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://unpkg.com https://cdn.jsdelivr.net; "
                "img-src 'self' data: https:; "
                "connect-src 'self' https://unpkg.com https://cdn.jsdelivr.net"
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
