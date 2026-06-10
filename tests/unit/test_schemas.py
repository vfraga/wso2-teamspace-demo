import pytest
from pydantic import ValidationError

from api.schemas import MeetingCreate, AgentConfigCreate, PersonalizationUpsert
from agent.schemas import ChatRequest, ChatResponse

def test_meeting_create_validation():
    # Correct data
    data = {
        "topic": "Sprint Planning",
        "date": "2026-05-22",
        "start_time": "10:00",
        "duration": "45",
        "time_zone": "America/Sao_Paulo"
    }
    meeting = MeetingCreate(**data)
    assert meeting.topic == "Sprint Planning"
    assert meeting.duration == 45
    
    # Missing required field topic
    bad_data = data.copy()
    del bad_data["topic"]
    with pytest.raises(ValidationError):
        MeetingCreate(**bad_data)

def test_agent_config_create_validation():
    # Correct data
    data = {
        "org": "numbainfinite",
        "agent_id": "agent-123",
        "agent_secret": "my-secret-key",
        "display_name": "Numbainfinite Agent"
    }
    config = AgentConfigCreate(**data)
    assert config.agent_id == "agent-123"
    assert config.org_client_id is None
    
    # Missing org
    bad_data = data.copy()
    del bad_data["org"]
    with pytest.raises(ValidationError):
        AgentConfigCreate(**bad_data)

def test_personalization_upsert_validation():
    # Optional fields can be empty/None
    data = {
        "org": "numbainfinite",
        "primary_color": "#ff0000"
    }
    upsert = PersonalizationUpsert(**data)
    assert upsert.org == "numbainfinite"
    assert upsert.logo_url is None

def test_chat_request_validation():
    # Correct data
    data = {
        "thread_id": "thread-abc",
        "message": "Hello, bot!"
    }
    req = ChatRequest(**data)
    assert req.thread_id == "thread-abc"
    assert req.message == "Hello, bot!"
    assert req.org_name == ""  # defaults to empty string

    # Missing thread_id
    with pytest.raises(ValidationError):
        ChatRequest(message="Help")


@pytest.mark.parametrize("bad_date", [
    "not-a-date",
    "2026/05/25",
    "2026-13-01",
    "2026-02-30",
    "abcdefg",
])
def test_meeting_create_rejects_garbage_dates(bad_date):
    data = {
        "topic": "Bad Date Meeting",
        "date": bad_date,
        "start_time": "10:00",
        "duration": "30",
        "time_zone": "UTC",
    }
    with pytest.raises(ValidationError):
        MeetingCreate(**data)


@pytest.mark.parametrize("bad_time", [
    "25:00",
    "10:60",
    "10",
    "10am",
    "ten-thirty",
])
def test_meeting_create_rejects_invalid_times(bad_time):
    data = {
        "topic": "Bad Time Meeting",
        "date": "2026-05-25",
        "start_time": bad_time,
        "duration": "30",
        "time_zone": "UTC",
    }
    with pytest.raises(ValidationError):
        MeetingCreate(**data)


@pytest.mark.parametrize("bad_tz", [
    "Atlantis/Lemuria",
    "not-a-zone",
    "EST5EDT-bogus",
])
def test_meeting_create_rejects_bogus_timezones(bad_tz):
    data = {
        "topic": "Bad TZ Meeting",
        "date": "2026-05-25",
        "start_time": "10:00",
        "duration": "30",
        "time_zone": bad_tz,
    }
    with pytest.raises(ValidationError):
        MeetingCreate(**data)


def test_meeting_create_rejects_oversized_topic():
    oversized = "A" * 501
    data = {
        "topic": oversized,
        "date": "2026-05-25",
        "start_time": "10:00",
        "duration": "30",
        "time_zone": "UTC",
    }
    with pytest.raises(ValidationError):
        MeetingCreate(**data)


def test_meeting_create_accepts_max_length_topic():
    boundary = "A" * 500
    data = {
        "topic": boundary,
        "date": "2026-05-25",
        "start_time": "10:00",
        "duration": "30",
        "time_zone": "UTC",
    }
    meeting = MeetingCreate(**data)
    assert meeting.topic == boundary
