"""Live AI agent in the browser: tester ⇄ CRM ⇄ Gemini Live (Vertex AI).

WebSocket /api/live-agent/ws?flow_id=&lead_id=&voice=&greet_name=
  browser → server: binary = mic PCM16 LE 16 kHz; text {"type": "end"}
  server → browser: binary = agent PCM16 LE 24 kHz; text JSON events:
    ready {call_id, model, voice} · transcript {who: agent|caller, text}
    interrupted · turn_complete · ending · ended {call} · error {message}

The HTTP auth middleware doesn't see WebSockets, so the session cookie is
checked here. The call is finalized (recording + Hot/Warm/Cold score) before
the socket closes — Cloud Run throttles CPU once the request is over.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request, WebSocket
from sqlalchemy import select

from app.database import SessionLocal
from app.models import AiCall, CallFlow, Lead, User
from app.schemas import AiCallOut
from app.services import live_agent
from app.services.auth import SESSION_COOKIE, decode_session_token

router = APIRouter(prefix="/api/live-agent", tags=["live-agent"])
log = logging.getLogger("uvicorn.error")


@router.get("/status")
def agent_status(request: Request):
    db = SessionLocal()
    try:
        return live_agent.status(db)
    finally:
        db.close()


def _ws_user(websocket: WebSocket, db) -> User | None:
    token = websocket.cookies.get(SESSION_COOKIE)
    payload = decode_session_token(token) if token else None
    user = db.get(User, payload.get("uid")) if payload else None
    return user if user and user.active else None


async def _send(websocket: WebSocket, payload) -> None:
    """Best-effort send — the tester may have closed the tab."""
    try:
        if isinstance(payload, (bytes, bytearray)):
            await websocket.send_bytes(payload)
        else:
            await websocket.send_json(payload)
    except Exception:
        pass


@router.websocket("/ws")
async def live_call(websocket: WebSocket):
    from google.genai import types
    from app.services.ai_agent import greeting_default

    await websocket.accept()
    db = SessionLocal()
    ai_call = None
    try:
        user = _ws_user(websocket, db)
        if user is None:
            await _send(websocket, {"type": "error", "message": "Your session expired — sign in again."})
            return
        q = websocket.query_params
        settings = live_agent.vertex_settings(db)
        voice = q.get("voice") if q.get("voice") in live_agent.VOICES else settings["voice"]
        flow = db.get(CallFlow, q["flow_id"]) if q.get("flow_id") else db.execute(
            select(CallFlow).where(CallFlow.is_default == True, CallFlow.active == True).limit(1)  # noqa: E712
        ).scalars().first()
        if flow is None:
            await _send(websocket, {"type": "error", "message": "No call flow — create one in Settings → Call Flows."})
            return
        lead = db.get(Lead, q["lead_id"]) if q.get("lead_id") else None
        greet = (q.get("greet_name") or "").strip()[:60] or greeting_default(user)

        ai_call = AiCall(user_id=user.id, to_number="browser", greet_name=greet, lead_id=lead.id if lead else None,
                         call_flow_id=flow.id, voice=voice, provider="vertex", status="in-progress")
        db.add(ai_call)
        db.commit()

        try:
            genai = live_agent.client(db)
        except live_agent.LiveAgentError as exc:
            ai_call.status, ai_call.error = "failed", str(exc)
            db.commit()
            await _send(websocket, {"type": "error", "message": str(exc)})
            return
        config, kickoff = live_agent.session_config(db, flow, greet, lead, voice)
        recorder = live_agent.CallRecorder()
        transcript = live_agent.Transcript()
        stop = asyncio.Event()
        reason = {"why": "ended"}

        async with genai.aio.live.connect(model=settings["model"], config=config) as session:
            await _send(websocket, {"type": "ready", "call_id": ai_call.id, "model": settings["model"], "voice": voice})
            await session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=kickoff)]), turn_complete=True)

            async def upstream():
                try:
                    while not stop.is_set():
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.disconnect":
                            reason["why"] = "tester left"
                            break
                        if msg.get("bytes"):
                            recorder.caller(msg["bytes"])
                            await session.send_realtime_input(
                                audio=types.Blob(data=msg["bytes"], mime_type="audio/pcm;rate=16000"))
                        elif msg.get("text") and json.loads(msg["text"]).get("type") == "end":
                            break
                except Exception as exc:
                    reason["why"] = f"connection error: {exc}"
                finally:
                    stop.set()

            async def downstream():
                try:
                    while not stop.is_set():
                        async for m in session.receive():
                            sc = m.server_content
                            if sc is None:
                                continue
                            if sc.interrupted:
                                recorder.interrupted()
                                await _send(websocket, {"type": "interrupted"})
                            if sc.model_turn:
                                for part in sc.model_turn.parts or []:
                                    if part.inline_data and part.inline_data.data:
                                        recorder.agent(part.inline_data.data)
                                        await _send(websocket, part.inline_data.data)
                            if sc.input_transcription and sc.input_transcription.text:
                                transcript.add("caller", sc.input_transcription.text)
                                await _send(websocket, {"type": "transcript", "who": "caller", "text": sc.input_transcription.text})
                            if sc.output_transcription and sc.output_transcription.text:
                                transcript.add("agent", sc.output_transcription.text)
                                await _send(websocket, {"type": "transcript", "who": "agent", "text": sc.output_transcription.text})
                            if sc.turn_complete:
                                await _send(websocket, {"type": "turn_complete"})
                except Exception as exc:
                    if not stop.is_set():
                        reason["why"] = f"agent disconnected: {exc}"
                        log.warning("Gemini Live session ended: %s", exc)
                finally:
                    stop.set()

            async def time_limit():
                await asyncio.sleep(live_agent.MAX_SECONDS)
                reason["why"] = "time limit reached"
                stop.set()

            tasks = [asyncio.create_task(t()) for t in (upstream, downstream, time_limit)]
            await stop.wait()
            for t in tasks:
                t.cancel()

        # ── Finalize before closing the socket ──
        await _send(websocket, {"type": "ending", "reason": reason["why"]})
        ai_call.status = "completed"
        ai_call.duration_seconds = recorder.seconds()
        ai_call.transcript = transcript.text()
        live_agent.save_recording(ai_call, recorder)
        if ai_call.transcript:
            try:
                result = await asyncio.to_thread(live_agent.score_call, db, flow, ai_call.transcript)
                ai_call.score = result["score"]
                ai_call.summary = live_agent.format_summary(result)
            except Exception as exc:
                log.warning("Live call scoring failed: %s", exc)
                ai_call.error = f"Scoring unavailable: {exc}"
        db.commit()
        db.refresh(ai_call)
        await _send(websocket, {"type": "ended", "call": AiCallOut.model_validate(ai_call).model_dump(mode="json")})
    except Exception as exc:
        log.exception("Live agent call failed")
        if ai_call is not None:
            ai_call.status, ai_call.error = "failed", str(exc)[:500]
            db.commit()
        await _send(websocket, {"type": "error", "message": f"Live agent error: {exc}"})
    finally:
        db.close()
        try:
            await websocket.close()
        except Exception:
            pass
