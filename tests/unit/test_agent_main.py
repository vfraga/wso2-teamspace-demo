import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient
from agent.main import app as agent_app

def test_agent_main_app_structure():
    client = TestClient(agent_app)
    # A GET request to /chat should return 405 Method Not Allowed
    resp = client.get("/chat")
    assert resp.status_code == 405

def test_chat_route_validation():
    client = TestClient(agent_app)
    # No X-Service-Authorization service token: rejected before the body is
    # even validated. 401 (unauthenticated) rather than the old 403, because
    # the caller now presents a credential we can identify rather than a
    # shared secret we can only compare.
    resp = client.post("/chat", json={"thread_id": "t1", "message": "hello", "org_name": "org"})
    assert resp.status_code == 401
    assert "X-Service-Authorization" in resp.json()["detail"]
