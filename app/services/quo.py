"""Quo (formerly OpenPhone) calling provider.

Quo's public API (https://api.quo.com/v1) cannot *place* calls — calls are
dialed by the operator in the Quo app.  So the integration is:

1. ``dial_url`` hands the seller's number to the Quo desktop/mobile app via a
   ``tel:`` link (Quo registers as the system's default calling app).  The
   operator presses Call — manual dial, no autodialer.
2. After hang-up the call record, Quo's AI summary + next steps, and the
   transcript come back either by webhook (automatic, needs a public HTTPS URL)
   or by :func:`sync_call` (the "Sync from Quo" button).
3. SMS goes out through the same Quo line, and inbound texts arrive by webhook
   so STOP keywords opt the number out.

Credentials: each user may save a personal Quo key (their own line); otherwise
the company key from Settings / the ``QUO_API_KEY`` env var is used.

Auth: the raw API key in the ``Authorization`` header (no "Bearer" prefix).
Quo sits behind Cloudflare, which rejects some default client user agents, so an
explicit User-Agent is always sent.

Webhook signatures (v1): header ``openphone-signature: hmac;1;<ts_ms>;<b64>``
where ``<b64>`` = base64(HMAC-SHA256(base64decode(key), ts + "." + raw_body)).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Call, CallOutcome, Phone, User
from app.timeutil import utcnow as _utcnow

BASE_URL = "https://api.quo.com"
WEBHOOK_PATH = "/api/calls/quo-webhook"
_TIMEOUT = 20.0
_USER_AGENT = "DRE-Capital-CRM/2.3"
_SIGNATURE_TOLERANCE_S = 300

# Quo call statuses that mean the seller never picked up.
_UNANSWERED = {"no-answer", "missed", "busy", "canceled", "failed", "abandoned"}

# (create-endpoint, events) for the webhooks the CRM registers.
_WEBHOOKS = [
    ("/v1/webhooks/calls", ["call.completed"]),
    ("/v1/webhooks/call-summaries", ["call.summary.completed"]),
    ("/v1/webhooks/call-transcripts", ["call.transcript.completed"]),
    ("/v1/webhooks/messages", ["message.received"]),
]


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


# ── Credentials ─────────────────────────────────────────────────────────────

def company_key(db: Session) -> str:
    from app.services.settings import get_setting
    return (get_setting(db, "quo_api_key") or "").strip()


def personal_key(user: Optional[User]) -> str:
    return ((user.quo_api_key or "").strip()) if user is not None else ""


def effective_key(db: Session, user: Optional[User] = None) -> str:
    """The caller's personal key if they saved one, else the company key."""
    return personal_key(user) or company_key(db)


