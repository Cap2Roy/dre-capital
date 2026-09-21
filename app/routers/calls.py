"""Calls router: click-to-call + outcome logging + Twilio status callback."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Call, CallOutcome, Lead, Phone
from app.schemas import CallInitiateRequest, CallLogRequest, CallOut
from app.services.calling import initiate_call, log_call_outcome
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
        call = db.query(Call).filter_by(call_sid=call_sid).first()
        if call:
            if duration:
                call.duration_seconds = int(duration)
            db.commit()
    return {"ok": True}
