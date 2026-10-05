"""Dial queue: pick the next lead to call for the quick-click workflow.

Priority order (per the manual's call-stack rules):
  1. WARM leads first (they answered / requested callback).
  2. Higher stack_depth first (3+ lists → call today).
  3. Overdue / earliest next_followup first.
  4. Must have at least one callable (non-DNC, non-opt-out) phone.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Lead, LeadStatus, Phone
from app.services.skiptrace import is_callable


def _callable_phone_for(db: Session, lead_id: str) -> Optional[Phone]:
    """Return the first callable phone for a lead, or None."""
    phones = db.execute(
        select(Phone).where(Phone.lead_id == lead_id)
    ).scalars().all()
    for p in phones:
        if is_callable(p):
            return p
    return None


def get_next_lead_to_dial(
    db: Session,
    exclude_lead_ids: Optional[set[str]] = None,
    state: Optional[str] = None,
) -> Optional[dict]:
    """Return the next lead + a callable phone to dial, or None if the queue is empty.

    Returns ``{"lead": Lead, "phone": Phone}`` so the UI can fire the call
    immediately without a second round-trip.
    """
    q = select(Lead).where(Lead.status != LeadStatus.DEAD)
    if state:
        q = q.where(Lead.property_state == state.upper())
    if exclude_lead_ids:
        q = q.where(~Lead.id.in_(exclude_lead_ids))
    # WARM first, then deeper stacks, then oldest follow-up.
    q = q.order_by(
        (Lead.status == LeadStatus.WARM).desc(),
        Lead.stack_depth.desc(),
        Lead.next_followup.asc().nulls_last(),
        Lead.created_at.asc(),
    )
    for lead in db.execute(q).scalars().all():
        phone = _callable_phone_for(db, lead.id)
        if phone:
            return {"lead": lead, "phone": phone}
    return None
