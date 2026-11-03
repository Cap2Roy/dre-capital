"""Buyers router: buyer network, buy-box matching and the buyer finder.

- ``/match/{lead_id}``: buyers covering a property's area, with score + reasons.
- ``/find``: find buyers for one property's area (``lead_id``) or a market (``city`` + ``state``).
  Grok research + county parcel query take a minute or two.
- ``/find-all``: search the next due market(s) of active properties (never searched or older than
  30 days); the UI calls it until ``remaining`` is 0.
- ``/markets``: coverage per market (properties, buyers covering it, last search).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Buyer, Lead, TitleCompany, Valuation
from app.schemas import (BuyerCreate, BuyerFindAllRequest, BuyerFindRequest, BuyerMatchOut, BuyerOut,
                         TitleCompanyCreate, TitleCompanyOut)
from app.services import buyer_finder
from app.services.buyers import lead_property_type, lead_value, rank_buyers_for_lead

router = APIRouter(prefix="/api/buyers", tags=["buyers"])


@router.get("", response_model=list[BuyerOut])
def list_buyers(db: Session = Depends(get_db)):
    return db.execute(select(Buyer).order_by(Buyer.ranking.asc(), Buyer.created_at.desc())).scalars().all()


@router.post("", response_model=BuyerOut, status_code=201)
def create_buyer(payload: BuyerCreate, db: Session = Depends(get_db)):
    buyer = Buyer(**payload.model_dump())
    db.add(buyer)
    db.commit()
    db.refresh(buyer)
    return buyer


def _latest_valuation(db: Session, lead_id: str) -> Valuation | None:
    return db.execute(
        select(Valuation).where(Valuation.lead_id == lead_id).order_by(Valuation.created_at.desc())
    ).scalars().first()


def _matches(db: Session, lead: Lead) -> dict:
    val = _latest_valuation(db, lead.id)
    ranked = rank_buyers_for_lead(db, lead, val)
    value, what = lead_value(lead, val)
    kind, kind_why = lead_property_type(lead)
    return {
        "buyers": [BuyerMatchOut(**BuyerOut.model_validate(m.buyer).model_dump(), score=m.score,
                                 reasons=m.reasons).model_dump(mode="json") for m in ranked],
        "total_matched": len(ranked),
        "property": {"value": value, "value_basis": what or None, "type": kind, "type_basis": kind_why or None},
    }


@router.get("/match/{lead_id}")
def match_for_lead(lead_id: str, db: Session = Depends(get_db)):
    """Buyers covering this property's area, best first (score + reasons). Top 5 first per the manual."""
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Property not found")
    return _matches(db, lead)


@router.post("/find")
def find_buyers(payload: BuyerFindRequest, db: Session = Depends(get_db)):
    """Run the buyer finder for a property's area or a market; returns the run + (for a property) matches."""
    try:
        if payload.lead_id:
            lead = db.get(Lead, payload.lead_id)
            if not lead:
                raise HTTPException(404, "Property not found")
            result = buyer_finder.find_buyers_for_lead(db, lead)
        elif payload.city and payload.state:
            lead = None
            result = buyer_finder.find_buyers_for_market(db, payload.city, payload.state, payload.zips)
        else:
            raise HTTPException(422, "Send lead_id, or city and state.")
    except buyer_finder.BuyerFinderError as exc:
        db.rollback()
        raise HTTPException(400, str(exc))
    db.commit()
    if lead is not None:
        db.refresh(lead)
        result["matches"] = _matches(db, lead)
    return result


@router.post("/find-all")
def find_all_markets(payload: BuyerFindAllRequest, db: Session = Depends(get_db)):
    """Search the next ``limit`` due markets; call again until ``remaining`` is 0."""
    return buyer_finder.find_all(db, payload.limit)


@router.get("/markets")
def market_coverage(db: Session = Depends(get_db)):
    """Per market of active properties: properties, buyers covering it, last search, due or not."""
    rows = buyer_finder.market_coverage(db)
    return {"markets": rows, "due": sum(1 for r in rows if r["due"])}


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
