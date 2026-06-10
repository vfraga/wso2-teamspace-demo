import json

import pytest

from common.fastapi_errors import mask_request_body


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
