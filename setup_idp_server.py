#!/usr/bin/env python3
"""
Setup script for the Federated IdP WSO2 IS instance (port 9444).
Creates tenant, application, groups, and test users.

Importable: ``from setup_idp_server import bootstrap_federated_idp``
Standalone: ``python setup_idp_server.py``  (used by live E2E test harness)
"""

import json
import re
import sys
import os
import stat
from urllib.parse import urlparse

import requests
import urllib3
from dotenv import load_dotenv

from common.config import CommonDefaults
from webapp.is_operations import build_is_branding_payload

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()

# ─── Configuration ───────────────────────────────────────────────────────────

BASE_URL = os.environ.get("FEDERATED_IS_BASE_URL", "https://localhost:9444").rstrip("/")
SUPER_ADMIN_USERNAME = os.environ.get("IS_SUPER_ADMIN_USERNAME", "admin")
SUPER_ADMIN_PASSWORD = os.environ.get("IS_SUPER_ADMIN_PASSWORD", "")
if not SUPER_ADMIN_PASSWORD:
    import warnings
    warnings.warn("IS_SUPER_ADMIN_PASSWORD not set — IS API calls will fail with 401", RuntimeWarning, stacklevel=2)
SUPER_ADMIN_AUTH = (SUPER_ADMIN_USERNAME, SUPER_ADMIN_PASSWORD)

# The federated tenant's identity. One setting moves the whole IdP to another
# domain: the tenant admin's login name, the management/SCIM endpoints below
# and the seeded users' email addresses are all derived from TENANT_DOMAIN.
TENANT_DOMAIN = os.environ.get("FEDERATED_IDP_TENANT_DOMAIN", "worklink.com")
TENANT_ADMIN_USERNAME = os.environ.get("FEDERATED_IDP_TENANT_ADMIN_USERNAME", "teamspaceadmin")
TENANT_ADMIN_PASSWORD = os.environ.get("IS_TENANT_ADMIN_PASSWORD", "")
# Deliberately NOT derived from TENANT_DOMAIN. This is the owner's email
# *attribute*, which setup_is.py sets to the same placeholder on the primary
# tenant; the account is signed in to as TENANT_ADMIN_AUTH below, which is
# where the domain does appear.
TENANT_ADMIN_EMAIL = os.environ.get("FEDERATED_IDP_TENANT_ADMIN_EMAIL", "teamspaceadmin@mail.com")
TENANT_ADMIN_AUTH = (f"{TENANT_ADMIN_USERNAME}@{TENANT_DOMAIN}", TENANT_ADMIN_PASSWORD)

FEDERATED_USER_PASSWORD = os.environ.get("FEDERATED_USER_PASSWORD", "")

SERVER_API = f"{BASE_URL}/api/server/v1"
TENANT_API = f"{BASE_URL}/t/{TENANT_DOMAIN}/api/server/v1"
TENANT_SCIM = f"{BASE_URL}/t/{TENANT_DOMAIN}/scim2"

# ─── Primary IS callbacks ────────────────────────────────────────────────────
# The federated IdP redirects back to the PRIMARY IS commonauth endpoints. Same
# env var and default as setup_is.py and the services use, so one setting drives
# every host.
IS_BASE_URL = os.environ.get("IS_BASE_URL", "https://localhost:9443").rstrip("/")

# Containerised app services reach a host-run primary IS as host.docker.internal,
# so the same paths on that hostname stay accepted.
_IS_URL_PARTS = urlparse(IS_BASE_URL)
_DOCKER_INTERNAL_NETLOC = "host.docker.internal" + (f":{_IS_URL_PARTS.port}" if _IS_URL_PARTS.port else "")
DOCKER_INTERNAL_IS_BASE_URL = _IS_URL_PARTS._replace(netloc=_DOCKER_INTERNAL_NETLOC).geturl()


