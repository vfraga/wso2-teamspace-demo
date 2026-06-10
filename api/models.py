import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, DateTime, func
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Meeting(Base):
    __tablename__ = "meetings"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    org = Column(String(200), nullable=False, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    topic = Column(String(200), nullable=False)
    date = Column(String(20), nullable=False)
    start_time = Column(String(10), nullable=False)
    duration = Column(String(10), nullable=False)
    time_zone = Column(String(50), nullable=False)
    user_id = Column(String(200), nullable=False)
    email_address = Column(String(200), nullable=True)
    actor_user_id = Column(String(200), nullable=True)


class AgentConfig(Base):
    __tablename__ = "agent_configs"

    org = Column(String(200), primary_key=True)
    agent_id = Column(String(200), nullable=False)
    agent_secret = Column(String(200), nullable=False)
    display_name = Column(String(200), nullable=False)
    description = Column(String(500), nullable=True)
    gemini_api_key = Column(String(200), nullable=True)
    org_client_id = Column(String(200), nullable=True)
    org_client_secret = Column(String(500), nullable=True)
    custom_prompt = Column(String(2000), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class Personalization(Base):
    __tablename__ = "personalizations"

    org = Column(String(200), primary_key=True)
    logo_url = Column(String(500), nullable=True)
    logo_alt_text = Column(String(200), nullable=True)
    favicon_url = Column(String(500), nullable=True)
    primary_color = Column(String(20), nullable=True)
    secondary_color = Column(String(20), nullable=True)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class OrganizationPlan(Base):
    __tablename__ = "organization_plans"

    org = Column(String(200), primary_key=True)
    plan = Column(String(50), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

