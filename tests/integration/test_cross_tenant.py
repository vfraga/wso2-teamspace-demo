import pytest
import uuid

from api.auth import UserInfo, get_current_user
from api.main import app as api_app
from api.models import (
    AgentConfig,
    Meeting,
    OrganizationPlan,
    Personalization,
)


def _user_for_org(org: str, scopes: str = "") -> UserInfo:
    return UserInfo({
        "sub": f"user-of-{org}",
        "user_org": org,
        "email": f"someone@{org}.com",
        "scope": scopes,
        "groups": [],
    })


def _seed_meeting(db, org: str, topic: str) -> str:
    meeting = Meeting(
        id=str(uuid.uuid4()),
        org=org,
        topic=topic,
        date="2026-07-01",
        start_time="10:00",
        duration="30",
        time_zone="UTC",
        user_id=f"user-of-{org}",
    )
    db.add(meeting)
    db.commit()
    db.refresh(meeting)
    return meeting.id


def _seed_agent_config(db, org: str) -> None:
    db.add(AgentConfig(
        org=org,
        agent_id=f"agent-{org}",
        agent_secret="shh",
        display_name=f"{org} Agent",
    ))
    db.commit()


def _seed_personalization(db, org: str) -> None:
    db.add(Personalization(
        org=org,
        logo_url=f"https://example.com/{org}.png",
        primary_color="#000000",
    ))
    db.commit()


def _seed_plan(db, org: str, plan: str = "basic") -> None:
    db.add(OrganizationPlan(org=org, plan=plan))
    db.commit()


def test_cross_tenant_meetings_filtered_out(api_client, db_session):
    _seed_meeting(db_session, "org_a", "Org A meeting")

    def override():
        return _user_for_org("org_b", scopes="list_meetings create_meeting")
    api_app.dependency_overrides[get_current_user] = override

    try:
        resp = api_client.get("/meetings/")
        assert resp.status_code == 200
        assert resp.json() == []
    finally:
        api_app.dependency_overrides.pop(get_current_user, None)


def test_cross_tenant_agent_config_403(api_client, db_session):
    _seed_agent_config(db_session, "org_a")

    def override():
        return _user_for_org("org_b", scopes="view_agent_config manage_agent_config")
    api_app.dependency_overrides[get_current_user] = override

    try:
        resp = api_client.get("/agent-config/org/org_a")
        assert resp.status_code == 403
    finally:
        api_app.dependency_overrides.pop(get_current_user, None)


def test_cross_tenant_personalization_403(api_client, db_session):
    _seed_personalization(db_session, "org_a")

    def override():
        return _user_for_org("org_b", scopes="create_basic_branding delete_branding")
    api_app.dependency_overrides[get_current_user] = override

    try:
        resp = api_client.get("/personalization/org/org_a")
        assert resp.status_code == 403
    finally:
        api_app.dependency_overrides.pop(get_current_user, None)


def test_cross_tenant_delete_meeting_404(api_client, db_session):
    meeting_id = _seed_meeting(db_session, "org_a", "Org A meeting")

    def override():
        return _user_for_org("org_b", scopes="list_meetings delete_meeting")
    api_app.dependency_overrides[get_current_user] = override

    try:
        resp = api_client.delete(f"/meetings/{meeting_id}")
        assert resp.status_code == 404
    finally:
        api_app.dependency_overrides.pop(get_current_user, None)


def test_same_org_can_see_own_meetings(api_client, db_session):
    _seed_meeting(db_session, "org_a", "Org A meeting")

    def override():
        return _user_for_org("org_a", scopes="list_meetings create_meeting")
    api_app.dependency_overrides[get_current_user] = override

    try:
        resp = api_client.get("/meetings/")
        assert resp.status_code == 200
        meetings = resp.json()
        assert any(m["topic"] == "Org A meeting" for m in meetings)
    finally:
        api_app.dependency_overrides.pop(get_current_user, None)
