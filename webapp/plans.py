from webapp.utils.roles import (
    TEAMSPACE_ADMIN, TEAMSPACE_USER, BASIC_BRANDING_EDITOR,
    ADVANCED_BRANDING_EDITOR, IDP_MANAGER,
)

PLAN_ROLES = {
    "basic": [],
    "business": [BASIC_BRANDING_EDITOR],
    "enterprise": [BASIC_BRANDING_EDITOR, ADVANCED_BRANDING_EDITOR, IDP_MANAGER],
}

PLANS = [
    {
        "id": "basic",
        "name": "Basic",
        "price": "Free",
        "features": ["Manage Meetings", "User Management", "Role Management"],
        "roles_shared": [TEAMSPACE_ADMIN, TEAMSPACE_USER],
        "upgrade_roles": [],
    },
    {
        "id": "business",
        "name": "Business",
        "price": "$5/user/month",
        "features": ["Everything in Basic", "Basic Branding", "Custom Logo & Colors"],
        "roles_shared": [TEAMSPACE_ADMIN, TEAMSPACE_USER, BASIC_BRANDING_EDITOR],
        "upgrade_roles": [BASIC_BRANDING_EDITOR],
    },
    {
        "id": "enterprise",
        "name": "Enterprise",
        "price": "$9/user/month",
        "features": [
            "Everything in Business", "Advanced Branding",
            "Federated IdP", "Security Settings", "AI Agents",
        ],
        "roles_shared": [
            TEAMSPACE_ADMIN, TEAMSPACE_USER,
            BASIC_BRANDING_EDITOR, ADVANCED_BRANDING_EDITOR, IDP_MANAGER,
        ],
        "upgrade_roles": [BASIC_BRANDING_EDITOR, ADVANCED_BRANDING_EDITOR, IDP_MANAGER],
    },
]
