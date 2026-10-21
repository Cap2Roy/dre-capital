"""AI voice agent test calls via Bland (https://docs.bland.ai).

The agent speaks a call flow (e.g. "Sam for Eylon"): the flow's role, rules,
ordered questions, close, and scoring become the agent's prompt, and the filled
opening line is its first sentence.  When a lead is attached, the property's
facts on record (from the call guide) are added so the agent rehearses that
exact conversation — but the call always goes to the tester's own number.

Bland endpoints used (auth: ``Authorization: Bearer <key>``):
- POST /v1/calls              start a call (record=true)
- GET  /v1/calls/{id}         status, duration, recording, transcript, summary
- POST /v1/calls/{id}/listen  live-listen WebSocket URL (PCM16 LE, 16 kHz mono)
- POST /v1/calls/{id}/stop    hang up
- GET  /v1/me, /v1/voices     key check + voice list
"""
from __future__ import annotations

import re
from typing import Any, Iterator, Optional

import httpx
from sqlalchemy.orm import Session

from app.models import AiCall, CallFlow, Lead, User

BASE_URL = "https://api.bland.ai"
_TIMEOUT = 20.0
MAX_DURATION_MIN = 6          # the script targets < 4 minutes; hard stop at 6
FINAL_STATUSES = {"completed", "no-answer", "busy", "failed", "canceled", "unknown"}
_PLACEHOLDER = re.compile(r"\[([^\[\]]+)\]")


class AiAgentError(RuntimeError):
    """Bland is unconfigured, unreachable, or rejected the request."""


# ── HTTP ────────────────────────────────────────────────────────────────────

def api_key(db: Session) -> str:
    from app.services.settings import get_setting
    return (get_setting(db, "bland_api_key") or "").strip()


