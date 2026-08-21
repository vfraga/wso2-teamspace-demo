#!/usr/bin/env python3
"""
Teamspace IS Setup Script

Creates the tenant (IS_ORG_HANDLE, default 'teamspace'), application, API
resources, roles and sharing configuration in WSO2 IS 7.2.0.

Idempotent: safe to run multiple times. Skips resources that already exist.

Requirements: Python 3.10+, requests (from project deps)

Usage:
    python setup_is.py
"""

import os
import re
import sys
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from webapp.is_client import ISClient
from common.config import CommonDefaults
from common.constants import DEFAULT_PRIMARY_COLOR, DEFAULT_SECONDARY_COLOR
from common.m2m_auth import SERVICE_SCOPE
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()

# ─── Configuration ───────────────────────────────────────────────────────────

SUPER_ADMIN_USERNAME = os.environ.get("IS_SUPER_ADMIN_USERNAME", "admin")
SUPER_ADMIN_PASSWORD = os.environ.get("IS_SUPER_ADMIN_PASSWORD", "")
if not SUPER_ADMIN_PASSWORD:
    import warnings
    warnings.warn("IS_SUPER_ADMIN_PASSWORD not set — IS API calls will fail with 401", RuntimeWarning, stacklevel=2)
# WSO2 IS stamps its configured hostname into every URL it issues (discovery,
# `iss`, jwks_uri), so provisioning must use the same host the services use.
BASE_URL = os.environ.get("IS_BASE_URL", "https://localhost:9443").rstrip("/")
HOST_DOMAIN = urlparse(BASE_URL).netloc

# Browser-facing base URLs of the two OIDC clients on the Teamspace application.
# AGENT_SERVICE_URL is the agent's own public origin, the same variable the
# agent reads to build AGENT_REDIRECT_URI — registering the callback from a
# second variable let the two drift apart and IS rejected the flow with
# "callback.not.match".
PORTAL_URL = os.environ.get("PORTAL_URL", "http://localhost:5001").rstrip("/")
AGENT_SERVICE_URL = os.environ.get("AGENT_SERVICE_URL", "http://localhost:8000").rstrip("/")

APP_ALLOWED_ORIGINS = [PORTAL_URL, AGENT_SERVICE_URL]
# WSO2 treats a `regexp=` callback as a regular expression, so the base URLs are
# run through re.escape(): an unescaped `.` in a hostname would match any char.
APP_CALLBACK_URLS = [
    f"regexp=({re.escape(PORTAL_URL)}.*|{re.escape(AGENT_SERVICE_URL)}.*)"
]

# The SAME variable the three services read, so the tenant this script creates
# and the tenant they look under cannot disagree. An empty handle means
# carbon.super for the services, but setup still needs a domain to create the
# tenant with — hence the `or` rather than a default argument.
TENANT_DOMAIN = os.environ.get("IS_ORG_HANDLE") or "teamspace"
TENANT_ADMIN_USERNAME = os.environ.get("IS_TENANT_ADMIN_USERNAME", "teamspaceadmin")
TENANT_ADMIN_PASSWORD = os.environ.get("IS_TENANT_ADMIN_PASSWORD", "")
# Deliberately NOT derived from TENANT_DOMAIN. This is the owner's email
# *attribute*; the account is signed in to as TENANT_ADMIN_AUTH below, which is
# where the domain does appear. Same split as setup_idp_server.py.
TENANT_ADMIN_EMAIL = os.environ.get("IS_TENANT_ADMIN_EMAIL", "teamspaceadmin@mail.com")

# Read by the portal too (webapp/config.py), which looks the application up by
# this name — a literal here would make renaming it break that lookup.
APP_NAME = os.getenv("APP_NAME", "Teamspace")

ROLE_NAMES = {
    "admin": "teamspace-admin",
    "user": "teamspace-user",
    "idp_manager": "idp-manager",
    "basic_branding": "basic-branding-editor",
    "advanced_branding": "advanced-branding-editor",
}

