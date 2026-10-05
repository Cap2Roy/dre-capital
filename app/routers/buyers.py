"""Buyers router: buyer network + buy-box matching."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Buyer, Lead, TitleCompany, Valuation
from app.schemas import BuyerCreate, BuyerOut, TitleCompanyCreate, TitleCompanyOut
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


# ── Sourcing overview ────────────────────────────────────────────────────────

@router.get("/sourcing")
def sourcing_overview(db: Session = Depends(get_db)):
    """How the buyer network is sourced, and which title partners feed it."""
    buyers = db.execute(select(Buyer).order_by(Buyer.ranking.asc())).scalars().all()
    active_buyers = [b for b in buyers if b.active]
    by_source: dict[str, int] = {}
    for b in active_buyers:
        by_source[b.source or "network"] = by_source.get(b.source or "network", 0) + 1

    partners = db.execute(
        select(TitleCompany).where(TitleCompany.active == True).order_by(TitleCompany.name.asc())
    ).scalars().all()
    partners_out = []
    for p in partners:
        refs = [b for b in active_buyers if b.title_company_id == p.id]
        partners_out.append({
            "id": p.id,
            "name": p.name,
            "contact_name": p.contact_name,
            "coverage_states": p.coverage_states,
            "referral_fee": p.referral_fee,
            "referred_buyers": len(refs),
            "buyer_names": [b.name for b in refs],
        })
    return {
        "total_buyers": len(active_buyers),
        "total_buyers_all": len(buyers),
        "by_source": by_source,
        "title_partners": partners_out,
        "total_partners": len(partners),
    }


# ── Title company partners ───────────────────────────────────────────────────

@router.get("/title-companies", response_model=list[TitleCompanyOut])
def list_title_companies(db: Session = Depends(get_db)):
    return db.execute(
        select(TitleCompany).order_by(TitleCompany.name.asc())
    ).scalars().all()


@router.post("/title-companies", response_model=TitleCompanyOut, status_code=201)
def create_title_company(payload: TitleCompanyCreate, db: Session = Depends(get_db)):
    tc = TitleCompany(**payload.model_dump())
    db.add(tc)
    db.commit()
    db.refresh(tc)
    return tc


@router.put("/title-companies/{tc_id}", response_model=TitleCompanyOut)
def update_title_company(tc_id: str, payload: TitleCompanyCreate, db: Session = Depends(get_db)):
    tc = db.get(TitleCompany, tc_id)
    if not tc:
        raise HTTPException(404, "Title company not found")
    for k, v in payload.model_dump().items():
        setattr(tc, k, v)
    db.commit()
    db.refresh(tc)
    return tc




@router.delete("/title-companies/{tc_id}")
def delete_title_company(tc_id: str, db: Session = Depends(get_db)):
    tc = db.get(TitleCompany, tc_id)
    if not tc:
        raise HTTPException(404, "Title company not found")
    # Unlink any buyers pointing at this partner; they revert to network sourcing.
    for b in tc.buyers:
        b.title_company_id = None
        if b.source == "title_partner":
            b.source = "network"
    db.delete(tc)
    db.commit()
    return {"ok": True}
