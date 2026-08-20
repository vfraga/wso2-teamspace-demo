import hashlib
import base64
import logging
import secrets
import urllib.parse

import jwt
import requests
from authlib.integrations.flask_client import OAuth
from flask import session, redirect, url_for, current_app
from werkzeug.wrappers import Response

from common.jwt_validation import decode_rs256, jwks_url, select_signing_key, token_issuer
from common.logging_setup import is_production
from common.safe_auth_logger import SafeAuthLogger
from webapp.utils.roles import TEAMSPACE_ADMIN

logger = logging.getLogger(__name__)

oauth = OAuth()


def init_oauth(app) -> None:
    oauth.init_app(app)
    if hasattr(oauth, "_clients") and "wso2is" in oauth._clients:
        del oauth._clients["wso2is"]
    is_base = app.config["IS_BASE_URL"]
    tenant_path = app.config.get("TENANT_PATH", "")

    # TLS verification for OIDC discovery, the token endpoint AND the JWKS
    # fetch. This was hardcoded to False, which meant `IS_VERIFY_TLS` had no
    # effect on the OIDC path at all — including in production, and including
    # the JWKS the id_token signature is checked against. Verifying a signature
    # with keys pulled over an unauthenticated channel is not verification.
    verify_tls = app.config.get("IS_VERIFY_TLS", True)
    if not verify_tls:
        logger.warning(
            "TLS verification is DISABLED for the WSO2 IS OIDC client (IS_VERIFY_TLS=false). "
            "Expected for local dev against a self-signed certificate; never set this in production."
        )

    oauth.register(
        name="wso2is",
        client_id=app.config["CLIENT_ID"],
        client_secret=app.config["CLIENT_SECRET"],
        server_metadata_url=f"{is_base}{tenant_path}/oauth2/token/.well-known/openid-configuration",
        client_kwargs={
            "scope": app.config["OIDC_SCOPES"],
            "token_endpoint_auth_method": "client_secret_post",
            "verify": verify_tls,
        },
        fetch_token=lambda: session.get("token"),
    )


def _explain_tls_failure(exc: Exception, stage: str) -> bool:
    """Log an actionable message for a TLS trust failure. True if that's what it was.

    WSO2 IS ships a self-signed localhost certificate, so the first thing a
    developer hits when IS_VERIFY_TLS is unset (it defaults to true) is a bare
    SSLCertVerificationError from deep inside httpx. Name the setting instead.
    """
    text = f"{type(exc).__name__}: {exc}"
    if "CERTIFICATE_VERIFY_FAILED" not in text and "SSLError" not in text and "SSLCertVerification" not in text:
        return False
    logger.error(
        "TLS verification failed talking to WSO2 IS during %s. If this is local dev "
        "against the default self-signed certificate, set IS_VERIFY_TLS=false (see "
        ".env.example). In production, install a CA-signed certificate rather than "
        "disabling verification. Underlying error: %s",
        stage, text,
    )
    return True


def start_login(org_id: str | None = None) -> Response:
    redirect_uri = current_app.config["OIDC_REDIRECT_URI"]
    logger.info("Starting login flow, org_id=%s, redirect_uri=%s", org_id, redirect_uri)

    code_verifier = secrets.token_urlsafe(32)
    session["code_verifier"] = code_verifier
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()

    # OIDC nonce: sent here, echoed by the IdP into the id_token, and checked in
    # handle_callback. Required — Authlib's parse_id_token takes `nonce` as a
    # positional argument, and without it the verified parse cannot be called
    # at all (see the comment there).
    nonce = secrets.token_urlsafe(32)
    session["oidc_nonce"] = nonce

    extra_params = {
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "nonce": nonce,
    }
    if org_id:
        extra_params["fidp"] = "OrganizationSSO"
        extra_params["orgId"] = org_id

    try:
        return oauth.wso2is.authorize_redirect(redirect_uri, **extra_params)
    except Exception as exc:
        if not _explain_tls_failure(exc, "the OIDC discovery/authorize step"):
            logger.exception("Failed to start the login flow")
        raise


def decode_jwt_unverified(token_str: str, name: str) -> dict | None:
    try:
        return jwt.decode(token_str, options={"verify_signature": False})
    except jwt.DecodeError:
        logger.error("Failed to decode %s", name)
        return None