# ─── Derived URLs ────────────────────────────────────────────────────────────

SERVER_API = f"{BASE_URL}/api/server/v1"
TENANT_API = f"{BASE_URL}/t/{TENANT_DOMAIN}/api/server/v1"
TENANT_SCIM = f"{BASE_URL}/t/{TENANT_DOMAIN}/scim2/v2"

SUPER_ADMIN_AUTH = (SUPER_ADMIN_USERNAME, SUPER_ADMIN_PASSWORD)
TENANT_ADMIN_AUTH = (f"{TENANT_ADMIN_USERNAME}@{TENANT_DOMAIN}", TENANT_ADMIN_PASSWORD)

CDN_IMG_BASE_URL = os.getenv("CDN_IMG_BASE_URL", CommonDefaults.CDN_IMG_BASE_URL)
DEFAULT_LOGO_URL = os.getenv("DEFAULT_LOGO_URL", f"{CDN_IMG_BASE_URL}/teamspace-logo.svg")
DEFAULT_FAVICON_URL = os.getenv("DEFAULT_FAVICON_URL", f"{CDN_IMG_BASE_URL}/teamspace-favicon.svg")

from webapp.is_operations import build_is_branding_payload

BRANDING_PAYLOAD = build_is_branding_payload({
    "primary_color": DEFAULT_PRIMARY_COLOR,
    "secondary_color": DEFAULT_SECONDARY_COLOR,
    "logo_url": DEFAULT_LOGO_URL,
    "logo_alt_text": "Teamspace App Logo",
    "favicon_url": DEFAULT_FAVICON_URL
}, org_name="")
BRANDING_PAYLOAD["name"] = TENANT_DOMAIN


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _session():
    s = requests.Session()
    s.verify = False
    s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    return s


def _ok(resp, accept_codes=(200, 201)):
    return resp.status_code in accept_codes


def step(num, msg):
    print(f"\n{'─' * 60}")
    print(f"  Step {num}: {msg}")
    print(f"{'─' * 60}")


def info(msg):
    print(f"  ✓ {msg}")


def warn(msg):
    print(f"  ⚠ {msg}")


def fail(msg):
    print(f"  ✗ {msg}", file=sys.stderr)


# ─── Step functions ──────────────────────────────────────────────────────────


def create_tenant(s):
    step(1, f"Create tenant '{TENANT_DOMAIN}'")
    resp = s.get(f"{SERVER_API}/tenants", params={"filter": f"domainName eq {TENANT_DOMAIN}"}, auth=SUPER_ADMIN_AUTH)
    tenants = resp.json().get("tenants", [])
    if tenants:
        tenant_id = tenants[0]["id"]
        info(f"Tenant already exists (id={tenant_id})")
        return tenant_id

    resp = s.post(f"{SERVER_API}/tenants", auth=SUPER_ADMIN_AUTH, json={
        "domain": TENANT_DOMAIN,
        "name": APP_NAME,
        "owners": [{
            "firstname": "Teamspace", "lastname": "Admin",
            "username": TENANT_ADMIN_USERNAME, "email": TENANT_ADMIN_EMAIL,
            "password": TENANT_ADMIN_PASSWORD, "provisioningMethod": "inline-password",
        }],
    })
    if not _ok(resp, (200, 201, 202)):
        fail(f"Failed to create tenant: {resp.text}")
        sys.exit(1)

    resp = s.get(f"{SERVER_API}/tenants", params={"filter": f"domainName eq {TENANT_DOMAIN}"}, auth=SUPER_ADMIN_AUTH)
    tenant_id = resp.json()["tenants"][0]["id"]
    info(f"Tenant created (id={tenant_id})")
    return tenant_id


