import pytest
from fastapi import HTTPException

from api.auth import UserInfo, require_scope


def _user(scope_string: str) -> UserInfo:
    return UserInfo({
        "sub": "u1",
        "user_org": "org1",
        "email": "u@org.com",
        "scope": scope_string,
        "groups": [],
    })


def test_require_scope_user_scope_passes():
    checker = require_scope("create_meeting")
    user = _user("create_meeting")
    assert checker(user=user) is user


def test_require_scope_agent_scope_passes():
    checker = require_scope("create_meeting")
    user = _user("create_meeting_agent")
    assert checker(user=user) is user


def test_require_scope_unrelated_scope_yields_403():
    checker = require_scope("create_meeting")
    user = _user("list_meetings delete_meeting")
    with pytest.raises(HTTPException) as exc_info:
        checker(user=user)
    assert exc_info.value.status_code == 403
    assert "create_meeting" in exc_info.value.detail


def test_require_scope_no_scopes_yields_403():
    checker = require_scope("create_meeting")
    user = _user("")
    with pytest.raises(HTTPException) as exc_info:
        checker(user=user)
    assert exc_info.value.status_code == 403