def _derive_display_name(id_claims: dict, email: str) -> tuple[str, str]:
    given_name = id_claims.get("given_name")
    family_name = id_claims.get("family_name")
    if given_name or family_name:
        return given_name or "", family_name or ""

    full_name = id_claims.get("name")
    if full_name and "@" not in full_name:
        parts = full_name.split(None, 1)
        return parts[0], parts[1] if len(parts) > 1 else ""

    if not email or "@" not in email:
        return "User", ""

    # Last-resort fallback when the IdP releases no name claims: derive a
    # display name from the email's local part and domain. The givenname /
    # lastname claims are now requested from WSO2 IS (see setup_is.py), so
    # this rarely runs — it only kicks in when those claims are absent.
    prefix, domain = email.split("@", 1)
    domain_name = domain.split(".", 1)[0]

    return prefix.title(), domain_name.title()


def _verify_access_token(raw_token: str) -> dict | None:
    """Verify the access token, or fall back to an unverified decode outside production.

    Its claims drive `user_scopes`, which gates what the UI offers. That is not
    the real authorization boundary — the Business API and the agent both verify
    this token themselves on every call — but a portal that renders its own menu
    from an unverified token is doing avoidable trust. Reuses the same
    primitives as the services (`common/jwt_validation.py`).

    Mirrors the id_token policy: production refuses rather than downgrading.
    """
    if not raw_token:
        return None

    is_base = current_app.config.get("IS_BASE_URL", "")
    tenant_path = current_app.config.get("TENANT_PATH", "")
    try:
        jwks = _fetch_jwks(is_base, tenant_path)
        key = select_signing_key(jwks, raw_token)
        return decode_rs256(
            raw_token,
            key,
            audience=current_app.config.get("CLIENT_ID", ""),
            issuer=token_issuer(is_base, tenant_path),
        )
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        if is_production():
            logger.error("access_token verification failed, refusing login: %s", detail)
            return None
        logger.warning(
            "access_token verification failed (%s); falling back to an UNVERIFIED "
            "decode because this is not a production deployment.", detail,
        )
        return decode_jwt_unverified(raw_token, "access_token")


def _fetch_jwks(is_base: str, tenant_path: str) -> dict:
    """Fetch the IdP's JWKS, honouring IS_VERIFY_TLS."""
    resp = requests.get(
        jwks_url(is_base, tenant_path),
        verify=current_app.config.get("IS_VERIFY_TLS", True),
        timeout=5,
    )
    resp.raise_for_status()
    return resp.json()


def handle_callback() -> Response:
    code_verifier = session.pop("code_verifier", "")
    try:
        token = oauth.wso2is.authorize_access_token(code_verifier=code_verifier)
    except Exception as exc:
        if not _explain_tls_failure(exc, "the OIDC token exchange"):
            logger.exception("Failed to exchange the authorization code")
        raise

    session["token"] = token
    session["access_token"] = token["access_token"]
    session["id_token_raw"] = token.get("id_token", "")
    session["refresh_token"] = token.get("refresh_token", "")

    # Verify the id_token: signature against the IdP's JWKS, plus issuer,
    # audience, expiry and the nonce we sent in start_login.
    #
    # `nonce` is a REQUIRED positional argument of Authlib's parse_id_token
    # (1.7.x). Calling it without one raised TypeError on every single login,
    # which the except branch below swallowed — silently downgrading every
    # login to an *unverified* decode. Keep the argument.
    nonce = session.pop("oidc_nonce", None)
    id_claims = None
    try:
        id_claims = dict(oauth.wso2is.parse_id_token(token, nonce=nonce))
    except Exception as e:
        detail = str(e).split('"')[0] if '"' in str(e) else str(e)
        if is_production():
            # Never fall back to an unverified identity in production.
            logger.error("id_token verification failed, refusing login: %s", detail)
            session.clear()
            return redirect(url_for("main.login"))
        logger.warning(
            "id_token verification failed (%s); falling back to an UNVERIFIED decode "
            "because this is not a production deployment. Set FLASK_ENV=production to "
            "make this a hard failure.", detail,
        )
        id_claims = decode_jwt_unverified(token.get("id_token", ""), "id_token")
    access_claims = _verify_access_token(token.get("access_token", ""))
    logger.debug("id_token claims: %s", id_claims)
    logger.debug("access_token claims: %s", access_claims)
    if id_claims is None or access_claims is None:
        session.clear()
        return redirect(url_for("main.login"))

    session["access_token_claims"] = access_claims

    session["user_scopes"] = access_claims.get("scope", "").split()

    email = id_claims.get("email") or id_claims.get("username") or ""
    given_name, family_name = _derive_display_name(id_claims, email)

    session["user"] = {
        "sub": id_claims.get("sub", ""),
        "email": email,
        "name": given_name,
        "family_name": family_name,
        "org_id": id_claims.get("org_id", ""),
        "org_name": id_claims.get("org_name", ""),
        "org_handle": id_claims.get("org_handle", ""),
        "groups": id_claims.get("groups", []),
        "roles": id_claims.get("roles", []),
    }

    user_roles = _extract_roles(id_claims, access_claims)

    session["is_admin"] = TEAMSPACE_ADMIN in user_roles
    session["user_roles"] = user_roles

    org_id = id_claims.get("org_id", "")
    org_handle = id_claims.get("org_handle", org_id)
    logger.info("Login complete: user=%s, org=%s, roles=%s", id_claims.get("sub"), org_handle, user_roles)
    return redirect(url_for("dashboard.home", org_handle=org_handle))


