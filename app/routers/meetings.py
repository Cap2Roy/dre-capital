"""Meetings router: schedule lead appointments + send reminder notifications."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Meeting
from app.schemas import MeetingCreate, MeetingOut
from app.services.meetings import process_due_reminders, send_meeting_reminder

router = APIRouter(prefix="/api/meetings", tags=["meetings"])


@router.get("", response_model=list[MeetingOut])
def list_meetings(upcoming: bool = False, db: Session = Depends(get_db)):
    """List meetings, optionally only upcoming (scheduled_at >= now)."""
    q = select(Meeting).order_by(Meeting.scheduled_at.asc())
    if upcoming:
        q = q.where(Meeting.scheduled_at >= datetime.now())
    return db.execute(q).scalars().all()


@router.post("", response_model=MeetingOut, status_code=201)
def create_meeting(payload: MeetingCreate, db: Session = Depends(get_db)):
    meeting = Meeting(**payload.model_dump())
    db.add(meeting)
    db.commit()
    db.refresh(meeting)
    return meeting


@router.patch("/{meeting_id}", response_model=MeetingOut)
def update_meeting(meeting_id: str, payload: MeetingCreate, db: Session = Depends(get_db)):
    meeting = db.get(Meeting, meeting_id)
    if not meeting:
        raise HTTPException(404, "Meeting not found")
    for k, v in payload.model_dump().items():
        setattr(meeting, k, v)
    # Moving a meeting reopens its reminder window.
    meeting.reminder_sent = False
    db.commit()
    db.refresh(meeting)
    return meeting


@router.delete("/{meeting_id}")
def delete_meeting(meeting_id: str, db: Session = Depends(get_db)):
    meeting = db.get(Meeting, meeting_id)
    if not meeting:
        raise HTTPException(404, "Meeting not found")
    db.delete(meeting)
    db.commit()
    return {"ok": True}


@router.post("/{meeting_id}/remind")
def remind(meeting_id: str, db: Session = Depends(get_db)):
    """Send the reminder for this meeting now (manual trigger)."""
    meeting = db.get(Meeting, meeting_id)
    if not meeting:
        raise HTTPException(404, "Meeting not found")
    res = send_meeting_reminder(db, meeting)
    db.commit()
    return {"meeting_id": meeting_id, **res}


@router.post("/reminders/process")
def process_reminders(db: Session = Depends(get_db)):
    """Fire all due meeting reminders now (also run by the scheduler)."""
    return process_due_reminders(db)