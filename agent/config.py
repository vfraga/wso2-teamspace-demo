import os
import secrets
from typing import Optional

from common.config import CommonDefaults, load_env

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
    INTERNAL_SECRET: str = os.getenv("AGENT_INTERNAL_SECRET", "")
    BUSINESS_API_INTERNAL_SECRET: str = os.getenv("BUSINESS_API_INTERNAL_SECRET", "")
    MOCK_LLM: bool = os.getenv("MOCK_LLM", "false").lower() == "true"
    IS_VERIFY_TLS: bool = os.getenv("IS_VERIFY_TLS", "true").lower() != "false"

    def state_jwt_signing_secret(self) -> str:
        raw_internal = os.getenv("AGENT_INTERNAL_SECRET", "")
        if raw_internal:
            return raw_internal
        if self.CLIENT_SECRET:
            return self.CLIENT_SECRET
        raise ValueError(
            "Cannot sign or verify OAuth state JWT: "
            "AGENT_INTERNAL_SECRET and CLIENT_SECRET are both empty"
        )


settings = Settings()

if not settings.INTERNAL_SECRET:
    settings.INTERNAL_SECRET = secrets.token_urlsafe(64)
