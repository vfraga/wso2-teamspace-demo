#!/usr/bin/env python3
"""
Setup script for the Federated IdP WSO2 IS instance (port 9444).
Creates tenant, application, groups, and test users.

Importable: ``from setup_idp_server import bootstrap_federated_idp``
Standalone: ``python setup_idp_server.py``  (used by live E2E test harness)
"""

import json
import sys
import os
import stat
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─── Configuration ───────────────────────────────────────────────────────────

BASE_URL = "https://localhost:9444"
SUPER_ADMIN_USERNAME = os.environ.get("IS_SUPER_ADMIN_USERNAME", "admin")
SUPER_ADMIN_PASSWORD = os.environ.get("IS_SUPER_ADMIN_PASSWORD", "")
if not SUPER_ADMIN_PASSWORD:
    import warnings
    warnings.warn("IS_SUPER_ADMIN_PASSWORD not set — IS API calls will fail with 401", RuntimeWarning, stacklevel=2)
SUPER_ADMIN_AUTH = (SUPER_ADMIN_USERNAME, SUPER_ADMIN_PASSWORD)

TENANT_DOMAIN = "worklink.com"
TENANT_ADMIN_USERNAME = "teamspaceadmin"
TENANT_ADMIN_PASSWORD = os.environ.get("IS_TENANT_ADMIN_PASSWORD", "")
TENANT_ADMIN_EMAIL = "teamspaceadmin@mail.com"
TENANT_ADMIN_AUTH = (f"{TENANT_ADMIN_USERNAME}@{TENANT_DOMAIN}", TENANT_ADMIN_PASSWORD)

FEDERATED_USER_PASSWORD = os.environ.get("FEDERATED_USER_PASSWORD", "")

SERVER_API = f"{BASE_URL}/api/server/v1"
TENANT_API = f"{BASE_URL}/t/{TENANT_DOMAIN}/api/server/v1"
TENANT_SCIM = f"{BASE_URL}/t/{TENANT_DOMAIN}/scim2"


def _session():
    s = requests.Session()
    s.verify = False
    return s


def _ok(resp, accept_codes=(200, 201)):
    return resp.status_code in accept_codes


def _bootstrap_tenant(s):
    print(f"Creating tenant '{TENANT_DOMAIN}'...")
    resp = s.get(f"{SERVER_API}/tenants", params={"filter": f"domainName eq {TENANT_DOMAIN}"}, auth=SUPER_ADMIN_AUTH)
    tenants = resp.json().get("tenants", [])
    if tenants:
        print("Tenant already exists.")
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
        print(f"Failed to create tenant: {resp.text}")
        raise RuntimeError("federated IdP bootstrap failed")
    print("Tenant created.")


def _bootstrap_groups_scope(s):
    # Claims are released by default when groups are mapped
    print("Creating custom OIDC scope 'groups'...")
    resp = s.post(f"{TENANT_API}/oidc/scopes", auth=TENANT_ADMIN_AUTH, json={
        "name": "groups",
        "displayName": "groups",
        "description": "User groups scope",
        "claims": ["groups", "roles"]
    })
    if resp.status_code == 409:
        print("Scope 'groups' already exists. Updating it...")
        resp = s.put(f"{TENANT_API}/oidc/scopes/groups", auth=TENANT_ADMIN_AUTH, json={
            "displayName": "groups",
            "description": "User groups scope",
            "claims": ["groups", "roles"]
        })
    if _ok(resp):
        print("Scope 'groups' configured successfully.")
    else:
        print(f"Failed to configure scope: {resp.text}")