def _commonauth_callback_regexp(base_urls):
    """Build the WSO2 ``regexp=`` callback covering commonauth on each base URL.

    The base URLs are run through re.escape() because the value is a regular
    expression — an unescaped `.` in a hostname would match any character. The
    `/o/.*/commonauth` wildcard (the organization id) is an intentional pattern
    and is therefore left unescaped.
    """
    alternatives = []
    for base_url in base_urls:
        escaped = re.escape(base_url)
        alternatives.append(f"{escaped}/commonauth.*")
        alternatives.append(f"{escaped}/o/.*/commonauth.*")
    return "regexp=(" + "|".join(alternatives) + ")"


PRIMARY_CALLBACK_URLS = [_commonauth_callback_regexp([IS_BASE_URL])]
# The final OIDC update widens the set to the container-facing hostname too.
PRIMARY_CALLBACK_URLS_WITH_DOCKER = [
    _commonauth_callback_regexp(list(dict.fromkeys([IS_BASE_URL, DOCKER_INTERNAL_IS_BASE_URL])))
]

# ─── Branding (Worklink IdP login page) ──────────────────────────────────────
# The federated IdP's hosted login page uses its own Worklink IdP assets so the
# SSO redirect is visibly a distinct, branded identity provider.
CDN_IMG_BASE_URL = os.getenv("CDN_IMG_BASE_URL", CommonDefaults.CDN_IMG_BASE_URL)
WORKLINK_IDP_LOGO_URL = os.getenv("WORKLINK_IDP_LOGO_URL", f"{CDN_IMG_BASE_URL}/worklink-idp-logo.svg")
WORKLINK_IDP_FAVICON_URL = os.getenv("WORKLINK_IDP_FAVICON_URL", f"{CDN_IMG_BASE_URL}/worklink-idp-favicon.svg")
WORKLINK_IDP_PRIMARY_COLOR = os.getenv("WORKLINK_IDP_PRIMARY_COLOR", "#155E75")
WORKLINK_IDP_SECONDARY_COLOR = os.getenv("WORKLINK_IDP_SECONDARY_COLOR", "#D7EEF5")


def _session():
    s = requests.Session()
    s.verify = False
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


def _bootstrap_tenant(s):
    step(1, f"Create tenant '{TENANT_DOMAIN}'")
    resp = s.get(f"{SERVER_API}/tenants", params={"filter": f"domainName eq {TENANT_DOMAIN}"}, auth=SUPER_ADMIN_AUTH)
    tenants = resp.json().get("tenants", [])
    if tenants:
        info("Tenant already exists")
        return
    resp = s.post(f"{SERVER_API}/tenants", auth=SUPER_ADMIN_AUTH, json={
        "domain": TENANT_DOMAIN,
        "name": "Teamspace Federated IdP",
        "owners": [{
            "firstname": "Federated", "lastname": "Admin",
            "username": TENANT_ADMIN_USERNAME, "email": TENANT_ADMIN_EMAIL,
            "password": TENANT_ADMIN_PASSWORD, "provisioningMethod": "inline-password",
        }],
    })
    if not _ok(resp, (200, 201, 202)):
        fail(f"Failed to create tenant: {resp.text}")
        raise RuntimeError("federated IdP bootstrap failed")
    info(f"Tenant created (admin: {TENANT_ADMIN_USERNAME}@{TENANT_DOMAIN})")


def _bootstrap_branding(s):
    step(2, "Set the Worklink IdP login-page branding (logo, favicon, colors)")
    payload = build_is_branding_payload({
        "primary_color": WORKLINK_IDP_PRIMARY_COLOR,
        "secondary_color": WORKLINK_IDP_SECONDARY_COLOR,
        "logo_url": WORKLINK_IDP_LOGO_URL,
        "logo_alt_text": "Worklink IdP",
        "favicon_url": WORKLINK_IDP_FAVICON_URL,
    }, org_name="Worklink")
    payload["name"] = TENANT_DOMAIN
    resp = s.post(f"{TENANT_API}/branding-preference", auth=TENANT_ADMIN_AUTH, json=payload)
    if _ok(resp):
        info("Branding set")
    elif resp.status_code == 409:
        resp = s.put(f"{TENANT_API}/branding-preference", auth=TENANT_ADMIN_AUTH, json=payload)
        if _ok(resp):
            info("Branding updated (already existed)")
        else:
            warn(f"Branding update failed: {resp.status_code} — {resp.text[:200]}")
    else:
        warn(f"Branding response: {resp.status_code} — {resp.text[:200]}")


