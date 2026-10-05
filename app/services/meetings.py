"""Meeting reminders — fire an SMS to the lead before a scheduled meeting.

Notifications are manual-trigger friendly: ``send_meeting_reminder`` is called
either by the scheduler loop (for upcoming meetings whose reminder window has
opened) or by an explicit endpoint.  Honors DNC/opt-out via ``send_sms``.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Meeting, MessageTemplate
from app.services.messaging import send_sms


def find_due_reminders(db: Session, now: Optional[datetime] = None) -> list[Meeting]:
    """Meetings whose reminder window has opened and haven't been sent yet.

    A reminder is due when ``now >= scheduled_at - reminder_minutes`` and the
    meeting hasn't passed by more than its window (so a missed run still fires
    shortly after, but not long after the meeting started).
    """
    now = now or datetime.now()
    return db.execute(
        select(Meeting).where(
            Meeting.reminder_sent == False,
            Meeting.scheduled_at <= now + timedelta(hours=24),  # only near-term
        )
    ).scalars().all()


def _reminder_is_due(meeting: Meeting, now: datetime) -> bool:
    window_open_at = meeting.scheduled_at - timedelta(minutes=meeting.reminder_minutes)
    return now >= window_open_at


def _reminder_template(db: Session) -> Optional[MessageTemplate]:
    return db.execute(
        select(MessageTemplate).where(
            MessageTemplate.category == "meeting_reminder",
            MessageTemplate.active == True,
        ).order_by(MessageTemplate.created_at.desc()).limit(1)
    ).scalars().first()


def send_meeting_reminder(db: Session, meeting: Meeting, sent_by: Optional[str] = None) -> dict:
    """Send the meeting reminder SMS for one meeting.

    Returns ``{"sent": bool, "reason": str}``.  Non-fatal: if there's no
    callable phone or no template, the meeting is marked reminded so the loop
    doesn't keep retrying (the operator can still send manually).
    """
    lead = meeting.lead
    if lead is None:
        meeting.reminder_sent = True
        db.flush()
        return {"sent": False, "reason": "no lead"}

    phone = next((p for p in lead.phones if not p.dnc_flagged and not p.opted_out), None)
    tpl = _reminder_template(db)
    if phone is None or tpl is None:
        meeting.reminder_sent = True
        db.flush()
        return {"sent": False, "reason": "no callable phone or no reminder template"}

    try:
        send_sms(db, lead, phone, template=tpl, sent_by=sent_by)
        meeting.reminder_sent = True
        db.flush()
        return {"sent": True, "reason": "ok"}
    except (ValueError, RuntimeError):
        # Don't block — mark sent to avoid retry storms; operator can resend.
        meeting.reminder_sent = True
        db.flush()
        return {"sent": False, "reason": "send failed (opted-out or unconfigured)"}


def process_due_reminders(db: Session, now: Optional[datetime] = None) -> dict:
    """Send reminders for every meeting whose window has opened. Batch entry."""
    now = now or datetime.now()
    sent = skipped = 0
    for m in find_due_reminders(db, now):
        if not _reminder_is_due(m, now):
            continue
        res = send_meeting_reminder(db, m)
        if res["sent"]:
            sent += 1
        else:
            skipped += 1
    if sent or skipped:
        db.commit()
    return {"sent": sent, "skipped": skipped}
