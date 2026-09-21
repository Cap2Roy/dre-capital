"""Buyers router: buyer network + buy-box matching."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Buyer, Lead, Valuation
from app.schemas import BuyerCreate, BuyerOut
from app.services.buyers import match_buyers_for_lead

router = APIRouter(prefix="/api/buyers", tags=["buyers"])


@router.get("", response_model=list[BuyerOut])
def list_buyers(db: Session = Depends(get_db)):
    return db.execute(select(Buyer).order_by(Buyer.ranking.asc())).scalars().all()


@router.post("", response_model=BuyerOut, status_code=201)
def create_buyer(payload: BuyerCreate, db: Session = Depends(get_db)):
    buyer = Buyer(**payload.model_dump())
    db.add(buyer)
    db.commit()
    db.refresh(buyer)
    return buyer


@router.get("/match/{lead_id}")
def match_for_lead(lead_id: str, db: Session = Depends(get_db)):
    """Find buyers whose buy-box matches a lead. Top 5 first per the manual."""
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    val = db.execute(
        select(Valuation).where(Valuation.lead_id == lead_id)
        .order_by(Valuation.created_at.desc())
    ).scalars().first()
    buyers = match_buyers_for_lead(db, lead, val)
    return {
        "buyers": [BuyerOut.model_validate(b).model_dump() for b in buyers[:5]],
        "total_matched": len(buyers),
    }
