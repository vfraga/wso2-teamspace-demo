import os
from typing import Optional

from common.config import CommonDefaults, VerifyTLS, load_env, verify_tls_from_env

load_env()


class Settings:
    IS_BASE_URL: str = os.getenv("IS_BASE_URL", CommonDefaults.IS_BASE_URL)
    IS_ORG_HANDLE: str = os.getenv("IS_ORG_HANDLE", "")
    CLIENT_ID: str = os.getenv("CLIENT_ID", "")
    # Service-to-service auth needs no shared secret: callers present an
    # OAuth 2.0 client-credentials token from WSO2 IS, verified against JWKS
    # with the same CLIENT_ID audience as a user token. See common/m2m_auth.py.

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
    # bool, or a path to a CA bundle (see pki/ and common.config.resolve_verify_tls).
    IS_VERIFY_TLS: VerifyTLS = verify_tls_from_env(
        "IS_VERIFY_TLS", label="Business API -> WSO2 IS",
        default=CommonDefaults.IS_VERIFY_TLS,
    )


settings = Settings()
