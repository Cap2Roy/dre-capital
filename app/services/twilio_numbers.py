"""Twilio phone numbers for calling agents: list, search, buy, and verify caller IDs.

Uses the default Twilio credentials (Agents → Default keys): an API key (SK… + secret)
on the account, or the account auth token.

- Buying a US number requires an approved Trust Hub Primary Customer Profile on the
  Twilio account; Twilio's refusal is passed through as a clear message.
- A number you already own elsewhere (e.g. the Quo line) can be used as the agents'
  caller ID once verified: Twilio calls it and someone enters the code shown here.
- Inbound calls to an agent number (sellers calling back) are forwarded to the team's
  line — the Quo number, else the operator number.
"""
from __future__ import annotations

import re
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Agent
from app.services.agents import default_credentials, twilio_auth

API = "https://api.twilio.com/2010-04-01"
INBOUND_PATH = "/api/ai-calls/twilio/inbound"


class NumbersError(RuntimeError):
    pass


def _creds(db: Session) -> dict:
    creds = default_credentials(db, "twilio_grok")
    if not creds.get("account_sid") or not creds.get("api_key"):
        raise NumbersError("Set the Twilio default key first (Agents → Default keys: Account SID + API key).")
    return creds


def _call(db: Session, method: str, path: str, *, params: Optional[dict] = None, data: Optional[dict] = None) -> dict:
    creds = _creds(db)
    try:
        r = httpx.request(method, f"{API}/Accounts/{creds['account_sid']}{path}", params=params, data=data,
                          auth=twilio_auth(creds), timeout=30)
    except httpx.HTTPError as exc:
        raise NumbersError(f"Twilio unreachable: {exc}") from exc
    try:
        body = r.json() if r.content else {}
    except ValueError:
        body = {}
    if r.status_code >= 400:
        msg = body.get("message") or r.text[:200]
        # Twilio reports an unapproved business profile as 401 / error 20003 — not a bad key.
        if r.status_code == 401 and "compliance" not in msg.lower():
            raise NumbersError("Twilio rejected the Account SID / API key.")
        if "compliance profile" in msg.lower() or "trust hub" in msg.lower():
            msg = ("Twilio won't sell numbers until the account's business profile is approved. In the Twilio "
                   "Console open Trust Hub → Customer Profiles, create the Primary Business Profile (legal name, "
                   "EIN, address, authorized representative) and submit it — approval usually takes 1–3 business "
                   "days. Meanwhile, verify a number you already own (e.g. your Quo line) as the caller ID.")
        raise NumbersError(msg)
    return body


def e164(number: str) -> str:
    digits = re.sub(r"\D", "", number or "")
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) != 11 or not digits.startswith("1"):
        raise NumbersError("Enter a US phone number, e.g. (214) 555-0100.")
    return "+" + digits


def inventory(db: Session) -> dict:
    """Numbers on the account, verified caller IDs, and which agent uses each."""
    owned = _call(db, "GET", "/IncomingPhoneNumbers.json", params={"PageSize": 100}).get("incoming_phone_numbers", [])
    verified = _call(db, "GET", "/OutgoingCallerIds.json", params={"PageSize": 100}).get("outgoing_caller_ids", [])
    by_number: dict[str, list[str]] = {}
    default_from = default_credentials(db, "twilio_grok").get("from_number") or ""
    for a in db.execute(select(Agent).where(Agent.kind == "calling")).scalars():
        num = a.from_number or (default_from if a.use_default_key else "")
        if num:
            by_number.setdefault(num, []).append(a.name)
    rows = [{"number": n["phone_number"], "type": "owned", "name": n.get("friendly_name") or "", "sid": n["sid"],
             "voice": bool((n.get("capabilities") or {}).get("voice")), "agents": by_number.get(n["phone_number"], []),
             "is_default": n["phone_number"] == default_from} for n in owned]
    rows += [{"number": v["phone_number"], "type": "verified", "name": v.get("friendly_name") or "", "sid": v["sid"],
              "voice": True, "agents": by_number.get(v["phone_number"], []),
              "is_default": v["phone_number"] == default_from} for v in verified]
    return {"numbers": rows, "default_from_number": default_from}


def available(db: Session, area_code: str = "", contains: str = "", limit: int = 10) -> list[dict]:
    params = {"VoiceEnabled": "true", "PageSize": max(1, min(limit, 30))}
    if area_code:
        if not re.fullmatch(r"\d{3}", area_code):
            raise NumbersError("Area code must be 3 digits.")
        params["AreaCode"] = area_code
    if contains:
        params["Contains"] = re.sub(r"[^\d*]", "", contains)[:10]
    res = _call(db, "GET", "/AvailablePhoneNumbers/US/Local.json", params=params)
    return [{"number": n["phone_number"], "locality": n.get("locality"), "region": n.get("region"),
             "sms": bool((n.get("capabilities") or {}).get("SMS"))} for n in res.get("available_phone_numbers", [])]


def inbound_url() -> str:
    return get_settings().app_base_url.rstrip("/") + INBOUND_PATH


def buy(db: Session, number: str, friendly_name: str) -> dict:
    """Buy a local number; inbound calls to it are forwarded to the team (see ``forward_to``)."""
    res = _call(db, "POST", "/IncomingPhoneNumbers.json", data={
        "PhoneNumber": e164(number), "FriendlyName": friendly_name[:64],
        "VoiceUrl": inbound_url(), "VoiceMethod": "POST"})
    return {"number": res["phone_number"], "sid": res["sid"]}


def start_caller_id_verification(db: Session, number: str, friendly_name: str) -> dict:
    """Twilio calls ``number``; whoever answers enters the returned 6-digit code."""
    res = _call(db, "POST", "/OutgoingCallerIds.json", data={"PhoneNumber": e164(number), "FriendlyName": friendly_name[:64]})
    return {"number": res.get("phone_number"), "validation_code": res.get("validation_code"),
            "call_sid": res.get("call_sid")}


def forward_to(db: Session) -> Optional[str]:
    """Where sellers calling an agent number back are connected: the Quo line, else the operator."""
    from app.services.quo_sona import quo_line_number
    from app.services.settings import get_setting
    for v in (quo_line_number(db), (get_setting(db, "operator_number") or "").strip()):
        if v:
            try:
                return e164(v)
            except NumbersError:
                continue
    return None


def inbound_twiml(db: Session) -> str:
    target = forward_to(db)
    if not target:
        return ("<Response><Say>Thanks for calling Direct Real Estate Capital. We'll call you back shortly."
                "</Say><Hangup/></Response>")
    return f'<Response><Dial timeout="25">{target}</Dial></Response>'
