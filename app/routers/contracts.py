"""Contracts router: lock it and hand it off."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Buyer, Contract, Lead, LeadStatus, Valuation
from app.schemas import ContractCreate, ContractOut

router = APIRouter(prefix="/api/contracts", tags=["contracts"])


@router.get("", response_model=list[ContractOut])
def list_contracts(db: Session = Depends(get_db)):
    return db.execute(select(Contract).order_by(Contract.created_at.desc())).scalars().all()


@router.post("", response_model=ContractOut, status_code=201)
def create_contract(payload: ContractCreate, lead_id: str = None, db: Session = Depends(get_db)):
    """Create a contract for a lead. Auto-marks lead as under_contract."""
    # lead_id passed as query param for simplicity in the UI
    if not lead_id:
        raise HTTPException(400, "lead_id query param required")
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    buyer = None
    if payload.buyer_id:
        buyer = db.get(Buyer, payload.buyer_id)
        if not buyer:
            raise HTTPException(404, "Buyer not found")
    contract = Contract(
        lead_id=lead_id,
        buyer_id=payload.buyer_id,
        contract_price=payload.contract_price,
        assignment_fee=payload.assignment_fee,
        buyer_price=payload.buyer_price,
        status="signed",
        signed_at=datetime.now(),
        disclosure_sent=payload.disclosure_sent,
        notes=payload.notes,
    )
    db.add(contract)
    lead.status = LeadStatus.UNDER_CONTRACT
    db.commit()
    db.refresh(contract)
    return contract


@router.patch("/{contract_id}/status", response_model=ContractOut)
def update_status(contract_id: str, status: str, db: Session = Depends(get_db)):
    contract = db.get(Contract, contract_id)
    if not contract:
        raise HTTPException(404, "Contract not found")
    contract.status = status
    if status == "assigned":
        contract.assigned_at = datetime.now()
        contract.lead.status = LeadStatus.ASSIGNED
    elif status == "closed":
        contract.closed_at = datetime.now()
        contract.lead.status = LeadStatus.CLOSED
    db.commit()
    db.refresh(contract)
    return contract
