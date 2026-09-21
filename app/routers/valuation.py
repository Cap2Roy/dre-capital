"""Valuation router: comps + MAO computation."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Comp, Lead
from app.schemas import ValuationOut, ValuationRequest
from app.services.valuation import mock_comps_for_lead, run_valuation

router = APIRouter(prefix="/api/leads/{lead_id}/valuation", tags=["valuation"])


@router.post("", response_model=ValuationOut)
def create_valuation(lead_id: str, payload: ValuationRequest, db: Session = Depends(get_db)):
    """Compute ARV + repairs + MAO for a lead.

    If no comps exist yet, the mock provider generates sold comps first.
    In production, comps come from MLS / PropStream.
    """
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    existing_comps = db.query(Comp).filter_by(lead_id=lead_id).count()
    if existing_comps == 0:
        for c in mock_comps_for_lead(lead, count=6):
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
    return val
