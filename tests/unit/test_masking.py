import json

import pytest

from common.fastapi_errors import mask_request_body
from common.m2m_auth import SERVICE_SCOPE
from common.safe_auth_logger import format_claims, mask_email, mask_token


class _FakeRequest:
    def __init__(self, body: bytes):
        self._body = body

    async def body(self):
        return self._body


async def _mask(body: bytes) -> str:
    return await mask_request_body(_FakeRequest(body))


@pytest.mark.asyncio
async def test_mask_request_body_masks_agent_secret():
    masked = await _mask(json.dumps({
        "org": "org1",
        "agent_secret": "super-secret-value",
    }).encode("utf-8"))
    assert "[MASKED]" in masked
    assert "super-secret-value" not in masked


@pytest.mark.asyncio
async def test_mask_request_body_masks_gemini_api_key():
    masked = await _mask(json.dumps({
        "org": "org1",
        "gemini_api_key": "AIzaSy-real-key",
    }).encode("utf-8"))
    assert "[MASKED]" in masked
    assert "AIzaSy-real-key" not in masked


@pytest.mark.asyncio
async def test_mask_request_body_masks_nested_dict():
    masked = await _mask(json.dumps({
        "config": {
            "org": "org1",
            "agent_secret": "nested-secret",
            "child": {
                "client_secret": "deep-secret",
            },
        },
    }).encode("utf-8"))
    assert "[MASKED]" in masked
    assert "nested-secret" not in masked
    assert "deep-secret" not in masked


@pytest.mark.asyncio
async def test_mask_request_body_non_json_body_returns_unreadable():
    masked = await _mask(b"not-valid-json{{")
    assert masked == "<unreadable>"


@pytest.mark.asyncio
async def test_mask_request_body_empty_body_returns_empty_dict_string():
    masked = await _mask(b"")
    assert masked == "{}"


@pytest.mark.asyncio
async def test_mask_request_body_non_dict_body_returns_empty_marker():
    masked = await _mask(b"[1, 2, 3]")
    assert masked == "<empty>"


@pytest.mark.asyncio
async def test_mask_request_body_non_sensitive_value_kept():
    masked = await _mask(json.dumps({
        "org": "org1",
        "display_name": "Friendly Agent",
    }).encode("utf-8"))
    assert "Friendly Agent" in masked
    assert "[MASKED]" not in masked


# ---------------------------------------------------------------------------
# format_claims — the masked replacement for the old full-payload JWT dump.
#
# The demo relies on these log lines to teach the OBO flow, so the tests pin
# both halves of the contract: the teaching claims stay legible, and the
# sensitive ones never appear.
# ---------------------------------------------------------------------------

_FULL_CLAIMS = {
    "sub": "b3f1c2d4-8a91-4c2e-9f00-112233445566",
    "org_id": "worklink",
    "aut": "APPLICATION_USER",
    "scope": "list_meetings create_meeting",
    "act": {"sub": "agent_7c2f9911-aaaa-bbbb"},
    "email": "jane@worklink.com",
    "iss": "https://localhost:9443/t/teamspace/oauth2/token",
    "aud": "client-abc",
    "exp": 1893456000,
}


def test_format_claims_keeps_the_teaching_claims_visible():
    out = format_claims(_FULL_CLAIMS)
    assert "org=worklink" in out
    assert "aut=APPLICATION_USER" in out
    # Scopes are the whole point of the walkthrough — never truncated.
    assert "scope=[list_meetings,create_meeting]" in out


def test_format_claims_shortens_subject_and_actor_for_correlation():
    out = format_claims(_FULL_CLAIMS)
    assert "sub=b3f1c2d4…" in out
    assert "act.sub=agent_7c…" in out
    # The full opaque identifiers are not reproduced.
    assert _FULL_CLAIMS["sub"] not in out
    assert _FULL_CLAIMS["act"]["sub"] not in out


def test_format_claims_masks_email_and_omits_unlisted_claims():
    out = format_claims(_FULL_CLAIMS)
    assert "email=j***@w***.com" in out
    assert "jane@worklink.com" not in out
    # iss/aud/exp are counted, not dumped — this is what replaced json.dumps().
    assert "(+3 more claims)" in out
    assert "client-abc" not in out
    assert "1893456000" not in out


def test_format_claims_handles_service_token_without_user_claims():
    out = format_claims({"sub": "svc", "aut": "APPLICATION", "scope": SERVICE_SCOPE})
    assert "aut=APPLICATION" in out
    assert f"scope=[{SERVICE_SCOPE}]" in out
    # No act/email present, so neither key is rendered at all.
    assert "act.sub" not in out
    assert "email" not in out


@pytest.mark.parametrize("decoded", [{}, None, "not-a-dict", {"act": "malformed"}])
def test_format_claims_never_raises_on_degenerate_input(decoded):
    # This runs on every authenticated request, so it must not be able to
    # turn a malformed token into a 500.
    assert isinstance(format_claims(decoded), str)


def test_mask_email_without_at_sign_is_fully_masked():
    assert mask_email("not-an-email") == "***"
    assert mask_email("") == "-"


def test_mask_token_leaves_short_values_alone():
    assert mask_token("abc") == "abc"
    masked = mask_token("a" * 40)
    assert masked.startswith("aaaaaaaaaa") and masked.endswith("aaaa") and "..." in masked