def set_branding(s):
    step(2, "Set tenant branding")
    resp = s.post(f"{TENANT_API}/branding-preference", auth=TENANT_ADMIN_AUTH, json=BRANDING_PAYLOAD)
    if _ok(resp):
        info("Branding set")
    elif resp.status_code == 409:
        resp = s.put(f"{TENANT_API}/branding-preference", auth=TENANT_ADMIN_AUTH, json=BRANDING_PAYLOAD)
        info("Branding updated (already existed)")
    else:
        warn(f"Branding response: {resp.status_code} — {resp.text[:200]}")


def create_application(s):
    step(3, f"Create application '{APP_NAME}'")
    resp = s.get(f"{TENANT_API}/applications", params={
        "excludeSystemPortals": "false", "filter": f"name eq {APP_NAME}",
    }, auth=TENANT_ADMIN_AUTH)
    apps = resp.json().get("applications", [])
    if apps:
        app_id = apps[0]["id"]
        info(f"Application already exists (id={app_id})")
        return app_id

    resp = s.post(f"{TENANT_API}/applications", auth=TENANT_ADMIN_AUTH, json={
        "name": APP_NAME,
        "advancedConfigurations": {
            "discoverableByEndUsers": False, "skipLogoutConsent": True, "skipLoginConsent": False,
        },
        "authenticationSequence": {
            "type": "DEFAULT",
            "steps": [{"id": 1, "options": [{"idp": "LOCAL", "authenticator": "basic"}]}],
        },
        "claimConfiguration": {
            "dialect": "LOCAL",
            "requestedClaims": [{"claim": {"uri": "http://wso2.org/claims/username"}}],
        },
        "inboundProtocolConfiguration": {
            "oidc": {
                "grantTypes": ["authorization_code", "client_credentials", "organization_switch", "refresh_token"],
                "allowedOrigins": APP_ALLOWED_ORIGINS,
                "callbackURLs": APP_CALLBACK_URLS,
                "publicClient": False,
                "accessToken": {
                    "accessTokenAttributes": ["email"],
                    "applicationAccessTokenExpiryInSeconds": 3600,
                    "bindingType": "None",
                    "revokeTokensWhenIDPSessionTerminated": False,
                    "type": "JWT",
                    "userAccessTokenExpiryInSeconds": 3600,
                    "validateTokenBinding": False,
                },
                "refreshToken": {"expiryInSeconds": 86400},
            },
        },
        "templateId": "b9c5e11e-fc78-484b-9bec-015d247561b8",
        "associatedRoles": {"allowedAudience": "APPLICATION", "roles": []},
    })
    if not _ok(resp):
        fail(f"Failed to create application: {resp.text}")
        sys.exit(1)

    resp = s.get(f"{TENANT_API}/applications", params={
        "excludeSystemPortals": "false", "filter": f"name eq {APP_NAME}",
    }, auth=TENANT_ADMIN_AUTH)
    app_id = resp.json()["applications"][0]["id"]
    info(f"Application created (id={app_id})")
    return app_id


def get_credentials(s, app_id):
    step(4, "Get OIDC credentials")
    resp = s.get(f"{TENANT_API}/applications/{app_id}/inbound-protocols/oidc", auth=TENANT_ADMIN_AUTH)
    data = resp.json()
    client_id = data.get("clientId", "")
    client_secret = data.get("clientSecret", "")
    info(f"Client ID:     {client_id}")
    info(f"Client Secret: {client_secret}")
    return client_id, client_secret


def set_initial_sharing(s, app_id):
    step(5, "Set initial sharing policy")
    resp = s.post(f"{TENANT_API}/applications/share-with-all", auth=TENANT_ADMIN_AUTH, json={
        "applicationId": app_id,
        "policy": "ALL_EXISTING_AND_FUTURE_ORGS",
        "roleSharing": {"mode": "NONE", "roles": []},
    })
    if _ok(resp, (200, 201, 202, 204)):
        info("Initial sharing policy set")
    else:
        code = resp.json().get("code", "") if resp.content else ""
        if "already" in resp.text.lower() or code:
            info("Sharing policy already set (skipped)")
        else:
            warn(f"Sharing response: {resp.status_code} — {resp.text[:200]}")


