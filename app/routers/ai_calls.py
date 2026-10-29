"""AI voice agent test calls: the agent speaks a call flow to the tester's phone.

Guardrails (test-only by design):
- the tester must confirm they own / may receive AI calls on the number;
- numbers that belong to a lead in the CRM are refused — AI-voice calls to
  sellers require their prior written consent (TCPA);
- one active test call per user, at most ``DAILY_LIMIT`` per user per day.
"""
from __future__ import annotations

import re
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models import Agent, AiCall, CallFlow, Lead, User
from app.schemas import AiCallCreate, AiCallOut
from app.services import ai_agent, phone_agent
from app.timeutil import utcnow

router = APIRouter(prefix="/api/ai-calls", tags=["ai-calls"])

DAILY_LIMIT = 20


def _user(request: Request, db: Session) -> User:
    u = getattr(request.state, "user", None)
    user = db.get(User, u.id) if u else None
    if user is None:
        raise HTTPException(401, "Authentication required")
    return user


def _own_call(db: Session, user: User, call_id: str) -> AiCall:
    call = db.get(AiCall, call_id)
    if call is None or (call.user_id != user.id and user.role != "admin"):
        raise HTTPException(404, "Test call not found")
    return call


@router.get("/status")
def agent_status(request: Request, db: Session = Depends(get_db)):
    """Whether the AI agent is configured; with a key, also balance + voices."""
    _user(request, db)
    from app.services.settings import get_setting
    if not ai_agent.api_key(db):
        return {"configured": False, "default_voice": get_setting(db, "ai_voice") or ""}
    try:
        return {"configured": True, "default_voice": get_setting(db, "ai_voice") or "", **ai_agent.account_status(db)}
    except ai_agent.AiAgentError as exc:
        return {"configured": True, "ok": False, "error": str(exc), "default_voice": get_setting(db, "ai_voice") or ""}


@router.get("/verify/{phone_id}")
def verify_number(phone_id: str, request: Request, db: Session = Depends(get_db)):
    """Pre-dial check: is this the right number for the property, and may the AI call it now?"""
    from app.models import Phone
    from app.services.number_check import check_phone
    _user(request, db)
    phone = db.get(Phone, phone_id)
    if phone is None:
        raise HTTPException(404, "Phone not found")
    return check_phone(db, phone.lead, phone, for_ai=True)


