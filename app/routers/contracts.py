"""Contracts router: lock it and hand it off."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Buyer, Contract, Lead, LeadStatus, Valuation
from app.schemas import ContractCreate, ContractOut

from app.services.contracts import (
    contract_context,
    find_contract_template,
    render_contract_document,
    send_contract_email,
)

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


@router.get("/{contract_id}/document")
def get_contract_document(
    contract_id: str,
    party: str = "seller",
    db: Session = Depends(get_db),
):
    """Render the contract document for a party ('seller' or 'buyer').

    400 if no template is configured for that party.
    """
    contract = db.get(Contract, contract_id)
    if not contract:
        raise HTTPException(404, "Contract not found")
    if party not in ("seller", "buyer"):
        raise HTTPException(400, "party must be 'seller' or 'buyer'")
    tpl = find_contract_template(db, party)
    if tpl is None:
        raise HTTPException(
            400,
            f"No active {party} contract template configured (create one in Message Templates).",
        )
    return {
        "contract_id": contract.id,
        "party": party,
        "template_id": tpl.id,
        "template_name": tpl.name,
        "subject": tpl.subject,
        "document": render_contract_document(db, contract, party),
        "context": contract_context(db, contract, party),
    }


@router.post("/{contract_id}/send")
def send_contract_document(
    contract_id: str,
    party: str = "seller",
    db: Session = Depends(get_db),
):
    """Email the rendered contract document to the seller or buyer party.

    502 if SMTP is not configured (email disabled), 400 if no template or no
    known recipient address.
    """
    contract = db.get(Contract, contract_id)
    if not contract:
        raise HTTPException(404, "Contract not found")
    if party not in ("seller", "buyer"):
        raise HTTPException(400, "party must be 'seller' or 'buyer'")
    try:
        entry = send_contract_email(db, contract, party)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(502, str(exc))
    db.commit()
    db.refresh(entry)
    return {
        "contract_id": contract.id,
        "party": party,
        "log_id": entry.id,
        "status": entry.status,
        "to": entry.to_number,
    }
