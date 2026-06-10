import re

from flask import session

from webapp.utils.roles import (
    TEAMSPACE_ADMIN, BASIC_BRANDING_EDITOR, ADVANCED_BRANDING_EDITOR, IDP_MANAGER,
)


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")


def mask_token(token: str, visible: int = 10) -> str:
    if len(token) <= visible:
        return token
    return token[:visible] + "..." + token[-4:]


def contrast_text(hex_color: str, light: str = "#FFFFFF", dark: str = "#111827") -> str:
    """Return a readable text colour for a given background `hex_color`.

    Used to give brand-coloured surfaces (buttons, badges) automatic contrast:
    a light background gets dark text, a dark background gets light text. The
    threshold (relative luminance > 0.179) is the WCAG cross-over point where
    black text yields a better contrast ratio than white. Falls back to `light`
    on any unparseable input so a bad brand value never crashes rendering.
    """
    try:
        h = hex_color.strip().lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except (ValueError, AttributeError, IndexError):
        return light

    def _linear(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    luminance = 0.2126 * _linear(r) + 0.7152 * _linear(g) + 0.0722 * _linear(b)
    return dark if luminance > 0.179 else light


_SECURITY_SIDEBAR_ITEM = {
    "id": "security",
    "label": "Security",
    "icon": "lock",
    "href": "/o/{org_handle}/admin/security",
    "roles": [TEAMSPACE_ADMIN, IDP_MANAGER],
}

SIDEBAR_ITEMS = [
    {"id": "home", "label": "Dashboard", "icon": "home", "href": "/o/{org_handle}", "roles": ["*"]},
    {"id": "meetings", "label": "Manage Meetings", "icon": "calendar", "href": "/o/{org_handle}/meetings", "roles": ["*"]},
    {"id": "users", "label": "Manage Users", "icon": "users", "href": "/o/{org_handle}/admin/users", "roles": [TEAMSPACE_ADMIN]},
    {"id": "roles", "label": "Manage Roles", "icon": "shield", "href": "/o/{org_handle}/admin/roles", "roles": [TEAMSPACE_ADMIN]},
    {"id": "personalization", "label": "Personalization", "icon": "palette", "href": "/o/{org_handle}/personalization", "roles": [TEAMSPACE_ADMIN, BASIC_BRANDING_EDITOR, ADVANCED_BRANDING_EDITOR]},
    {"id": "agents", "label": "AI Agents", "icon": "bot", "href": "/o/{org_handle}/admin/agents", "roles": [TEAMSPACE_ADMIN]},
    {**_SECURITY_SIDEBAR_ITEM, "children": ["/o/{org_handle}/admin/idp", "/o/{org_handle}/admin/security/login-flow"]},
    {"id": "subscription", "label": "Subscription", "icon": "credit-card", "href": "/o/{org_handle}/subscription", "roles": [TEAMSPACE_ADMIN]},
]


def get_sidebar_items(org_handle: str, user_roles: list[str], request_path: str = "") -> list[dict]:
    items = []
    for item in SIDEBAR_ITEMS:
        if "*" in item["roles"] or any(r in user_roles for r in item["roles"]):
            href = item["href"].format(org_handle=org_handle)
            resolved = {
                **item,
                "href": href,
            }
            # Compute active state server-side to avoid Jinja2 scoping issues
            if request_path:
                path = request_path.rstrip("/")
                href_stripped = href.rstrip("/")
                if item["id"] == "home":
                    is_active = path == href_stripped
                else:
                    is_active = (
                        path == href_stripped
                        or request_path.startswith(href + "/")
                    )
                # Check child paths (e.g. Security -> IdP, Login Flow)
                if not is_active and "children" in item:
                    for child in item["children"]:
                        child_resolved = child.format(org_handle=org_handle)
                        child_stripped = child_resolved.rstrip("/")
                        if path == child_stripped or request_path.startswith(child_resolved + "/"):
                            is_active = True
                            break
                resolved["is_active"] = is_active
            items.append(resolved)
    return items


def has_role(*roles) -> bool:
    user_roles = session.get("user_roles", [])
    return any(r in user_roles for r in roles)


def has_scope(*scopes) -> bool:
    user_scopes = session.get("user_scopes", [])
    return any(s in user_scopes for s in scopes)
