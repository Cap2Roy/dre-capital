"""Call flows router: CRUD for reusable call-qualification scripts.

A call flow is the structured script the operator follows on a manual call:
persona/role, opening line, ordered questions, do/don't rules, close, and a
Hot/Warm/Cold scoring rubric.  Exactly one flow may be marked ``is_default``.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CallFlow
from app.schemas import CallFlowCreate, CallFlowOut, CallFlowUpdate
from app.services.auth import require_admin, require_user

router = APIRouter(prefix="/api/call-flows", tags=["call-flows"])


def _serialize_questions(v) -> str:
    if isinstance(v, str):
        return v
    return json.dumps(v or [])


def _serialize_scoring(v) -> str:
    if isinstance(v, str):
        return v
    return json.dumps(v or {})


def _set_default_exclusively(db: Session, keep_id: str) -> None:
    """Ensure only the flow with ``keep_id`` is marked default."""
    others = db.execute(
        select(CallFlow).where(CallFlow.is_default == True, CallFlow.id != keep_id)
    ).scalars().all()
    for o in others:
        o.is_default = False


@router.get("", response_model=list[CallFlowOut])
def list_call_flows(
    active: bool | None = None,
    db: Session = Depends(get_db),
    _user=Depends(require_user),
):
    q = select(CallFlow).order_by(CallFlow.is_default.desc(), CallFlow.created_at.desc())
    if active is not None:
        q = q.where(CallFlow.active == active)
    return db.execute(q).scalars().all()


@router.get("/default", response_model=CallFlowOut | None)
def get_default_call_flow(db: Session = Depends(get_db), _user=Depends(require_user)):
    """Return the default call flow, or the most recent active one, or None."""
    flow = db.execute(
        select(CallFlow).where(CallFlow.is_default == True).limit(1)
    ).scalars().first()
    if flow is None:
        flow = db.execute(
            select(CallFlow).where(CallFlow.active == True).order_by(CallFlow.created_at.desc()).limit(1)
        ).scalars().first()
    return flow


@router.get("/{flow_id}", response_model=CallFlowOut)
def get_call_flow(flow_id: str, db: Session = Depends(get_db), _user=Depends(require_user)):
    flow = db.get(CallFlow, flow_id)
    if not flow:
        raise HTTPException(404, "Call flow not found")
    return flow


@router.post("", response_model=CallFlowOut, status_code=201)
def create_call_flow(payload: CallFlowCreate, db: Session = Depends(get_db), _user=Depends(require_admin)):
    flow = CallFlow(
        name=payload.name,
        description=payload.description,
        role=payload.role,
        opening=payload.opening,
        questions=_serialize_questions(payload.questions),
        rules=_serialize_questions(payload.rules),
        close=payload.close,
        scoring=_serialize_scoring(payload.scoring),
        is_default=payload.is_default,
        active=payload.active,
    )
    db.add(flow)
    db.flush()
    if payload.is_default:
        _set_default_exclusively(db, flow.id)
    db.commit()
    db.refresh(flow)
    return flow


@router.patch("/{flow_id}", response_model=CallFlowOut)
def update_call_flow(
    flow_id: str,
    payload: CallFlowUpdate,
    db: Session = Depends(get_db),
    _user=Depends(require_admin),
):
    flow = db.get(CallFlow, flow_id)
    if not flow:
        raise HTTPException(404, "Call flow not found")
    data = payload.model_dump(exclude_unset=True)
    if "questions" in data:
        data["questions"] = _serialize_questions(data["questions"])
    if "rules" in data:
        data["rules"] = _serialize_questions(data["rules"])
    if "scoring" in data:
        data["scoring"] = _serialize_scoring(data["scoring"])
    for k, v in data.items():
        setattr(flow, k, v)
    if flow.is_default:
        _set_default_exclusively(db, flow.id)
    db.commit()
    db.refresh(flow)
    return flow


@router.delete("/{flow_id}")
def delete_call_flow(flow_id: str, db: Session = Depends(get_db), _user=Depends(require_admin)):
    flow = db.get(CallFlow, flow_id)
    if not flow:
        raise HTTPException(404, "Call flow not found")
    if flow.is_default:
        # Don't leave the system without a default — promote another active flow.
        replacement = db.execute(
            select(CallFlow).where(CallFlow.active == True, CallFlow.id != flow_id).limit(1)
        ).scalars().first()
        if replacement is not None:
            replacement.is_default = True
    db.delete(flow)
    db.commit()
    return {"ok": True}
