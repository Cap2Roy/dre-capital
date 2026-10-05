"""Valuation router: comps + MAO computation."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Comp, Lead
from app.schemas import ValuationOut, ValuationRequest
from app.services.comps import get_comps_for_lead
from app.services.valuation import run_valuation

router = APIRouter(prefix="/api/leads/{lead_id}/valuation", tags=["valuation"])


@router.post("", response_model=ValuationOut)
def create_valuation(lead_id: str, payload: ValuationRequest, db: Session = Depends(get_db)):
    """Compute ARV + repairs + MAO for a lead.

    If no comps exist yet (or ``refresh_comps`` is set), the configured comps
    provider generates sold comps.  The response surfaces the provider name
    and whether it was a mock fallback so callers never mistake synthetic
    comps for live data.
    """
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    provider = None
    if payload.refresh_comps:
        comps, provider = get_comps_for_lead(db, lead, count=6)
        # Replace stored comps for this lead with the fresh set.
        for c in db.execute(select(Comp).where(Comp.lead_id == lead_id)).scalars().all():
            db.delete(c)
        db.flush()
        for c in comps:
            db.add(c)
        db.flush()
    else:
        existing = db.execute(select(Comp).where(Comp.lead_id == lead_id)).scalars().all()
        if not existing:
            comps, provider = get_comps_for_lead(db, lead, count=6)
            for c in comps:
                db.add(c)
            db.flush()

    val = run_valuation(
        db, lead,
        repair_level=payload.repair_level, fee=payload.fee,
        roof=payload.roof, hvac=payload.hvac, foundation=payload.foundation,
        repipe=payload.repipe, panel=payload.panel, notes=payload.notes,
    )
    db.commit()
    db.refresh(val)
    resp = ValuationOut.model_validate(val)
    if provider:
        resp.comps_provider = provider
        resp.comps_is_mock = provider == "mock" or provider.startswith("mock:")
    return resp