def _request(api_key: str, method: str, path: str, *, params: Any = None, json_body: Any = None) -> dict:
    if not api_key:
        raise QuoError("No Quo API key — add yours in Settings → Profile, or ask an admin to set the company key.")
    headers = {"Authorization": api_key, "Accept": "application/json", "User-Agent": _USER_AGENT}
    try:
        resp = httpx.request(method, BASE_URL + path, headers=headers, params=params,
                             json=json_body, timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        raise QuoError(f"Quo API unreachable: {exc}") from exc
    if resp.status_code == 401:
        raise QuoError("Quo rejected the API key (401).")
    if resp.status_code == 404:
        return {}
    if resp.status_code >= 400:
        raise QuoError(f"Quo API error {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.content else {}


def list_phone_numbers(api_key: str) -> list[dict]:
    data = _request(api_key, "GET", "/v1/phone-numbers").get("data", [])
    return [{"id": n["id"], "number": n["number"], "name": n.get("name")} for n in data]


def resolve_line(db: Session, user: Optional[User] = None) -> dict:
    """The Quo line used for ``user``: ``{"id": "PN…", "number": "+1…", "personal": bool}``.

    Personal key → the user's own configured/auto-detected number; otherwise the
    company line from Settings.  Whatever gets auto-detected is persisted.
    """
    from app.services.settings import get_setting, set_setting

    personal = bool(personal_key(user))
    if personal:
        pn_id, number = (user.quo_phone_number_id or "").strip(), (user.quo_from_number or "").strip()
    else:
        pn_id = (get_setting(db, "quo_phone_number_id") or "").strip()
        number = (get_setting(db, "quo_from_number") or "").strip()
    if pn_id and number:
        return {"id": pn_id, "number": number, "personal": personal}

    numbers = list_phone_numbers(effective_key(db, user))
    if not numbers:
        raise QuoError("No phone numbers found in this Quo workspace.")
    match = next((n for n in numbers if n["id"] == pn_id), None) if pn_id else numbers[0]
    if match is None:
        raise QuoError(f"Quo phone number ID {pn_id} not found for this key.")
    if personal:
        user.quo_phone_number_id, user.quo_from_number = match["id"], match["number"]
    else:
        set_setting(db, "quo_phone_number_id", match["id"])
        set_setting(db, "quo_from_number", match["number"])
    db.flush()
    return {"id": match["id"], "number": match["number"], "personal": personal}


def connection_status(db: Session, user: Optional[User] = None) -> dict:
    """Live check of the key + line, safe to show in the UI (never returns the key)."""
    try:
        numbers = list_phone_numbers(effective_key(db, user))
        line = resolve_line(db, user)
        return {"ok": True, "line": line, "numbers": numbers, "error": None}
    except QuoError as exc:
        return {"ok": False, "line": None, "numbers": [], "error": str(exc)}


# ── Calls ───────────────────────────────────────────────────────────────────

def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)


def find_call(db: Session, participant: str, since_utc: datetime, user: Optional[User] = None) -> Optional[dict]:
    """Most recent outgoing Quo call to ``participant`` created at/after ``since_utc`` (naive UTC)."""
    line = resolve_line(db, user)
    params = {
        "phoneNumberId": line["id"],
        "participants": [to_e164(participant)],
        "maxResults": 10,
        "createdAfter": since_utc.replace(microsecond=0).isoformat() + "Z",
    }
    calls = _request(effective_key(db, user), "GET", "/v1/calls", params=params).get("data", [])
    outgoing = [c for c in calls if c.get("direction") == "outgoing"] or calls
    if not outgoing:
        return None
    return max(outgoing, key=lambda c: _parse_ts(c.get("createdAt")) or since_utc)


def format_summary(status: str, summary: list, next_steps: list) -> Optional[str]:
    if status != "completed" or not (summary or next_steps):
        return None
    lines = [f"• {s}" for s in summary or []]
    if next_steps:
        lines.append("Next steps:")
        lines.extend(f"→ {s}" for s in next_steps)
    return "\n".join(lines)


def format_transcript(status: str, dialogue: list, seller_number: str) -> Optional[str]:
    if status != "completed" or not dialogue:
        return None
    seller_digits = re.sub(r"\D", "", seller_number or "")[-10:]
    out = []
    for turn in dialogue:
        ident = re.sub(r"\D", "", turn.get("identifier") or "")[-10:]
        who = "Seller" if seller_digits and ident == seller_digits and not turn.get("userId") else "Us"
        out.append(f"{who}: {(turn.get('content') or '').strip()}")
    return "\n".join(out)


def _apply_call_record(call: Call, record: dict) -> Optional[str]:
    """Copy duration from a Quo call object; return a suggested outcome."""
    call.call_sid = record["id"]
    call.provider = "quo"
    if record.get("duration") is not None:
        call.duration_seconds = int(record["duration"])
    else:
        answered, completed = _parse_ts(record.get("answeredAt")), _parse_ts(record.get("completedAt"))
        if answered and completed:
            call.duration_seconds = max(0, int((completed - answered).total_seconds()))
        elif completed:
            call.duration_seconds = 0
    status = record.get("status", "")
    if status in _UNANSWERED or (status == "completed" and not record.get("answeredAt")):
        return CallOutcome.NO_ANSWER.value
    return None


def sync_call(db: Session, call: Call) -> dict:
    """Pull the Quo record for a CRM call and store duration/summary/transcript.

    Uses the caller's personal key when they have one, else the company key.
    Matches the newest outgoing Quo call to the call's phone placed after the
    CRM call row was created (minus a small clock-skew allowance).  Summary and
    transcript are generated a little after hang-up — re-sync (or let the
    webhook deliver them) if they're still processing.
    """
    phone = db.get(Phone, call.phone_id) if call.phone_id else None
    if phone is None:
        raise QuoError("This call has no phone number to match in Quo.")
    user = call.caller
    key = effective_key(db, user)

    if call.call_sid:
        record = _request(key, "GET", f"/v1/calls/{call.call_sid}").get("data")
    else:
        record = find_call(db, phone.number, (call.created_at or _utcnow()) - timedelta(minutes=5), user)
    if not record:
        return {"found": False, "message": "No matching Quo call yet — place the call in Quo, then sync again."}

    suggested = _apply_call_record(call, record)
    summary = _request(key, "GET", f"/v1/call-summaries/{record['id']}").get("data") or {}
    transcript = _request(key, "GET", f"/v1/call-transcripts/{record['id']}").get("data") or {}
    s_status, t_status = summary.get("status", "absent"), transcript.get("status", "absent")
    formatted = format_summary(s_status, summary.get("summary") or [], summary.get("nextSteps") or [])
    if formatted:
        call.ai_summary = formatted
    text = format_transcript(t_status, transcript.get("dialogue") or [], phone.number)
    if text:
        call.transcript = text
    db.flush()
    return {
        "found": True,
        "quo_call_id": record["id"],
        "status": record.get("status", ""),
        "duration_seconds": call.duration_seconds,
        "suggested_outcome": suggested,
        "summary_status": s_status,
        "transcript_status": t_status,
        "ai_summary": call.ai_summary,
        "transcript": call.transcript,
    }


# ── SMS ─────────────────────────────────────────────────────────────────────

def send_message(db: Session, user: Optional[User], to_number: str, body: str) -> dict:
    """Send a text from the user's (or company's) Quo line. Returns ``{id, status, from}``."""
    line = resolve_line(db, user)
    data = _request(effective_key(db, user), "POST", "/v1/messages", json_body={
        "content": body,
        "from": line["id"],
        "to": [to_e164(to_number)],
    }).get("data") or {}
    return {"id": data.get("id"), "status": data.get("status", "queued"), "from": line["number"]}


# ── Webhooks ────────────────────────────────────────────────────────────────

def _key_group(user: Optional[User]) -> str:
    return f"user:{user.id}" if personal_key(user) else "company"


def _stored_webhook_keys(db: Session) -> dict[str, list[str]]:
    from app.services.settings import get_setting
    raw = get_setting(db, "quo_webhook_keys") or ""
    try:
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def webhook_url(base_url: str) -> str:
    return base_url.rstrip("/") + WEBHOOK_PATH


def public_base_url(base_url: str) -> bool:
    """Quo can only deliver to a public HTTPS URL."""
    return base_url.startswith("https://") and "localhost" not in base_url and "127.0.0.1" not in base_url


def register_webhooks(db: Session, base_url: str, user: Optional[User] = None) -> dict:
    """(Re)create the CRM's Quo webhooks for the user's/company key and store their signing keys.

    Idempotent: webhooks already pointing at our URL are deleted first, so
    restarts and re-saves never pile up duplicates.
    """
    from app.services.settings import set_setting

    if not public_base_url(base_url):
        return {"registered": 0, "skipped": "Auto-sync needs a public HTTPS app URL (APP_BASE_URL); use Sync from Quo locally."}
    key = effective_key(db, user)
    url = webhook_url(base_url)
    existing = _request(key, "GET", "/v1/webhooks").get("data", [])
    for wh in existing:
        if wh.get("url") == url:
            _request(key, "DELETE", f"/v1/webhooks/{wh['id']}")
    signing_keys: list[str] = []
    for endpoint, events in _WEBHOOKS:
        created = _request(key, "POST", endpoint, json_body={
            "url": url, "events": events, "label": "DRE-Capital CRM", "resourceIds": ["*"],
        }).get("data") or {}
        if created.get("key"):
            signing_keys.append(created["key"])
    stored = _stored_webhook_keys(db)
    stored[_key_group(user)] = signing_keys
    set_setting(db, "quo_webhook_keys", json.dumps(stored))
    db.flush()
    return {"registered": len(signing_keys), "url": url}


def verify_signature(db: Session, header: Optional[str], raw_body: bytes, now_ms: Optional[int] = None) -> bool:
    """True if ``openphone-signature`` matches any stored webhook signing key."""
    if not header:
        return False
    keys = [k for group in _stored_webhook_keys(db).values() for k in group]
    if not keys:
        return False
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    for sig in header.split(","):
        parts = sig.strip().split(";")
        if len(parts) != 4 or parts[0] != "hmac":
            continue
        ts, provided = parts[2], parts[3]
        try:
            if abs(now_ms - int(ts)) > _SIGNATURE_TOLERANCE_S * 1000:
                continue  # replay protection
        except ValueError:
            continue
        signed = ts.encode() + b"." + raw_body
        for key in keys:
            try:
                digest = hmac.new(base64.b64decode(key), signed, hashlib.sha256).digest()
            except (ValueError, TypeError):
                continue
            if hmac.compare_digest(base64.b64encode(digest).decode(), provided):
                return True
    return False


def handle_event(db: Session, event: dict) -> dict:
    """Apply a verified Quo webhook event to the CRM."""
    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    if etype == "call.completed":
        if obj.get("direction") != "outgoing":
            return {"handled": False, "reason": "inbound call"}
        call = db.execute(select(Call).where(Call.call_sid == obj.get("id"))).scalars().first()
        if call is None:
            from app.services.messaging import find_phone_by_number
            phone = find_phone_by_number(db, obj.get("to") or "")
            if phone is None:
                return {"handled": False, "reason": "number not in CRM"}
            call = db.execute(
                select(Call).where(
                    Call.phone_id == phone.id, Call.provider == "quo", Call.call_sid.is_(None),
                    Call.created_at >= _utcnow() - timedelta(hours=3),
                ).order_by(Call.created_at.desc()).limit(1)
            ).scalars().first()
        if call is None:
            return {"handled": False, "reason": "no open CRM call for this number"}
        _apply_call_record(call, obj)
        db.flush()
        return {"handled": True, "call_id": call.id}

    if etype in ("call.summary.completed", "call.transcript.completed"):
        call = db.execute(select(Call).where(Call.call_sid == obj.get("callId"))).scalars().first()
        if call is None:
            return {"handled": False, "reason": "unknown call"}
        if etype == "call.summary.completed":
            text = format_summary(obj.get("status", ""), obj.get("summary") or [], obj.get("nextSteps") or [])
            if text:
                call.ai_summary = text
        else:
            phone = db.get(Phone, call.phone_id) if call.phone_id else None
            text = format_transcript(obj.get("status", ""), obj.get("dialogue") or [], phone.number if phone else "")
            if text:
                call.transcript = text
        db.flush()
        return {"handled": True, "call_id": call.id}

    if etype == "message.received":
        from app.services.messaging import handle_inbound_sms
        result = handle_inbound_sms(db, obj.get("from") or "", obj.get("body") or "", obj.get("to") or "")
        return {"handled": True, **result}

    return {"handled": False, "reason": f"ignored {etype}"}
