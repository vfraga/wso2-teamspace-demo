import re
from datetime import date, datetime
from typing import Optional
from zoneinfo import available_timezones

from pydantic import BaseModel, ConfigDict, Field, field_validator

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_FORBIDDEN_TOPIC_CHARS = set("<>&\"")


class MeetingCreate(BaseModel):
    topic: str = Field(..., max_length=500)
    date: str
    start_time: str
    duration: int = Field(..., ge=1, le=480)
    time_zone: str

    @field_validator("topic")
    @classmethod
    def _topic_safe(cls, v: str) -> str:
        bad = sorted(c for c in v if c in _FORBIDDEN_TOPIC_CHARS or c == "'")
        if bad:
            raise ValueError(f"topic contains forbidden characters: {bad}")
        return v

    @field_validator("date")
    @classmethod
    def _date_iso(cls, v: str) -> str:
        if not _ISO_DATE_RE.match(v):
            raise ValueError("date must be ISO format YYYY-MM-DD")
        try:
            date.fromisoformat(v)
        except ValueError as e:
            raise ValueError(f"date is not a valid calendar date: {e}") from e
        return v

    @field_validator("start_time")
    @classmethod
    def _start_time_hhmm(cls, v: str) -> str:
        if not _ISO_TIME_RE.match(v):
            raise ValueError("start_time must be HH:MM (24-hour)")
        return v

    @field_validator("time_zone")
    @classmethod
    def _time_zone_known(cls, v: str) -> str:
        if v not in available_timezones():
            raise ValueError(f"unknown IANA time zone: {v}")
        return v

    @field_validator("duration", mode="before")
    @classmethod
    def _duration_coerce(cls, v):
        if isinstance(v, str):
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise ValueError("duration must be an integer number of minutes") from None
        return v


class MeetingResponse(BaseModel):
    id: str
    org: str
    created_at: datetime
    topic: str
    date: str
    start_time: str
    duration: str
    time_zone: str
    user_id: str
    email_address: Optional[str] = None
    actor_user_id: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class AgentConfigCreate(BaseModel):
    org: str
    agent_id: str
    agent_secret: str
    display_name: str
    description: Optional[str] = None
    gemini_api_key: Optional[str] = None
    org_client_id: Optional[str] = None
    org_client_secret: Optional[str] = None
    custom_prompt: Optional[str] = None


class AgentConfigResponse(BaseModel):
    org: str
    agent_id: str
    agent_secret: str
    display_name: str
    description: Optional[str] = None
    gemini_api_key: Optional[str] = None
    org_client_id: Optional[str] = None
    org_client_secret: Optional[str] = None
    custom_prompt: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AgentConfigSecretResponse(AgentConfigResponse):
    """Explicit variant for endpoints that need to confirm secrets are present.

    Currently identical to AgentConfigResponse — kept as a marker for future
    scope-gating if a public API surface is added.
    """

    pass


class PersonalizationUpsert(BaseModel):
    org: str
    logo_url: Optional[str] = None
    logo_alt_text: Optional[str] = None
    favicon_url: Optional[str] = None
    primary_color: Optional[str] = None
    secondary_color: Optional[str] = None


class PersonalizationResponse(PersonalizationUpsert):
    model_config = ConfigDict(from_attributes=True)


class OrganizationPlanUpsert(BaseModel):
    org: str
    plan: str


class OrganizationPlanResponse(OrganizationPlanUpsert):
    model_config = ConfigDict(from_attributes=True)

