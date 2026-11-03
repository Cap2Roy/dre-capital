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

from sqlalchemy import select

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


def _v30_mock_numbers(lead: Lead) -> set[str]:
    """Exactly the numbers the pre-3.1 mock skip trace produced for this lead (same algorithm)."""
    seed = lead.owner_name or lead.property_address
    h = hashlib.sha256(seed.encode()).digest()
    out = set()
    for i in range(5):   # it produced 2–5; the first five cover every count
        npa, nxx = 200 + (h[i] % 600), 200 + (h[i + 1] % 600)
        out.add(f"+1{npa}{nxx}{(h[i + 2] * 7 + i * 31) % 10000:04d}")
    return out


def remove_fabricated_phones(db: Session) -> int:
    """Delete fake numbers the old mock skip trace attached to real properties (v3.0's 'Skip Trace Now').

    Only rows with no source whose number is exactly what that mock generated for the lead's owner;
    a number already used for a call / text / AI call is kept but labeled ``demo``. Idempotent.
    """
    from app.models import AiCall, Call, MessageLog
    removed = 0
    for p in db.execute(select(Phone).where(Phone.source.is_(None))).scalars().all():
        lead = db.get(Lead, p.lead_id)
        if lead is None or p.number not in _v30_mock_numbers(lead):
            continue
        used = any(db.execute(select(m.id).where(m.phone_id == p.id).limit(1)).first() for m in (Call, MessageLog, AiCall))
        if used:
            p.source, p.note = "demo", "Fake number from the old demo skip trace — not a real contact."
            p.quality = PhoneQuality.BAD
        else:
            db.delete(p)
            removed += 1
    db.commit()
    return removed
