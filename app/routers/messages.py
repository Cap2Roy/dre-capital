"""Messaging router: SMS/email templates, send SMS, message history, inbound webhook."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Lead, MessageChannel, MessageLog, MessageTemplate, Phone
from app.schemas import (
    MessageLogOut,
    MessageTemplateCreate,
    MessageTemplateOut,
    MessageTemplateUpdate,
    SendSmsRequest,
)
from app.services.messaging import handle_inbound_sms, send_sms

router = APIRouter(prefix="/api/messages", tags=["messages"])


# ── Templates ────────────────────────────────────────────────────────────────

@router.get("/templates", response_model=list[MessageTemplateOut])
def list_templates(channel: str | None = None, active: bool | None = None, db: Session = Depends(get_db)):
    q = select(MessageTemplate).order_by(MessageTemplate.created_at.desc())
    if channel:
        try:
            q = q.where(MessageTemplate.channel == MessageChannel(channel))
        except ValueError:
            raise HTTPException(400, "channel must be 'sms' or 'email'")
    if active is not None:
        q = q.where(MessageTemplate.active == active)
    return db.execute(q).scalars().all()


@router.post("/templates", response_model=MessageTemplateOut, status_code=201)
def create_template(payload: MessageTemplateCreate, db: Session = Depends(get_db)):
    try:
        channel = MessageChannel(payload.channel)
    except ValueError:
        raise HTTPException(400, "channel must be 'sms' or 'email'")
    row = MessageTemplate(
        name=payload.name,
        channel=channel,
        category=payload.category,
        subject=payload.subject,
        body=payload.body,
        active=payload.active,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.patch("/templates/{template_id}", response_model=MessageTemplateOut)
def update_template(template_id: str, payload: MessageTemplateUpdate, db: Session = Depends(get_db)):
    row = db.get(MessageTemplate, template_id)
    if not row:
        raise HTTPException(404, "Template not found")
    data = payload.model_dump(exclude_unset=True)
    if "channel" in data:
        try:
            data["channel"] = MessageChannel(data["channel"])
        except (ValueError, KeyError):
            raise HTTPException(400, "channel must be 'sms' or 'email'")
    for k, v in data.items():
        setattr(row, k, v)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/templates/{template_id}")
def delete_template(template_id: str, db: Session = Depends(get_db)):
    row = db.get(MessageTemplate, template_id)
    if not row:
        raise HTTPException(404, "Template not found")
    db.delete(row)
    db.commit()
    return {"ok": True}


# ── Send / history ───────────────────────────────────────────────────────────

@router.post("/send", response_model=MessageLogOut)
def send_message(payload: SendSmsRequest, db: Session = Depends(get_db)):
    """Send a manual SMS to a lead's phone (body or template)."""
    phone = db.get(Phone, payload.phone_id)
    if not phone:
        raise HTTPException(404, "Phone not found")
    lead = db.get(Lead, phone.lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    template = None
    if payload.template_id:
        template = db.get(MessageTemplate, payload.template_id)
        if not template:
            raise HTTPException(404, "Template not found")
    try:
        entry = send_sms(db, lead, phone, body=payload.body, template=template)
        db.commit()
        db.refresh(entry)
        return entry
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(502, str(exc))


@router.get("/history", response_model=list[MessageLogOut])
def message_history(lead_id: str | None = None, phone_id: str | None = None, db: Session = Depends(get_db)):
    q = select(MessageLog).order_by(MessageLog.created_at.desc())
    if lead_id:
        q = q.where(MessageLog.lead_id == lead_id)
    if phone_id:
        q = q.where(MessageLog.phone_id == phone_id)
    return db.execute(q.limit(200)).scalars().all()


# ── Twilio inbound webhook (public — no session cookie) ────────────────────

@router.post("/inbound")
async def inbound_sms(request: Request, db: Session = Depends(get_db)):
    """Twilio message webhook.  Logs inbound SMS; STOP keywords opt the number out."""
    form = await request.form()
    from_number = form.get("From", "")
    body = form.get("Body", "")
    to_number = form.get("To", "")
    result = handle_inbound_sms(db, from_number or "", body or "", to_number=to_number or "")
    db.commit()
    # Twilio responds with an empty 200 to acknowledge receipt.
    return result