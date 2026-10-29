"""Live AI agent ("Sam") on Google Gemini Live API (Vertex AI).

The browser streams the tester's microphone (PCM16 LE, 16 kHz) to the CRM over
a WebSocket; the CRM relays it to a Gemini Live session and streams the agent's
voice back (PCM16 LE, 24 kHz). The agent's instructions come from the call
flow (role, ordered questions, rules, close, scoring) — and, when a lead is
attached, the property's facts on record — via ``ai_agent.build_agent_script``.

Auth: Application Default Credentials (the Cloud Run service account in prod;
``gcloud auth application-default login`` locally). No API key.

After the call, both voices are mixed into one 16 kHz WAV and the transcript
is scored Hot/Warm/Cold against the flow's rubric with a Gemini text model.
"""
from __future__ import annotations

import io
import json
import os
import re
import time
import wave
from array import array
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.models import AiCall, CallFlow, Lead

DEFAULT_MODEL = "gemini-3.8-live"
DEFAULT_VOICE = "Charon"
DEFAULT_LOCATION = "us-central1"
TEXT_MODEL = "gemini-2.5-flash"
MAX_SECONDS = 6 * 60
IN_RATE, OUT_RATE = 16000, 24000
RECORDINGS_DIR = Path(os.getenv("RECORDINGS_DIR", "recordings"))

VOICES = [
    "Charon", "Puck", "Kore", "Fenrir", "Aoede", "Leda", "Orus", "Zephyr", "Achird", "Algenib",
    "Algieba", "Alnilam", "Autonoe", "Callirrhoe", "Despina", "Enceladus", "Erinome", "Gacrux",
    "Iapetus", "Laomedeia", "Pulcherrima", "Rasalgethi", "Sadachbia", "Sadaltager", "Schedar",
    "Sulafat", "Umbriel", "Vindemiatrix", "Zubenelgenubi", "Achernar",
]


class LiveAgentError(RuntimeError):
    """Vertex AI is unreachable, unauthenticated, or rejected the session."""


# ── Vertex client ───────────────────────────────────────────────────────────

def vertex_settings(db: Session) -> dict:
    from app.services.settings import get_setting
    return {
        "model": (get_setting(db, "vertex_live_model") or "").strip() or DEFAULT_MODEL,
        "voice": (get_setting(db, "vertex_live_voice") or "").strip() or DEFAULT_VOICE,
        "location": (get_setting(db, "vertex_location") or "").strip() or DEFAULT_LOCATION,
    }


def _credentials():
    """ADC credentials + project. Overridable in tests."""
    import google.auth
    try:
        creds, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    except Exception as exc:  # DefaultCredentialsError and friends
        raise LiveAgentError(
            "No Google Cloud credentials. On Cloud Run the service account is used automatically; "
            "locally run `gcloud auth application-default login`.") from exc
    return creds, os.getenv("GOOGLE_CLOUD_PROJECT") or project


def client(db: Session):
    from google import genai
    creds, project = _credentials()
    if not project:
        raise LiveAgentError("Google Cloud project unknown — set GOOGLE_CLOUD_PROJECT.")
    return genai.Client(vertexai=True, project=project, location=vertex_settings(db)["location"], credentials=creds)


def status(db: Session) -> dict:
    s = vertex_settings(db)
    try:
        creds, project = _credentials()
        return {"configured": bool(project), "project": project, **s, "voices": VOICES, "error": None}
    except LiveAgentError as exc:
        return {"configured": False, "project": None, **s, "voices": VOICES, "error": str(exc)}


# ── Session config ──────────────────────────────────────────────────────────

def session_config(db: Session, flow: CallFlow, greet_name: str, lead: Optional[Lead], voice: str, *,
                   real_call: bool = False, phone: bool = False, extra_instructions: Optional[str] = None):
    """(LiveConnectConfig, kickoff_text) for a call-flow agent.

    ``phone`` adds the ``end_call`` tool so the agent can hang up itself (wrong
    number, stop request, voicemail, call finished). ``real_call`` makes it verify
    it reached the owner before anything else.
    """
    from google.genai import types
    from app.services.ai_agent import build_agent_script

    script = build_agent_script(db, flow, greet_name, lead, real_call=real_call)
    instruction = (
        script["task"]
        + "\n\nThis is a live phone conversation. Keep every turn short (1–2 sentences), speak naturally, "
          "let the other person finish, and stop talking immediately if they interrupt. "
          f"Start the call by saying exactly: \"{script['first_sentence']}\""
    )
    if extra_instructions:
        instruction += "\n\nADDITIONAL INSTRUCTIONS FROM YOUR TEAM:\n" + extra_instructions.strip()
    tools = None
    if phone:
        instruction += (
            "\n\nYou can hang up with the end_call tool. Say a brief polite goodbye first, then call end_call. "
            "Use it when: the call is complete; it's a wrong number or not the owner; they ask you to stop or remove "
            "them; you reached voicemail or an answering machine (do not leave a message); or they're hostile.")
        tools = [types.Tool(function_declarations=[types.FunctionDeclaration(
            name="end_call",
            description="Hang up the phone call after saying goodbye.",
            parameters={"type": "OBJECT", "properties": {"reason": {"type": "STRING", "enum": [
                "completed", "wrong_number", "stop_request", "voicemail", "not_interested", "hostile", "other"]}},
                "required": ["reason"]},
        )])]
    cfg = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=instruction,
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)),
            language_code="en-US",
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        tools=tools,
    )
    kickoff = f"(The call has connected and {greet_name} picked up. Say your opening line now.)"
    return cfg, kickoff


