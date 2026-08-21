import os
from typing import Optional

from common.config import CommonDefaults, VerifyTLS, load_env, verify_tls_from_env
from common.logging_setup import is_production
from common.m2m_auth import M2MConfig, ServiceTokenClient

load_env()


class Settings:
    IS_BASE_URL: str = os.getenv("IS_BASE_URL", CommonDefaults.IS_BASE_URL)
    IS_ORG_HANDLE: str = os.getenv("IS_ORG_HANDLE", "")

    _tenant_path: Optional[str] = None

    @property
    def TENANT_PATH(self) -> str:
        if self._tenant_path is not None:
            return self._tenant_path
        return f"/t/{self.IS_ORG_HANDLE}" if self.IS_ORG_HANDLE else ""

    @TENANT_PATH.setter
    def TENANT_PATH(self, value: str):
        self._tenant_path = value

    CLIENT_ID: str = os.getenv("CLIENT_ID", "")
    CLIENT_SECRET: str = os.getenv("CLIENT_SECRET", "")
    AGENT_REDIRECT_URI: str = os.getenv("AGENT_REDIRECT_URI", CommonDefaults.AGENT_REDIRECT_URI)
    AGENT_SERVICE_URL: str = os.getenv("AGENT_SERVICE_URL", CommonDefaults.AGENT_SERVICE_URL)
    BUSINESS_API_URL: str = os.getenv("BUSINESS_API_URL", CommonDefaults.BUSINESS_API_URL)
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    ALLOWED_ORIGINS: list[str] = os.getenv(
        "ALLOWED_ORIGINS", "http://localhost:5001"
    ).split(",")
    MOCK_LLM: bool = os.getenv("MOCK_LLM", "false").lower() == "true"
    # Both accept a bool or a path to a CA bundle. IS_VERIFY_TLS covers calls to
    # WSO2 IS; SERVICE_VERIFY_TLS covers the agent -> Business API hop, which is
    # HTTPS whenever the Business API is served with a certificate. They are
    # separate settings because the two can legitimately have different trust:
    # WSO2 might use a corporate CA while our own services use the demo CA.
    IS_VERIFY_TLS: VerifyTLS = verify_tls_from_env("IS_VERIFY_TLS", label="AI Agent -> WSO2 IS")
    SERVICE_VERIFY_TLS: VerifyTLS = verify_tls_from_env(
        "SERVICE_VERIFY_TLS", label="AI Agent -> Business API"
    )

    # HMAC key for the agent's OAuth `state` JWT. It must be stable and shared
    # across every agent instance: /authorize signs the state and /callback
    # verifies it, and with more than one worker those are different processes.
    STATE_SIGNING_SECRET: str = os.getenv("AGENT_STATE_SIGNING_SECRET", "")

    def state_jwt_signing_secret(self) -> str:
        """Return the HMAC key for the OAuth state JWT, or raise.

        Falls back to ``CLIENT_SECRET`` — already a stable, per-deployment
        secret — so the demo needs no extra configuration. Deliberately never
        generates a value: a random key verifies only on the instance that
        signed it, which silently breaks the OBO callback under scaling.
        """
        if self.STATE_SIGNING_SECRET:
            return self.STATE_SIGNING_SECRET
        if self.CLIENT_SECRET:
            return self.CLIENT_SECRET
        raise ValueError(
            "Cannot sign or verify the OAuth state JWT: set AGENT_STATE_SIGNING_SECRET "
            "(or CLIENT_SECRET). A generated fallback is intentionally not used — it "
            "would only verify on the instance that signed it."
        )


settings = Settings()


def m2m_config() -> M2MConfig:
    """Snapshot of what the agent needs to mint a service token.

    Read per call, not captured once: TENANT_PATH is computed from
    IS_ORG_HANDLE and the test suite swaps IS_BASE_URL at runtime.
    """
    return M2MConfig(
        is_base_url=settings.IS_BASE_URL,
        tenant_path=settings.TENANT_PATH,
        client_id=settings.CLIENT_ID,
        client_secret=settings.CLIENT_SECRET,
        verify_tls=settings.IS_VERIFY_TLS,
    )


#: Service token used for the agent -> Business API hop.
service_token_client = ServiceTokenClient(m2m_config, label="AI Agent")

if is_production():
    # A production deployment must not discover a missing state-signing key
    # halfway through a user's OBO consent flow.
    try:
        settings.state_jwt_signing_secret()
    except ValueError as exc:
        raise RuntimeError(f"FLASK_ENV=production but the agent cannot sign OAuth state: {exc}") from exc
