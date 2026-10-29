"""Quo Sona calling engine: Quo's AI agent places the call from a Quo number.

Quo exposes Sona calls only through its hosted MCP server (``https://mcp.quo.com/mcp``)
with OAuth 2.1 (authorization code + S256 PKCE, dynamic client registration) — there
is no API-key path. Flow:

1. An admin clicks "Connect Quo" (Agents page) → we register this app as an OAuth
   client (once), redirect to Quo's consent page, and exchange the code for tokens.
2. Tokens rotate on every refresh. They're stored in the settings table and — on Cloud
   Run, where the DB is recreated on each deploy — in Secret Manager, so the connection
   survives deploys (``QUO_MCP_OAUTH`` env var = latest secret version at start).
3. A call: ``start-agent-call`` {from, to, task (≤ 2,000 chars, built from the call
   flow), idempotencyKey = our call id, maxDurationSeconds}; ``get-agent-call`` until
   final (summary + collected data); ``cancel-agent-call`` to hang up.

Sona speaks with Quo's own voice and business knowledge; the call flow is condensed into
its task. Twilio + Grok Voice / Gemini remain the engines that run the full script.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Agent, AiCall, CallFlow, Lead

log = logging.getLogger("uvicorn.error")
MCP_URL = "https://mcp.quo.com/mcp"
ISSUER = "https://mcp.quo.com"
SCOPES = "read:account read:calls write:calls read:agents write:agents"
CALLBACK_PATH = "/api/agents/quo/callback"
STATE_KEY = "quo_mcp_oauth"          # settings key (JSON: client + tokens)
PENDING_KEY = "quo_mcp_pending"      # settings key (JSON: state → PKCE verifier)
SECRET_NAME = "dre-capital-quo-mcp-oauth"
TASK_LIMIT = 2000
MAX_CALL_SECONDS = 360
FINAL = {"completed", "failed", "no-answer", "busy", "voicemail", "voicemail-left", "canceled"}
PROTOCOL_VERSION = "2025-06-18"


class QuoSonaError(RuntimeError):
    pass


# ── Stored OAuth state ──────────────────────────────────────────────────────

def _load(db: Session) -> dict:
    from app.services.settings import get_setting
    raw = get_setting(db, STATE_KEY) or os.environ.get("QUO_MCP_OAUTH", "")
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def _save(db: Session, data: dict) -> None:
    from app.services.settings import set_setting
    set_setting(db, STATE_KEY, json.dumps(data))
    db.commit()
    _persist_secret(data)


def _persist_secret(data: dict) -> None:
    """On Cloud Run, keep the rotated tokens in Secret Manager so they survive redeploys."""
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not (os.environ.get("K_SERVICE") and project):
        return
    try:
        import google.auth
        import google.auth.transport.requests
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
        headers = {"Authorization": f"Bearer {creds.token}"}
        base = f"https://secretmanager.googleapis.com/v1/projects/{project}/secrets/{SECRET_NAME}"
        payload = base64.b64encode(json.dumps(data).encode()).decode()
        r = httpx.post(f"{base}:addVersion", headers=headers, json={"payload": {"data": payload}}, timeout=20)
        r.raise_for_status()
        new = r.json()["name"]
        # Destroy older versions — only the latest is ever read.
        versions = httpx.get(f"{base}/versions", headers=headers, params={"filter": "state:ENABLED"}, timeout=20).json()
        for v in versions.get("versions", []):
            if v["name"] != new:
                httpx.post(f"https://secretmanager.googleapis.com/v1/{v['name']}:destroy", headers=headers, json={}, timeout=20)
    except Exception as exc:  # the DB copy still works until the next deploy
        log.warning("Couldn't store the Quo connection in Secret Manager: %s", exc)


def redirect_uri() -> str:
    return get_settings().app_base_url.rstrip("/") + CALLBACK_PATH


def is_connected(db: Session) -> bool:
    return bool(_load(db).get("refresh_token"))


def status(db: Session) -> dict:
    data = _load(db)
    return {"connected": bool(data.get("refresh_token")), "connected_at": data.get("connected_at"),
            "scope": data.get("scope")}


# ── OAuth 2.1 + PKCE ────────────────────────────────────────────────────────

def _register(db: Session, data: dict) -> dict:
    if data.get("client_id") and data.get("redirect_uri") == redirect_uri():
        return data
    r = httpx.post(f"{ISSUER}/register", timeout=20, json={
        "client_name": "DRE-Capital CRM (AI calling agents)", "redirect_uris": [redirect_uri()],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none", "scope": SCOPES})
    if r.status_code >= 400:
        raise QuoSonaError(f"Quo client registration failed ({r.status_code}): {r.text[:200]}")
    reg = r.json()
    data = {"client_id": reg["client_id"], "client_secret": reg.get("client_secret"), "redirect_uri": redirect_uri()}
    _save(db, data)
    return data


def authorize_url(db: Session) -> str:
    """Start "Connect Quo": register (once), remember the PKCE verifier, return Quo's consent URL."""
    from app.services.settings import get_setting, set_setting
    data = _register(db, _load(db))
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    try:
        pending = json.loads(get_setting(db, PENDING_KEY) or "{}")
    except ValueError:
        pending = {}
    pending = {k: v for k, v in pending.items() if v.get("at", 0) > time.time() - 900}
    pending[state] = {"verifier": verifier, "at": time.time()}
    set_setting(db, PENDING_KEY, json.dumps(pending))
    db.commit()
    return f"{ISSUER}/authorize?" + urlencode({
        "response_type": "code", "client_id": data["client_id"], "redirect_uri": redirect_uri(),
        "scope": SCOPES, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        "resource": MCP_URL})


