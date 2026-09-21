"""Dashboard router: pipeline overview stats + call queue."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Call, FollowUp, Lead, LeadStatus, Phone
from app.schemas import DashboardStats

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("", response_model=DashboardStats)
def dashboard(db: Session = Depends(get_db)):
    today = datetime.now().date()
    today_start = datetime.combine(today, datetime.min.time())

    total = db.execute(select(func.count(Lead.id))).scalar() or 0
    new = db.execute(select(func.count(Lead.id)).where(Lead.status == LeadStatus.NEW)).scalar() or 0
    warm = db.execute(select(func.count(Lead.id)).where(Lead.status == LeadStatus.WARM)).scalar() or 0
    under_contract = db.execute(
        select(func.count(Lead.id)).where(Lead.status == LeadStatus.UNDER_CONTRACT)
    ).scalar() or 0
    calls_today = db.execute(
        select(func.count(Call.id)).where(Call.created_at >= today_start)
    ).scalar() or 0
    followups_today = db.execute(
        select(func.count(FollowUp.id)).where(
            FollowUp.due_at >= today_start, FollowUp.done == False
        )
    ).scalar() or 0
    stack_today = db.execute(
        select(func.count(Lead.id)).where(Lead.stack_depth >= 3)
    ).scalar() or 0
    stack_week = db.execute(
        select(func.count(Lead.id)).where(Lead.stack_depth == 2)
    ).scalar() or 0
    stack_mail = db.execute(
        select(func.count(Lead.id)).where(Lead.stack_depth <= 1)
    ).scalar() or 0
    dnc = db.execute(
        select(func.count(Phone.id)).where(Phone.dnc_flagged == True)
    ).scalar() or 0

    return DashboardStats(
        total_leads=total, new_leads=new, warm_leads=warm,
        under_contract=under_contract, calls_today=calls_today,
        followups_today=followups_today, stack_today=stack_today,
        stack_this_week=stack_week, stack_mail_only=stack_mail, dnc_blocked=dnc,
    )


@router.get("/queue")
def call_queue(db: Session = Depends(get_db)):
    """Today's call queue: 3+ stack leads first (call today), then 2-stack.

    Sorted by stack_depth desc, then next_followup asc.  Excludes DNC-only
    leads that have no callable phones.
    """
    leads = db.execute(
        select(Lead).where(Lead.stack_depth >= 2).order_by(
            Lead.stack_depth.desc(), Lead.next_followup.asc().nulls_last()
        )
    ).scalars().all()
    queue = []
    for lead in leads:
        callable_phones = [p for p in lead.phones if not p.dnc_flagged and not p.opted_out]
        if not callable_phones:
            continue
        queue.append({
            "id": lead.id,
            "address": lead.property_address,
            "city": lead.property_city,
            "state": lead.property_state,
            "owner": lead.owner_name,
            "stack_depth": lead.stack_depth,
            "tier": "today" if lead.stack_depth >= 3 else "this_week",
            "status": lead.status.value if lead.status else "new",
            "next_followup": lead.next_followup.isoformat() if lead.next_followup else None,
            "has_callable_phone": True,
            "call_count": len(lead.calls),
            "phone_preview": callable_phones[0].number[-4:] if callable_phones else None,
        })
    return {"queue": queue, "count": len(queue)}
