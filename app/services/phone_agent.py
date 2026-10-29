"""Calling module: Twilio dials; Grok Voice (xAI) or Gemini Live speaks the call flow.

    CRM ──POST Calls.json──▶ Twilio ──rings──▶ phone
    phone audio ⇄ Twilio Media Stream (μ-law 8 kHz, WebSocket) ⇄ this bridge
    bridge ⇄ Grok Voice (μ-law 8 kHz both ways, no conversion)  or
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
from app.services.agents import twilio_auth

log = logging.getLogger("uvicorn.error")
TWILIO_API = "https://api.twilio.com/2010-04-01"
STREAM_PATH = "/api/ai-calls/twilio/stream"
STATUS_PATH = "/api/ai-calls/twilio/status"
MAX_SECONDS = 6 * 60
RING_TIMEOUT = 25
PROVIDERS = ("twilio_grok", "twilio_gemini")   # calling providers this module dials through Twilio
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
    # Our own signed token: Twilio's X-Twilio-Signature can only be checked with the account
    # auth token, which an API-key setup doesn't have.
    return f"{_base_url()}{STATUS_PATH}?ai_call_id={ai_call_id}&t={_signer.dumps({'s': ai_call_id})}"


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
        r = httpx.request(method, url, data=data, auth=twilio_auth(creds), timeout=20)
    except httpx.HTTPError as exc:
        raise PhoneAgentError(f"Twilio unreachable: {exc}") from exc
    if r.status_code == 401:
        raise PhoneAgentError("Twilio rejected the Account SID / API key.")
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
    ai_call.provider = (db.get(Agent, ai_call.agent_id).provider if ai_call.agent_id else "twilio_gemini")
    ai_call.provider_call_id = res.get("sid")
    ai_call.status = "queued"
    db.flush()
    return ai_call


def hang_up(db: Session, ai_call: AiCall) -> None:
    if ai_call.provider_call_id:
        _twilio(_creds(db, ai_call), "POST", f"/Calls/{ai_call.provider_call_id}.json", [("Status", "completed")])


def verify_callback(ai_call: AiCall, token: str) -> bool:
    """Status callbacks carry the signed token from ``status_url`` (valid for the call's lifetime)."""
    try:
        return _signer.loads(token or "", max_age=6 * 3600).get("s") == ai_call.id
    except BadSignature:
        return False


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

GROK_VOICES = ["rex", "leo", "sal", "orion", "atlas", "helios", "kepler", "castor", "cosmo", "altair", "lux", "lumen",
               "perseus", "rigel", "sirius", "zenith", "helix", "naksh", "zagan",
               "eve", "ara", "aurora", "carina", "celeste", "iris", "liora", "luna", "ursa"]
GROK_DEFAULT_VOICE = "rex"
GROK_REALTIME_URL = "wss://api.x.ai/v1/realtime"


def upsample_8k_to_24k(src: array) -> array:
    out = array("h", bytes(6 * len(src)))
    for i, s in enumerate(src):
        nxt = src[i + 1] if i + 1 < len(src) else s
        out[3 * i] = s
        out[3 * i + 1] = (2 * s + nxt) // 3
        out[3 * i + 2] = (s + 2 * nxt) // 3
    return out


class _Call:
    """Everything one phone call shares between Twilio, the voice engine, and finalization."""

    def __init__(self, websocket, db: Session, ai_call: AiCall, stream_sid: str) -> None:
        from app.services import live_agent
        from app.services.ai_agent import owner_greeting
        self.ws, self.db, self.ai_call, self.stream_sid = websocket, db, ai_call, stream_sid
        self.agent = db.get(Agent, ai_call.agent_id)
        self.flow = db.get(CallFlow, ai_call.call_flow_id)
        self.lead = db.get(Lead, ai_call.lead_id) if ai_call.lead_id else None
        self.real_call = ai_call.phone_id is not None
        self.greet = ai_call.greet_name or (owner_greeting(self.lead) if self.lead else "there")
        self.instructions, self.kickoff = live_agent.agent_instructions(
            db, self.flow, self.greet, self.lead, real_call=self.real_call, phone=True,
            extra_instructions=self.agent.instructions)
        self.recorder = live_agent.CallRecorder()
        self.transcript = live_agent.Transcript()
        self.stop = asyncio.Event()
        self.kicked = False
        self.heard = 0.0
        self.end_reason: Optional[str] = None
        self.hangup_at: Optional[float] = None
        self.agent_sent_until = 0.0
        self.t0 = time.monotonic()

    # Twilio side
    async def send_twilio(self, payload: dict) -> None:
        try:
            await self.ws.send_text(json.dumps(payload))
        except Exception:
            self.stop.set()

    async def play(self, ulaw: bytes, pcm8: array) -> None:
        """Agent audio → phone (+ live listeners)."""
        _publish(self.ai_call.id, 1, upsample_8k_to_16k(pcm8).tobytes())
        await self.send_twilio({"event": "media", "streamSid": self.stream_sid,
                                "media": {"payload": base64.b64encode(ulaw).decode()}})
        self.agent_sent_until = max(self.agent_sent_until, time.monotonic()) + len(pcm8) / 8000

    async def barge_in(self) -> None:
        self.recorder.interrupted()
        _publish(self.ai_call.id, 2)
        self.agent_sent_until = time.monotonic()
        await self.send_twilio({"event": "clear", "streamSid": self.stream_sid})

    def end_call(self, reason: Optional[str]) -> None:
        self.end_reason = reason or "completed"
        # let the goodbye finish playing on the phone
        self.hangup_at = max(time.monotonic(), self.agent_sent_until) + 0.8

    async def twilio_audio(self):
        """Yield (ulaw bytes, pcm16 8 kHz) caller frames; records + publishes them, tracks speech for the kickoff."""
        try:
            while not self.stop.is_set():
                msg = json.loads(await self.ws.receive_text())
                ev = msg.get("event")
                if ev == "media":
                    ulaw = base64.b64decode(msg["media"]["payload"])
                    pcm8 = ulaw_to_pcm16(ulaw)
                    pcm16 = upsample_8k_to_16k(pcm8).tobytes()
                    self.recorder.caller(pcm16)
                    _publish(self.ai_call.id, 0, pcm16)
                    if max((abs(x) for x in pcm8), default=0) > 1200:
                        self.heard = time.monotonic()
                    yield ulaw, pcm8, pcm16
                elif ev == "stop":
                    self.end_reason = self.end_reason or "caller hung up"
                    break
        except Exception as exc:
            self.end_reason = self.end_reason or f"stream closed: {exc}"
        finally:
            self.stop.set()

    async def supervise(self, kick) -> None:
        """Kick off the opening line, enforce the time limit, and hang up after end_call."""
        while not self.stop.is_set():
            now = time.monotonic()
            if not self.kicked:
                # Let the callee say "Hello?" first; speak after a short pause, or after 3.5 s regardless.
                if (self.heard and now - self.heard > 0.7) or now - self.t0 > 3.5:
                    self.kicked = True
                    await kick()
            if self.hangup_at and now >= self.hangup_at:
                try:
                    await asyncio.to_thread(hang_up, self.db, self.ai_call)
                except PhoneAgentError as exc:
                    log.warning("Hang-up failed: %s", exc)
                self.stop.set()
                break
            if now - self.t0 > MAX_SECONDS:
                self.end_reason = "time limit"
                self.stop.set()
                break
            await asyncio.sleep(0.2)

    async def run(self, *workers) -> None:
        tasks = [asyncio.create_task(w) for w in workers]
        await self.stop.wait()
        for t in tasks:
            t.cancel()

    async def finalize(self) -> None:
        """Recording, transcript, score, lead outcome — while the request is still open (Cloud Run CPU)."""
        from app.services import live_agent
        from app.timeutil import utcnow
        db, ai_call = self.db, self.ai_call
        _publish(ai_call.id, 3)
        db.refresh(ai_call)
        ai_call.status = "completed"
        ai_call.duration_seconds = ai_call.duration_seconds or self.recorder.seconds()
        ai_call.transcript = self.transcript.text()
        if self.end_reason == "voicemail":
            ai_call.answered_by = "voicemail"
        live_agent.save_recording(ai_call, self.recorder)
        self.agent.last_used_at = utcnow()
        if ai_call.transcript:
            try:
                result = await asyncio.to_thread(live_agent.score_call, db, self.flow, ai_call.transcript)
                ai_call.score = result["score"]
                ai_call.summary = live_agent.format_summary(result)
            except Exception as exc:
                ai_call.error = f"Scoring unavailable: {exc}"
        if self.end_reason and not ai_call.summary:
            ai_call.summary = f"Call ended: {self.end_reason}."
        db.commit()
        if self.real_call:
            try:
                await asyncio.to_thread(_finalize_lead, db, ai_call)
                db.commit()
            except Exception as exc:
                log.exception("Lead call finalize failed")
                ai_call.error = f"Couldn't log the call on the lead: {exc}"
                db.commit()


async def _gemini_engine(call: _Call) -> None:
    """Gemini Live on Vertex AI: PCM16 16 kHz in, 24 kHz out (resampled to/from Twilio's μ-law 8 kHz)."""
    from google.genai import types
    from app.services import live_agent

    settings = live_agent.vertex_settings(call.db)
    voice = call.ai_call.voice if call.ai_call.voice in live_agent.VOICES else settings["voice"]
    model = call.agent.model or settings["model"]
    config, _ = live_agent.session_config(call.db, call.flow, call.greet, call.lead, voice,
                                          real_call=call.real_call, phone=True,
                                          extra_instructions=call.agent.instructions)
    genai = live_agent.client(call.db)  # keep a reference: the SDK closes its HTTP client on GC
    async with genai.aio.live.connect(model=model, config=config) as session:

        async def kick() -> None:
            await session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=call.kickoff)]), turn_complete=True)

        async def from_twilio() -> None:
            async for _ulaw, _pcm8, pcm16 in call.twilio_audio():
                if call.kicked:
                    # Before the opening line, the callee's "Hello?" only times our
                    # greeting — forwarding it would make the agent answer it as well.
                    await session.send_realtime_input(audio=types.Blob(data=pcm16, mime_type="audio/pcm;rate=16000"))

        async def from_gemini() -> None:
            try:
                while not call.stop.is_set():
                    async for m in session.receive():
                        if m.tool_call:
                            for fc in m.tool_call.function_calls or []:
                                if fc.name == "end_call":
                                    call.end_call((fc.args or {}).get("reason"))
                                await session.send_tool_response(function_responses=[types.FunctionResponse(
                                    id=fc.id, name=fc.name, response={"ok": True})])
                        sc = m.server_content
                        if sc is None:
                            continue
                        if sc.interrupted:
                            await call.barge_in()
                        if sc.model_turn:
                            for part in sc.model_turn.parts or []:
                                if part.inline_data and part.inline_data.data:
                                    call.recorder.agent(part.inline_data.data)
                                    pcm8 = downsample_24k_to_8k(_pcm(part.inline_data.data))
                                    await call.play(pcm16_to_ulaw(pcm8), pcm8)
                        if sc.input_transcription and sc.input_transcription.text:
                            call.transcript.add("caller", sc.input_transcription.text)
                        if sc.output_transcription and sc.output_transcription.text:
                            call.transcript.add("agent", sc.output_transcription.text)
            except Exception as exc:
                if not call.stop.is_set():
                    call.end_reason = call.end_reason or f"agent disconnected: {exc}"
                    log.warning("Gemini Live (phone) ended: %s", exc)
            finally:
                call.stop.set()

        await call.run(from_twilio(), from_gemini(), call.supervise(kick))