def logout() -> Response:
    id_token = session.get("id_token_raw", "")
    session.clear()
    tenant_path = current_app.config.get("TENANT_PATH", "")
    logout_url = (
        f"{current_app.config['IS_BASE_URL']}{tenant_path}/oidc/logout"
        f"?id_token_hint={id_token}"
        f"&post_logout_redirect_uri={urllib.parse.quote(current_app.config['OIDC_POST_LOGOUT_URI'])}"
    )
    return redirect(logout_url)


def _normalize_roles(raw: list) -> list[str]:
    """Handle roles as strings, objects [{"value": "..."}], or prefixed 'Application/...'."""
    roles = set()
    for entry in raw:
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict):
            name = entry.get("value") or entry.get("display") or entry.get("displayName", "")
        else:
            continue
        if "/" in name:
            name = name.rsplit("/", 1)[-1]
        if name:
            roles.add(name)
    return list(roles)


def _extract_roles(id_claims: dict, access_claims: dict) -> list[str]:
    all_roles = set()
    sources = [("id_token", id_claims), ("access_token", access_claims)]
    for source_name, claims in sources:
        for key in ("roles", "application_roles", "groups"):
            raw = claims.get(key)
            if not raw:
                continue
            if isinstance(raw, str):
                raw = [raw]
            if isinstance(raw, list):
                roles = _normalize_roles(raw)
                if roles:
                    logger.debug("Extracted roles from %s.%s: %s", source_name, key, roles)
                    all_roles.update(roles)
    if all_roles:
        return list(all_roles)
    logger.warning("No roles found in any token claim")
    return []


def _request_token(grant_type: str, scope: str, **extra) -> str:
    """POST to IS /oauth2/token and return the access_token (or "" on failure)."""
    tenant_path = current_app.config.get("TENANT_PATH", "")
    payload = {
        "grant_type": grant_type,
        "client_id": current_app.config["CLIENT_ID"],
        "client_secret": current_app.config["CLIENT_SECRET"],
        "scope": scope,
    }
    payload.update(extra)
    try:
        resp = requests.post(
            f"{current_app.config['IS_BASE_URL']}{tenant_path}/oauth2/token",
            data=payload,
            verify=current_app.config.get("IS_VERIFY_TLS", False),
            timeout=10,
        )
    except requests.RequestException as e:
        logger.error("Exception requesting token (grant=%s): %s", grant_type, e)
        return ""
    if resp.status_code not in (200, 201):
        SafeAuthLogger.log_token_error(grant_type, resp, prefix="Failed to get token")
        return ""
    try:
        data = resp.json()
    except ValueError:
        SafeAuthLogger.log_token_error(grant_type, resp, prefix="Failed to parse token response")
        return ""
    access_token = data.get("access_token")
    if access_token:
        logger.debug("Token obtained successfully (grant=%s)", grant_type)
        return access_token
    SafeAuthLogger.log_token_error(grant_type, data, prefix="Token response missing access_token")
    return ""


def get_client_credentials_token() -> str:
    logger.debug("Requesting client_credentials token from IS")
    return _request_token(
        "client_credentials",
        "internal_organization_create internal_organization_view "
        "internal_shared_application_create",
    )


def get_root_role_management_token() -> str:
    logger.debug("Requesting client_credentials token with role management scopes from IS")
    return _request_token(
        "client_credentials",
        "internal_org_role_mgt_view internal_org_role_mgt_update "
        "internal_role_mgt_view internal_role_mgt_update",
    )


def get_agent_management_token() -> str:
    return _request_token(
        "client_credentials",
        "internal_agent_mgt_create internal_agent_mgt_list "
        "internal_agent_mgt_view internal_agent_mgt_delete",
    )


def switch_org_token(org_id: str, token: str) -> str:
    logger.info("Switching org context to org_id=%s", org_id)
    return _request_token(
        "organization_switch",
        "internal_org_user_mgt_create internal_org_user_mgt_list internal_org_user_mgt_view "
        "internal_org_role_mgt_create internal_org_role_mgt_view internal_org_role_mgt_update",
        token=token,
        switching_organization=org_id,
    )
