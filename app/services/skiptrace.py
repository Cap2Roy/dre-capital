"""Skip tracing + DNC compliance.

Skip tracing matches the owner of record to phone numbers through a data
vendor.  The manual:

  * Trace the owner of record, not the address.
  * For an LLC, pull the member from the Secretary of State, then trace.
  * Expect 2–5 numbers and a 70–80% hit rate; ~1/3 stale.
  * Scrub every list against the federal DNC registry; flagged → mail only.
  * Honor opt-outs instantly and log them.

The provider is pluggable: ``SKIPTRACE_PROVIDER=mock`` (default) returns a
deterministic synthetic result so the pipeline runs with no external key.
"""
from __future__ import annotations

import hashlib
import random
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Lead, Phone, PhoneQuality

# A small, stable fake DNC "registry" for the mock provider.  In production
# this is the federal DNC list scrub.  Numbers whose last 4 digits hash into
# this set are treated as DNC for demonstration of the compliance path.
_DNC_SEEDS = {"0000", "1111", "1234", "9999", "4321", "7777"}


def _dnc_check(number: str) -> bool:
    """Mock DNC registry check.  Production: scrub against federal list."""
    last4 = number[-4:] if len(number) >= 4 else number
    return last4 in _DNC_SEEDS


def _gen_numbers(seed_name: str, count: int) -> list[str]:
    """Deterministically generate `count` fake US phone numbers from a name."""
    h = hashlib.sha256(seed_name.encode()).digest()
    nums = []
    for i in range(count):
        npa = 200 + (h[i] % 600)          # 200-799
        nxx = 200 + (h[i + 1] % 600)
        # vary last 4 by index so some land on DNC seeds
        last4_int = (h[i + 2] * 7 + i * 31) % 10000
        last4 = f"{last4_int:04d}"
        nums.append(f"+1{npa}{nxx}{last4[:4]}")
    return nums


def skip_trace_lead(db: Session, lead: Lead) -> list[Phone]:
    """Skip trace a lead's owner of record and persist phone numbers.

    Returns the list of Phone rows (including pre-existing ones).
    Idempotent: will not duplicate numbers already on the lead.
    """
    # If lead already has phones, return them (skip tracing already done).
    existing = db.execute(select(Phone).where(Phone.lead_id == lead.id)).scalars().all()
    if existing:
        return list(existing)

    provider = _get_provider()
    records = provider(lead)

    phones: list[Phone] = []
    for i, rec in enumerate(records):
        dnc = _dnc_check(rec["number"])
        quality = PhoneQuality.DNC if dnc else PhoneQuality.UNVERIFIED
        phone = Phone(
            lead_id=lead.id,
            number=rec["number"],
            quality=quality,
            dnc_flagged=dnc,
            is_primary=(i == 0),
        )
        db.add(phone)
        phones.append(phone)
    db.flush()
    return phones


def _get_provider():
    from app.config import get_settings
    provider = get_settings().skiptrace_provider.lower()
    if provider == "mock":
        return _mock_provider
    # Real providers (Twilio Lookup, BeenVerified, etc.) would plug in here.
    return _mock_provider


def _mock_provider(lead: Lead) -> list[dict]:
    """Deterministic mock: 2–5 numbers, ~70-80% hit rate, ~1/3 stale.

    For LLC owners we still return numbers (in production, first pull the
    member from the SoS, then trace that person).
    """
    seed = lead.owner_name or lead.property_address
    h = hashlib.sha256(seed.encode()).digest()
    # 70-80% hit rate: ~20% of leads get no numbers.
    if h[0] % 10 < 2:
        return []
    count = 2 + (h[1] % 4)  # 2-5 numbers
    raw = _gen_numbers(seed, count)
    return [{"number": n} for n in raw]


def mark_opted_out(db: Session, phone: Phone) -> Phone:
    """Instantly honor an opt-out and log it."""
    phone.opted_out = True
    phone.opted_out_at = datetime.now()
    phone.quality = PhoneQuality.OPTED_OUT
    db.flush()
    return phone


def is_callable(phone: Phone) -> bool:
    """A phone is callable only if not DNC-flagged and not opted out."""
    return not phone.dnc_flagged and not phone.opted_out