# ── Recording ───────────────────────────────────────────────────────────────

class CallRecorder:
    """Mixes caller (16 kHz) and agent (24 kHz) audio onto one 16 kHz timeline.

    Caller audio lands at its wall-clock arrival time. Agent audio arrives
    faster than real time, so it's laid end-to-end from "now" — the same way
    the browser plays it — and an interruption rewinds the agent cursor to now.
    """

    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.buf = array("h")
        self.agent_cursor = 0

    def _now(self) -> int:
        return int((time.monotonic() - self.t0) * IN_RATE)

    def _mix(self, start: int, samples: array) -> None:
        end = start + len(samples)
        if len(self.buf) < end:
            self.buf.extend(array("h", bytes(2 * (end - len(self.buf)))))
        buf = self.buf
        for i, s in enumerate(samples):
            v = buf[start + i] + s
            buf[start + i] = 32767 if v > 32767 else (-32768 if v < -32768 else v)

    @staticmethod
    def _pcm(data: bytes) -> array:
        a = array("h")
        a.frombytes(data[: len(data) - (len(data) % 2)])
        return a

    def caller(self, data: bytes) -> None:
        self._mix(self._now(), self._pcm(data))

    def agent(self, data: bytes) -> None:
        src = self._pcm(data)
        # 24 kHz → 16 kHz: take every 1.5th sample (linear interpolation).
        n = (len(src) * 2) // 3
        out = array("h", bytes(2 * n))
        for i in range(n):
            pos = i * 1.5
            j = int(pos)
            frac = pos - j
            a = src[j]
            b = src[j + 1] if j + 1 < len(src) else a
            out[i] = int(a + (b - a) * frac)
        start = max(self.agent_cursor, self._now())
        self._mix(start, out)
        self.agent_cursor = start + n

    def interrupted(self) -> None:
        self.agent_cursor = self._now()

    def seconds(self) -> int:
        return int(time.monotonic() - self.t0)

    def wav_bytes(self) -> bytes:
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(IN_RATE)
            w.writeframes(self.buf.tobytes())
        return out.getvalue()


def save_recording(ai_call: AiCall, recorder: CallRecorder) -> None:
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    path = RECORDINGS_DIR / f"{ai_call.id}.wav"
    path.write_bytes(recorder.wav_bytes())
    ai_call.recording_url = f"file:{path}"


# ── Transcript + post-call scoring ──────────────────────────────────────────

class Transcript:
    """Accumulates streamed transcription fragments into speaker turns."""

    def __init__(self) -> None:
        self.turns: list[list[str]] = []   # [who, text]

    def add(self, who: str, fragment: str) -> None:
        # Gemini marks non-speech as "<noise>", "<no speech detected>" — not words anyone said.
        fragment = re.sub(r"<[^<>]{1,40}>", "", fragment or "")
        if not fragment:
            return
        if self.turns and self.turns[-1][0] == who:
            self.turns[-1][1] += fragment
        else:
            self.turns.append([who, fragment])

    def text(self) -> Optional[str]:
        label = {"agent": "Sam", "caller": "Them"}
        lines = [f"{label[w]}: {' '.join(t.split())}" for w, t in self.turns if t.strip()]
        return "\n".join(lines) or None


def score_call(db: Session, flow: CallFlow, transcript: str) -> dict:
    """Hot/Warm/Cold + summary from the transcript, using the flow's rubric."""
    from google.genai import types

    rubric = json.loads(flow.scoring or "{}")
    questions = json.loads(flow.questions or "[]")
    prompt = (
        "You review a real-estate seller qualification call transcript. 'Sam' is our agent, 'Them' is the seller.\n"
        f"Questions Sam should confirm: {json.dumps(questions)}\n"
        f"Scoring rubric: {json.dumps(rubric)}\n\n"
        f"Transcript:\n{transcript}\n\n"
        "Return JSON: {\"score\": \"hot\"|\"warm\"|\"cold\"|null (null if not enough was answered), "
        "\"reasons\": [short strings], \"summary\": \"2-3 sentences\", \"next_steps\": [short strings], "
        "\"callback\": \"agreed callback time or null\"}"
    )
    # Keep a reference: the SDK closes its HTTP client when the Client is garbage-collected.
    genai = client(db)
    resp = genai.models.generate_content(
        model=TEXT_MODEL, contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2),
    )
    data = json.loads(resp.text or "{}")
    score = (data.get("score") or "").lower() or None
    return {**data, "score": score if score in ("hot", "warm", "cold") else None}


def format_summary(result: dict) -> str:
    lines = [result.get("summary") or ""]
    if result.get("reasons"):
        lines.append("Why: " + "; ".join(r.rstrip(". ") for r in result["reasons"]))
    if result.get("callback"):
        lines.append(f"Callback: {result['callback']}")
    if result.get("next_steps"):
        lines.append("Next steps: " + "; ".join(s.rstrip(". ") for s in result["next_steps"]))
    return "\n".join(l for l in lines if l)