def update_claims(s, app_id):
    step(6, "Update application claims")
    resp = s.patch(f"{TENANT_API}/applications/{app_id}", auth=TENANT_ADMIN_AUTH, json={
        "claimConfiguration": {
            "dialect": "LOCAL",
            "requestedClaims": [
                {"claim": {"uri": "http://wso2.org/claims/identity/emailVerified"}, "mandatory": False},
                {"claim": {"uri": "http://wso2.org/claims/emailaddress"}, "mandatory": False},
                {"claim": {"uri": "http://wso2.org/claims/username"}, "mandatory": False},
                {"claim": {"uri": "http://wso2.org/claims/givenname"}, "mandatory": False},
                {"claim": {"uri": "http://wso2.org/claims/lastname"}, "mandatory": False},
                {"claim": {"uri": "http://wso2.org/claims/roles"}, "mandatory": False},
                {"claim": {"uri": "http://wso2.org/claims/groups"}, "mandatory": False},
            ],
            "role": {"claim": {"uri": "http://wso2.org/claims/roles"}, "includeUserDomain": False},
            "subject": {
                "claim": {"uri": "http://wso2.org/claims/userid"},
                "includeTenantDomain": False, "includeUserDomain": False,
                "mappedLocalSubjectMandatory": False, "useMappedLocalSubject": True,
            },
        },
    })
    if _ok(resp):
        info("Claims updated")
    else:
        warn(f"Claims response: {resp.status_code} — {resp.text[:200]}")


def update_oidc_config(s, app_id, client_id):
    step(7, "Update OIDC configuration")
    resp = s.put(f"{TENANT_API}/applications/{app_id}/inbound-protocols/oidc", auth=TENANT_ADMIN_AUTH, json={
        "clientId": client_id,
        "grantTypes": ["authorization_code", "client_credentials", "organization_switch", "refresh_token"],
        "allowedOrigins": APP_ALLOWED_ORIGINS,
        "callbackURLs": APP_CALLBACK_URLS,
        "publicClient": False,
        "accessToken": {
            "accessTokenAttributes": ["email"],
            "applicationAccessTokenExpiryInSeconds": 3600,
            "bindingType": "sso-session",
            "revokeTokensWhenIDPSessionTerminated": True,
            "type": "JWT",
            "userAccessTokenExpiryInSeconds": 3600,
            "validateTokenBinding": False,
        },
        "refreshToken": {"expiryInSeconds": 86400},
    })
    if _ok(resp):
        info("OIDC config updated")
    else:
        warn(f"OIDC config response: {resp.status_code} — {resp.text[:200]}")


def _create_api_resource(s, name, identifier, description, scopes):
    print(f"    Creating API: {name} ... ", end="")
    resp = s.post(f"{TENANT_API}/api-resources", auth=TENANT_ADMIN_AUTH, json={
        "name": name, "identifier": identifier,
        "description": description, "requiresAuthorization": True, "scopes": scopes,
    })
    if _ok(resp):
        api_id = resp.json().get("id", "")
        print(f"created ({api_id})")
        return api_id

    resp2 = s.get(f"{TENANT_API}/api-resources", params={"filter": f"identifier eq {identifier}"}, auth=TENANT_ADMIN_AUTH)
    resources = resp2.json().get("apiResources", [])
    if resources:
        api_id = resources[0]["id"]
        print(f"already exists ({api_id})")
        return api_id

    print(f"FAILED: {resp.text[:150]}")
    return None


def _create_mcp_api_resource(s, name, identifier, description, scopes):
    print(f"    Creating MCP API: {name} ... ", end="")
    resp = s.post(f"{TENANT_API}/api-resources", auth=TENANT_ADMIN_AUTH, json={
        "name": name, "identifier": identifier,
        "description": description, "requiresAuthorization": True, "scopes": scopes,
        "resourceType": "MCP",
    })
    if _ok(resp):
        api_id = resp.json().get("id", "")
        print(f"created ({api_id})")
        return api_id

    resp2 = s.get(f"{TENANT_API}/api-resources", params={"filter": f"identifier eq {identifier}"}, auth=TENANT_ADMIN_AUTH)
    resources = resp2.json().get("apiResources", [])
    if resources:
        api_id = resources[0]["id"]
        print(f"already exists ({api_id})")
        return api_id

    print(f"FAILED: {resp.text[:150]}")
    return None


