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


# ─── Default agent name ──────────────────────────────────────────────────────
# DEFAULT_AGENT_NAME had five hand-typed copies across schemas.py, main.py and
# gemini_agent.py. That is the same shape as the CommonDefaults.IS_VERIFY_TLS
# bug: a constant holding the right value while the code quietly used its own.
# These assert against the CONSTANT, not the string, so editing
# common/constants.py can never leave a call site behind. The one test that
# pins the literal value on purpose is test_store.py's — changing what users
# actually see should stay a visible, deliberate edit.

def test_chat_request_default_agent_name_comes_from_the_constant():
    from agent.schemas import ChatRequest
    from common.constants import DEFAULT_AGENT_NAME

    req = ChatRequest(thread_id="t1", message="hi")
    assert req.agent_name == DEFAULT_AGENT_NAME


@pytest.mark.parametrize("func_name", ["run_agent", "run_agent_stream"])
def test_gemini_agent_signature_defaults_come_from_the_constant(func_name):
    import inspect

    from agent import gemini_agent
    from common.constants import DEFAULT_AGENT_NAME

    sig = inspect.signature(getattr(gemini_agent, func_name))
    assert sig.parameters["agent_name"].default == DEFAULT_AGENT_NAME


def test_no_module_retypes_the_agent_name_literal():
    # common/constants.py is the only place the string itself may appear.
    from pathlib import Path

    import agent
    import common.constants

    owner = Path(common.constants.__file__).resolve()
    offenders = [
        str(path)
        for path in Path(agent.__file__).resolve().parent.glob("*.py")
        if path != owner and '"Worklink Assistant"' in path.read_text()
    ]
    assert offenders == [], f"agent-name literal re-typed in: {offenders}"
