"""List stacking engine.

The manual: "Any single list is being mailed by every investor in your market.
The money is in the overlap."  Stack depth = how many lists a property lands on.

Call order:
  3+ lists → call today
  2 lists  → call this week
  1 list   → mail and drip only

We dedupe by a normalized address key, count memberships per lead, and
persist stack_depth on the lead for fast sort.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Lead, LeadListMembership, ListType, SourceList


def normalize_address(address: str) -> str:
    """Normalize an address for dedup.

    Lowercase, strip punctuation, collapse whitespace, expand common suffixes.
    Good enough for county-CSV dedup without a geocoder.
    """
    if not address:
        return ""
    a = address.lower().strip()
    a = re.sub(r"[.,#]", " ", a)
    a = re.sub(r"\s+", " ", a)
    replacements = {
        "street": "st", "avenue": "ave", "boulevard": "blvd",
        "road": "rd", "drive": "dr", "lane": "ln", "court": "ct",
        "circle": "cir", "place": "pl", "parkway": "pkwy",
        "apartment": "apt", "suite": "ste", "north": "n", "south": "s",
        "east": "e", "west": "w",
    }
    for k, v in replacements.items():
        a = re.sub(rf"\b{k}\b", v, a)
    return a.strip()


def address_key(address: str, zip_code: str | None = None) -> str:
    z = (zip_code or "").strip()
    return f"{normalize_address(address)}|{z}"


def get_or_create_lead(
    db: Session,
    *,
    property_address: str,
    property_zip: str | None = None,
    property_city: str | None = None,
    property_state: str | None = None,
    owner_name: str | None = None,
    mailing_address: str | None = None,
    mailing_city: str | None = None,
    mailing_state: str | None = None,
    mailing_zip: str | None = None,
    beds: int | None = None,
    baths: float | None = None,
    sqft: int | None = None,
    year_built: int | None = None,
    assessed_value: float | None = None,
    taxes_owed: float | None = None,
    owner_is_llc: bool = False,
) -> tuple[Lead, bool]:
    """Find a lead by normalized address+zip, or create it.

    Returns (lead, created).  Updates fields on existing leads when new data
    is non-null (keeps the richest known record).
    """
    key = address_key(property_address, property_zip)
    stmt = select(Lead).where(Lead.property_address == property_address)
    if property_zip:
        stmt = stmt.where(Lead.property_zip == property_zip)
    lead = db.execute(stmt).scalar_one_or_none()

    # Fallback: normalized match (catches "123 Main St" vs "123 main st.")
    if lead is None:
        all_leads = db.execute(select(Lead)).scalars().all()
        for l in all_leads:
            if address_key(l.property_address, l.property_zip) == key:
                lead = l
                break

    if lead is None:
        absentee = bool(mailing_address and mailing_address.strip() and
                        normalize_address(mailing_address) != normalize_address(property_address))
        lead = Lead(
            property_address=property_address,
            property_zip=property_zip,
            property_city=property_city,
            property_state=property_state,
            owner_name=owner_name or "",
            owner_is_llc=owner_is_llc,
            mailing_address=mailing_address,
            mailing_city=mailing_city,
            mailing_state=mailing_state,
            mailing_zip=mailing_zip,
            absentee=absentee,
            beds=beds, baths=baths, sqft=sqft, year_built=year_built,
            assessed_value=assessed_value, taxes_owed=taxes_owed,
        )
        db.add(lead)
        db.flush()
        return lead, True

    # Update with any richer data
    updated = False
    for attr, val in [("owner_name", owner_name), ("property_city", property_city),
                      ("property_state", property_state), ("mailing_address", mailing_address),
                      ("mailing_city", mailing_city), ("mailing_state", mailing_state),
                      ("mailing_zip", mailing_zip), ("beds", beds), ("baths", baths),
                      ("sqft", sqft), ("year_built", year_built),
                      ("assessed_value", assessed_value), ("taxes_owed", taxes_owed)]:
        if val is not None and not getattr(lead, attr):
            setattr(lead, attr, val)
            updated = True
    if updated:
        lead.absentee = bool(lead.mailing_address and
                             normalize_address(lead.mailing_address) != normalize_address(lead.property_address))
    return lead, False


def add_lead_to_list(db: Session, lead: Lead, source_list: SourceList) -> bool:
    """Add a lead to a source list if not already on it. Returns True if added."""
    existing = db.execute(
        select(LeadListMembership).where(
            LeadListMembership.lead_id == lead.id,
            LeadListMembership.source_list_id == source_list.id,
        )
    ).scalar_one_or_none()
    if existing:
        return False
    db.add(LeadListMembership(lead_id=lead.id, source_list_id=source_list.id))
    return True


def recompute_stack_depths(db: Session, lead_ids: Iterable[str] | None = None) -> dict[str, int]:
    """Recount list memberships per lead and update stack_depth.

    Returns a {lead_id: depth} map.  If lead_ids is None, recomputes all.
    """
    db.flush()  # ensure pending inserts visible to count query (autoflush=False)
    stmt = select(LeadListMembership.lead_id)
    if lead_ids is not None:
        ids = list(lead_ids)
        if not ids:
            return {}
        stmt = stmt.where(LeadListMembership.lead_id.in_(ids))
    rows = db.execute(stmt).all()
    counts: Counter[str] = Counter(r[0] for r in rows)
    result: dict[str, int] = {}
    if lead_ids is not None:
        for lid in ids:
            depth = counts.get(lid, 0)
            lead = db.get(Lead, lid)
            if lead:
                lead.stack_depth = depth
            result[lid] = depth
    else:
        for lead in db.execute(select(Lead)).scalars().all():
            depth = counts.get(lead.id, 0)
            lead.stack_depth = depth
            result[lead.id] = depth
    db.flush()
    return result


def call_priority_tier(stack_depth: int) -> str:
    """Manual rule: 3+ today, 2 this week, 1 mail only."""
    if stack_depth >= 3:
        return "today"
    if stack_depth == 2:
        return "this_week"
    return "mail_only"
