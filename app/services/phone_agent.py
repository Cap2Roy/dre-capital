"""Calling module: Twilio dials, Gemini Live speaks the call flow.

    CRM ──POST Calls.json──▶ Twilio ──rings──▶ phone
    phone audio ⇄ Twilio Media Stream (μ-law 8 kHz, WebSocket) ⇄ this bridge
    bridge ⇄ Gemini Live (PCM16 16 kHz in / 24 kHz out, Vertex AI)

- The stream WebSocket carries a signed, short-lived token (Twilio's request
  has no session cookie); Twilio status / answering-machine callbacks are
  verified with Twilio's X-Twilio-Signature using the agent's auth token.
- Barge-in: when Gemini reports an interruption we send Twilio a ``clear``.
- The agent hangs up itself through the ``end_call`` tool (wrong number, stop
  request, voicemail, done); answering machines are hung up on (no messages).
- Recording, transcript, Hot/Warm/Cold score, and — for calls to a lead's
  number — outcome logging + number confirmation run before the socket closes.
- Live listen: the bridge publishes both voices to in-process listeners
  (Cloud Run runs a single instance) for the browser player.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from array import array
from typing import Optional

import httpx
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Agent, AiCall, CallFlow, Lead

log = logging.getLogger("uvicorn.error")
TWILIO_API = "https://api.twilio.com/2010-04-01"
STREAM_PATH = "/api/ai-calls/twilio/stream"
STATUS_PATH = "/api/ai-calls/twilio/status"
MAX_SECONDS = 6 * 60
RING_TIMEOUT = 25
_signer = URLSafeTimedSerializer(get_settings().secret_key, salt="twilio-stream")


class PhoneAgentError(RuntimeError):
    pass


# ── G.711 μ-law ⇄ PCM16 ─────────────────────────────────────────────────────

def _ulaw_decode_byte(u: int) -> int:
    u = ~u & 0xFF
    sign, exponent, mantissa = u & 0x80, (u >> 4) & 0x07, u & 0x0F
    sample = (((mantissa << 3) + 0x84) << exponent) - 0x84
    return -sample if sign else sample


_ULAW_DECODE = array("h", [_ulaw_decode_byte(i) for i in range(256)])


def _ulaw_encode_sample(s: int) -> int:
    sign = 0x80 if s < 0 else 0
    s = min(-s if s < 0 else s, 32635) + 0x84
    exponent = max(0, (s >> 7).bit_length() - 1) if (s >> 7) else 0
    mantissa = (s >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


_ULAW_ENCODE = bytes(_ulaw_encode_sample(i - 32768) for i in range(65536))


def ulaw_to_pcm16(data: bytes) -> array:
    return array("h", (_ULAW_DECODE[b] for b in data))


def pcm16_to_ulaw(samples: array) -> bytes:
    return bytes(_ULAW_ENCODE[s + 32768] for s in samples)


def upsample_8k_to_16k(src: array) -> array:
    out = array("h", bytes(4 * len(src)))
    for i, s in enumerate(src):
        nxt = src[i + 1] if i + 1 < len(src) else s
        out[2 * i] = s
        out[2 * i + 1] = (s + nxt) // 2
    return out


def downsample_24k_to_8k(src: array) -> array:
    n = len(src) // 3
    return array("h", ((src[3 * i] + src[3 * i + 1] + src[3 * i + 2]) // 3 for i in range(n)))


def _pcm(data: bytes) -> array:
    a = array("h")
    a.frombytes(data[: len(data) - (len(data) % 2)])
    return a


# ── Tokens + Twilio REST ────────────────────────────────────────────────────

def stream_token(ai_call_id: str) -> str:
    return _signer.dumps({"c": ai_call_id})


def verify_stream_token(token: str, ai_call_id: str) -> bool:
    try:
        return _signer.loads(token, max_age=900).get("c") == ai_call_id
    except BadSignature:
        return False


def _base_url() -> str:
    return get_settings().app_base_url.rstrip("/")


def _ws_url() -> str:
    base = _base_url()
    return ("wss://" + base[len("https://"):] if base.startswith("https://") else "ws://" + base[len("http://"):]) + STREAM_PATH


def status_url(ai_call_id: str) -> str:
    return f"{_base_url()}{STATUS_PATH}?ai_call_id={ai_call_id}"


def _creds(db: Session, ai_call: AiCall) -> dict:
    from app.services.agents import AgentError, credentials
    agent = db.get(Agent, ai_call.agent_id) if ai_call.agent_id else None
    if agent is None:
        raise PhoneAgentError("This call has no calling agent.")
    try:
        return credentials(db, agent)
    except AgentError as exc:
        raise PhoneAgentError(str(exc)) from exc


def _twilio(creds: dict, method: str, path: str, data: Optional[list] = None) -> dict:
    url = f"{TWILIO_API}/Accounts/{creds['account_sid']}{path}"
    try:
        r = httpx.request(method, url, data=data, auth=(creds["account_sid"], creds["api_key"]), timeout=20)
    except httpx.HTTPError as exc:
        raise PhoneAgentError(f"Twilio unreachable: {exc}") from exc
    if r.status_code == 401:
        raise PhoneAgentError("Twilio rejected the Account SID / Auth Token.")
    if r.status_code >= 400:
        try:
            msg = r.json().get("message")
        except ValueError:
            msg = r.text[:200]
        raise PhoneAgentError(f"Twilio error {r.status_code}: {msg}")
    return r.json() if r.content else {}


def start_call(db: Session, ai_call: AiCall) -> AiCall:
    """Have Twilio dial ``ai_call.to_number`` and connect the media stream to us."""
    base = _base_url()
    if not base.startswith("https://") and not get_settings().app_base_url.startswith("http://127.0.0.1"):
        raise PhoneAgentError("Phone calls need the app's public HTTPS URL (APP_BASE_URL) so Twilio can reach it.")
    creds = _creds(db, ai_call)
    twiml = (f'<Response><Connect><Stream url="{_ws_url()}">'
             f'<Parameter name="ai_call_id" value="{ai_call.id}"/>'
             f'<Parameter name="token" value="{stream_token(ai_call.id)}"/>'
             f'</Stream></Connect></Response>')
    cb = status_url(ai_call.id)
    data = [("To", ai_call.to_number), ("From", creds["from_number"]), ("Twiml", twiml),
            ("Timeout", str(RING_TIMEOUT)), ("TimeLimit", str(MAX_SECONDS)),
            ("StatusCallback", cb), ("StatusCallbackMethod", "POST"),
            ("MachineDetection", "Enable"), ("AsyncAmd", "true"),
            ("AsyncAmdStatusCallback", cb), ("AsyncAmdStatusCallbackMethod", "POST")]
    for ev in ("initiated", "ringing", "answered", "completed"):
        data.append(("StatusCallbackEvent", ev))
    res = _twilio(creds, "POST", "/Calls.json", data)
    ai_call.provider = "twilio_gemini"
    ai_call.provider_call_id = res.get("sid")
    ai_call.status = "queued"
    db.flush()
    return ai_call


def hang_up(db: Session, ai_call: AiCall) -> None:
    if ai_call.provider_call_id:
        _twilio(_creds(db, ai_call), "POST", f"/Calls/{ai_call.provider_call_id}.json", [("Status", "completed")])


def verify_twilio_signature(db: Session, ai_call: AiCall, url: str, params: dict, signature: str) -> bool:
    from twilio.request_validator import RequestValidator
    try:
        token = _creds(db, ai_call)["api_key"]
    except PhoneAgentError:
        return False
    return bool(signature) and RequestValidator(token).validate(url, params, signature)


_TWILIO_STATUS = {"queued": "queued", "initiated": "queued", "ringing": "queued", "in-progress": "in-progress",
                  "completed": "completed", "busy": "busy", "no-answer": "no-answer", "failed": "failed",
                  "canceled": "canceled"}


def handle_status_callback(db: Session, ai_call: AiCall, params: dict) -> None:
    """Call progress + async answering-machine result."""
    answered_by = params.get("AnsweredBy")
    if answered_by:
        ai_call.answered_by = "voicemail" if answered_by.startswith(("machine", "fax")) else (
            "human" if answered_by == "human" else "unknown")
        if ai_call.answered_by == "voicemail":
            try:
                hang_up(db, ai_call)   # never leave AI messages on machines
            except PhoneAgentError as exc:
                log.warning("AMD hang-up failed: %s", exc)
    status = _TWILIO_STATUS.get(params.get("CallStatus", ""))
    bridged = ai_call.status == "completed" and bool(ai_call.transcript)   # the bridge already finalized
    if status and not bridged:
        ai_call.status = status
    if params.get("CallDuration"):
        try:
            ai_call.duration_seconds = int(params["CallDuration"])
        except ValueError:
            pass
    if ai_call.status in ("busy", "no-answer", "failed", "canceled") or (
            ai_call.status == "completed" and not bridged):
        _finalize_lead(db, ai_call)
    db.flush()


def _finalize_lead(db: Session, ai_call: AiCall) -> None:
    """Log a call to a lead's number once (outcome, follow-up, number confirmation)."""
    if ai_call.phone_id and ai_call.outcome is None:
        from app.services.ai_agent import finalize_lead_call
        finalize_lead_call(db, ai_call)


