import base64
import hashlib
import logging
import secrets
import time
from typing import Any

import httpx

from agent.config import settings
from common.safe_auth_logger import SafeAuthLogger

logger = logging.getLogger(__name__)


class AuthManager:
    _instance = None

    def __init__(self):
        self._agent_token: dict[str, dict[str, Any]] = {}
        self._obo_tokens: dict[str, dict[str, Any]] = {}
        self._pkce_verifiers: dict[str, str] = {}
        self._obo_jwt_raw: dict[str, dict[str, Any]] = {}
        self._agent_jwt_raw: dict[str, dict[str, Any]] = {}

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls):
        inst = cls.get_instance()
        inst._agent_token.clear()
        inst._obo_tokens.clear()
        inst._pkce_verifiers.clear()
        inst._obo_jwt_raw.clear()
        inst._agent_jwt_raw.clear()

    @property
    def _base(self) -> str:
        """Root org OAuth base URL — all OAuth flows go through the root org."""
        return f"{settings.IS_BASE_URL}{settings.TENANT_PATH}"

    async def _init_agent_auth_flow(self, code_challenge: str, agent_id: str) -> tuple[str, str]:
        logger.debug("Step 1/3: Initiating agent auth (response_mode=direct) agent=%s", agent_id)
        resp = await self._http_post(
            f"{self._base}/oauth2/authorize",
            data={
                "client_id": settings.CLIENT_ID,
                "client_secret": settings.CLIENT_SECRET,
                "response_type": "code",
                "redirect_uri": settings.AGENT_REDIRECT_URI,
                "scope": "openid create_meeting_agent list_meetings_agent update_meeting_agent delete_meeting_agent",
                "response_mode": "direct",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
        )
        flow_data = resp.json()
        flow_id = flow_data.get("flowId")
        if not flow_id:
            logger.error("No flowId in authorize response: %s", flow_data)
            raise ValueError(f"Agent auth failed at authorize step: {flow_data}")

        authenticators = flow_data.get("nextStep", {}).get("authenticators", [])
        authenticator_id = ""
        for auth in authenticators:
            if auth.get("authenticator") == "Username & Password":
                authenticator_id = auth["authenticatorId"]
                break
        if not authenticator_id and authenticators:
            authenticator_id = authenticators[0]["authenticatorId"]
        logger.debug("Got flowId=%s, authenticatorId=%s", flow_id, authenticator_id)
        return flow_id, authenticator_id

    async def _submit_agent_credentials(self, flow_id: str, authenticator_id: str, agent_id: str, agent_secret: str) -> str:
        logger.debug("Step 2/3: Submitting agent credentials (agent_id=%s)", agent_id)
        resp = await self._http_post(
            f"{self._base}/oauth2/authn",
            json={
                "flowId": flow_id,
                "selectedAuthenticator": {
                    "authenticatorId": authenticator_id,
                    "params": {
                        "username": agent_id,
                        "password": agent_secret,
                    },
                },
            },
        )
        authn_data = resp.json()
        code = (
            authn_data.get("authData", {}).get("code", "")
            or authn_data.get("code", "")
            or authn_data.get("authorizationCode", "")
        )
        if not code:
            logger.error("No authorization code in authn response: %s", authn_data)
            raise ValueError(f"Agent auth failed at authn step: {authn_data}")
        logger.debug("Got authorization code")
        return code

    async def _exchange_code_for_agent_token(self, code: str, code_verifier: str) -> tuple[str, int]:
        logger.debug("Step 3/3: Exchanging code for agent token")
        resp = await self._http_post(
            f"{self._base}/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "client_id": settings.CLIENT_ID,
                "client_secret": settings.CLIENT_SECRET,
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": settings.AGENT_REDIRECT_URI,
            },
        )
        token_data = resp.json()
        if "access_token" not in token_data:
            SafeAuthLogger.log_token_error(
                "authorization_code", token_data,
                prefix="Agent token exchange failed",
            )
            raise ValueError(f"Agent auth failed at token exchange: {token_data}")
        return token_data["access_token"], token_data.get("expires_in", 3600)

    def _store_agent_token(self, token: str, expires_in: int, thread_id: str) -> None:
        entry = {
            "token": token,
            "expires_at": time.time() + expires_in - 60,
        }
        key = thread_id or "_default"
        self._agent_token[key] = entry
        self._agent_jwt_raw[key] = entry
        logger.debug("Agent token obtained successfully")

    async def fetch_agent_token(self, agent_id: str = "", agent_secret: str = "", thread_id: str = "") -> str:
        """Authenticate as the agent itself (no user involvement).

        Uses the Authentication API with response_mode=direct:
        1. POST /oauth2/authorize → get flowId
        2. POST /oauth2/authn → submit agent credentials → get code
        3. POST /oauth2/token → exchange code for access token

        All requests go through the ROOT org's endpoint with the ROOT app's client_id.
        """
        if not agent_id:
            raise ValueError("agent_id is required")
        if not agent_secret:
            raise ValueError("agent_secret is required")

        # PKCE is required even for agent auth with response_mode=direct
        code_verifier = secrets.token_urlsafe(32)
        code_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )

        flow_id, authenticator_id = await self._init_agent_auth_flow(code_challenge, agent_id)
        code = await self._submit_agent_credentials(flow_id, authenticator_id, agent_id, agent_secret)
        token, expires_in = await self._exchange_code_for_agent_token(code, code_verifier)
        self._store_agent_token(token, expires_in, thread_id)
        return token

    def clear_obo_tokens(self, thread_id: str):
        """Clear cached OBO tokens and raw JWTs for the given thread."""
        self._obo_tokens.pop(thread_id, None)
        self._obo_jwt_raw.pop(thread_id, None)
        self._agent_token.pop(thread_id, None)
        self._agent_jwt_raw.pop(thread_id, None)
        logger.debug("Cleared cached OBO tokens and agent tokens for thread=%s", thread_id)

    def get_obo_authorization_url(self, thread_id: str, scopes: list[str], agent_id: str = "", action: str = "") -> str:
        """Generate a local authorize URL that initiates the secure OBO flow.

        The scopes are accepted for forward-compatibility; the /authorize endpoint
        currently derives required scopes from the action parameter.
        """
        return f"{settings.AGENT_SERVICE_URL}/authorize?thread_id={thread_id}&action={action}"

    def get_real_wso2_authorization_url(self, thread_id: str, scopes: list[str], state_token: str, agent_id: str = "") -> str:
        """Generate the actual WSO2 IS authorize URL with PKCE and CSRF state token."""
        if not agent_id:
            raise ValueError("agent_id is required")
        self.clear_obo_tokens(thread_id)
        code_verifier = secrets.token_urlsafe(32)
        self._pkce_verifiers[thread_id] = code_verifier
        code_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )

        params = {
            "client_id": settings.CLIENT_ID,
            "response_type": "code",
            "redirect_uri": settings.AGENT_REDIRECT_URI,
            "scope": " ".join(scopes),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state_token,
            "requested_actor": agent_id,
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{self._base}/oauth2/authorize?{query}"
        logger.debug(
            "Generated real OBO authorization URL for thread=%s, scopes=%s, state=%s",
            thread_id, scopes, state_token,
        )
        return url

    async def exchange_obo_code(self, thread_id: str, code: str, agent_id: str = "", agent_secret: str = "") -> str:
        """Exchange authorization code for OBO token.

        Uses the ROOT org's token endpoint. The agent's own token is sent
        in the Authorization header as the actor_token.
        """
        logger.debug("Exchanging OBO code for thread=%s, stored_verifiers=%s", thread_id, list(self._pkce_verifiers.keys()))
        agent_token = await self.fetch_agent_token(agent_id, agent_secret, thread_id=thread_id)
        code_verifier = self._pkce_verifiers.pop(thread_id, "")
        if not code_verifier:
            logger.error("No PKCE code_verifier found for thread=%s — agent service may have restarted", thread_id)

        resp = await self._http_post(
            f"{self._base}/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "client_id": settings.CLIENT_ID,
                "client_secret": settings.CLIENT_SECRET,
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": settings.AGENT_REDIRECT_URI,
                "actor_token": agent_token,
                "actor_token_type": "urn:ietf:params:oauth:token-type:jwt",
            },
        )
        token_data = resp.json()
        if "access_token" not in token_data:
            SafeAuthLogger.log_token_error(
                "urn:ietf:params:oauth:grant-type:token-exchange", token_data,
                thread_id=thread_id, prefix="OBO token exchange failed",
            )
            raise ValueError(f"OBO token exchange failed: {token_data}")
        expires_in = token_data.get("expires_in", 3600)
        entry = {
            "token": token_data["access_token"],
            "expires_at": time.time() + expires_in - 60,
        }
        self._obo_tokens[thread_id] = entry
        self._obo_jwt_raw[thread_id] = entry
        logger.debug("OBO token obtained for thread=%s", thread_id)
        return token_data["access_token"]

    def get_agent_jwt_raw(self, thread_id: str = "") -> str | None:
        key = thread_id or "_default"
        entry = self._agent_jwt_raw.get(key)
        if entry is None and thread_id:
            entry = self._agent_jwt_raw.get("_default")
        if entry is None:
            return None
        if entry["expires_at"] < time.time():
            return None
        return entry["token"]

    def get_obo_token(self, thread_id: str) -> str | None:
        entry = self._obo_tokens.get(thread_id)
        if entry is None:
            return None
        if entry["expires_at"] < time.time():
            return None
        return entry["token"]

    def get_obo_jwt_raw(self, thread_id: str) -> str | None:
        entry = self._obo_jwt_raw.get(thread_id)
        if entry is None:
            return None
        if entry["expires_at"] < time.time():
            return None
        return entry["token"]

    async def _http_post(self, url, **kwargs):
        async with httpx.AsyncClient(verify=settings.IS_VERIFY_TLS) as client:
            return await client.post(url, **kwargs)