async def _grok_engine(call: _Call) -> None:
    """xAI Grok Voice (realtime WebSocket): Twilio's μ-law 8 kHz passes straight through both ways."""
    import websockets
    from app.services.agents import credentials
    from app.services.live_agent import END_CALL_REASONS

    creds = credentials(call.db, call.agent)
    model = call.agent.model or "grok-voice-latest"
    voice = call.ai_call.voice if call.ai_call.voice in GROK_VOICES else (
        call.agent.voice if call.agent.voice in GROK_VOICES else GROK_DEFAULT_VOICE)
    keyterms = [t for t in {call.greet, "DRE Capital", "Direct Real Estate Capital"} if t and len(t) <= 50]
    session = {
        "instructions": call.instructions,
        "voice": voice,
        "turn_detection": {"type": "server_vad"},
        "audio": {"input": {"format": {"type": "audio/pcmu"},
                            "transcription": {"model": "grok-transcribe", "language_hint": "en", "keyterms": keyterms}},
                  "output": {"format": {"type": "audio/pcmu"}}},
        "tools": [{"type": "function", "name": "end_call", "description": "Hang up the phone call after saying goodbye.",
                   "parameters": {"type": "object", "properties": {"reason": {
                       "type": "string", "enum": END_CALL_REASONS}},
                       "required": ["reason"]}}],
    }
    # Grok re-sends the caller's transcript as it grows (cumulative, several "completed" per
    # utterance) — keep one transcript turn per input item and replace its text in place.
    caller_turns: dict[str, list[str]] = {}

    def caller_said(item_id: Optional[str], text: str) -> None:
        if not text.strip():
            return
        turn = caller_turns.get(item_id or "")
        if turn is None:
            turn = ["caller", text]
            call.transcript.turns.append(turn)
            caller_turns[item_id or ""] = turn
        else:
            turn[1] = text

    async with websockets.connect(f"{GROK_REALTIME_URL}?model={model}", max_size=None,
                                  additional_headers={"Authorization": f"Bearer {creds['xai_api_key']}"}) as grok:
        await grok.send(json.dumps({"type": "session.update", "session": session}))

        async def kick() -> None:
            await grok.send(json.dumps({"type": "conversation.item.create", "item": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": call.kickoff}]}}))
            await grok.send(json.dumps({"type": "response.create"}))

        async def from_twilio() -> None:
            async for ulaw, _pcm8, _pcm16 in call.twilio_audio():
                if call.kicked:
                    await grok.send(json.dumps({"type": "input_audio_buffer.append",
                                                "audio": base64.b64encode(ulaw).decode()}))

        async def from_grok() -> None:
            try:
                async for raw in grok:
                    e = json.loads(raw)
                    t = e.get("type", "")
                    if t in ("response.output_audio.delta", "response.audio.delta"):
                        ulaw = base64.b64decode(e.get("delta") or "")
                        if ulaw:
                            pcm8 = ulaw_to_pcm16(ulaw)
                            call.recorder.agent(upsample_8k_to_24k(pcm8).tobytes())
                            await call.play(ulaw, pcm8)
                    elif t == "input_audio_buffer.speech_started":
                        # The caller started talking. Grok cancels any response it's still generating, but
                        # audio already sent to Twilio keeps playing — clear it if the agent is still audible.
                        remaining = call.agent_sent_until - time.monotonic()
                        log.info("Grok speech_started; agent audio still queued %.2fs", remaining)
                        if remaining > 0:
                            await call.barge_in()
                    elif t in ("response.output_audio_transcript.delta", "response.audio_transcript.delta"):
                        call.transcript.add("agent", e.get("delta") or "")
                    elif t in ("conversation.item.input_audio_transcription.updated",
                               "conversation.item.input_audio_transcription.completed"):
                        caller_said(e.get("item_id"), e.get("transcript") or "")
                    elif t == "response.function_call_arguments.done":
                        if e.get("name") == "end_call":
                            try:
                                call.end_call(json.loads(e.get("arguments") or "{}").get("reason"))
                            except ValueError:
                                call.end_call(None)
                        # acknowledge without asking for another response — the call is ending
                        await grok.send(json.dumps({"type": "conversation.item.create", "item": {
                            "type": "function_call_output", "call_id": e.get("call_id"), "output": '{"ok": true}'}}))
                    elif t == "error":
                        log.warning("Grok Voice error: %s", e.get("error"))
            except Exception as exc:
                if not call.stop.is_set():
                    call.end_reason = call.end_reason or f"agent disconnected: {exc}"
                    log.warning("Grok Voice (phone) ended: %s", exc)
            finally:
                call.stop.set()

        await call.run(from_twilio(), from_grok(), call.supervise(kick))


async def bridge(websocket, db: Session) -> None:
    """Run one Twilio media stream ⇄ voice-agent call (Grok Voice or Gemini Live) until either side ends."""
    # 1. Wait for Twilio's "start" message (carries our signed parameters).
    while True:
        msg = json.loads(await websocket.receive_text())
        if msg.get("event") == "start":
            start = msg["start"]
            params = start.get("customParameters") or {}
            ai_call = db.get(AiCall, params.get("ai_call_id", ""))
            if ai_call is None or not verify_stream_token(params.get("token", ""), ai_call.id):
                log.warning("Rejected Twilio stream with bad token")
                return
            break
        if msg.get("event") == "stop":
            return
    call = _Call(websocket, db, ai_call, start["streamSid"])
    ai_call.status = "in-progress"
    db.commit()
    try:
        await (_grok_engine if call.agent.provider == "twilio_grok" else _gemini_engine)(call)
    except Exception as exc:
        log.exception("Voice engine failed")
        call.end_reason = call.end_reason or f"voice engine error: {exc}"
        ai_call.error = f"Voice engine error: {exc}"[:500]
    await call.finalize()