def _request(key: str, method: str, path: str, *, json_body: Any = None) -> dict:
    if not key:
        raise AiAgentError("No Bland API key — an admin can add it in Settings → Calling & API Keys → AI Voice Agent.")
    try:
        resp = httpx.request(method, BASE_URL + path, json=json_body, timeout=_TIMEOUT,
                             headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise AiAgentError(f"Bland API unreachable: {exc}") from exc
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code == 401:
        raise AiAgentError("Bland rejected the API key (401).")
    if resp.status_code >= 400 or data.get("status") == "error":
        raise AiAgentError(_error_message(data) or f"Bland API error {resp.status_code}")
    return data


def _error_message(data: dict) -> str:
    errors = data.get("errors") or []
    parts = []
    for e in errors:
        parts.append(e.get("message") or e.get("error") if isinstance(e, dict) else str(e))
    return "; ".join(p for p in parts if p) or data.get("message") or ""


def account_status(db: Session) -> dict:
    """Validate the key; return balance + curated voice names (never the key)."""
    key = api_key(db)
    me = _request(key, "GET", "/v1/me")
    voices = _request(key, "GET", "/v1/voices").get("voices", [])
    curated = sorted({v["name"] for v in voices if "Bland Curated" in (v.get("tags") or []) and v.get("name")})
    return {
        "ok": True,
        "balance": (me.get("billing") or {}).get("current_balance"),
        "account_status": me.get("status"),
        "voices": curated or sorted({v["name"] for v in voices if v.get("name")})[:40],
    }


# ── Prompt ──────────────────────────────────────────────────────────────────

def _speakable(text: str) -> str:
    """Drop brackets from placeholders nobody filled ([Dallas-Fort Worth] → Dallas-Fort Worth)."""
    return _PLACEHOLDER.sub(lambda m: m.group(1), text or "")


def build_agent_script(db: Session, flow: CallFlow, greet_name: str, lead: Optional[Lead] = None) -> dict:
    """``{"task": prompt, "first_sentence": opening}`` for the Bland agent."""
    import json
    from app.services.call_guide import _placeholder_values, build_call_guide, fill

    if lead is not None:
        guide = build_call_guide(db, lead, flow)
        values = _placeholder_values(db, lead)
        questions = guide["questions"]
        context = guide["context"]
    else:
        from app.services.settings import get_setting
        company = get_setting(db, "company_name") or "Direct Real Estate Capital LLC"
        values = {"company name": company, "company": company,
                  "property address": "the property you asked us about", "address": "the property you asked us about"}
        questions = [{"index": i, "text": q, "known": [], "tip": None, "mode": "ask"}
                     for i, q in enumerate(json.loads(flow.questions or "[]"))]
        context = []
    values["lead name"] = values["owner name"] = greet_name

    role = _speakable(fill(flow.role, values)[0])
    opening = _speakable(fill(flow.opening, values)[0])
    close = fill(flow.close, values)[0]
    rules = [_speakable(fill(r, values)[0]) for r in json.loads(flow.rules or "[]")]
    scoring = json.loads(flow.scoring or "{}")

    q_lines = []
    for q in questions:
        line = f"{q['index'] + 1}. {_speakable(q['text'])}"
        # Never hand the AI our ARV/MAO/repair numbers — it must not be able to quote them.
        notes = [k for k in (q.get("known") or []) if not re.search(r"ARV|MAO|repair estimate|\$", k)]
        if q.get("tip"):
            notes.append(q["tip"])
        if notes:
            line += "\n   (Internal notes — use to confirm, never read numbers aloud: " + " ".join(notes) + ")"
        q_lines.append(line)

    parts = [
        role,
        "",
        "You are on a live phone call. Speak naturally and briefly, one question at a time, and wait for the answer.",
        f"You already opened with: \"{opening}\"",
        "If they say they never inquired or aren't interested: apologize, thank them, and end the call.",
    ]
    if context:
        parts += ["", "WHAT WE ALREADY KNOW (internal):"] + [f"- {c}" for c in context]
    parts += ["", "CONFIRM THESE DETAILS, IN ORDER (skip any they decline — don't push):"] + q_lines
    parts += ["", "RULES:"] + [f"- {r}" for r in rules]
    parts += [
        "",
        f"CLOSE with: \"{_speakable(close.replace('[agreed time]', 'at the time they gave you'))}\" "
        "— use the callback time they agreed to, then end the call politely.",
    ]
    if scoring:
        parts += ["", "SCORING (internal, never mention): "
                  + " ".join(f"{k.title()}: {v}" for k, v in scoring.items())]
    return {"task": "\n".join(parts), "first_sentence": opening}


# ── Calls ───────────────────────────────────────────────────────────────────

def start_call(db: Session, ai_call: AiCall, flow: CallFlow, lead: Optional[Lead]) -> AiCall:
    script = build_agent_script(db, flow, ai_call.greet_name or "there", lead)
    body = {
        "phone_number": ai_call.to_number,
        "task": script["task"],
        "first_sentence": script["first_sentence"],
        "record": True,
        "wait_for_greeting": True,
        "max_duration": MAX_DURATION_MIN,
        "voicemail": {"action": "hangup"},
        "metadata": {"source": "dre-capital-crm", "ai_call_id": ai_call.id, "test_call": True},
    }
    if ai_call.voice:
        body["voice"] = ai_call.voice
    data = _request(api_key(db), "POST", "/v1/calls", json_body=body)
    ai_call.provider_call_id = data.get("call_id")
    ai_call.status = "queued"
    db.flush()
    return ai_call


def _format_transcript(turns: list[dict]) -> Optional[str]:
    lines = []
    for t in turns or []:
        who = t.get("user")
        if who in ("assistant", "agent"):
            lines.append(f"Sam: {t.get('text', '').strip()}")
        elif who == "user":
            lines.append(f"Them: {t.get('text', '').strip()}")
    return "\n".join(lines) or None


def refresh(db: Session, ai_call: AiCall) -> AiCall:
    """Pull the latest status, duration, recording, transcript, and summary."""
    if not ai_call.provider_call_id:
        return ai_call
    # Done refreshing once final — completed calls wait for recording + transcript.
    if ai_call.status in FINAL_STATUSES and (ai_call.status != "completed" or (ai_call.recording_url and ai_call.transcript)):
        return ai_call
    d = _request(api_key(db), "GET", f"/v1/calls/{ai_call.provider_call_id}")
    queue = (d.get("queue_status") or "").lower()
    status = (d.get("status") or "").lower()
    if queue.endswith("error") or status == "failed":
        ai_call.status = "failed"
        ai_call.error = d.get("error_message") or ai_call.error
    elif d.get("completed") or queue == "complete" or status in FINAL_STATUSES:
        ai_call.status = status if status in FINAL_STATUSES else "completed"
    elif queue == "started":
        ai_call.status = "in-progress"
    else:
        ai_call.status = "queued"
    ai_call.answered_by = d.get("answered_by") or ai_call.answered_by
    if d.get("corrected_duration") not in (None, ""):
        try:
            ai_call.duration_seconds = int(float(d["corrected_duration"]))
        except (TypeError, ValueError):
            pass
    elif d.get("call_length") is not None:
        ai_call.duration_seconds = int(round(float(d["call_length"]) * 60))
    ai_call.recording_url = d.get("recording_url") or ai_call.recording_url
    ai_call.transcript = _format_transcript(d.get("transcripts") or []) or ai_call.transcript
    ai_call.summary = d.get("summary") or ai_call.summary
    if d.get("price") is not None:
        ai_call.price = float(d["price"])
    if d.get("error_message") and ai_call.status == "failed":
        ai_call.error = d["error_message"]
    db.flush()
    return ai_call


def listen_url(db: Session, ai_call: AiCall) -> str:
    """WebSocket URL streaming the live call audio (PCM16 LE, 16 kHz, mono)."""
    if not ai_call.provider_call_id:
        raise AiAgentError("The call hasn't been placed yet.")
    try:
        data = _request(api_key(db), "POST", f"/v1/calls/{ai_call.provider_call_id}/listen")
    except AiAgentError as exc:
        if "listen" in str(exc).lower() and "pref" in str(exc).lower():
            raise AiAgentError("Live listen is off for your Bland account — enable it in Bland → Settings "
                               "(organization preferences: live listen).") from exc
        raise
    url = (data.get("data") or {}).get("url")
    if not url:
        raise AiAgentError("Bland didn't return a listen URL (the call may not have started yet).")
    return url


def stop(db: Session, ai_call: AiCall) -> None:
    if ai_call.provider_call_id and ai_call.status not in FINAL_STATUSES:
        _request(api_key(db), "POST", f"/v1/calls/{ai_call.provider_call_id}/stop")


def stream_recording(db: Session, ai_call: AiCall) -> tuple[Iterator[bytes], str]:
    """Stream the recording through the CRM (the provider URL is never exposed)."""
    if not ai_call.recording_url:
        raise AiAgentError("No recording yet — it's ready about a minute after the call ends.")
    headers = {"Authorization": f"Bearer {api_key(db)}"} if "bland.ai" in ai_call.recording_url else {}
    client = httpx.Client(timeout=60.0, follow_redirects=True)
    resp = client.send(client.build_request("GET", ai_call.recording_url, headers=headers), stream=True)
    if resp.status_code >= 400:
        resp.close(); client.close()
        raise AiAgentError(f"Recording unavailable ({resp.status_code}).")
    media = resp.headers.get("content-type", "audio/mpeg")

    def body() -> Iterator[bytes]:
        try:
            yield from resp.iter_bytes()
        finally:
            resp.close(); client.close()

    return body(), media


def greeting_default(user: Optional[User]) -> str:
    return (user.name.split()[0] if user and user.name else "there")
