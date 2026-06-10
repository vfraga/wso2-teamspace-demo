import os
from typing import Optional

from common.config import CommonDefaults, load_env

load_env()


class Settings:
    IS_BASE_URL: str = os.getenv("IS_BASE_URL", CommonDefaults.IS_BASE_URL)
    IS_ORG_HANDLE: str = os.getenv("IS_ORG_HANDLE", "")
    CLIENT_ID: str = os.getenv("CLIENT_ID", "")
    # Shared secret for service-to-service auth. The webapp and the agent
    # service both know this value (from env); they present it as the
    # X-Internal-Secret header when calling endpoints that opt in to M2M
    # auth. Keep this distinct from the webapp→agent AGENT_INTERNAL_SECRET.
    INTERNAL_SECRET: str = os.getenv("INTERNAL_SECRET", "")

    _tenant_path: Optional[str] = None

    @property
    def TENANT_PATH(self) -> str:
        if self._tenant_path is not None:
            return self._tenant_path
        return f"/t/{self.IS_ORG_HANDLE}" if self.IS_ORG_HANDLE else ""

    @TENANT_PATH.setter
    def TENANT_PATH(self, value: str):
        self._tenant_path = value
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///teamspace.db")
    ALLOWED_ORIGINS: list[str] = os.getenv(
        "ALLOWED_ORIGINS", "http://localhost:5001"
    ).split(",")
    IS_VERIFY_TLS: bool = os.getenv("IS_VERIFY_TLS", "true").lower() != "false"


settings = Settings()