def _bootstrap_application(s):
    print("Creating Application 'FederatedClient'...")
    resp = s.get(f"{TENANT_API}/applications", params={"filter": "name eq FederatedClient"}, auth=TENANT_ADMIN_AUTH)
    apps = resp.json().get("applications", [])
    if apps:
        app_id = apps[0]["id"]
        print(f"Application already exists (id={app_id})")
        return app_id
    resp = s.post(f"{TENANT_API}/applications", auth=TENANT_ADMIN_AUTH, json={
        "name": "FederatedClient",
        "templateId": "b9c5e11e-fc78-484b-9bec-015d247561b8",
        "inboundProtocolConfiguration": {
            "oidc": {
                "grantTypes": ["authorization_code", "refresh_token"],
                "allowedOrigins": [],
                "callbackURLs": ["regexp=(https://localhost:9443/commonauth.*|https://localhost:9443/o/.*/commonauth.*)"],
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
        print(f"Failed to create app: {resp.text}")
        raise RuntimeError("federated IdP bootstrap failed")

    # Query again to get app ID
    resp = s.get(f"{TENANT_API}/applications", params={"filter": "name eq FederatedClient"}, auth=TENANT_ADMIN_AUTH)
    app_id = resp.json()["applications"][0]["id"]
    print(f"Application created (id={app_id})")
    return app_id


def _bootstrap_application_claims_and_oidc(s, app_id):
    print("Updating claims and OIDC config for FederatedClient...")
    resp = s.patch(f"{TENANT_API}/applications/{app_id}", auth=TENANT_ADMIN_AUTH, json={
        "claimConfiguration": {
            "dialect": "LOCAL",
            "requestedClaims": [
                {"claim": {"uri": "http://wso2.org/claims/emailaddress"}, "mandatory": False},
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
        print(f"Failed to update app claims: {resp.text}")
        raise RuntimeError("federated IdP bootstrap failed")

    resp = s.get(f"{TENANT_API}/applications/{app_id}/inbound-protocols/oidc", auth=TENANT_ADMIN_AUTH)
    data = resp.json()
    client_id = data.get("clientId", "")
    client_secret = data.get("clientSecret", "")
    print(f"Federated Client ID: {client_id}\tClient Secret: {client_secret}")

    print("Updating OIDC configuration for FederatedClient...")
    resp = s.put(f"{TENANT_API}/applications/{app_id}/inbound-protocols/oidc", auth=TENANT_ADMIN_AUTH, json={
        "clientId": client_id,
        "grantTypes": ["authorization_code", "refresh_token"],
        "allowedOrigins": [],
        "callbackURLs": ["regexp=(https://localhost:9443/commonauth.*|https://localhost:9443/o/.*/commonauth.*)"],
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
        print(f"Failed to update app OIDC config: {resp.text}")
        raise RuntimeError("federated IdP bootstrap failed")

    # Write to a JSON file in scratch/ so the tests and system can read it
    os.makedirs("scratch", exist_ok=True)
    credentials_path = "scratch/idp_credentials.json"
    with open(credentials_path, "w") as f:
        json.dump({"client_id": client_id, "client_secret": client_secret}, f)
    os.chmod(credentials_path, stat.S_IRUSR | stat.S_IWUSR)


def _bootstrap_users(s, users_to_create):
    if not FEDERATED_USER_PASSWORD:
        print("FEDERATED_USER_PASSWORD environment variable is required to create federated test users")
        raise RuntimeError("federated IdP bootstrap failed")
    user_ids = {}
    for u in users_to_create:
        print(f"Checking/Creating user {u['username']}...")
        resp = s.get(f"{TENANT_SCIM}/Users?filter=userName+eq+{u['username']}", auth=TENANT_ADMIN_AUTH)
        resources = resp.json().get("Resources", [])
        if resources:
            uid = resources[0]["id"]
            print(f"User {u['username']} already exists (id={uid}). Skipping creation.")
        else:
            resp = s.post(f"{TENANT_SCIM}/Users", auth=TENANT_ADMIN_AUTH, json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "userName": u["username"],
                "password": FEDERATED_USER_PASSWORD,
                "name": {"givenName": u["firstname"], "familyName": u["lastname"]},
                "emails": [{"value": f"{u['username']}@worklink.com", "primary": True}],
            })
            if not _ok(resp, (200, 201)):
                print(f"Failed to create user {u['username']}: {resp.text}")
                raise RuntimeError("federated IdP bootstrap failed")
            uid = resp.json()["id"]
            print(f"User created (id={uid})")
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
        print("  Added via PUT fallback")
    else:
        print(f"  PUT fallback also failed (status {r.status_code}): {r.text[:200]}")


def _add_user_to_existing_group(s, group_id, group_name, u, uid, scim_headers):
    full_resp = s.get(f"{TENANT_SCIM}/Groups/{group_id}", auth=TENANT_ADMIN_AUTH)
    full_data = full_resp.json() if full_resp.status_code == 200 else {}
    existing_members = full_data.get("members", [])

    if any(m["value"] == uid for m in existing_members):
        print(f"  User already in group '{group_name}' — skipping")
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
            print(f"  Added to group '{group_name}' (verified, {len(vmembers)} total members)")
        else:
            print("  PATCH returned OK but member not found — trying PUT fallback")
            _put_member_fallback(s, group_id, group_name, uid, u, user_ref, existing_members, scim_headers)
        return

    print(f"  PATCH failed (status {resp.status_code}): {resp.text[:200]} — trying PUT fallback")
    _put_member_fallback(s, group_id, group_name, uid, u, user_ref, existing_members, scim_headers)
    resp = s.patch(f"{TENANT_SCIM}/Groups/{group_id}", auth=TENANT_ADMIN_AUTH, json=patch_payload, headers=scim_headers)
    if _ok(resp, (200, 204)):
        print("  Added via PATCH fallback")
    else:
        print(f"  PATCH also failed: {resp.text[:200]}")


def _create_group_with_member(s, group_name, u, uid, scim_headers):
    resp = s.post(f"{TENANT_SCIM}/Groups", auth=TENANT_ADMIN_AUTH, json={
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
        "displayName": group_name,
        "members": [{"value": uid, "display": u["username"], "$ref": f"{BASE_URL}/t/{TENANT_DOMAIN}/scim2/Users/{uid}"}]
    }, headers=scim_headers)
    if not _ok(resp, (200, 201)):
        print(f"Failed to create group and assign: {resp.text}")
    else:
        print(f"Group '{group_name}' created and user assigned.")


def _bootstrap_groups(s, users_to_create, user_ids, scim_headers):
    for u in users_to_create:
        group_name = u["group"]
        uid = user_ids[u["username"]]
        print(f"Assigning user to group '{group_name}'...")
        resp = s.get(f"{TENANT_SCIM}/Groups?filter=displayName+eq+{group_name}", auth=TENANT_ADMIN_AUTH)
        resources = resp.json().get("Resources", [])
        if resources:
            _add_user_to_existing_group(s, resources[0]["id"], group_name, u, uid, scim_headers)
        else:
            _create_group_with_member(s, group_name, u, uid, scim_headers)


USERS_TO_CREATE = [
    {"username": "john", "firstname": "John", "lastname": "Doe", "group": "user"},
    {"username": "tom", "firstname": "Tom", "lastname": "Admin", "group": "admin"},
]


def bootstrap_federated_idp():
    s = _session()
    print("Connecting to second IS instance at 9444...")

    _bootstrap_tenant(s)
    _bootstrap_groups_scope(s)
    app_id = _bootstrap_application(s)
    _bootstrap_application_claims_and_oidc(s, app_id)
    user_ids = _bootstrap_users(s, USERS_TO_CREATE)
    scim_headers = {"Content-Type": "application/scim+json", "Accept": "application/json"}
    _bootstrap_groups(s, USERS_TO_CREATE, user_ids, scim_headers)

    print("Federated Identity Provider Server setup complete!")


def main():
    try:
        bootstrap_federated_idp()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