def _bootstrap_groups_scope(s):
    # Claims are released by default when groups are mapped
    step(3, "Create the custom OIDC scope 'groups'")
    resp = s.post(f"{TENANT_API}/oidc/scopes", auth=TENANT_ADMIN_AUTH, json={
        "name": "groups",
        "displayName": "groups",
        "description": "User groups scope",
        "claims": ["groups", "roles"]
    })
    if resp.status_code == 409:
        info("Scope 'groups' already exists; updating it")
        resp = s.put(f"{TENANT_API}/oidc/scopes/groups", auth=TENANT_ADMIN_AUTH, json={
            "displayName": "groups",
            "description": "User groups scope",
            "claims": ["groups", "roles"]
        })
    if _ok(resp):
        info("Scope 'groups' configured")
    else:
        warn(f"Failed to configure scope: {resp.text}")


def _bootstrap_application(s):
    step(4, "Create application 'FederatedClient'")
    resp = s.get(f"{TENANT_API}/applications", params={"filter": "name eq FederatedClient"}, auth=TENANT_ADMIN_AUTH)
    apps = resp.json().get("applications", [])
    if apps:
        app_id = apps[0]["id"]
        info(f"Application already exists (id={app_id})")
        return app_id
    resp = s.post(f"{TENANT_API}/applications", auth=TENANT_ADMIN_AUTH, json={
        "name": "FederatedClient",
        "templateId": "b9c5e11e-fc78-484b-9bec-015d247561b8",
        "inboundProtocolConfiguration": {
            "oidc": {
                "grantTypes": ["authorization_code", "refresh_token"],
                "allowedOrigins": [],
                "callbackURLs": PRIMARY_CALLBACK_URLS,
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
            }
        }
    })
    if not _ok(resp):
        fail(f"Failed to create app: {resp.text}")
        raise RuntimeError("federated IdP bootstrap failed")

    # Query again to get app ID
    resp = s.get(f"{TENANT_API}/applications", params={"filter": "name eq FederatedClient"}, auth=TENANT_ADMIN_AUTH)
    app_id = resp.json()["applications"][0]["id"]
    info(f"Application created (id={app_id})")
    return app_id


