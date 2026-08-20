"""Tests for the agent's OAuth state-signing key resolution.

The OBO flow signs a `state` JWT in /authorize and verifies it in /callback.
Those are different processes once the agent runs more than one worker, so the
key must be stable and shared — a generated one silently breaks the callback.
These tests pin that a key is never invented, and that a missing key surfaces
as a handled error instead of a 500 in the user's popup window.
"""
import pytest
from fastapi.testclient import TestClient

from agent.config import Settings


def _settings(state_secret: str = "", client_secret: str = "") -> Settings:
    s = Settings()
    s.STATE_SIGNING_SECRET = state_secret
    s.CLIENT_SECRET = client_secret
    return s


def test_explicit_state_signing_secret_wins():
    assert _settings("explicit-key", "client-key").state_jwt_signing_secret() == "explicit-key"


def test_falls_back_to_client_secret():
    # CLIENT_SECRET is already stable and per-deployment, so the demo needs no
    # extra configuration to have a working OBO flow.
    assert _settings("", "client-key").state_jwt_signing_secret() == "client-key"


def test_raises_rather_than_generating_a_key():
    with pytest.raises(ValueError, match="AGENT_STATE_SIGNING_SECRET"):
        _settings("", "").state_jwt_signing_secret()


def test_no_hardcoded_default_key_remains():
    # Regression guard: the old code fell back to a literal
    # "default_secret_key_123" in agent/main.py.
    import pathlib
    source = pathlib.Path("agent/main.py").read_text()
    assert "default_secret_key_123" not in source


def test_authorize_reports_misconfiguration_instead_of_crashing(monkeypatch):
    from agent import main as agent_main
    from agent.state_manager import StateManager
    from agent.store import InMemoryStore, set_store

    def _raise():
        raise ValueError("no key configured")

    monkeypatch.setattr(agent_main.settings, "state_jwt_signing_secret", _raise)
    # Seed agent credentials so the request gets past the no-agent guard and
    # actually reaches the signing-key resolution this test is about.
    set_store(InMemoryStore())
    StateManager.get_instance().set_agent_credentials("t1", "agent-1", "secret-1")
    client = TestClient(agent_main.app, raise_server_exceptions=False)

    try:
        resp = client.get("/authorize?thread_id=t1&action=book")
    finally:
        set_store(None)
    assert resp.status_code == 500
    assert "not configured to sign OAuth state" in resp.text
    # The failure is explained, not leaked as a stack trace.
    assert "Traceback" not in resp.text


def test_callback_reports_misconfiguration_instead_of_crashing(monkeypatch):
    from agent import main as agent_main

    def _raise():
        raise ValueError("no key configured")

    monkeypatch.setattr(agent_main.settings, "state_jwt_signing_secret", _raise)
    client = TestClient(agent_main.app, raise_server_exceptions=False)

    resp = client.get("/callback?code=abc&state=xyz")
    assert resp.status_code == 500
    assert "not configured to sign OAuth state" in resp.text


# --- Import-time production guard -----------------------------------------
#
# The fail-fast lives at module scope in agent/config.py, so it can only be
# exercised in a fresh interpreter. A subprocess is the honest way to test it.

_PROBE = """
import common.config as cc
cc.load_env = lambda: None          # ignore the developer's local .env
cc._ENV_LOADED = True
import agent.config
print("IMPORTED")
"""


def _probe(env: dict) -> tuple[int, str]:
    import os
    import subprocess
    import sys

    child_env = {k: v for k, v in os.environ.items() if k not in (
        "CLIENT_SECRET", "AGENT_STATE_SIGNING_SECRET", "FLASK_ENV",
    )}
    child_env.update(env)
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True, text=True, env=child_env, cwd=os.getcwd(),
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_production_without_any_signing_key_fails_at_import():
    code, output = _probe({"FLASK_ENV": "production"})
    assert code != 0
    assert "cannot sign OAuth state" in output


def test_production_with_a_signing_key_imports_cleanly():
    code, output = _probe({
        "FLASK_ENV": "production",
        "AGENT_STATE_SIGNING_SECRET": "a-stable-shared-key",
    })
    assert code == 0, output
    assert "IMPORTED" in output


def test_non_production_without_a_key_still_imports():
    # The demo must keep booting on a bare checkout; the error only surfaces
    # if someone actually starts an OBO flow.
    code, output = _probe({})
    assert code == 0, output
    assert "IMPORTED" in output


# --- /authorize robustness on public input ---------------------------------
#
# /authorize carries no service token by design: the browser opens it in a
# popup, which cannot send custom headers. So it is reachable by anyone with
# any thread_id, and must degrade rather than 500.


def test_authorize_with_unknown_thread_returns_a_handled_error(monkeypatch):
    from agent import main as agent_main
    from agent.store import InMemoryStore, set_store

    set_store(InMemoryStore())  # no agent credentials for any thread
    try:
        client = TestClient(agent_main.app, raise_server_exceptions=False)
        resp = client.get("/authorize?thread_id=never-seen&action=book")
        assert resp.status_code == 400, resp.status_code
        assert "no AI agent associated" in resp.text
        assert "Traceback" not in resp.text
    finally:
        set_store(None)


def test_authorize_with_unknown_action_is_a_400_not_a_500():
    from agent import main as agent_main
    from agent.store import InMemoryStore, set_store

    set_store(InMemoryStore())
    try:
        client = TestClient(agent_main.app, raise_server_exceptions=False)
        resp = client.get("/authorize?thread_id=t&action=drop-database")
        assert resp.status_code == 400
    finally:
        set_store(None)
