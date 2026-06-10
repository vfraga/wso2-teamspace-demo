from typing import Optional
from pydantic import BaseModel


class ChatRequest(BaseModel):
    thread_id: str
    message: str
    org_name: str = ""
    agent_id: Optional[str] = None
    agent_secret: Optional[str] = None
    gemini_api_key: Optional[str] = None
    custom_prompt: Optional[str] = None
    language: Optional[str] = "en"
    agent_name: Optional[str] = "Worklink Assistant"


class ChatResponse(BaseModel):
    message: str
    state: str = "IDLE"
    authorization_url: Optional[str] = None
    obo_jwt: Optional[str] = None
    agent_jwt: Optional[str] = None