def _token_request(data: dict, form: dict) -> dict:
    form = {**form, "client_id": data["client_id"]}
    if data.get("client_secret"):
        form["client_secret"] = data["client_secret"]
    r = httpx.post(f"{ISSUER}/token", data=form, timeout=20)
    if r.status_code >= 400:
        raise QuoSonaError(f"Quo sign-in failed ({r.status_code}): {r.text[:200]}")
    return r.json()


def complete_authorization(db: Session, code: str, state: str) -> None:
    from app.services.settings import get_setting, set_setting
    from app.timeutil import iso_utc, utcnow
    try:
        pending = json.loads(get_setting(db, PENDING_KEY) or "{}")
    except ValueError:
        pending = {}
    entry = pending.pop(state, None)
    set_setting(db, PENDING_KEY, json.dumps(pending))
    if not entry or entry.get("at", 0) < time.time() - 900:
        raise QuoSonaError("This Quo sign-in link expired — click Connect Quo again.")
    data = _load(db)
    tok = _token_request(data, {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
                                "code_verifier": entry["verifier"], "resource": MCP_URL})
    data.update(access_token=tok["access_token"], refresh_token=tok.get("refresh_token"),
                expires_at=time.time() + int(tok.get("expires_in") or 3600) - 60,
                scope=tok.get("scope"), connected_at=iso_utc(utcnow()))
    _save(db, data)


def disconnect(db: Session) -> None:
    data = _load(db)
    if data.get("refresh_token"):
        try:
            httpx.post(f"{ISSUER}/token", timeout=15, data={"token": data["refresh_token"],
                                                             "token_type_hint": "refresh_token",
                                                             "client_id": data.get("client_id", "")})
        except httpx.HTTPError:
            pass
    keep = {k: data[k] for k in ("client_id", "client_secret", "redirect_uri") if k in data}
    _save(db, keep)


def _access_token(db: Session) -> str:
    data = _load(db)
    if not data.get("refresh_token"):
        raise QuoSonaError("Quo isn't connected — an admin can click Connect Quo on the Agents page.")
    if data.get("access_token") and data.get("expires_at", 0) > time.time():
        return data["access_token"]
    try:
        tok = _token_request(data, {"grant_type": "refresh_token", "refresh_token": data["refresh_token"],
                                    "resource": MCP_URL})
    except QuoSonaError as exc:
        if "invalid_grant" in str(exc):
            disconnect(db)
            raise QuoSonaError("The Quo connection expired — click Connect Quo again on the Agents page.") from exc
        raise
    data.update(access_token=tok["access_token"], refresh_token=tok.get("refresh_token") or data["refresh_token"],
                expires_at=time.time() + int(tok.get("expires_in") or 3600) - 60)
    _save(db, data)   # refresh tokens rotate — persist the new one right away
    return data["access_token"]


# ── Minimal MCP client (Streamable HTTP) ────────────────────────────────────

def _parse_rpc(r: httpx.Response, req_id: int) -> dict:
    if "text/event-stream" in r.headers.get("content-type", ""):
        for line in r.text.splitlines():
            if line.startswith("data:"):
                try:
                    msg = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if msg.get("id") == req_id:
                    return msg
        raise QuoSonaError("Quo MCP returned no result.")
    return r.json() if r.content else {}


_SCHEMAS: dict[str, dict] = {}


