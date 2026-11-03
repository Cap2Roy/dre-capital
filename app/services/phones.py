"""Shared helpers for storing phone numbers found by research / skip tracing.

Every finder (skip-trace APIs, public web research, listing pages) goes through
``upsert_phone`` so a number is stored once per property, with its best source.
"""
from __future__ import annotations

import re
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Lead, Phone, PhoneQuality
from app.timeutil import utcnow

ROLES = ("owner", "listing_agent", "agency")
_CONF_RANK = {None: 0, "low": 1, "medium": 2, "high": 3}
# Toll-free and service numbers are never an owner's personal line.
_TOLL_FREE = {"800", "833", "844", "855", "866", "877", "888"}


def normalize_us(number: str) -> Optional[str]:
    """E.164 for a plausible US/CA number, else None (rejects 555-01xx fiction, N11, bad area codes)."""
    digits = re.sub(r"\D", "", number or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    area, exch = digits[:3], digits[3:6]
    if area[0] in "01" or exch[0] in "01" or area[1:] == "11" or exch[1:] == "11":
        return None
    if exch == "555" and digits[6:8] == "01":
        return None
    return "+1" + digits


def is_toll_free(e164: str) -> bool:
    return e164[2:5] in _TOLL_FREE


def upsert_phone(db: Session, lead: Lead, number: str, *, role: str = "owner", source: str,
                 source_url: Optional[str] = None, confidence: Optional[str] = None,
                 line_type: Optional[str] = None, contact_name: Optional[str] = None,
                 note: Optional[str] = None, dnc: Optional[bool] = None) -> tuple[Optional[Phone], bool]:
    """Store ``number`` on ``lead`` once. Returns (phone, created); (None, False) for an invalid number.

    - Existing rows keep their quality, opt-out, and owner confirmation; only missing details
      are filled, and a higher-confidence source replaces a lower one.
    - ``dnc`` is set only by a real DNC scrub (True/False); None means "not checked".
    - If the same number turns up as both an owner and a contact, owner wins only with at least the
      contact's confidence (a weak web hit never turns an agent's line into the seller's).
    """
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    e164 = normalize_us(number)
    if e164 is None:
        return None, False
    if role == "owner" and is_toll_free(e164):
        role = "agency"   # a toll-free line is a business / agency contact, not the owner's phone
    existing = next((p for p in lead.phones if normalize_us(p.number) == e164), None)
    if existing is None:
        phone = Phone(lead_id=lead.id, number=e164, quality=PhoneQuality.UNVERIFIED, role=role, source=source,
                      source_url=source_url, confidence=confidence, line_type=line_type,
                      contact_name=contact_name, note=(note or "")[:500] or None)
        if dnc is not None:
            phone.dnc_flagged = bool(dnc)
            phone.dnc_checked_at = utcnow()
            if dnc:
                phone.quality = PhoneQuality.DNC
        db.add(phone)
        lead.phones.append(phone)
        return phone, True
    p = existing
    if role == "owner" and p.role != "owner":
        # Only as-strong evidence turns a known agent / agency contact into the owner's number.
        if _CONF_RANK.get(confidence, 0) >= _CONF_RANK.get(p.confidence, 0):
            p.role = "owner"
        else:
            return p, False
    if _CONF_RANK.get(confidence, 0) > _CONF_RANK.get(p.confidence, 0) and p.source != "manual":
        p.source, p.source_url, p.confidence = source, source_url or p.source_url, confidence
    for attr, val in (("line_type", line_type), ("contact_name", contact_name), ("source_url", source_url)):
        if val and not getattr(p, attr):
            setattr(p, attr, val)
    if note and not p.note:
        p.note = note[:500]
    if dnc is not None:
        p.dnc_checked_at = utcnow()
        if dnc and not p.dnc_flagged:
            p.dnc_flagged = True
            if p.quality not in (PhoneQuality.OPTED_OUT,):
                p.quality = PhoneQuality.DNC
    return p, False