def _bootstrap_application_claims_and_oidc(s, app_id):
    step(5, "Configure claims and the OIDC inbound protocol for FederatedClient")
    resp = s.patch(f"{TENANT_API}/applications/{app_id}", auth=TENANT_ADMIN_AUTH, json={
        "claimConfiguration": {
            "dialect": "LOCAL",
            "requestedClaims": [
                {"claim": {"uri": "http://wso2.org/claims/emailaddress"}, "mandatory": False},
                {"claim": {"uri": "http://wso2.org/claims/givenname"}, "mandatory": False},
                {"claim": {"uri": "http://wso2.org/claims/lastname"}, "mandatory": False},
                {"claim": {"uri": "http://wso2.org/claims/username"}, "mandatory": False},
                {"claim": {"uri": "http://wso2.org/claims/groups"}, "mandatory": False},
            ],
            "role": {"claim": {"uri": "http://wso2.org/claims/groups"}, "includeUserDomain": False},
            "subject": {
                "claim": {"uri": "http://wso2.org/claims/emailaddress"},
                "includeTenantDomain": False,
                "includeUserDomain": False,
                "mappedLocalSubjectMandatory": False,
                "useMappedLocalSubject": True
            }
        },
    })
    if not _ok(resp):
        fail(f"Failed to update app claims: {resp.text}")
        raise RuntimeError("federated IdP bootstrap failed")
    info("Requested claims and subject mapping updated")

    resp = s.get(f"{TENANT_API}/applications/{app_id}/inbound-protocols/oidc", auth=TENANT_ADMIN_AUTH)
    data = resp.json()
    client_id = data.get("clientId", "")
    client_secret = data.get("clientSecret", "")
    info(f"Federated Client ID: {client_id}\tClient Secret: {client_secret}")

    resp = s.put(f"{TENANT_API}/applications/{app_id}/inbound-protocols/oidc", auth=TENANT_ADMIN_AUTH, json={
        "clientId": client_id,
        "grantTypes": ["authorization_code", "refresh_token"],
        "allowedOrigins": [],
        "callbackURLs": PRIMARY_CALLBACK_URLS_WITH_DOCKER,
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
    if not _ok(resp):
        fail(f"Failed to update app OIDC config: {resp.text}")
        raise RuntimeError("federated IdP bootstrap failed")
    info("OIDC configuration updated")

    # Write to a JSON file in scratch/ so the tests and system can read it
    os.makedirs("scratch", exist_ok=True)
    credentials_path = "scratch/idp_credentials.json"
    with open(credentials_path, "w") as f:
        json.dump({"client_id": client_id, "client_secret": client_secret}, f)
    os.chmod(credentials_path, stat.S_IRUSR | stat.S_IWUSR)
    info(f"Credentials written to {credentials_path}")


def _bootstrap_users(s, users_to_create):
    step(6, f"Create the federated test users on '{TENANT_DOMAIN}'")
    if not FEDERATED_USER_PASSWORD:
        fail("FEDERATED_USER_PASSWORD environment variable is required to create federated test users")
        raise RuntimeError("federated IdP bootstrap failed")
    user_ids = {}
    for u in users_to_create:
        print(f"  Checking/creating user {u['username']}...")
        resp = s.get(f"{TENANT_SCIM}/Users?filter=userName+eq+{u['username']}", auth=TENANT_ADMIN_AUTH)
        resources = resp.json().get("Resources", [])
        if resources:
            uid = resources[0]["id"]
            info(f"User {u['username']} already exists (id={uid}); skipping creation")
        else:
            resp = s.post(f"{TENANT_SCIM}/Users", auth=TENANT_ADMIN_AUTH, json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "userName": u["username"],
                "password": FEDERATED_USER_PASSWORD,
                "name": {"givenName": u["firstname"], "familyName": u["lastname"]},
                "emails": [{"value": f"{u['username']}@{TENANT_DOMAIN}", "primary": True}],
            })
            if not _ok(resp, (200, 201)):
                fail(f"Failed to create user {u['username']}: {resp.text}")
                raise RuntimeError("federated IdP bootstrap failed")
            uid = resp.json()["id"]
            info(f"User {u['username']}@{TENANT_DOMAIN} created (id={uid})")
        user_ids[u["username"]] = uid
    return user_ids


def _put_member_fallback(sess, gid, gname, member_uid, user_info, ref, existing, scim_headers):
    new_members = [
        {"value": m["value"], "display": m.get("display", m["value"]), "$ref": m.get("$ref", "")}
        for m in existing
    ]
    new_members.append({"value": member_uid, "display": user_info["username"], "$ref": ref})
    put_payload = {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
        "displayName": gname,
        "members": new_members,
    }
    r = sess.put(f"{TENANT_SCIM}/Groups/{gid}", auth=TENANT_ADMIN_AUTH, json=put_payload, headers=scim_headers)
    if _ok(r, (200, 204)):
        info("Added via PUT fallback")
    else:
        warn(f"PUT fallback also failed (status {r.status_code}): {r.text[:200]}")


def _add_user_to_existing_group(s, group_id, group_name, u, uid, scim_headers):
    full_resp = s.get(f"{TENANT_SCIM}/Groups/{group_id}", auth=TENANT_ADMIN_AUTH)
    full_data = full_resp.json() if full_resp.status_code == 200 else {}
    existing_members = full_data.get("members", [])

    if any(m["value"] == uid for m in existing_members):
        info(f"User already in group '{group_name}' — skipping")
        return

    user_ref = f"{BASE_URL}/t/{TENANT_DOMAIN}/scim2/Users/{uid}"

    # PATCH to add member (idiomatic SCIM)
    patch_payload = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [{
            "op": "add",
            "path": "members",
            "value": [{"value": uid, "display": u["username"], "$ref": user_ref}],
        }],
    }
    resp = s.patch(f"{TENANT_SCIM}/Groups/{group_id}", auth=TENANT_ADMIN_AUTH, json=patch_payload, headers=scim_headers)
    if _ok(resp, (200, 204)):
        verify_resp = s.get(f"{TENANT_SCIM}/Groups/{group_id}", auth=TENANT_ADMIN_AUTH)
        vdata = verify_resp.json() if verify_resp.status_code == 200 else {}
        vmembers = vdata.get("members", [])
        if any(m["value"] == uid for m in vmembers):
            info(f"Added to group '{group_name}' (verified, {len(vmembers)} total members)")
        else:
            warn("PATCH returned OK but member not found — trying PUT fallback")
            _put_member_fallback(s, group_id, group_name, uid, u, user_ref, existing_members, scim_headers)
        return

    warn(f"PATCH failed (status {resp.status_code}): {resp.text[:200]} — trying PUT fallback")
    _put_member_fallback(s, group_id, group_name, uid, u, user_ref, existing_members, scim_headers)
    resp = s.patch(f"{TENANT_SCIM}/Groups/{group_id}", auth=TENANT_ADMIN_AUTH, json=patch_payload, headers=scim_headers)
    if _ok(resp, (200, 204)):
        info("Added via PATCH fallback")
    else:
        warn(f"PATCH also failed: {resp.text[:200]}")


