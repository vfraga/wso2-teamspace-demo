import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from api.auth import UserInfo, get_current_user
from api.database import get_db
from api.models import OrganizationPlan
from api.schemas import OrganizationPlanUpsert, OrganizationPlanResponse


logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/org/{org_id}", response_model=OrganizationPlanResponse)
def get_plan(org_id: str, db: Session = Depends(get_db)):
    p = db.query(OrganizationPlan).filter(OrganizationPlan.org == org_id).first()
    if not p:
        logger.debug("No plan found for org=%s", org_id)
        raise HTTPException(status_code=404, detail="No plan found")
    logger.debug("Retrieved organization plan for org=%s: %s", org_id, p.plan)
    return p


@router.post("/", response_model=OrganizationPlanResponse)
def upsert_plan(
    data: OrganizationPlanUpsert,
    user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if data.org != user.org:
        raise HTTPException(status_code=403, detail="Forbidden: organization mismatch")
    existing = (
        db.query(OrganizationPlan).filter(OrganizationPlan.org == data.org).first()
    )
    if existing:
        existing.plan = data.plan
        logger.info("Updated plan for org=%s to %s by user=%s", data.org, data.plan, user.user_id)
    else:
        existing = OrganizationPlan(**data.model_dump())
        db.add(existing)
        logger.info("Created plan for org=%s as %s by user=%s", data.org, data.plan, user.user_id)
    db.commit()
    db.refresh(existing)
    return existing