def _get_api_resource_id(s, filter_query, name):
    print(f"    Looking up: {name} ... ", end="")
    resp = s.get(f"{TENANT_API}/api-resources", params={"filter": filter_query}, auth=TENANT_ADMIN_AUTH)
    resources = resp.json().get("apiResources", [])
    if resources:
        api_id = resources[0]["id"]
        print(f"found ({api_id})")
        return api_id
    print("NOT FOUND")
    return None


def _authorize_api(s, app_id, api_id, scopes, policy="RBAC"):
    """Authorize an API resource for the application."""
    if not api_id:
        return
    resp = s.post(f"{TENANT_API}/applications/{app_id}/authorized-apis", auth=TENANT_ADMIN_AUTH, json={
        "id": api_id, "policyIdentifier": policy, "scopes": scopes,
    })
    if _ok(resp, (200, 201)):
        return
    code = resp.json().get("code", "") if resp.content else ""
    if code in ("APP-65002", "APP-60509"):
        return
    warn(f"    Authorization error for {api_id}: {resp.text[:150]}")


def enable_api_based_auth(s, app_id):
    step(8, "Enable API-based authentication (for agent response_mode=direct)")
    resp = s.patch(f"{TENANT_API}/applications/{app_id}", auth=TENANT_ADMIN_AUTH, json={
        "advancedConfigurations": {
            "enableAPIBasedAuthentication": True,
        },
    })
    if _ok(resp):
        info("API-based authentication enabled")
    else:
        warn(f"Failed to enable API-based auth: {resp.status_code} — {resp.text[:200]}")