@router.post("", response_model=AiCallOut, status_code=201)
def start_test_call(payload: AiCallCreate, request: Request, db: Session = Depends(get_db)):
    """AI agent call: either a rehearsal to the tester's own phone, or a real call to a
    lead's number — the latter only after the number passes every verification check."""
    from app.models import Phone
    from app.services.messaging import find_phone_by_number
    from app.services.number_check import check_phone
    from app.services.quo import to_e164
    from app.services.settings import get_setting

    user = _user(request, db)
    lead = None
    phone = None
    if payload.phone_id:
        phone = db.get(Phone, payload.phone_id)
        if phone is None:
            raise HTTPException(404, "Phone not found")
        lead = phone.lead
        verdict = check_phone(db, lead, phone, for_ai=True)
        if not verdict["ai_allowed"]:
            raise HTTPException(403, "The AI can't call this number: " + verdict.get("reason", "verification failed"))
        number = to_e164(phone.number)
    else:
        if not payload.consent:
            raise HTTPException(400, "Confirm this is your number and you agree to receive an AI test call.")
        number = to_e164(payload.to_number or "")
        if find_phone_by_number(db, number) is not None:
            raise HTTPException(403, "That number belongs to a lead in the CRM — call the owner from the lead's verified "
                                     "number instead (it checks consent and that the number is correct).")
    if not re.fullmatch(r"\+1\d{10}", number):
        raise HTTPException(400, "Enter a US/Canada phone number, e.g. (555) 123-4567.")
    active = db.execute(select(AiCall).where(
        AiCall.user_id == user.id, AiCall.status.in_(["queued", "in-progress"]),
        AiCall.created_at >= utcnow() - timedelta(minutes=ai_agent.MAX_DURATION_MIN + 5),
    )).scalars().first()
    if active is not None:
        raise HTTPException(409, "You already have an AI call in progress — hang it up first.")
    today = db.execute(select(func.count(AiCall.id)).where(
        AiCall.user_id == user.id, AiCall.created_at >= utcnow() - timedelta(days=1),
    )).scalar() or 0
    if today >= DAILY_LIMIT:
        raise HTTPException(429, f"Daily limit of {DAILY_LIMIT} AI calls reached.")

    flow = db.get(CallFlow, payload.call_flow_id) if payload.call_flow_id else db.execute(
        select(CallFlow).where(CallFlow.is_default == True, CallFlow.active == True).limit(1)  # noqa: E712
    ).scalars().first()
    if flow is None:
        raise HTTPException(404, "Call flow not found — create one in Settings → Call Flows.")
    if lead is None and payload.lead_id:
        lead = db.get(Lead, payload.lead_id)
        if lead is None:
            raise HTTPException(404, "Lead not found")

    agent = None
    if payload.agent_id:
        agent = db.get(Agent, payload.agent_id)
        if agent is None or agent.kind != "calling":
            raise HTTPException(404, "Calling agent not found")
        if not agent.active:
            raise HTTPException(400, f"{agent.name} is inactive.")
    else:
        from app.services.agents import default_agent
        agent = default_agent(db, "calling")
    if agent is not None and agent.call_flow_id and not payload.call_flow_id:
        flow = db.get(CallFlow, agent.call_flow_id) or flow

    greet = (payload.greet_name or "").strip()
    if phone is not None and not greet:
        greet = ai_agent.owner_greeting(lead)
    call = AiCall(
        user_id=user.id,
        agent_id=agent.id if agent else None,
        to_number=number,
        phone_id=phone.id if phone else None,
        greet_name=greet or ai_agent.greeting_default(user),
        lead_id=lead.id if lead else None,
        call_flow_id=flow.id,
        voice=(payload.voice or "").strip() or (agent.voice if agent else None)
              or (get_setting(db, "ai_voice") or "").strip() or None,
    )
    db.add(call)
    db.flush()
    try:
        if agent is not None and agent.provider in phone_agent.PROVIDERS:
            phone_agent.start_call(db, call)
        else:
            ai_agent.start_call(db, call, flow, lead)
    except (ai_agent.AiAgentError, phone_agent.PhoneAgentError) as exc:
        db.rollback()
        if agent is not None:
            agent.last_error = str(exc)[:500]
            db.commit()
        raise HTTPException(502, str(exc))
    if agent is not None:
        agent.last_used_at, agent.last_error = utcnow(), None
    db.commit()
    db.refresh(call)
    return call


# ── Twilio phone calls (Gemini Live agent) ──────────────────────────────────

@router.post("/twilio/status")
async def twilio_status(request: Request, db: Session = Depends(get_db)):
    """Twilio call-progress + answering-machine callbacks (public; signature-verified)."""
    call = db.get(AiCall, request.query_params.get("ai_call_id", ""))
    if call is None:
        raise HTTPException(404, "Unknown call")
    params = dict(await request.form())
    url = phone_agent.status_url(call.id)
    if not phone_agent.verify_twilio_signature(db, call, url, params, request.headers.get("X-Twilio-Signature", "")):
        raise HTTPException(403, "Invalid Twilio signature")
    phone_agent.handle_status_callback(db, call, params)
    db.commit()
    return {"ok": True}


@router.websocket("/twilio/stream")
async def twilio_stream(websocket: WebSocket):
    """Twilio Media Stream ⇄ Gemini Live. Authenticated by the signed stream token."""
    await websocket.accept()
    db = SessionLocal()
    try:
        await phone_agent.bridge(websocket, db)
    except Exception:
        import logging
        logging.getLogger("uvicorn.error").exception("Twilio stream bridge failed")
    finally:
        db.close()
        try:
            await websocket.close()
        except Exception:
            pass


