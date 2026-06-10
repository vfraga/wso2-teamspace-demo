from functools import wraps
from flask import session, redirect, url_for, abort, request

from webapp.api_proxy import get_organization_plan


def _route_org_handle(kwargs):
    return kwargs.get("org_handle") or (
        request.view_args.get("org_handle") if request.view_args else None
    )


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            org_handle = _route_org_handle(kwargs)
            if org_handle:
                return redirect(url_for("main.login", org_handle=org_handle))
            return redirect(url_for("main.login"))
        route_org = _route_org_handle(kwargs)
        session_org = session.get("user", {}).get("org_handle", "")
        if route_org and session_org and route_org != session_org:
            abort(403)
        return f(*args, **kwargs)
    return decorated


def org_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            abort(401)
        route_org = _route_org_handle(kwargs)
        session_org = session.get("user", {}).get("org_handle", "")
        if route_org and session_org and route_org != session_org:
            abort(403)
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            abort(403)
        return f(*args, **kwargs)
    return decorated


def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user_roles = session.get("user_roles", [])
            if not any(r in user_roles for r in roles):
                abort(403)
            return f(*args, **kwargs)
        return decorated
    return decorator


def require_plan_and_role(required_plan: str, required_role: str):
    """Check org plan level and user role before allowing access."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            org_id = session.get("user", {}).get("org_id", "")
            plan_info = get_organization_plan(org_id)
            org_plan = plan_info.get("plan", "basic")
            if org_plan != required_plan:
                abort(403)
            user_roles = session.get("user_roles", [])
            if required_role not in user_roles:
                abort(403)
            return f(*args, **kwargs)
        return decorated
    return decorator