def setup_api_resources(s, app_id):
    step(9, "API Resources & Authorizations")

    # `identifier` is an opaque key, not a reachable URL. WSO2 matches an
    # existing resource by it, so changing one registers a NEW resource rather
    # than reusing the old — treat these as fixed once an instance is live.
    print("  Custom APIs:")
    meeting_scopes = [
        {"name": "list_meetings", "displayName": "List Meetings"},
        {"name": "create_meeting", "displayName": "Create Meetings"},
        {"name": "view_meeting", "displayName": "View Meetings"},
        {"name": "update_meeting", "displayName": "Update Meetings"},
        {"name": "delete_meeting", "displayName": "Delete Meetings"},
        {"name": "view_agent_config", "displayName": "View Agent Config"},
        {"name": "manage_agent_config", "displayName": "Manage Agent Config"},
    ]
    meeting_id = _create_api_resource(s, "Meeting Service", "urn:teamspace:meetings", "Meeting Service API", meeting_scopes)
    _authorize_api(s, app_id, meeting_id, ["create_meeting", "delete_meeting", "list_meetings", "update_meeting", "view_meeting", "view_agent_config", "manage_agent_config"])

    print("  MCP Meeting Agent API:")
    mcp_scopes = [
        {"name": "create_meeting_agent", "displayName": "Create Meeting Agent"},
        {"name": "list_meetings_agent", "displayName": "List Meetings Agent"},
        {"name": "delete_meeting_agent", "displayName": "Delete Meeting Agent"},
        {"name": "update_meeting_agent", "displayName": "Update Meeting Agent"},
    ]
    mcp_id = _create_mcp_api_resource(s, "Meeting Agent", "mcp://meeting-agent", "Meeting Agent MCP API", mcp_scopes)
    _authorize_api(s, app_id, mcp_id, ["create_meeting_agent", "list_meetings_agent", "delete_meeting_agent", "update_meeting_agent"])

    personalization_scopes = [
        {"name": "create_basic_branding", "displayName": "Create Basic Branding"},
        {"name": "create_advanced_branding", "displayName": "Create Advanced Branding"},
        {"name": "update_branding", "displayName": "Update Branding"},
        {"name": "delete_branding", "displayName": "Delete Branding"},
    ]
    pers_id = _create_api_resource(s, "Personalization Service", "urn:teamspace:personalization", "Personalization Service API", personalization_scopes)
    _authorize_api(s, app_id, pers_id, ["create_advanced_branding", "create_basic_branding", "delete_branding", "update_branding"])

    print("  Internal Services API (M2M client credentials):")
    # Backs the X-Service-Authorization header used for service-to-service
    # calls (webapp -> agent, webapp/agent -> Business API).
    internal_scopes = [
        {"name": SERVICE_SCOPE, "displayName": "Internal Service Call"},
    ]
    internal_id = _create_api_resource(
        s, "Teamspace Internal Services", "urn:teamspace:internal",
        "Service-to-service authentication for the Teamspace microservices", internal_scopes,
    )
    _authorize_api(s, app_id, internal_id, [SERVICE_SCOPE])

    print("  Agent APIs:")
    agent_api_id = _get_api_resource_id(s, "identifier eq /scim2/Agents", "SCIM2 Agents API")
    _authorize_api(s, app_id, agent_api_id,
                   ["internal_agent_mgt_create", "internal_agent_mgt_delete", "internal_agent_mgt_list", "internal_agent_mgt_update", "internal_agent_mgt_view"])

    print("  Org-Level APIs:")
    org_apis = [
        ("identifier sw /o/scim2/Users", "Org SCIM Users",
         ["internal_org_user_mgt_create", "internal_org_user_mgt_delete", "internal_org_user_mgt_list", "internal_org_user_mgt_update", "internal_org_user_mgt_view"]),
        ("identifier sw /o/scim2/Roles", "Org SCIM Roles",
         ["internal_org_role_mgt_create", "internal_org_role_mgt_delete", "internal_org_role_mgt_update", "internal_org_role_mgt_view"]),
        ("identifier sw /o/scim2/Groups", "Org SCIM Groups",
         ["internal_org_group_mgt_create", "internal_org_group_mgt_delete", "internal_org_group_mgt_update", "internal_org_group_mgt_view"]),
        ("identifier sw /o/api/server/v1/identity-provider", "Org IDP Mgt",
         ["internal_org_idp_create", "internal_org_idp_delete", "internal_org_idp_update", "internal_org_idp_view"]),
        ("identifier sw /o/api/server/v1/applications", "Org App Mgt",
         ["internal_org_application_mgt_create", "internal_org_application_mgt_delete", "internal_org_application_mgt_update", "internal_org_application_mgt_view"]),
        ("identifier sw /o/api/server/v1/claim-dialects", "Org Claim Mgt",
         ["internal_org_claim_meta_update", "internal_org_claim_meta_view"]),
        ("identifier sw /o/api/server/v1/branding-preference", "Org Branding",
         ["internal_org_branding_preference_update"]),
        ("identifier eq /o/api/server/v1/organizations", "Org Org Mgt",
         ["internal_org_organization_create", "internal_org_organization_delete", "internal_org_organization_update", "internal_org_organization_view"]),
        ("identifier eq /o/api/server/v1/userstore", "Org Userstore Mgt",
         ["internal_org_userstore_create", "internal_org_userstore_delete", "internal_org_userstore_update", "internal_org_userstore_view"]),
    ]
    for filter_q, name, scopes in org_apis:
        api_id = _get_api_resource_id(s, filter_q, name)
        _authorize_api(s, app_id, api_id, scopes)

    print("  Root-Level APIs:")
    root_apis = [
        ("identifier eq /api/server/v1/organizations", "Root Org Mgt",
         ["internal_organization_create", "internal_organization_delete", "internal_organization_update", "internal_organization_view"]),
        ("identifier eq /api/server/v1/applications", "Root App Mgt",
         ["internal_application_mgt_create", "internal_application_mgt_delete", "internal_application_mgt_update", "internal_application_mgt_view"]),
        ("identifier eq /api/server/v1/applications/share", "Shared App Mgt",
         ["internal_shared_application_create", "internal_shared_application_delete", "internal_shared_application_view"]),
        ("identifier eq /scim2/Roles", "Root SCIM Roles",
         ["internal_role_mgt_create", "internal_role_mgt_delete", "internal_role_mgt_update", "internal_role_mgt_view"]),
        ("identifier eq /scim2/Users", "Root SCIM Users",
         ["internal_user_mgt_create", "internal_user_mgt_delete", "internal_user_mgt_list", "internal_user_mgt_update", "internal_user_mgt_view"]),
    ]

    for filter_q, name, scopes in root_apis:
        api_id = _get_api_resource_id(s, filter_q, name)
        _authorize_api(s, app_id, api_id, scopes)

    info("All API resources configured")


