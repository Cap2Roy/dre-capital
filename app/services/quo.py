"""Quo (formerly OpenPhone) calling provider.

Quo's public API (https://api.quo.com/v1) cannot *place* calls — calls are
dialed by the operator in the Quo app.  So the integration is:

1. ``dial_url`` hands the seller's number to the Quo desktop/mobile app via a
   ``tel:`` link (Quo registers as the system's default calling app).  The
   operator presses Call — manual dial, no autodialer.
2. After hanging up, :func:`sync_call` finds that call through the API and pulls
   back the status, duration, Quo's AI summary + next steps, and the transcript.

Auth: the raw API key in the ``Authorization`` header (no "Bearer" prefix).
Quo sits behind Cloudflare, which rejects some default client user agents, so an
explicit User-Agent is always sent.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from sqlalchemy.orm import Session

from app.models import Call, CallOutcome, Phone

BASE_URL = "https://api.quo.com"
_TIMEOUT = 20.0
_USER_AGENT = "DRE-Capital-CRM/2.2"

# Quo call statuses that mean the seller never picked up.
_UNANSWERED = {"no-answer", "missed", "busy", "canceled", "failed", "abandoned"}


class QuoError(RuntimeError):
    """Quo API is unconfigured, unreachable, or rejected the request."""


def to_e164(number: str) -> str:
    """Normalize a stored phone number to E.164 (US default)."""
    digits = re.sub(r"\D", "", number or "")
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits if digits else ""


def dial_url(number: str) -> str:
    """``tel:`` link the Quo app opens with the number pre-filled."""
    return "tel:" + to_e164(number)


def _api_key(db: Session) -> str:
    from app.services.settings import get_setting

    key = (get_setting(db, "quo_api_key") or "").strip()
    if not key:
        raise QuoError("Quo API key is not configured (Settings → Calling).")
    return key


def _request(db: Session, method: str, path: str, *, params: Any = None, json: Any = None) -> dict:
    headers = {
        "Authorization": _api_key(db),
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }
    try:
        resp = httpx.request(method, BASE_URL + path, headers=headers, params=params,
                             json=json, timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        raise QuoError(f"Quo API unreachable: {exc}") from exc
    if resp.status_code == 401:
        raise QuoError("Quo rejected the API key (401). Check Settings → Calling.")
    if resp.status_code == 404:
        return {}
    if resp.status_code >= 400:
        raise QuoError(f"Quo API error {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def list_phone_numbers(db: Session) -> list[dict]:
    data = _request(db, "GET", "/v1/phone-numbers").get("data", [])
    return [{"id": n["id"], "number": n["number"], "name": n.get("name")} for n in data]


def resolve_line(db: Session) -> dict:
    """The Quo line calls are placed from: ``{"id": "PN…", "number": "+1…"}``.

    Uses the configured number ID, else the workspace's first number, and
    persists what it found so later lookups skip the API round-trip.
    """
    from app.services.settings import get_setting, set_setting

    pn_id = (get_setting(db, "quo_phone_number_id") or "").strip()
    number = (get_setting(db, "quo_from_number") or "").strip()
    if pn_id and number:
        return {"id": pn_id, "number": number}
    numbers = list_phone_numbers(db)
    if not numbers:
        raise QuoError("No phone numbers found in this Quo workspace.")
    match = next((n for n in numbers if n["id"] == pn_id), None) if pn_id else numbers[0]
    if match is None:
        raise QuoError(f"Quo phone number ID {pn_id} not found in this workspace.")
    set_setting(db, "quo_phone_number_id", match["id"])
    set_setting(db, "quo_from_number", match["number"])
    db.flush()
    return {"id": match["id"], "number": match["number"]}


def connection_status(db: Session) -> dict:
    """Live check of the key + line, safe to show in the UI (never returns the key)."""
    try:
        numbers = list_phone_numbers(db)
        line = resolve_line(db)
        return {"ok": True, "line": line, "numbers": numbers, "error": None}
    except QuoError as exc:
        return {"ok": False, "line": None, "numbers": [], "error": str(exc)}


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def find_call(db: Session, participant: str, since: datetime) -> Optional[dict]:
    """Most recent outgoing Quo call to ``participant`` created at/after ``since``."""
    line = resolve_line(db)
    since_utc = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
    params = {
        "phoneNumberId": line["id"],
        "participants": [to_e164(participant)],
        "maxResults": 10,
        "createdAfter": since_utc.isoformat().replace("+00:00", "Z"),
    }
    calls = _request(db, "GET", "/v1/calls", params=params).get("data", [])
    outgoing = [c for c in calls if c.get("direction") == "outgoing"] or calls
    if not outgoing:
        return None
    return max(outgoing, key=lambda c: _parse_ts(c.get("createdAt")) or since_utc)


def get_summary(db: Session, quo_call_id: str) -> dict:
    data = _request(db, "GET", f"/v1/call-summaries/{quo_call_id}").get("data") or {}
    return {
        "status": data.get("status", "absent"),
        "summary": data.get("summary") or [],
        "next_steps": data.get("nextSteps") or [],
    }


def get_transcript(db: Session, quo_call_id: str) -> dict:
    data = _request(db, "GET", f"/v1/call-transcripts/{quo_call_id}").get("data") or {}
    return {"status": data.get("status", "absent"), "dialogue": data.get("dialogue") or []}


def _format_summary(summary: dict) -> Optional[str]:
    if summary["status"] != "completed" or not (summary["summary"] or summary["next_steps"]):
        return None
    lines = [f"• {s}" for s in summary["summary"]]
    if summary["next_steps"]:
        lines.append("Next steps:")
        lines.extend(f"→ {s}" for s in summary["next_steps"])
    return "\n".join(lines)


def _format_transcript(transcript: dict, operator_number: str) -> Optional[str]:
    if transcript["status"] != "completed" or not transcript["dialogue"]:
        return None
    out = []
    for turn in transcript["dialogue"]:
        who = "Us" if turn.get("userId") or turn.get("identifier") == operator_number else "Seller"
        out.append(f"{who}: {turn.get('content', '').strip()}")
    return "\n".join(out)


def sync_call(db: Session, call: Call) -> dict:
    """Pull the Quo record for a CRM call and store duration/summary/transcript.

    Matches the newest outgoing Quo call to the call's phone placed after the
    CRM call row was created (minus a small clock-skew allowance).  Summary and
    transcript are generated by Quo a little after hang-up, so their status may
    be ``in-progress`` on the first sync — re-sync later to fill them in.
    """
    phone = db.get(Phone, call.phone_id) if call.phone_id else None
    if phone is None:
        raise QuoError("This call has no phone number to match in Quo.")

    if call.call_sid:
        record = _request(db, "GET", f"/v1/calls/{call.call_sid}").get("data")
    else:
        record = find_call(db, phone.number, (call.created_at or datetime.now()) - timedelta(minutes=5))
    if not record:
        return {"found": False, "message": "No matching Quo call yet — place the call in Quo, then sync again."}

    call.call_sid = record["id"]
    call.provider = "quo"
    if record.get("duration") is not None:
        call.duration_seconds = int(record["duration"])

    status = record.get("status", "")
    suggested = CallOutcome.NO_ANSWER.value if status in _UNANSWERED else None

    summary = get_summary(db, record["id"])
    transcript = get_transcript(db, record["id"])
    line = resolve_line(db)
    formatted_summary = _format_summary(summary)
    formatted_transcript = _format_transcript(transcript, line["number"])
    if formatted_summary:
        call.ai_summary = formatted_summary
    if formatted_transcript:
        call.transcript = formatted_transcript
    db.flush()

    return {
        "found": True,
        "quo_call_id": record["id"],
        "status": status,
        "duration_seconds": call.duration_seconds,
        "suggested_outcome": suggested,
        "summary_status": summary["status"],
        "transcript_status": transcript["status"],
        "ai_summary": call.ai_summary,
        "transcript": call.transcript,
    }
