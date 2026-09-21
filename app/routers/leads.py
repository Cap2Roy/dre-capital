"""Leads router: CRUD, filtering, detail, follow-ups, notes."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import FollowUp, Lead, LeadStatus, ListType
from app.schemas import FollowUpCreate, LeadCreate, LeadOut, LeadUpdate

router = APIRouter(prefix="/api/leads", tags=["leads"])


@router.get("")
def list_leads(
    db: Session = Depends(get_db),
    q: Optional[str] = None,
    status: Optional[str] = None,
    state: Optional[str] = None,
    min_stack: Optional[int] = None,
    sort: str = "stack",
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
):
    """List leads with filtering + sort. Default sort: stack_depth desc (call order)."""
    stmt = select(Lead)
    if q:
        pat = f"%{q}%"
        stmt = stmt.where(or_(
            Lead.property_address.ilike(pat),
            Lead.owner_name.ilike(pat),
            Lead.property_city.ilike(pat),
            Lead.property_zip.ilike(pat),
        ))
    if status:
        try:
            stmt = stmt.where(Lead.status == LeadStatus(status))
        except ValueError:
            raise HTTPException(400, f"Invalid status: {status}")
    if state:
        stmt = stmt.where(Lead.property_state == state.upper())
    if min_stack is not None:
        stmt = stmt.where(Lead.stack_depth >= min_stack)

    if sort == "stack":
        stmt = stmt.order_by(Lead.stack_depth.desc(), Lead.next_followup.asc().nulls_last())
    elif sort == "followup":
        stmt = stmt.order_by(Lead.next_followup.asc().nulls_last())
    elif sort == "newest":
        stmt = stmt.order_by(Lead.created_at.desc())
    elif sort == "warm":
        stmt = stmt.order_by(Lead.status == LeadStatus.WARM, Lead.next_followup.asc().nulls_last())

    total = db.execute(select(Lead).with_only_columns(Lead.id)).all()
    total_count = len(total)
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    leads = db.execute(stmt).scalars().all()
    return {
        "leads": [LeadOut.model_validate(l).model_dump() for l in leads],
        "total": total_count,
        "page": page,
        "page_size": page_size,
    }


@router.get("/{lead_id}", response_model=LeadOut)
def get_lead(lead_id: str, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    return lead


@router.post("", response_model=LeadOut, status_code=201)
def create_lead(payload: LeadCreate, db: Session = Depends(get_db)):
    from app.services.stacking import get_or_create_lead
    lead, created = get_or_create_lead(db, **payload.model_dump())
    db.commit()
    db.refresh(lead)
    return lead


@router.patch("/{lead_id}", response_model=LeadOut)
def update_lead(lead_id: str, payload: LeadUpdate, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    if payload.status:
        try:
            lead.status = LeadStatus(payload.status)
        except ValueError:
            raise HTTPException(400, f"Invalid status: {payload.status}")
    if payload.notes is not None:
        lead.notes = payload.notes
    if payload.next_followup:
        lead.next_followup = payload.next_followup
    db.commit()
    db.refresh(lead)
    return lead


@router.post("/{lead_id}/followups", response_model=LeadOut)
def add_followup(lead_id: str, payload: FollowUpCreate, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    fu = FollowUp(lead_id=lead_id, due_at=payload.due_at, reason=payload.reason)
    db.add(fu)
    lead.next_followup = payload.due_at
    db.commit()
    db.refresh(lead)
    return lead


@router.get("/{lead_id}/timeline")
def lead_timeline(lead_id: str, db: Session = Depends(get_db)):
    """Unified timeline: calls + followups + valuations + contracts in chrono order."""
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    events = []
    for c in lead.calls:
        events.append({"type": "call", "at": c.created_at.isoformat(), "outcome": c.outcome.value,
                       "attempt": c.attempt_count, "notes": c.notes})
    for f in lead.followups:
        events.append({"type": "followup", "at": f.due_at.isoformat(), "reason": f.reason, "done": f.done})
    for v in lead.valuations:
        events.append({"type": "valuation", "at": v.created_at.isoformat(),
                       "arv": v.arv, "mao": v.mao, "repairs": v.repair_estimate})
    for c in lead.contracts:
        events.append({"type": "contract", "at": (c.signed_at or c.created_at).isoformat(),
                       "status": c.status, "price": c.contract_price})
    events.sort(key=lambda e: e["at"], reverse=True)
    return {"events": events}