# ── Live listeners (browser "Listen live") ──────────────────────────────────

LISTENERS: dict[str, set[asyncio.Queue]] = {}


def _publish(call_id: str, kind: int, pcm16_16k: bytes = b"") -> None:
    for q in list(LISTENERS.get(call_id, ())):
        if q.qsize() < 400:
            q.put_nowait(bytes([kind]) + pcm16_16k)


def subscribe(call_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    LISTENERS.setdefault(call_id, set()).add(q)
    return q


def unsubscribe(call_id: str, q: asyncio.Queue) -> None:
    LISTENERS.get(call_id, set()).discard(q)


# ── The bridge ──────────────────────────────────────────────────────────────

async def bridge(websocket, db: Session) -> None:
    """Run one Twilio media stream ⇄ Gemini Live call until either side ends."""
    from google.genai import types
    from app.services import live_agent
    from app.services.ai_agent import owner_greeting

    # 1. Wait for Twilio's "start" message (carries our signed parameters).
    stream_sid = None
    ai_call = None
    while True:
        msg = json.loads(await websocket.receive_text())
        if msg.get("event") == "start":
            start = msg["start"]
            stream_sid = start["streamSid"]
            params = start.get("customParameters") or {}
            ai_call = db.get(AiCall, params.get("ai_call_id", ""))
            if ai_call is None or not verify_stream_token(params.get("token", ""), ai_call.id):
                log.warning("Rejected Twilio stream with bad token")
                return
            break
        if msg.get("event") == "stop":
            return

    agent = db.get(Agent, ai_call.agent_id)
    flow = db.get(CallFlow, ai_call.call_flow_id)
    lead = db.get(Lead, ai_call.lead_id) if ai_call.lead_id else None
    real_call = ai_call.phone_id is not None
    greet = ai_call.greet_name or (owner_greeting(lead) if lead else "there")
    settings = live_agent.vertex_settings(db)
    voice = ai_call.voice if ai_call.voice in live_agent.VOICES else settings["voice"]
    model = agent.model or settings["model"]
    config, kickoff = live_agent.session_config(db, flow, greet, lead, voice, real_call=real_call, phone=True,
                                                extra_instructions=agent.instructions)
    ai_call.status = "in-progress"
    db.commit()

    recorder = live_agent.CallRecorder()
    transcript = live_agent.Transcript()
    stop = asyncio.Event()
    state = {"kicked": False, "heard": 0.0, "end_reason": None, "hangup_at": None, "agent_sent_until": 0.0}
    t0 = time.monotonic()
    genai = live_agent.client(db)

    async def send_twilio(payload: dict) -> None:
        try:
            await websocket.send_text(json.dumps(payload))
        except Exception:
            stop.set()

    async with genai.aio.live.connect(model=model, config=config) as session:

        async def kick() -> None:
            if not state["kicked"]:
                state["kicked"] = True
                await session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part(text=kickoff)]), turn_complete=True)

        async def from_twilio() -> None:
            try:
                while not stop.is_set():
                    msg = json.loads(await websocket.receive_text())
                    ev = msg.get("event")
                    if ev == "media":
                        pcm8 = ulaw_to_pcm16(base64.b64decode(msg["media"]["payload"]))
                        pcm16 = upsample_8k_to_16k(pcm8).tobytes()
                        recorder.caller(pcm16)
                        _publish(ai_call.id, 0, pcm16)
                        if state["kicked"]:
                            # Before the opening line, the callee's "Hello?" only times our
                            # greeting — forwarding it would make Gemini answer it as well.
                            await session.send_realtime_input(audio=types.Blob(data=pcm16, mime_type="audio/pcm;rate=16000"))
                        energy = max((abs(s) for s in pcm8), default=0)
                        if energy > 1200:
                            state["heard"] = time.monotonic()
                    elif ev == "stop":
                        state["end_reason"] = state["end_reason"] or "caller hung up"
                        break
            except Exception as exc:
                state["end_reason"] = state["end_reason"] or f"stream closed: {exc}"
            finally:
                stop.set()

        async def from_gemini() -> None:
            try:
                while not stop.is_set():
                    async for m in session.receive():
                        if m.tool_call:
                            for fc in m.tool_call.function_calls or []:
                                if fc.name == "end_call":
                                    state["end_reason"] = (fc.args or {}).get("reason", "completed")
                                    # let the goodbye finish playing on the phone
                                    state["hangup_at"] = max(time.monotonic(), state["agent_sent_until"]) + 0.8
                                await session.send_tool_response(function_responses=[types.FunctionResponse(
                                    id=fc.id, name=fc.name, response={"ok": True})])
                        sc = m.server_content
                        if sc is None:
                            continue
                        if sc.interrupted:
                            recorder.interrupted()
                            _publish(ai_call.id, 2)
                            state["agent_sent_until"] = time.monotonic()
                            await send_twilio({"event": "clear", "streamSid": stream_sid})
                        if sc.model_turn:
                            for part in sc.model_turn.parts or []:
                                if part.inline_data and part.inline_data.data:
                                    data = part.inline_data.data
                                    recorder.agent(data)
                                    pcm24 = _pcm(data)
                                    pcm8 = downsample_24k_to_8k(pcm24)
                                    _publish(ai_call.id, 1, upsample_8k_to_16k(pcm8).tobytes())
                                    payload = base64.b64encode(pcm16_to_ulaw(pcm8)).decode()
                                    await send_twilio({"event": "media", "streamSid": stream_sid, "media": {"payload": payload}})
                                    now = time.monotonic()
                                    state["agent_sent_until"] = max(state["agent_sent_until"], now) + len(pcm8) / 8000
                        if sc.input_transcription and sc.input_transcription.text:
                            transcript.add("caller", sc.input_transcription.text)
                        if sc.output_transcription and sc.output_transcription.text:
                            transcript.add("agent", sc.output_transcription.text)
            except Exception as exc:
                if not stop.is_set():
                    state["end_reason"] = state["end_reason"] or f"agent disconnected: {exc}"
                    log.warning("Gemini Live (phone) ended: %s", exc)
            finally:
                stop.set()

        async def supervisor() -> None:
            """Kick off the opening line, enforce the time limit, and hang up after end_call."""
            while not stop.is_set():
                now = time.monotonic()
                if not state["kicked"]:
                    # Let the callee say "Hello?" first; speak after a short pause, or after 3.5 s regardless.
                    if (state["heard"] and now - state["heard"] > 0.7) or now - t0 > 3.5:
                        await kick()
                if state["hangup_at"] and now >= state["hangup_at"]:
                    try:
                        await asyncio.to_thread(hang_up, db, ai_call)
                    except PhoneAgentError as exc:
                        log.warning("Hang-up failed: %s", exc)
                    stop.set()
                    break
                if now - t0 > MAX_SECONDS:
                    state["end_reason"] = "time limit"
                    stop.set()
                    break
                await asyncio.sleep(0.2)

        tasks = [asyncio.create_task(t()) for t in (from_twilio, from_gemini, supervisor)]
        await stop.wait()
        for t in tasks:
            t.cancel()

    # ── Finalize while the request is still open (Cloud Run CPU) ──
    _publish(ai_call.id, 3)
    db.refresh(ai_call)
    ai_call.status = "completed"
    ai_call.duration_seconds = ai_call.duration_seconds or recorder.seconds()
    ai_call.transcript = transcript.text()
    if state["end_reason"] == "voicemail":
        ai_call.answered_by = "voicemail"
    live_agent.save_recording(ai_call, recorder)
    from app.timeutil import utcnow
    agent.last_used_at = utcnow()
    if ai_call.transcript:
        try:
            result = await asyncio.to_thread(live_agent.score_call, db, flow, ai_call.transcript)
            ai_call.score = result["score"]
            ai_call.summary = live_agent.format_summary(result)
        except Exception as exc:
            ai_call.error = f"Scoring unavailable: {exc}"
    if state["end_reason"] and not ai_call.summary:
        ai_call.summary = f"Call ended: {state['end_reason']}."
    db.commit()
    if real_call:
        try:
            await asyncio.to_thread(_finalize_lead, db, ai_call)
            db.commit()
        except Exception as exc:
            log.exception("Lead call finalize failed")
            ai_call.error = f"Couldn't log the call on the lead: {exc}"
            db.commit()
