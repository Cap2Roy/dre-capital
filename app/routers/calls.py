"""Calls router: click-to-call + outcome logging + Twilio status callback."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Call, CallOutcome, Lead, Phone
from app.schemas import CallInitiateRequest, CallLogRequest, CallOut, NoAnswerSmsRequest
from app.services.calling import initiate_call, log_call_outcome
from app.services.messaging import send_sms
from app.services.queue import get_next_lead_to_dial
from app.services.skiptrace import is_callable

router = APIRouter(prefix="/api/calls", tags=["calls"])


@router.post("/initiate", response_model=CallOut)
def start_call(payload: CallInitiateRequest, db: Session = Depends(get_db)):
    """Initiate a compliant manual click-to-call.

    The operator clicks once per call; Twilio bridges operator → seller.
    No autodialer, no prerecorded messages.
    """
    phone = db.get(Phone, payload.phone_id)
    if not phone:
        raise HTTPException(404, "Phone not found")
    lead = db.get(Lead, phone.lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    if not is_callable(phone):
        raise HTTPException(403, "Phone is DNC-flagged or opted out. Mail only.")
    call = initiate_call(db, lead, phone, operator_number=payload.operator_number)
    db.commit()
    db.refresh(call)
    return call


@router.post("/{call_id}/log", response_model=CallOut)
def log_outcome(call_id: str, payload: CallLogRequest, db: Session = Depends(get_db)):
    """Log the outcome of a call and auto-schedule follow-up per the manual."""
    call = db.get(Call, call_id)
    if not call:
        raise HTTPException(404, "Call not found")
    try:
        outcome = CallOutcome(payload.outcome)
    except ValueError:
        raise HTTPException(400, f"Invalid outcome. Valid: {[e.value for e in CallOutcome]}")
    call = log_call_outcome(db, call, outcome, notes=payload.notes,
                           duration_seconds=payload.duration_seconds)
    db.commit()
    db.refresh(call)
    return call


@router.post("/twilio-status")
async def twilio_status(request: Request, db: Session = Depends(get_db)):
    """Twilio status callback. Updates call duration / status."""
    form = await request.form()
    call_sid = form.get("CallSid")
    status = form.get("CallStatus")
    duration = form.get("CallDuration")
    if call_sid:
        call = db.execute(select(Call).where(Call.call_sid == call_sid)).scalars().first()
        if call:
            if duration:
                try:
                    call.duration_seconds = int(duration)
                except (TypeError, ValueError):
                    pass  # malformed callback — keep prior value
            db.commit()
    return {"ok": True}


@router.post("/{call_id}/no-answer")
def no_answer_quick_click(
    call_id: str,
    payload: NoAnswerSmsRequest,
    db: Session = Depends(get_db),
):
    """Quick-click: log NO_ANSWER, fire the no-answer SMS template, return next lead.

    One click advances the operator through the dial queue: this call is
    recorded as no-answer, the seller is texted (if a no_answer template and a
    callable phone exist), and the next lead + phone to dial is returned so the
    UI can immediately offer the next call.  Missing template / opted-out phone
    is non-fatal — the call is still logged and the queue advances.
    """
    call = db.get(Call, call_id)
    if not call:
        raise HTTPException(404, "Call not found")
    call = log_call_outcome(db, call, CallOutcome.NO_ANSWER, notes=payload.notes)

    # Fire the no-answer SMS template if one is configured and the phone is callable.
    sms_sent = None
    phone = db.get(Phone, call.phone_id) if call.phone_id else None
    lead = call.lead
    if phone and lead and is_callable(phone):
        from app.models import MessageTemplate
        template = None
        if payload.template_id:
            template = db.get(MessageTemplate, payload.template_id)
        if template is None:
            template = db.execute(
                select(MessageTemplate).where(
                    MessageTemplate.category == "no_answer",
                    MessageTemplate.active == True,
                ).order_by(MessageTemplate.created_at.desc()).limit(1)
            ).scalars().first()
        if template is not None:
            try:
                sms_sent = send_sms(db, lead, phone, template=template)
            except (ValueError, RuntimeError):
                # SMS failure must not block the dial queue from advancing.
                sms_sent = None
    db.commit()
    if sms_sent is not None:
        db.refresh(sms_sent)

    # Next lead to dial (exclude the one we just worked).
    nxt = get_next_lead_to_dial(db, exclude_lead_ids={lead.id} if lead else None)
    return {
        "call_id": call.id,
        "sms": {"id": sms_sent.id, "status": sms_sent.status} if sms_sent else None,
        "next_lead": nxt,
    }


@router.get("/queue/next")
def next_in_queue(state: str | None = None, exclude: str | None = None, db: Session = Depends(get_db)):
    """Return the next lead + callable phone to dial (drives keep-dialing).

    ``exclude`` is a comma-separated list of lead IDs already worked this session.
    When ``state`` is omitted, defaults to the configured Primary Focus State.
    """
    if state is None:
        from app.services.settings import get_setting
        state = (get_setting(db, "focus_state") or "").strip().upper() or None
    exclude_ids = {x for x in (exclude or "").split(",") if x} if exclude else None
    return get_next_lead_to_dial(db, exclude_lead_ids=exclude_ids, state=state)
