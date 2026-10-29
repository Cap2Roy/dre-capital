"""Agents: add / edit AI agents, give each its own key or the "default key" tag,
test them, and manage the provider default keys (admin)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Agent, CallFlow, User
from app.services import agents as svc
from app.timeutil import iso_utc, utcnow

router = APIRouter(prefix="/api/agents", tags=["agents"])

MASK = "••••"


class AgentIn(BaseModel):
    name: str
    kind: str
    provider: str
    model: Optional[str] = None
    use_default_key: bool = True
    api_key: Optional[str] = None
    key_sid: Optional[str] = None
    account_sid: Optional[str] = None
    from_number: Optional[str] = None
    base_url: Optional[str] = None
    instructions: Optional[str] = None
    call_flow_id: Optional[str] = None
    voice: Optional[str] = None
    max_pages: int = 5
    is_default: bool = False
    active: bool = True


class AgentPatch(BaseModel):
    name: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    use_default_key: Optional[bool] = None
    api_key: Optional[str] = None
    key_sid: Optional[str] = None
    account_sid: Optional[str] = None
    from_number: Optional[str] = None
    base_url: Optional[str] = None
    instructions: Optional[str] = None
    call_flow_id: Optional[str] = None
    voice: Optional[str] = None
    max_pages: Optional[int] = None
    is_default: Optional[bool] = None
    active: Optional[bool] = None


def _user(request: Request, db: Session) -> User:
    u = getattr(request.state, "user", None)
    user = db.get(User, u.id) if u else None
    if user is None:
        raise HTTPException(401, "Authentication required")
    return user


def _can_edit(user: User, agent: Agent) -> None:
    if user.role != "admin" and agent.created_by != user.id:
        raise HTTPException(403, "Only the agent's creator or an admin can change it.")


def out(db: Session, a: Agent) -> dict:
    return {
        "id": a.id, "name": a.name, "kind": a.kind, "provider": a.provider,
        "provider_label": svc.PROVIDERS[a.provider]["label"], "model": a.model,
        "effective_model": _safe_model(db, a),
        "use_default_key": a.use_default_key, "has_own_key": bool(a.api_key),
        "api_key": MASK if a.api_key else "", "key_sid": a.key_sid or "", "account_sid": a.account_sid or "",
        "from_number": a.from_number or "",
        "base_url": a.base_url or "", "instructions": a.instructions or "", "call_flow_id": a.call_flow_id,
        "voice": a.voice or "", "max_pages": a.max_pages, "is_default": a.is_default, "active": a.active,
        "created_by": a.created_by, "last_used_at": iso_utc(a.last_used_at), "last_error": a.last_error,
        "key_status": svc.key_status(db, a), "created_at": iso_utc(a.created_at),
    }


def _safe_model(db: Session, a: Agent) -> Optional[str]:
    try:
        return svc.model_for(db, a)
    except svc.AgentError:
        return None


def _apply(db: Session, a: Agent, data: dict) -> None:
    for secret in ("api_key",):
        if secret in data and (data[secret] is None or set(data[secret] or "") == {"•"}):
            data.pop(secret)  # masked echo / omitted → keep current
    for k, v in data.items():
        if isinstance(v, str):
            v = v.strip() or None
        setattr(a, k, v)
    if a.call_flow_id and db.get(CallFlow, a.call_flow_id) is None:
        raise HTTPException(404, "Call flow not found")
    if a.kind == "calling" and a.voice:
        from app.services.live_agent import VOICES
        from app.services.phone_agent import GROK_VOICES
        if a.provider == "twilio_gemini" and a.voice not in VOICES:
            raise HTTPException(400, f"Unknown Gemini voice '{a.voice}'.")
        if a.provider == "twilio_grok" and a.voice not in GROK_VOICES:
            raise HTTPException(400, f"Unknown Grok voice '{a.voice}'.")
    a.max_pages = max(1, min(int(a.max_pages or 5), 25))
    if a.is_default:
        for other in db.execute(select(Agent).where(Agent.kind == a.kind, Agent.id != a.id)).scalars():
            other.is_default = False


@router.get("/providers")
def providers():
    from app.services.live_agent import VOICES
    from app.services.phone_agent import GROK_VOICES
    return {"kinds": list(svc.KINDS), "providers": svc.PROVIDERS, "gemini_voices": VOICES, "grok_voices": GROK_VOICES}


@router.get("")
def list_agents(request: Request, kind: Optional[str] = None, db: Session = Depends(get_db)):
    _user(request, db)
    q = select(Agent).order_by(Agent.kind, Agent.is_default.desc(), Agent.name)
    if kind:
        q = q.where(Agent.kind == kind)
    return [out(db, a) for a in db.execute(q).scalars()]


@router.post("", status_code=201)
def create_agent(payload: AgentIn, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    try:
        svc.validate(payload.kind, payload.provider)
    except svc.AgentError as exc:
        raise HTTPException(400, str(exc))
    if not payload.name.strip():
        raise HTTPException(400, "Name is required.")
    a = Agent(created_by=user.id, kind=payload.kind)
    db.add(a)
    _apply(db, a, payload.model_dump(exclude={"kind"}))
    db.commit()
    db.refresh(a)
    return out(db, a)


@router.get("/defaults")
def get_defaults(request: Request, db: Session = Depends(get_db)):
    """Which provider default keys are set (values never returned)."""
    _user(request, db)
    res = {}
    for p in svc.PROVIDERS:
        if p == "twilio_grok":   # same Twilio account as twilio_gemini (its voice uses the xAI key)
            continue
        if p == "quo_sona":
            from app.services.quo_sona import is_connected
            num = svc.default_credentials(db, p)["from_number"]
            res[p] = {"label": "Quo (Sona calls)", "set": is_connected(db), "fields": [], "from_number": num,
                      "base_url": "", "connect": True}
            continue
        creds = svc.default_credentials(db, p)
        need = svc.PROVIDERS[p]["fields"]
        label = "Twilio (all phone-calling agents)" if p == "twilio_gemini" else svc.PROVIDERS[p]["label"]
        missing = [f for f in need if not (creds.get(f) or "").strip()]
        res[p] = {"label": label, "set": not missing, "missing": missing,
                  "fields": need, "from_number": creds.get("from_number") or "", "base_url": creds.get("base_url") or ""}
    return res


@router.put("/defaults")
def set_defaults(payload: dict, request: Request, db: Session = Depends(get_db)):
    """Admin: set provider default keys. Masked/blank values keep the current key."""
    from app.services.settings import set_setting
    user = _user(request, db)
    if user.role != "admin":
        raise HTTPException(403, "Admin only")
    mapping = {
        ("openai", "api_key"): "openai_api_key", ("anthropic", "api_key"): "anthropic_api_key",
        ("bland", "api_key"): "bland_api_key", ("xai", "api_key"): "xai_api_key",
        ("openai_compatible", "api_key"): "llm_api_key", ("openai_compatible", "base_url"): "llm_base_url",
        ("twilio_gemini", "account_sid"): "twilio_account_sid", ("twilio_gemini", "key_sid"): "twilio_api_key_sid",
        ("twilio_gemini", "api_key"): "twilio_api_key_secret", ("twilio_gemini", "from_number"): "twilio_from_number",
    }
    updated = []
    for provider, fields in (payload or {}).items():
        for field, value in (fields or {}).items():
            key = mapping.get((provider, field))
            if key is None or value is None or set(str(value)) == {"•"} or str(value).strip() == "":
                continue
            set_setting(db, key, str(value).strip())
            updated.append(f"{provider}.{field}")
    db.commit()
    return {"updated": updated}


# ── Quo connection for Sona calls (OAuth 2.1 to Quo's MCP server) ───────────

@router.get("/quo/status")
def quo_status(request: Request, db: Session = Depends(get_db)):
    from app.services import quo_sona
    _user(request, db)
    return {**quo_sona.status(db), "numbers": quo_sona.quo_numbers(db),
            "default_number": svc.default_credentials(db, "quo_sona")["from_number"]}


@router.get("/quo/connect")
def quo_connect(request: Request, db: Session = Depends(get_db)):
    """Admin: start the Quo sign-in (consent) that lets Sona place calls for the CRM."""
    from fastapi.responses import RedirectResponse
    from app.services import quo_sona
    _admin(request, db)
    try:
        return RedirectResponse(quo_sona.authorize_url(db), status_code=302)
    except quo_sona.QuoSonaError as exc:
        raise HTTPException(502, str(exc))


@router.get("/quo/callback")
def quo_callback(request: Request, code: str = "", state: str = "", error: str = "", db: Session = Depends(get_db)):
    from urllib.parse import quote
    from fastapi.responses import RedirectResponse
    from app.services import quo_sona
    _admin(request, db)
    if error or not code:
        return RedirectResponse(f"/agents?quo_error={quote(error or 'Quo sign-in was cancelled.')}", status_code=302)
    try:
        quo_sona.complete_authorization(db, code, state)
    except quo_sona.QuoSonaError as exc:
        return RedirectResponse(f"/agents?quo_error={quote(str(exc))}", status_code=302)
    return RedirectResponse("/agents?quo=connected", status_code=302)


@router.delete("/quo")
def quo_disconnect(request: Request, db: Session = Depends(get_db)):
    from app.services import quo_sona
    _admin(request, db)
    quo_sona.disconnect(db)
    return {"connected": False}


# ── Twilio phone numbers for calling agents (admin) ─────────────────────────

def _admin(request: Request, db: Session) -> User:
    user = _user(request, db)
    if user.role != "admin":
        raise HTTPException(403, "Admin only")
    return user


class NumberBuy(BaseModel):
    number: str
    agent_id: Optional[str] = None


class CallerIdVerify(BaseModel):
    number: str
    name: Optional[str] = None


@router.get("/twilio/numbers")
def twilio_numbers(request: Request, db: Session = Depends(get_db)):
    """Numbers on the Twilio account + verified caller IDs, and which agents use them."""
    from app.services import twilio_numbers as tn
    _admin(request, db)
    try:
        return tn.inventory(db)
    except tn.NumbersError as exc:
        raise HTTPException(409, str(exc))


@router.get("/twilio/available")
def twilio_available(request: Request, area_code: str = "", contains: str = "", db: Session = Depends(get_db)):
    from app.services import twilio_numbers as tn
    _admin(request, db)
    try:
        return tn.available(db, area_code.strip(), contains.strip())
    except tn.NumbersError as exc:
        raise HTTPException(409, str(exc))


@router.post("/twilio/numbers", status_code=201)
def twilio_buy(payload: NumberBuy, request: Request, db: Session = Depends(get_db)):
    """Buy a number (monthly fee on the Twilio account) and optionally give it to a calling agent."""
    from app.services import twilio_numbers as tn
    _admin(request, db)
    agent = None
    if payload.agent_id:
        agent = db.get(Agent, payload.agent_id)
        if agent is None or agent.kind != "calling" or agent.provider not in svc.TWILIO_PROVIDERS:
            raise HTTPException(404, "Twilio calling agent not found")
    try:
        res = tn.buy(db, payload.number, f"DRE-Capital AI agent{' — ' + agent.name if agent else ''}")
    except tn.NumbersError as exc:
        raise HTTPException(409, str(exc))
    if agent is not None:
        agent.from_number = res["number"]
    elif not svc.default_credentials(db, "twilio_grok").get("from_number"):
        from app.services.settings import set_setting
        set_setting(db, "twilio_from_number", res["number"])   # first number → the agents' default caller ID
    db.commit()
    return res


@router.post("/twilio/caller-ids")
def twilio_verify_caller_id(payload: CallerIdVerify, request: Request, db: Session = Depends(get_db)):
    """Use a number you already own (e.g. the Quo line) as caller ID: Twilio calls it; enter the code shown."""
    from app.services import twilio_numbers as tn
    _admin(request, db)
    try:
        return tn.start_caller_id_verification(db, payload.number, payload.name or "DRE-Capital caller ID")
    except tn.NumbersError as exc:
        raise HTTPException(409, str(exc))


@router.get("/{agent_id}")
def get_agent(agent_id: str, request: Request, db: Session = Depends(get_db)):
    _user(request, db)
    a = db.get(Agent, agent_id)
    if a is None:
        raise HTTPException(404, "Agent not found")
    return out(db, a)


@router.patch("/{agent_id}")
def update_agent(agent_id: str, payload: AgentPatch, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    a = db.get(Agent, agent_id)
    if a is None:
        raise HTTPException(404, "Agent not found")
    _can_edit(user, a)
    data = payload.model_dump(exclude_unset=True)
    if "provider" in data:
        try:
            svc.validate(a.kind, data["provider"])
        except svc.AgentError as exc:
            raise HTTPException(400, str(exc))
    _apply(db, a, data)
    db.commit()
    db.refresh(a)
    return out(db, a)


@router.delete("/{agent_id}")
def delete_agent(agent_id: str, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    a = db.get(Agent, agent_id)
    if a is None:
        raise HTTPException(404, "Agent not found")
    _can_edit(user, a)
    db.delete(a)
    db.commit()
    return {"ok": True}


@router.post("/{agent_id}/test")
def test_agent(agent_id: str, request: Request, db: Session = Depends(get_db)):
    _user(request, db)
    a = db.get(Agent, agent_id)
    if a is None:
        raise HTTPException(404, "Agent not found")
    try:
        result = svc.test(db, a)
        a.last_error = None
    except Exception as exc:  # AgentError, AiAgentError, LiveAgentError …
        a.last_error = str(exc)[:500]
        db.commit()
        return {"ok": False, "detail": str(exc)}
    a.last_used_at = utcnow()
    db.commit()
    return result
