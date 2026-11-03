"""Pipeline board: every active property as a card in its deal stage.

Stages are the property status (no separate field):
New → Outreach (working) → Warm → Dispo (under contract, finding a buyer) →
Title (buyer assigned, closing) → Done (closed); Dead is off the board by default.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models import Lead, LeadStatus

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])

STAGES = [
    ("new", "New", "Just added — research the owner and phones."),
    ("working", "Outreach", "Calling / texting the owner."),
    ("warm", "Warm", "Interested — comp it and make an offer."),
    ("under_contract", "Dispo", "Under contract — send to buyers."),
    ("assigned", "Title", "Buyer assigned — closing at title."),
    ("closed", "Done", "Closed."),
    ("dead", "Dead", "Not pursuing."),
]


def card_value(lead: Lead) -> tuple[Optional[float], str]:
    """Best value we have for the card: estimate → assessed → sale price (minimum bid / list price)."""
    rec = lead.property_record
    if rec is not None and rec.est_value:
        return rec.est_value, "est. value"
    if lead.assessed_value:
        return lead.assessed_value, "assessed"
    for x in lead.listings or []:
        if x.price:
            return x.price, (x.price_label or "price").lower()
    return None, ""


def card(lead: Lead) -> dict:
    value, label = card_value(lead)
    owners = [p for p in lead.phones if (p.role or "owner") == "owner" and not p.dnc_flagged and not p.opted_out]
    contacts = [p for p in lead.phones if (p.role or "owner") != "owner"]
    sites = sorted({x.site_label for x in lead.listings or [] if x.source != "import"})
    origin = next((x.site_label for x in lead.listings or [] if x.source == "import"), None)
    return {
        "id": lead.id, "address": lead.property_address, "city": lead.property_city, "state": lead.property_state,
        "zip": lead.property_zip, "owner": lead.owner_name or None, "status": lead.status.value, "tag": lead.tag,
        "value": value, "value_label": label, "stack": lead.stack_depth,
        "owner_phones": len(owners), "contacts": len(contacts), "listed_on": sites, "origin": origin,
        "next_followup": lead.next_followup.isoformat() + "Z" if lead.next_followup else None,
        "calls": len(lead.calls),
    }


@router.get("")
def pipeline(include_dead: bool = False, q: Optional[str] = None, state: Optional[str] = None,
             tag: Optional[str] = None, db: Session = Depends(get_db)):
    stmt = (select(Lead).where(Lead.archived_at.is_(None))
            .options(selectinload(Lead.phones), selectinload(Lead.listings), selectinload(Lead.calls),
                     selectinload(Lead.property_record))
            .order_by(Lead.stack_depth.desc(), Lead.updated_at.desc()))
    if not include_dead:
        stmt = stmt.where(Lead.status != LeadStatus.DEAD)
    if q:
        pat = f"%{q}%"
        stmt = stmt.where(Lead.property_address.ilike(pat) | Lead.owner_name.ilike(pat) | Lead.property_city.ilike(pat))
    if state:
        stmt = stmt.where(Lead.property_state == state.upper())
    if tag:
        stmt = stmt.where(Lead.tag == tag)
    cards = [card(l) for l in db.execute(stmt).scalars()]
    stages = []
    for sid, label, hint in STAGES:
        if sid == "dead" and not include_dead:
            continue
        in_stage = [c for c in cards if c["status"] == sid]
        stages.append({"id": sid, "label": label, "hint": hint, "count": len(in_stage),
                       "total_value": round(sum(c["value"] or 0 for c in in_stage)),
                       "cards": in_stage})
    return {"stages": stages, "total": len(cards)}