def _patch_role_permissions(s, app_id, role_name, permissions):
    """Find an existing role by name and update its permissions."""
    resp = s.get(
        f"{TENANT_SCIM}/Roles",
        params={"filter": f"displayName eq {role_name} and audience.value eq {app_id}"},
        auth=TENANT_ADMIN_AUTH,
    )
    roles = resp.json().get("Resources", [])
    if not roles:
        print(f"      could not find role '{role_name}' to patch")
        return
    role_id = roles[0]["id"]
    resp = s.patch(f"{TENANT_SCIM}/Roles/{role_id}", auth=TENANT_ADMIN_AUTH, json={
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [{
            "op": "replace",
            "path": "permissions",
            "value": [{"value": p} for p in permissions],
        }],
    })
    if resp.status_code in (200, 204):
        print(f"      patched ({len(permissions)} permissions)")
    else:
        print(f"      patch failed: {resp.status_code} — {resp.text[:150]}")


def _create_role(s, app_id, role_name, permissions):
    print(f"    Creating role: {role_name} ... ", end="")
    resp = s.post(f"{TENANT_SCIM}/Roles", auth=TENANT_ADMIN_AUTH, json={
        "audience": {"type": "APPLICATION", "value": app_id},
        "displayName": role_name,
        "permissions": [{"value": p} for p in permissions],
        "schemas": [],
    })
    if resp.status_code == 401:
        print("ERROR: 401 Unauthorized (check SCIM endpoint version)")
        return
    data = resp.json() if resp.content else {}
    if resp.status_code in (200, 201):
        print("created")
    elif resp.status_code == 409:
        print("already exists — patching permissions")
        _patch_role_permissions(s, app_id, role_name, permissions)
    else:
        detail = data.get("detail", resp.text[:100])
        print(f"ERROR: {resp.status_code} — {detail}")


