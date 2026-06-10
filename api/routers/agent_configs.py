import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from api.auth import UserInfo, get_current_user, require_internal_secret, require_scope
from api.database import get_db
from api.models import AgentConfig
from api.schemas import AgentConfigCreate, AgentConfigResponse


logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/org/{org_id}", response_model=AgentConfigResponse)
def get_agent_config(
    org_id: str,
    user: Optional[UserInfo] = Depends(get_current_user),
    x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret"),
    db: Session = Depends(get_db),
):
    """Read the agent configuration for an organization.

    Auth priority:
    1. `X-Internal-Secret` is the trust gate. When present and valid, the caller
       is a trusted service (webapp or agent). The user JWT — if also present —
       is recorded for audit/observability only, NOT used for scope checks.
       This is the path used by the webapp's chat send_message and send_stream
       routes so non-admins can start the OBO flow (which needs agent_id/
       agent_secret to obtain the actor_token).
    2. If only a user JWT is present, the `view_agent_config` or
       `view_agent_config_agent` scope is required, and the JWT's `org` claim
       must match the path `org_id`. This is the legacy admin-UI path.
    3. If neither is present, the call is rejected (401).

    The org path parameter is the tenant boundary for the M2M path. The
    internal secret proves the caller is a trusted service; we trust it to
    know which org it wants (e.g. the agent fetches its own config by the
    org it already knows it's serving).
    """
    if x_internal_secret is not None:
        require_internal_secret(x_internal_secret)
        actor = f"user {user.user_id}" if user is not None else "M2M service"
        auth_source = f"internal-secret trusted, acting as {actor}"
    elif user is not None:
        scopes = set(user.scopes)
        if "view_agent_config" not in scopes and "view_agent_config_agent" not in scopes:
            raise HTTPException(
                status_code=403,
                detail="Missing required scope: view_agent_config or view_agent_config_agent",
            )
        if org_id != user.org:
            raise HTTPException(status_code=403, detail="Forbidden: organization mismatch")
        auth_source = f"user {user.user_id} (scope-based)"
    else:
        raise HTTPException(
            status_code=401,
            detail="Authentication required: provide X-Internal-Secret header or user JWT with view_agent_config scope",
        )

    config = db.query(AgentConfig).filter(AgentConfig.org == org_id).first()
    if not config:
        logger.info("Agent config not found for org=%s (requested by %s)", org_id, auth_source)
        raise HTTPException(status_code=404, detail="No agent config found")
    logger.info("Agent config served for org=%s to %s", org_id, auth_source)
    return config


@router.post("/", response_model=AgentConfigResponse, status_code=201)
def create_agent_config(
    data: AgentConfigCreate,
    user: UserInfo = Depends(require_scope("manage_agent_config")),
    db: Session = Depends(get_db),
):
    if data.org != user.org:
        raise HTTPException(status_code=403, detail="Forbidden: organization mismatch")
    existing = db.query(AgentConfig).filter(AgentConfig.org == data.org).first()
    if existing:
        for field, value in data.model_dump().items():
            setattr(existing, field, value)
        logger.info("Updated agent config for org=%s", data.org)
    else:
        existing = AgentConfig(**data.model_dump())
        db.add(existing)
        logger.info("Created agent config for org=%s", data.org)
    db.commit()
    db.refresh(existing)
    return existing


@router.delete("/org/{org_id}", status_code=204)
def delete_agent_config(
    org_id: str,
    user: UserInfo = Depends(require_scope("manage_agent_config")),
    db: Session = Depends(get_db),
):
    if org_id != user.org:
        raise HTTPException(status_code=403, detail="Forbidden: organization mismatch")
    db.query(AgentConfig).filter(AgentConfig.org == org_id).delete()
    db.commit()
    logger.info("Deleted agent config for org=%s by user=%s", org_id, user.user_id)