def _create_group_with_member(s, group_name, u, uid, scim_headers):
    resp = s.post(f"{TENANT_SCIM}/Groups", auth=TENANT_ADMIN_AUTH, json={
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
        "displayName": group_name,
        "members": [{"value": uid, "display": u["username"], "$ref": f"{BASE_URL}/t/{TENANT_DOMAIN}/scim2/Users/{uid}"}]
    }, headers=scim_headers)
    if not _ok(resp, (200, 201)):
        warn(f"Failed to create group and assign: {resp.text}")
    else:
        info(f"Group '{group_name}' created and user assigned")


def _bootstrap_groups(s, users_to_create, user_ids, scim_headers):
    step(7, "Assign the federated test users to their groups")
    for u in users_to_create:
        group_name = u["group"]
        uid = user_ids[u["username"]]
        print(f"  Assigning {u['username']} to group '{group_name}'...")
        resp = s.get(f"{TENANT_SCIM}/Groups?filter=displayName+eq+{group_name}", auth=TENANT_ADMIN_AUTH)
        resources = resp.json().get("Resources", [])
        if resources:
            _add_user_to_existing_group(s, resources[0]["id"], group_name, u, uid, scim_headers)
        else:
            _create_group_with_member(s, group_name, u, uid, scim_headers)


# Seeded federated test users. Their email addresses are derived from
# TENANT_DOMAIN in _bootstrap_users, so the defaults produce john@worklink.com
# and tom@worklink.com — the accounts the live E2E suite signs in as.
USERS_TO_CREATE = [
    {"username": "john", "firstname": "John", "lastname": "Doe", "group": "user"},
    {"username": "tom", "firstname": "Tom", "lastname": "Admin", "group": "admin"},
]


def bootstrap_federated_idp():
    print("=" * 60)
    print("  Teamspace Federated IdP Setup")
    print(f"  Host:   {BASE_URL}")
    print(f"  Tenant: {TENANT_DOMAIN}")
    print(f"  Admin:  {TENANT_ADMIN_USERNAME}@{TENANT_DOMAIN}")
    print("=" * 60)

    s = _session()

    _bootstrap_tenant(s)
    _bootstrap_branding(s)
    _bootstrap_groups_scope(s)
    app_id = _bootstrap_application(s)
    _bootstrap_application_claims_and_oidc(s, app_id)
    user_ids = _bootstrap_users(s, USERS_TO_CREATE)
    scim_headers = {"Content-Type": "application/scim+json", "Accept": "application/json"}
    _bootstrap_groups(s, USERS_TO_CREATE, user_ids, scim_headers)

    print(f"\n{'=' * 60}")
    print("  Federated Identity Provider setup complete!")
    print(f"  Sign in at {BASE_URL}/t/{TENANT_DOMAIN} as john@{TENANT_DOMAIN} or tom@{TENANT_DOMAIN}")
    print(f"{'=' * 60}")


def main():
    try:
        bootstrap_federated_idp()
    except RuntimeError as e:
        fail(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
