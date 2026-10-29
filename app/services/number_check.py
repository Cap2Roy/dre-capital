"""Is this the right number for this property — and may we call it now?

Every check returns ``{key, label, status: pass|warn|fail, detail}``.

Human dialers see the checks as guidance (only DNC / opt-out block, as before).
The AI agent may dial a lead only when nothing fails AND there's positive
evidence the number reaches this property's owner:
  - the owner already confirmed on an earlier call on this number, or
  - the number is skip-trace "good" AND the lead's owner matches the owner of
    public record (so the number isn't a previous owner's).
Plus the legal gates for artificial-voice calls: prior express written consent
on file (TCPA), not DNC / opted out, and 8 am–9 pm at the property.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Call, CallOutcome, Lead, Phone, PhoneQuality

CALL_WINDOW = (8, 21)  # 8:00 – 20:59 local, per TCPA telephone-solicitation hours

# Primary time zone per state (split states use the zone most of the population is in).
_STATE_TZ = {
    "AL": "America/Chicago", "AK": "America/Anchorage", "AZ": "America/Phoenix", "AR": "America/Chicago",
    "CA": "America/Los_Angeles", "CO": "America/Denver", "CT": "America/New_York", "DE": "America/New_York",
    "DC": "America/New_York", "FL": "America/New_York", "GA": "America/New_York", "HI": "Pacific/Honolulu",
    "ID": "America/Boise", "IL": "America/Chicago", "IN": "America/Indiana/Indianapolis", "IA": "America/Chicago",
    "KS": "America/Chicago", "KY": "America/New_York", "LA": "America/Chicago", "ME": "America/New_York",
    "MD": "America/New_York", "MA": "America/New_York", "MI": "America/Detroit", "MN": "America/Chicago",
    "MS": "America/Chicago", "MO": "America/Chicago", "MT": "America/Denver", "NE": "America/Chicago",
    "NV": "America/Los_Angeles", "NH": "America/New_York", "NJ": "America/New_York", "NM": "America/Denver",
    "NY": "America/New_York", "NC": "America/New_York", "ND": "America/Chicago", "OH": "America/New_York",
    "OK": "America/Chicago", "OR": "America/Los_Angeles", "PA": "America/New_York", "RI": "America/New_York",
    "SC": "America/New_York", "SD": "America/Chicago", "TN": "America/Chicago", "TX": "America/Chicago",
    "UT": "America/Denver", "VT": "America/New_York", "VA": "America/New_York", "WA": "America/Los_Angeles",
    "WV": "America/New_York", "WI": "America/Chicago", "WY": "America/Denver",
}


def local_time(state: Optional[str], now_utc: Optional[datetime] = None) -> tuple[datetime, str]:
    tz = _STATE_TZ.get((state or "").upper(), "America/Chicago")
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(ZoneInfo(tz)), tz


def _c(key: str, label: str, status: str, detail: str = "") -> dict:
    return {"key": key, "label": label, "status": status, "detail": detail}


def check_phone(db: Session, lead: Lead, phone: Phone, *, for_ai: bool, now_utc: Optional[datetime] = None) -> dict:
    from app.services.property_data import _names, owner_matches

    checks: list[dict] = []
    if phone.lead_id != lead.id:
        checks.append(_c("belongs", "Number belongs to this lead", "fail", "This number is attached to a different lead."))
    else:
        checks.append(_c("belongs", "Number belongs to this lead", "pass"))

    checks.append(_c("dnc", "Not on the Do-Not-Call registry",
                     "fail" if phone.dnc_flagged or phone.quality == PhoneQuality.DNC else "pass"))
    checks.append(_c("opt_out", "Owner hasn't asked us to stop",
                     "fail" if phone.opted_out or phone.quality == PhoneQuality.OPTED_OUT else "pass"))

    wrong = db.execute(select(Call).where(Call.phone_id == phone.id, Call.outcome == CallOutcome.WRONG_NUMBER)
                       .order_by(Call.created_at.desc()).limit(1)).scalars().first()
    if wrong is not None or phone.quality == PhoneQuality.BAD:
        checks.append(_c("wrong_number", "Never reported as a wrong number", "fail",
                         "A previous call reached the wrong person." if wrong else "Skip trace marked this number bad."))
    else:
        checks.append(_c("wrong_number", "Never reported as a wrong number", "pass"))

    if phone.owner_confirmed_at:
        checks.append(_c("confirmed", "Owner confirmed on this number", "pass",
                         f"Confirmed on a call {phone.owner_confirmed_at:%b %d, %Y}."))
    else:
        checks.append(_c("confirmed", "Owner confirmed on this number", "warn", "Not confirmed on a call yet."))

    quality = {PhoneQuality.GOOD: "pass", PhoneQuality.UNVERIFIED: "warn", PhoneQuality.STALE: "warn"}.get(phone.quality, "fail")
    checks.append(_c("quality", "Skip-trace quality", quality, phone.quality.value))

    names = _names(lead.property_record)
    match = owner_matches(lead.owner_name, names)
    if match is True:
        checks.append(_c("owner_record", "Owner matches public records", "pass", ", ".join(names)))
    elif match is False:
        checks.append(_c("owner_record", "Owner matches public records", "fail",
                         f"Records list {', '.join(names)} — the property may have sold, so this number may reach "
                         "the previous owner."))
    else:
        checks.append(_c("owner_record", "Owner matches public records", "warn",
                         "No public record pulled yet — refresh property data."))

    local, tz = local_time(lead.property_state, now_utc)
    in_window = CALL_WINDOW[0] <= local.hour < CALL_WINDOW[1]
    checks.append(_c("hours", "Within 8 am–9 pm at the property", "pass" if in_window else "fail",
                     f"It's {local:%-I:%M %p} there ({tz})."))

    if for_ai:
        if lead.ai_call_consent_at:
            checks.append(_c("consent", "Written consent to AI calls on file", "pass", lead.ai_call_consent_source or ""))
        else:
            checks.append(_c("consent", "Written consent to AI calls on file", "fail",
                             "AI-voice calls require the owner's prior express written consent (TCPA). Record it on the lead first."))

    fails = [c for c in checks if c["status"] == "fail"]
    positively_verified = bool(phone.owner_confirmed_at) or (phone.quality == PhoneQuality.GOOD and match is True)
    verdict = {
        "number": phone.number,
        "phone_id": phone.id,
        "checks": checks,
        "fails": [c["key"] for c in fails],
        "verified": positively_verified and not fails,
    }
    if for_ai:
        verdict["ai_allowed"] = not fails and positively_verified
        if not fails and not positively_verified:
            verdict["reason"] = ("Not enough evidence this number reaches the owner: confirm the owner on a human call "
                                 "first, or have a 'good' skip-trace number with an owner that matches public records.")
        elif fails:
            verdict["reason"] = "; ".join(c["label"] + (f" — {c['detail']}" if c["detail"] else "") for c in fails)
    return verdict


def check_all_phones(db: Session, lead: Lead) -> list[dict]:
    """Per-number verification for the call screen (AI eligibility included)."""
    return [check_phone(db, lead, p, for_ai=True) for p in lead.phones]
