import time
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from api.auth import UserInfo, get_current_user
from api.config import settings
from fastapi.security import HTTPAuthorizationCredentials


KID = "test-key-1"
ISSUER = f"{settings.IS_BASE_URL}{settings.TENANT_PATH}/oauth2/token"
AUDIENCE = settings.CLIENT_ID or "test-client-id"


def _make_rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_numbers = private_key.public_key().public_numbers()
    public_key = private_key.public_key()

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    def _b64uint(value: int) -> str:
        import base64
        byte_length = (value.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(value.to_bytes(byte_length, "big")).rstrip(b"=").decode()

    jwk = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": KID,
        "n": _b64uint(public_numbers.n),
        "e": _b64uint(public_numbers.e),
    }
    return private_pem, public_pem, jwk


def _sign(payload: dict, private_pem: bytes) -> str:
    return jwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": KID})


def _cred(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _patch_jwks(jwks_payload: dict):
    return patch("api.auth.get_jwks", return_value=jwks_payload)


def _valid_payload(extra: dict | None = None) -> dict:
    base = {
        "sub": "user-jwt",
        "user_org": "org-jwt",
        "email": "jwt@org.com",
        "scope": "openid email create_meeting",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    if extra:
        base.update(extra)
    return base


def test_get_current_user_valid_token_returns_userinfo():
    private_pem, _, jwk = _make_rsa_keypair()
    token = _sign(_valid_payload(), private_pem)

    with _patch_jwks({"keys": [jwk]}):
        user = get_current_user(_cred(token))

    assert isinstance(user, UserInfo)
    assert user.user_id == "user-jwt"
    assert user.org == "org-jwt"
    assert "create_meeting" in user.scopes


def test_get_current_user_expired_token_yields_401():
    private_pem, _, jwk = _make_rsa_keypair()
    expired = _valid_payload({"exp": int(time.time()) - 60})
    token = _sign(expired, private_pem)

    with _patch_jwks({"keys": [jwk]}):
        with pytest.raises(HTTPException) as exc_info:
            get_current_user(_cred(token))
    assert exc_info.value.status_code == 401


def test_get_current_user_wrong_signing_key_yields_401():
    _, _, jwk = _make_rsa_keypair()
    other_private, _, _ = _make_rsa_keypair()
    token = _sign(_valid_payload(), other_private)

    with _patch_jwks({"keys": [jwk]}):
        with pytest.raises(HTTPException) as exc_info:
            get_current_user(_cred(token))
    assert exc_info.value.status_code == 401


def test_get_current_user_unknown_kid_yields_401():
    private_pem, _, _ = _make_rsa_keypair()
    token = _sign(_valid_payload(), private_pem)

    other_jwk_no_match = {**_, "kid": "different-kid"}
    with _patch_jwks({"keys": [other_jwk_no_match]}):
        with pytest.raises(HTTPException) as exc_info:
            get_current_user(_cred(token))
    assert exc_info.value.status_code == 401
    assert "key" in str(exc_info.value.detail).lower()
