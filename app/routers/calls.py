"""Calls router: click-to-call (Quo / Twilio) + outcome logging + provider sync."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Call, CallFlow, CallOutcome, Lead, Phone, User
from app.schemas import CallInitiateOut, CallInitiateRequest, CallLogRequest, CallOut, NoAnswerSmsRequest
from app.services.calling import initiate_call, log_call_outcome
from app.services.messaging import send_sms
from app.services.queue import get_next_lead_to_dial
from app.services.skiptrace import is_callable

router = APIRouter(prefix="/api/calls", tags=["calls"])

_SCORES = {"hot", "warm", "cold"}


def _current_user(request: Request, db: Session) -> User | None:
    """The logged-in user bound to this request's session (middleware's copy is detached)."""
    user = getattr(request.state, "user", None)
    return db.get(User, user.id) if user else None


@router.get("/provider")
def calling_provider(request: Request, check: bool = False, db: Session = Depends(get_db)):
    """The caller's active provider, whether their Quo line is personal or the
    company's, and the default operator number (Twilio bridge prompt).
    ``check=true`` also tests the Quo connection live."""
    from app.services.quo import personal_key
    from app.services.settings import get_calling_provider, get_setting
    user = _current_user(request, db)
    provider = get_calling_provider(db, user)
    out: dict = {
        "provider": provider,
        "quo_line": "personal" if personal_key(user) else ("company" if provider == "quo" else None),
        "operator_number": get_setting(db, "operator_number") or "",
    }
    if check and provider == "quo":
        from app.services.quo import connection_status
        out["quo"] = connection_status(db, user)
        db.commit()  # persist an auto-detected line
    return out


@router.post("/quo-webhook")
async def quo_webhook(request: Request, db: Session = Depends(get_db)):
    """Quo event receiver (public; authenticated by the openphone-signature HMAC).

    call.completed / call.summary.completed / call.transcript.completed fill in
    the CRM call automatically; message.received honors STOP opt-outs.
    """
    from app.services import quo
    raw = await request.body()
    if not quo.verify_signature(db, request.headers.get("openphone-signature"), raw):
        raise HTTPException(401, "Invalid Quo signature")
    try:
        import json
        event = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    result = quo.handle_event(db, event)
    db.commit()
    return result


@router.post("/quo/webhooks")
def register_quo_webhooks(request: Request, db: Session = Depends(get_db)):
    """Turn on Quo auto-sync for the company key (admin) — idempotent."""
    user = _current_user(request, db)
    if not user or user.role != "admin":
        raise HTTPException(403, "Admin only")
    from app.config import get_settings
    from app.services import quo
    if not quo.company_key(db):
        raise HTTPException(400, "Set the company Quo API key first.")
    try:
        result = quo.register_webhooks(db, get_settings().app_base_url)
    except quo.QuoError as exc:
        raise HTTPException(502, str(exc))
    db.commit()
    return result


@router.post("/initiate", response_model=CallInitiateOut)
def start_call(payload: CallInitiateRequest, request: Request, db: Session = Depends(get_db)):
    """Initiate a compliant manual click-to-call.

    Quo: returns ``dial_url`` (tel: link) — the operator presses Call in the Quo
    app.  Twilio: rings the operator, then bridges to the seller.  No
    autodialer, no prerecorded messages.
    """
    phone = db.get(Phone, payload.phone_id)
    if not phone:
        raise HTTPException(404, "Phone not found")
    lead = db.get(Lead, phone.lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    if not is_callable(phone):
        raise HTTPException(403, "Phone is DNC-flagged or opted out. Mail only.")
    user = getattr(request.state, "user", None)
    try:
        call = initiate_call(db, lead, phone, operator_number=payload.operator_number,
                             caller_id=user.id if user else None)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(502, str(exc))
    db.commit()
    db.refresh(call)
    out = CallInitiateOut.model_validate(call)
    if call.provider == "quo":
        from app.services.quo import dial_url
        out.dial_url = dial_url(phone.number)
    return out


@router.post("/{call_id}/log", response_model=CallOut)
def log_outcome(call_id: str, payload: CallLogRequest, db: Session = Depends(get_db)):
    """Log the outcome (+ call-guide answers and score) and auto-schedule follow-up."""
    call = db.get(Call, call_id)
    if not call:
        raise HTTPException(404, "Call not found")
    try:
        outcome = CallOutcome(payload.outcome)
    except ValueError:
        raise HTTPException(400, f"Invalid outcome. Valid: {[e.value for e in CallOutcome]}")
    score = (payload.lead_score or "").strip().lower() or None
    if score is not None and score not in _SCORES:
        raise HTTPException(400, "lead_score must be hot, warm, or cold")
    flow = db.get(CallFlow, payload.call_flow_id) if payload.call_flow_id else None
    if payload.call_flow_id and flow is None:
        raise HTTPException(404, "Call flow not found")
    if score is None and payload.answers and flow is not None:
        from app.services.call_guide import score_answers
        score = score_answers(db, call.lead, flow, payload.answers)["score"]
    call = log_call_outcome(
        db, call, outcome,
        notes=payload.notes,
        duration_seconds=payload.duration_seconds,
        call_flow_id=payload.call_flow_id,
        answers=payload.answers,
        lead_score=score,
    )
    db.commit()
    db.refresh(call)
    return call


@router.post("/{call_id}/sync")
def sync_from_provider(call_id: str, db: Session = Depends(get_db)):
    """Pull duration, AI summary, and transcript for a Quo call after hang-up."""
    call = db.get(Call, call_id)
    if not call:
        raise HTTPException(404, "Call not found")
    if call.provider != "quo":
        raise HTTPException(400, f"Sync is only available for Quo calls (this call used {call.provider}).")
    from app.services.quo import QuoError, sync_call
    try:
        result = sync_call(db, call)
    except QuoError as exc:
        raise HTTPException(502, str(exc))
    db.commit()
    return result


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
                sms_sent = send_sms(db, lead, phone, template=template, sent_by=call.caller_id)
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
