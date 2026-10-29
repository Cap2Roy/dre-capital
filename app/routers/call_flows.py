"""Call flows router: CRUD for reusable call-qualification scripts.

A call flow is the structured script the operator follows on a manual call:
persona/role, opening line, ordered questions, do/don't rules, close, and a
Hot/Warm/Cold scoring rubric.  Exactly one flow may be marked ``is_default``.
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Agent, CallFlow, Lead
from app.schemas import CallFlowCreate, CallFlowOut, CallFlowUpdate, CallScoreRequest
from app.services.auth import require_admin, require_user
from app.timeutil import utcnow

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


@router.get("/guide/{lead_id}")
def get_call_guide(
    lead_id: str,
    flow_id: str | None = None,
    db: Session = Depends(get_db),
    _user=Depends(require_user),
):
    """The call flow adapted to one lead: filled placeholders, per-question facts
    on record, asking tips, and answers captured on earlier calls."""
    from app.services.call_guide import build_call_guide
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Property not found")
    flow = None
    if flow_id:
        flow = db.get(CallFlow, flow_id)
        if not flow:
            raise HTTPException(404, "Call flow not found")
    return build_call_guide(db, lead, flow)


@router.post("/score")
def score_call(payload: CallScoreRequest, db: Session = Depends(get_db), _user=Depends(require_user)):
    """Apply the flow's Hot/Warm/Cold rubric to the answers captured so far."""
    from app.services.call_guide import score_answers
    lead = db.get(Lead, payload.lead_id)
    if not lead:
        raise HTTPException(404, "Property not found")
    flow = db.get(CallFlow, payload.flow_id) if payload.flow_id else db.execute(
        select(CallFlow).where(CallFlow.is_default == True).limit(1)  # noqa: E712
    ).scalars().first()
    if not flow:
        raise HTTPException(404, "Call flow not found")
    return score_answers(db, lead, flow, payload.answers)


class CallFlowDraftRequest(BaseModel):
    agent_id: Optional[str] = None
    brief: str = ""
    base_flow_id: Optional[str] = None
    list_type: Optional[str] = None
    use_call_history: bool = False


BRIEF_MIN, BRIEF_MAX = 10, 4000


@router.post("/draft")
def draft_call_flow(payload: CallFlowDraftRequest, db: Session = Depends(get_db), _user=Depends(require_admin)):
    """Have a scripting agent write (or improve) a call flow. Returns the draft — nothing is saved."""
    from app.services import script_agent
    from app.services.agents import AgentError, default_agent

    brief = (payload.brief or "").strip()
    if len(brief) < BRIEF_MIN:
        raise HTTPException(400, f"Describe what the script is for in at least {BRIEF_MIN} characters.")
    if len(brief) > BRIEF_MAX:
        raise HTTPException(400, f"Keep the brief under {BRIEF_MAX} characters.")
    list_type = (payload.list_type or "").strip() or None
    if list_type and list_type not in script_agent.LIST_TYPE_TONE:
        raise HTTPException(400, f"Unknown list type '{list_type}'.")

    if payload.agent_id:
        agent = db.get(Agent, payload.agent_id)
        if agent is None:
            raise HTTPException(400, "That agent no longer exists.")
        if agent.kind != "scripting":
            raise HTTPException(400, f"{agent.name} is a {agent.kind} agent — pick a scripting agent.")
        if not agent.active:
            raise HTTPException(400, f"{agent.name} is inactive. Activate it on the Agents page or pick another.")
    else:
        agent = default_agent(db, "scripting")
        if agent is None:
            raise HTTPException(400, "No active scripting agent. Add one on the Agents page (/agents).")

    base_flow = None
    if payload.base_flow_id:
        base_flow = db.get(CallFlow, payload.base_flow_id)
        if base_flow is None:
            raise HTTPException(404, "Call flow not found")

    try:
        result = script_agent.draft(db, agent, brief, base_flow=base_flow, list_type=list_type,
                                    use_call_history=bool(payload.use_call_history and base_flow))
    except AgentError as exc:
        agent.last_error = str(exc)[:500]
        db.commit()
        raise HTTPException(502, str(exc))
    agent.last_used_at = utcnow()
    agent.last_error = None
    db.commit()
    result["agent"] = {"id": agent.id, "name": agent.name}
    return result



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
