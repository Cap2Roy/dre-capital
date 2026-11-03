"""Skip tracing + DNC compliance.

Skip tracing matches the owner of record to phone numbers. The manual:

  * Trace the owner of record, not the address.
  * For an LLC, pull the member from the Secretary of State, then trace.
  * Scrub every number against the federal DNC registry; flagged → mail only.
  * Honor opt-outs instantly and log them.

``skip_trace_lead`` runs the real phone finder (``app.services.owner_phones``):
skip-trace APIs (with their own DNC scrub), public web research and the
property's sale page. It never makes numbers up.

``seed_demo_phones`` exists only for ``python -m app.seed`` (local demo data):
it writes clearly fake numbers with ``source="demo"`` and must never run on
real properties.
"""
from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from app.models import Lead, Phone, PhoneQuality
from app.timeutil import utcnow


def skip_trace_lead(db: Session, lead: Lead) -> dict:
    """Find and store real phone numbers for ``lead`` (see ``owner_phones.find_owner_phones``)."""
    from app.services.owner_phones import find_owner_phones
    return find_owner_phones(db, lead)


# Demo-only DNC hits: numbers ending in these digits are flagged so the seeded demo shows the
# compliance path. Not a registry — real DNC status only comes from a skip-trace provider's scrub.
_DEMO_DNC_LAST4 = {"0000", "1111", "1234", "9999", "4321", "7777"}


def seed_demo_phones(db: Session, lead: Lead) -> list[Phone]:
    """Local demo seeding only: 0 or 2–5 deterministic fake numbers per demo lead (source="demo")."""
    if lead.phones:
        return list(lead.phones)
    seed = lead.owner_name or lead.property_address
    h = hashlib.sha256(seed.encode()).digest()
    if h[0] % 10 < 2:  # ~20% of demo leads get no numbers
        return []
    phones: list[Phone] = []
    for i in range(2 + h[1] % 4):
        npa = 200 + (h[i] % 600)
        nxx = 200 + (h[i + 1] % 600)
        last4 = f"{(h[i + 2] * 7 + i * 31) % 10000:04d}"
        dnc = last4 in _DEMO_DNC_LAST4
        phone = Phone(lead_id=lead.id, number=f"+1{npa}{nxx}{last4}", is_primary=(i == 0),
                      quality=PhoneQuality.DNC if dnc else PhoneQuality.UNVERIFIED, dnc_flagged=dnc,
                      role="owner", source="demo", confidence="low",
                      note="Demo data (python -m app.seed) — not a real number.")
        db.add(phone)
        lead.phones.append(phone)
        phones.append(phone)
    db.flush()
    return phones


def mark_opted_out(db: Session, phone: Phone) -> Phone:
    """Instantly honor an opt-out and log it."""
    phone.opted_out = True
    phone.opted_out_at = utcnow()
    phone.quality = PhoneQuality.OPTED_OUT
    db.flush()
    return phone


def is_callable(phone: Phone) -> bool:
    """A phone is callable only if not DNC-flagged and not opted out."""
    return not phone.dnc_flagged and not phone.opted_out