def _fit_arguments(name: str, arguments: dict) -> dict:
    """Map our arguments onto the tool's real input schema (from tools/list): the call id goes to
    whichever ``*id`` parameter the tool takes, and optional fields the tool doesn't know are dropped."""
    schema = _SCHEMAS.get(name)
    if not schema:
        return arguments
    props = schema.get("properties") or {}
    out = {}
    for k, v in arguments.items():
        if k in props:
            out[k] = v
        elif k == "agentCallId":
            target = next((p for p in props if p.lower().endswith("id")), None)
            if target:
                out[target] = v
    return out


def call_tool(db: Session, name: str, arguments: dict) -> Any:
    """Run one Quo MCP tool; returns its structured result (or parsed text)."""
    headers = {"Authorization": f"Bearer {_access_token(db)}", "Accept": "application/json, text/event-stream",
               "Content-Type": "application/json", "MCP-Protocol-Version": PROTOCOL_VERSION}
    with httpx.Client(timeout=60) as http:
        init = http.post(MCP_URL, headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": "dre-capital-crm", "version": "1"}}})
        if init.status_code == 401:
            raise QuoSonaError("Quo rejected the connection — click Connect Quo again on the Agents page.")
        if init.status_code >= 400:
            raise QuoSonaError(f"Quo MCP error {init.status_code}: {init.text[:200]}")
        _parse_rpc(init, 1)
        if init.headers.get("mcp-session-id"):
            headers["Mcp-Session-Id"] = init.headers["mcp-session-id"]
        http.post(MCP_URL, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        if not _SCHEMAS:
            listed = _parse_rpc(http.post(MCP_URL, headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"}), 3)
            for tool in (listed.get("result") or {}).get("tools", []):
                _SCHEMAS[tool["name"]] = tool.get("inputSchema") or {}
            if _SCHEMAS and name not in _SCHEMAS:
                raise QuoSonaError(f"This Quo connection can't use {name} — click Connect Quo again to grant Sona access.")
        r = http.post(MCP_URL, headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                      "params": {"name": name, "arguments": _fit_arguments(name, arguments)}})
        if r.status_code >= 400:
            raise QuoSonaError(f"Quo {name} failed ({r.status_code}): {r.text[:200]}")
        msg = _parse_rpc(r, 2)
    if msg.get("error"):
        raise QuoSonaError(f"Quo {name}: {msg['error'].get('message')}")
    result = msg.get("result") or {}
    text = " ".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text").strip()
    if result.get("isError"):
        raise QuoSonaError(f"Quo {name}: {text[:300] or 'failed'}")
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    try:
        return json.loads(text)
    except ValueError:
        return {"text": text}


def _find(obj: Any, *keys: str) -> Any:
    """First value for any of ``keys`` anywhere in a nested result."""
    if isinstance(obj, dict):
        for k in keys:
            if obj.get(k) not in (None, ""):
                return obj[k]
        for v in obj.values():
            found = _find(v, *keys)
            if found not in (None, ""):
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find(v, *keys)
            if found not in (None, ""):
                return found
    return None


# ── Calls ───────────────────────────────────────────────────────────────────

def build_task(db: Session, flow: CallFlow, greet: str, lead: Optional[Lead], real_call: bool,
               extra: Optional[str] = None) -> str:
    """The call flow condensed into Sona's task (≤ 2,000 characters)."""
    from app.services.ai_agent import _speakable, script_values
    from app.services.call_guide import fill
    values = script_values(db, greet, lead)
    say = lambda text: _speakable(fill(text or "", values)[0])  # noqa: E731
    questions = [q for q in json.loads(flow.questions or "[]") if q]
    rules = [r for r in json.loads(flow.rules or "[]") if r]
    prop = (f"{lead.property_address}{', ' + lead.property_city if lead.property_city else ''}" if lead else None)
    head = [f"Call {greet}" + (f" about the property at {prop}" if prop else "") + ". Goal: a short, friendly "
            "qualification call so our acquisitions manager can call back prepared — no offers, no price talk."]
    if real_call:
        head.append(f"First confirm you reached {greet}, the owner of {prop or 'the property'}. If it's a wrong number "
                    "or not the owner, apologize, say you'll remove the number, and end the call.")
    head.append("Open with: " + say(flow.opening))
    tail = []
    if rules:
        tail.append("Rules: " + " ".join(say(r).rstrip(".") + "." for r in rules[:6]))
    tail.append("If they ask to stop or not be called, agree, say they won't be called again, and end the call. "
                "Be honest that you're an AI assistant if asked.")
    tail.append("Close: " + say((flow.close or "").replace("[agreed time]", "at the time they gave you")))
    tail.append("Done when the questions are answered and a callback time is agreed, or they decline. "
                "Collect: outcome (owner_confirmed, wrong_number, not_interested, callback_requested, warm, "
                "dnc_request or voicemail), owner confirmed (yes/no), each answer, callback time, interest (hot/warm/cold).")
    if extra:
        tail.append("Team notes: " + extra.strip())
    fixed = "\n".join(head + tail)
    budget = TASK_LIMIT - len(fixed) - 40
    asked, used = [], 0
    for i, q in enumerate(questions, 1):
        line = f"{i}. {say(q)}"
        if used + len(line) + 1 > budget:
            break
        asked.append(line); used += len(line) + 1
    task = "\n".join(head + (["Ask, in order:\n" + "\n".join(asked)] if asked else []) + tail)
    return task[:TASK_LIMIT]


def start_call(db: Session, ai_call: AiCall, flow: CallFlow, lead: Optional[Lead]) -> AiCall:
    from app.services.agents import credentials
    agent = db.get(Agent, ai_call.agent_id)
    creds = credentials(db, agent)
    task = build_task(db, flow, ai_call.greet_name or "there", lead, ai_call.phone_id is not None, agent.instructions)
    res = call_tool(db, "start-agent-call", {
        "from": creds["from_number"], "to": ai_call.to_number, "task": task,
        "idempotencyKey": ai_call.id, "maxDurationSeconds": MAX_CALL_SECONDS})
    call_id = _find(res, "agentCallId", "id", "callId")
    if not call_id:
        m = re.search(r"\bAR[0-9A-Za-z]+", json.dumps(res))
        call_id = m.group(0) if m else None
    if not call_id:
        raise QuoSonaError(f"Quo didn't return a call id: {json.dumps(res)[:200]}")
    ai_call.provider = "quo_sona"
    ai_call.provider_call_id = call_id
    ai_call.status = "queued"
    db.flush()
    return ai_call


_STATUS = {"queued": "queued", "initiated": "queued", "ringing": "queued", "dialing": "queued",
           "in-progress": "in-progress", "in_progress": "in-progress", "active": "in-progress",
           "completed": "completed", "failed": "failed", "no-answer": "no-answer", "busy": "busy",
           "voicemail": "completed", "voicemail-left": "completed", "canceled": "canceled"}


def refresh(db: Session, ai_call: AiCall) -> AiCall:
    """Pull Sona's status; once final, store the summary + collected data as the call's record."""
    from app.services.ai_agent import FINAL_STATUSES
    if not ai_call.provider_call_id or (ai_call.status in FINAL_STATUSES and ai_call.summary):
        return ai_call
    res = call_tool(db, "get-agent-call", {"agentCallId": ai_call.provider_call_id})
    raw = str(_find(res, "status") or "").lower()
    ai_call.status = _STATUS.get(raw, ai_call.status)
    if raw in ("voicemail", "voicemail-left"):
        ai_call.answered_by = "voicemail"
    dur = _find(res, "durationSeconds", "duration")
    if isinstance(dur, (int, float)):
        ai_call.duration_seconds = int(dur)
    if raw in FINAL:
        title = _find(res, "title") or ""
        summary = _find(res, "summary") or ""
        data = _find(res, "collectedData", "data")
        parts = [p for p in (title, summary) if p]
        if data:
            parts.append("Collected: " + (json.dumps(data) if not isinstance(data, str) else data))
        ai_call.summary = "\n".join(parts) or f"Sona call {raw}."
        # Sona returns a summary, not a transcript: it's what we classify and log on the lead.
        if ai_call.status == "completed" and ai_call.answered_by != "voicemail":
            ai_call.transcript = f"Sona (Quo AI agent) call summary — {ai_call.summary}"
    return ai_call


def stop(db: Session, ai_call: AiCall) -> None:
    if ai_call.provider_call_id:
        call_tool(db, "cancel-agent-call", {"agentCallId": ai_call.provider_call_id})


def quo_line_number(db: Session) -> str:
    """The company Quo line (E.164): the stored/auto-detected one, else the workspace's first number."""
    from app.services.settings import get_setting, set_setting
    num = (get_setting(db, "quo_from_number") or "").strip()
    if num:
        return num
    nums = quo_numbers(db)
    if nums:
        num = nums[0]["number"]
        set_setting(db, "quo_from_number", num)
        db.commit()
    return num or ""


def quo_numbers(db: Session) -> list[dict]:
    """The workspace's Quo numbers (company API key) for picking Sona's ``from`` number."""
    from app.services import quo
    try:
        return quo.list_phone_numbers(quo.company_key(db))
    except Exception:
        return []
