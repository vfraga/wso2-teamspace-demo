import base64
import json
from webapp.blueprints.chat import decode_jwt

def create_mock_jwt(header: dict, payload: dict) -> str:
    header_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    signature_b64 = "mocksignature"
    return f"{header_b64}.{payload_b64}.{signature_b64}"

def test_decode_jwt_success():
    header = {"alg": "RS256", "typ": "JWT", "kid": "123"}
    payload = {"sub": "admin", "org_id": "teamspace", "scope": "openid email"}
    token = create_mock_jwt(header, payload)
    
    decoded = decode_jwt(token)
    assert "header" in decoded
    assert "payload" in decoded
    assert decoded["header"] == header
    assert decoded["payload"] == payload

def test_decode_jwt_malformed():
    # Empty string
    assert decode_jwt("") == {}
    
    # Missing parts (less than 2 dots)
    assert decode_jwt("part1.part2") == {}
    
    # Too many parts
    assert decode_jwt("part1.part2.part3.part4") == {}
    
    # None type check
    assert decode_jwt(None) == {}

def test_decode_jwt_invalid_base64():
    # Invalid characters for base64 decoding
    assert decode_jwt("%%%.%%%.mocksignature") == {}
