import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from api.auth import UserInfo, require_current_user, require_scope
from api.database import get_db
from api.models import Personalization
from api.schemas import PersonalizationUpsert, PersonalizationResponse


logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/org/{org_id}", response_model=PersonalizationResponse)
def get_personalization(
    org_id: str,
    user: UserInfo = Depends(require_current_user),
    db: Session = Depends(get_db),
):
    if org_id != user.org:
        raise HTTPException(status_code=403, detail="Forbidden: organization mismatch")
    p = db.query(Personalization).filter(Personalization.org == org_id).first()
    if not p:
        logger.debug("No personalization found for org=%s", org_id)
        raise HTTPException(status_code=404, detail="No personalization found")
    logger.debug("Retrieved personalization for org=%s", org_id)
    return p


@router.post("/", response_model=PersonalizationResponse)
def upsert_personalization(
    data: PersonalizationUpsert,
    user: UserInfo = Depends(require_scope("create_basic_branding")),
    db: Session = Depends(get_db),
):
    if data.org != user.org:
        raise HTTPException(status_code=403, detail="Forbidden: organization mismatch")
    existing = (
        db.query(Personalization).filter(Personalization.org == data.org).first()
    )
    if existing:
        for field, value in data.model_dump().items():
            setattr(existing, field, value)
        logger.info("Updated personalization for org=%s by user=%s", data.org, user.user_id)
    else:
        existing = Personalization(**data.model_dump())
        db.add(existing)
        logger.info("Created personalization for org=%s by user=%s", data.org, user.user_id)
    db.commit()
    db.refresh(existing)
    return existing


@router.delete("/org/{org_id}", status_code=204)
def delete_personalization_by_org(
    org_id: str,
    user: UserInfo = Depends(require_scope("delete_branding")),
    db: Session = Depends(get_db),
):
    if org_id != user.org:
        raise HTTPException(status_code=403, detail="Forbidden: organization mismatch")
    db.query(Personalization).filter(Personalization.org == org_id).delete()
    db.commit()
    logger.info("Deleted personalization for org=%s by user=%s", org_id, user.user_id)
