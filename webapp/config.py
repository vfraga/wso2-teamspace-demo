import os

from common.config import CommonDefaults, load_env

load_env()


class Config:
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY")
    if not SECRET_KEY:
        import secrets
        import warnings
        SECRET_KEY = secrets.token_hex(32)
        warnings.warn(
            "FLASK_SECRET_KEY not set — using ephemeral key. Sessions will be lost on restart.",
            RuntimeWarning,
            stacklevel=2,
        )
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.getenv("FLASK_ENV") == "production"
    SESSION_TYPE = "cachelib"
    SESSION_CACHELIB = None

    IS_BASE_URL = os.getenv("IS_BASE_URL", CommonDefaults.IS_BASE_URL)
    IS_ORG_HANDLE = os.getenv("IS_ORG_HANDLE", "")
    TENANT_PATH = ""
    IS_VERIFY_TLS = os.getenv("IS_VERIFY_TLS", "true").lower() != "false"

    CLIENT_ID = os.getenv("CLIENT_ID", "")
    CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
    APP_ID = os.getenv("APP_ID", "")
    APP_NAME = os.getenv("APP_NAME", "Teamspace")

    FLASK_HOST = os.getenv("FLASK_HOST", CommonDefaults.FLASK_HOST)
    FLASK_PORT = int(os.getenv("FLASK_PORT", str(CommonDefaults.FLASK_PORT)))

    BUSINESS_API_URL = os.getenv("BUSINESS_API_URL", CommonDefaults.BUSINESS_API_URL)
    AGENT_SERVICE_URL = os.getenv("AGENT_SERVICE_URL", CommonDefaults.AGENT_SERVICE_URL)
    AGENT_INTERNAL_SECRET = os.getenv("AGENT_INTERNAL_SECRET", "")
    BUSINESS_API_INTERNAL_SECRET = os.getenv("BUSINESS_API_INTERNAL_SECRET", "")

    DEFAULT_LOGO_URL = os.getenv("DEFAULT_LOGO_URL", CommonDefaults.DEFAULT_LOGO_URL)
    DEFAULT_FAVICON_URL = os.getenv("DEFAULT_FAVICON_URL", CommonDefaults.DEFAULT_FAVICON_URL)

    FEDERATED_IDP_URL = os.getenv("FEDERATED_IDP_URL", "https://localhost:9444/t/worklink.com/scim2/Users")
    FEDERATED_IDP_ADMIN_USER = os.getenv("FEDERATED_IDP_ADMIN_USER", "teamspaceadmin@worklink.com")
    FEDERATED_IDP_ADMIN_PASSWORD = os.environ.get("FEDERATED_IDP_ADMIN_PASSWORD", "")

    # OIDC_REDIRECT_URI and OIDC_POST_LOGOUT_URI are computed in create_app()
    # because they depend on FLASK_HOST / FLASK_PORT and may be overridden by tests.
    OIDC_REDIRECT_URI = ""
    OIDC_POST_LOGOUT_URI = ""

    OIDC_SCOPES = " ".join([
        "openid", "email", "profile", "groups", "roles", "internal_login",
        "internal_organization_create", "internal_organization_view",
        "internal_org_user_mgt_create", "internal_org_user_mgt_list",
        "internal_org_user_mgt_view", "internal_org_user_mgt_update",
        "internal_org_user_mgt_delete",
        "internal_org_role_mgt_create", "internal_org_role_mgt_view",
        "internal_org_role_mgt_update", "internal_org_role_mgt_delete",
        "internal_org_idp_create", "internal_org_idp_view",
        "internal_org_idp_update", "internal_org_idp_delete",
        "internal_org_application_mgt_view", "internal_org_application_mgt_update",
        "internal_org_branding_preference_update",
        "internal_agent_mgt_create", "internal_agent_mgt_list",
        "internal_agent_mgt_view", "internal_agent_mgt_delete",
        "list_meetings", "create_meeting", "view_meeting",
        "update_meeting", "delete_meeting",
        "create_basic_branding", "create_advanced_branding",
        "update_branding", "delete_branding",
        "view_agent_config", "manage_agent_config",
    ])