def create_roles(s, app_id):
    step(10, "Create application roles")

    admin_perms = [
        "internal_org_user_mgt_delete", "internal_org_user_mgt_update", "internal_org_user_mgt_view",
        "internal_org_user_mgt_create", "internal_org_user_mgt_list",
        "internal_org_role_mgt_view", "internal_org_role_mgt_create",
        "internal_org_role_mgt_update", "internal_org_role_mgt_delete",
        "internal_org_group_mgt_create", "internal_org_group_mgt_delete",
        "internal_org_group_mgt_view", "internal_org_group_mgt_update",
        "internal_org_idp_view",
        "internal_org_application_mgt_create", "internal_org_application_mgt_update",
        "internal_org_application_mgt_delete", "internal_org_application_mgt_view",
        "internal_org_claim_meta_update", "internal_org_claim_meta_view",
        "internal_agent_mgt_create", "internal_agent_mgt_delete",
        "internal_agent_mgt_list", "internal_agent_mgt_view",
        "view_meeting", "list_meetings", "update_meeting", "delete_meeting", "create_meeting",
        "view_agent_config", "manage_agent_config",
        "create_meeting_agent", "list_meetings_agent", "delete_meeting_agent", "update_meeting_agent",
    ]
    _create_role(s, app_id, ROLE_NAMES["admin"], admin_perms)

    user_perms = [
        "view_meeting", "list_meetings", "update_meeting", "delete_meeting", "create_meeting",
        "create_meeting_agent", "list_meetings_agent", "delete_meeting_agent", "update_meeting_agent",
    ]
    _create_role(s, app_id, ROLE_NAMES["user"], user_perms)

    idp_perms = ["internal_org_idp_view", "internal_org_idp_create", "internal_org_idp_delete", "internal_org_idp_update"]
    _create_role(s, app_id, ROLE_NAMES["idp_manager"], idp_perms)

    basic_branding_perms = ["create_basic_branding", "delete_branding", "update_branding", "internal_org_branding_preference_update"]
    _create_role(s, app_id, ROLE_NAMES["basic_branding"], basic_branding_perms)

    adv_branding_perms = ["update_branding", "create_advanced_branding", "create_basic_branding", "delete_branding", "internal_org_branding_preference_update"]
    _create_role(s, app_id, ROLE_NAMES["advanced_branding"], adv_branding_perms)

    info("All roles configured")


def share_with_roles(s, app_id):
    step(11, "Share base roles with all organizations")
    # share-with-all is used ONLY here (setup), and ONLY for the universal base
    # roles every org needs. Plan-specific roles (idp-manager, branding editors)
    # are shared per-org at signup/upgrade via the applications/share API, so
    # that re-sharing one org never clobbers another org's roles.
    resp = s.post(f"{TENANT_API}/applications/share-with-all", auth=TENANT_ADMIN_AUTH, json={
        "applicationId": app_id,
        "policy": "ALL_EXISTING_AND_FUTURE_ORGS",
        "roleSharing": {
            "mode": "SELECTED",
            "roles": [
                {"audience": {"display": APP_NAME, "type": "application"}, "displayName": ROLE_NAMES["admin"]},
                {"audience": {"display": APP_NAME, "type": "application"}, "displayName": ROLE_NAMES["user"]},
            ],
        },
    })
    if _ok(resp, (200, 201, 202, 204)):
        info("Application shared with role sharing")
    else:
        code = resp.json().get("code", "") if resp.content else ""
        if code or "already" in resp.text.lower():
            info("Sharing already configured (updated)")
        else:
            warn(f"Sharing response: {resp.status_code} — {resp.text[:200]}")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  Teamspace IS Setup")
    print(f"  Host:   {BASE_URL}")
    print(f"  Tenant: {TENANT_DOMAIN}")
    print(f"  Admin:  {TENANT_ADMIN_USERNAME}@{TENANT_DOMAIN}")
    print("=" * 60)

    s = ISClient(BASE_URL)

    try:
        s.get(f"{BASE_URL}/api/server/v1/tenants", auth=SUPER_ADMIN_AUTH, timeout=5)
    except requests.ConnectionError:
        fail(f"Cannot connect to WSO2 IS at {BASE_URL}. Is it running?")
        sys.exit(1)

    create_tenant(s)
    set_branding(s)
    app_id = create_application(s)
    client_id, client_secret = get_credentials(s, app_id)
    set_initial_sharing(s, app_id)
    update_claims(s, app_id)
    update_oidc_config(s, app_id, client_id)
    enable_api_based_auth(s, app_id)
    setup_api_resources(s, app_id)
    create_roles(s, app_id)
    share_with_roles(s, app_id)

    print(f"\n{'=' * 60}")
    print("  Setup complete!")
    print("")
    print("  Add these to your .env file:")
    print(f"  CLIENT_ID={client_id}")
    print(f"  CLIENT_SECRET={client_secret}")
    print(f"  APP_ID={app_id}")
    print(f"  IS_ORG_HANDLE={TENANT_DOMAIN}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
