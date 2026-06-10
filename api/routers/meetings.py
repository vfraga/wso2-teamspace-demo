import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from api.auth import UserInfo, require_scope
from api.database import get_db
from api.models import Meeting
from api.schemas import MeetingCreate, MeetingResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/", response_model=list[MeetingResponse])
def list_meetings(
    user: UserInfo = Depends(require_scope("list_meetings")),
    db: Session = Depends(get_db),
) -> list[Meeting]:
    meetings = db.query(Meeting).filter(Meeting.org == user.org).all()
    logger.info("Listed %d meetings for org=%s, user=%s", len(meetings), user.org, user.user_id)
    return meetings


@router.post("/", response_model=MeetingResponse, status_code=201)
def create_meeting(
    data: MeetingCreate,
    user: UserInfo = Depends(require_scope("create_meeting")),
    db: Session = Depends(get_db),
) -> Meeting:
    meeting = Meeting(
        org=user.org,
        topic=data.topic,
        date=data.date,
        start_time=data.start_time,
        duration=data.duration,
        time_zone=data.time_zone,
        user_id=user.user_id,
        email_address=user.email,
        actor_user_id=user.actor_user_id or None,
    )
    db.add(meeting)
    db.commit()
    db.refresh(meeting)
    logger.info("Created meeting id=%s, topic='%s' for org=%s by user=%s (actor=%s)", meeting.id, meeting.topic, user.org, user.user_id, user.actor_user_id or "self")
    return meeting


@router.get("/{meeting_id}", response_model=MeetingResponse)
def get_meeting(
    meeting_id: str,
    user: UserInfo = Depends(require_scope("view_meeting")),
    db: Session = Depends(get_db),
) -> Meeting:
    meeting = (
        db.query(Meeting)
        .filter(Meeting.id == meeting_id, Meeting.org == user.org)
        .first()
    )
    if not meeting:
        logger.warning("Meeting not found: id=%s, org=%s", meeting_id, user.org)
        raise HTTPException(status_code=404, detail="Meeting not found")
    logger.debug("Retrieved meeting id=%s for org=%s", meeting_id, user.org)
    return meeting


@router.put("/{meeting_id}", response_model=MeetingResponse)
def update_meeting(
    meeting_id: str,
    data: MeetingCreate,
    user: UserInfo = Depends(require_scope("update_meeting")),
    db: Session = Depends(get_db),
) -> Meeting:
    meeting = (
        db.query(Meeting)
        .filter(Meeting.id == meeting_id, Meeting.org == user.org)
        .first()
    )
    if not meeting:
        logger.warning("Update failed — meeting not found: id=%s, org=%s", meeting_id, user.org)
        raise HTTPException(status_code=404, detail="Meeting not found")
    for field, value in data.model_dump().items():
        setattr(meeting, field, value)
    db.commit()
    db.refresh(meeting)
    logger.info("Updated meeting id=%s for org=%s by user=%s", meeting_id, user.org, user.user_id)
    return meeting


@router.delete("/{meeting_id}", status_code=204)
def delete_meeting(
    meeting_id: str,
    user: UserInfo = Depends(require_scope("delete_meeting")),
    db: Session = Depends(get_db),
) -> None:
    meeting = (
        db.query(Meeting)
        .filter(Meeting.id == meeting_id, Meeting.org == user.org)
        .first()
    )
    if not meeting:
        logger.warning("Delete failed — meeting not found: id=%s, org=%s", meeting_id, user.org)
        raise HTTPException(status_code=404, detail="Meeting not found")
    db.delete(meeting)
    db.commit()
    logger.info("Deleted meeting id=%s for org=%s by user=%s", meeting_id, user.org, user.user_id)