@router.websocket("/{call_id}/listen-ws")
async def listen_ws(call_id: str, websocket: WebSocket):
    """Browser 'Listen live' for Twilio + Gemini calls. Frames: 1 byte track
    (0 caller, 1 agent, 2 clear agent audio, 3 call ended) + PCM16 LE 16 kHz."""
    from app.services.auth import SESSION_COOKIE, decode_session_token
    await websocket.accept()
    db = SessionLocal()
    try:
        token = websocket.cookies.get(SESSION_COOKIE)
        payload = decode_session_token(token) if token else None
        user = db.get(User, payload.get("uid")) if payload else None
        call = db.get(AiCall, call_id)
        if user is None or call is None or (call.user_id != user.id and user.role != "admin"):
            await websocket.close(code=4403)
            return
    finally:
        db.close()
    q = phone_agent.subscribe(call_id)
    try:
        while True:
            frame = await q.get()
            await websocket.send_bytes(frame)
            if frame[:1] == b"\x03":
                break
    except Exception:
        pass
    finally:
        phone_agent.unsubscribe(call_id, q)
        try:
            await websocket.close()
        except Exception:
            pass


@router.get("", response_model=list[AiCallOut])
def list_test_calls(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    return db.execute(
        select(AiCall).where(AiCall.user_id == user.id).order_by(AiCall.created_at.desc()).limit(25)
    ).scalars().all()


@router.get("/{call_id}", response_model=AiCallOut)
def get_test_call(call_id: str, request: Request, db: Session = Depends(get_db)):
    """Current state — refreshed from Bland until the call is final and processed."""
    call = _own_call(db, _user(request, db), call_id)
    try:
        if call.provider == "bland":
            ai_agent.refresh(db, call)
        if call.phone_id and call.outcome is None:
            ai_agent.finalize_lead_call(db, call)
    except ai_agent.AiAgentError as exc:
        call.error = str(exc)
    db.commit()
    db.refresh(call)
    return call


@router.post("/{call_id}/listen")
def listen(call_id: str, request: Request, db: Session = Depends(get_db)):
    """WebSocket URL for live audio (PCM16 little-endian, 16 kHz, mono)."""
    call = _own_call(db, _user(request, db), call_id)
    if call.provider in phone_agent.PROVIDERS:
        if call.status not in ("queued", "in-progress"):
            raise HTTPException(409, "The call isn't live.")
        return {"url": f"/api/ai-calls/{call.id}/listen-ws", "sample_rate": 16000, "format": "dre_tracks"}
    try:
        return {"url": ai_agent.listen_url(db, call), "sample_rate": 16000, "format": "pcm_s16le"}
    except ai_agent.AiAgentError as exc:
        raise HTTPException(409, str(exc))


@router.post("/{call_id}/stop", response_model=AiCallOut)
def hang_up(call_id: str, request: Request, db: Session = Depends(get_db)):
    call = _own_call(db, _user(request, db), call_id)
    try:
        if call.provider in phone_agent.PROVIDERS:
            phone_agent.hang_up(db, call)
        else:
            ai_agent.stop(db, call)
    except (ai_agent.AiAgentError, phone_agent.PhoneAgentError) as exc:
        raise HTTPException(502, str(exc))
    # Don't mark it final locally: the provider still finalizes the call
    # (duration, recording, transcript) — keep tracking its real status.
    try:
        if call.provider == "bland":
            ai_agent.refresh(db, call)
    except ai_agent.AiAgentError:
        pass
    db.commit()
    db.refresh(call)
    return call


@router.get("/{call_id}/recording")
def recording(call_id: str, request: Request, db: Session = Depends(get_db)):
    """Play the call recording. CRM-recorded (live) calls are served as files so the
    browser gets a length and can seek; provider recordings are proxied so the
    provider URL stays private."""
    call = _own_call(db, _user(request, db), call_id)
    if call.recording_url and call.recording_url.startswith("file:"):
        from pathlib import Path
        from fastapi.responses import FileResponse
        path = Path(call.recording_url[len("file:"):])
        if not path.is_file():
            raise HTTPException(404, "This recording is no longer on the server (recordings reset on redeploy).")
        return FileResponse(path, media_type="audio/wav", filename=f"call-{call.id}.wav", content_disposition_type="inline")
    try:
        body, media = ai_agent.stream_recording(db, call)
    except ai_agent.AiAgentError as exc:
        raise HTTPException(404, str(exc))
    return StreamingResponse(body, media_type=media)
