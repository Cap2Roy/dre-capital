"""Properties (leads) router: CRUD, filtering, detail, edit, archive / restore / delete, follow-ups, notes."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models import FollowUp, Lead, LeadStatus, ListType
from app.schemas import FollowUpCreate, LeadArchive, LeadCreate, LeadOut, LeadUpdate
from app.timeutil import iso_utc, utcnow

router = APIRouter(prefix="/api/leads", tags=["leads"])


@router.get("")
def list_leads(
    db: Session = Depends(get_db),
    q: Optional[str] = None,
    status: Optional[str] = None,
    state: Optional[str] = None,
    min_stack: Optional[int] = None,
    sort: str = "stack",
    archived: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
):
    """Active properties by default; ``archived=true`` lists the archive instead."""
    stmt = select(Lead).where(Lead.archived_at.isnot(None) if archived else Lead.archived_at.is_(None))
    # Eager-load nested collections to avoid N+1 for LeadOut serialization.
    stmt = stmt.options(
        selectinload(Lead.phones),
        selectinload(Lead.comps),
        selectinload(Lead.valuations),
        selectinload(Lead.calls),
        selectinload(Lead.followups),
        selectinload(Lead.contracts),
    )
    if q:
        pat = f"%{q}%"
        stmt = stmt.where(or_(
            Lead.property_address.ilike(pat),
            Lead.owner_name.ilike(pat),
            Lead.property_city.ilike(pat),
            Lead.property_zip.ilike(pat),
        ))
    if status:
        try:
            stmt = stmt.where(Lead.status == LeadStatus(status))
        except ValueError:
            raise HTTPException(400, f"Invalid status: {status}")
    if state:
        stmt = stmt.where(Lead.property_state == state.upper())
    if min_stack is not None:
        stmt = stmt.where(Lead.stack_depth >= min_stack)

    if archived:
        stmt = stmt.order_by(Lead.archived_at.desc())
    elif sort == "stack":
        stmt = stmt.order_by(Lead.stack_depth.desc(), Lead.next_followup.asc().nulls_last())
    elif sort == "followup":
        stmt = stmt.order_by(Lead.next_followup.asc().nulls_last())
    elif sort == "newest":
        stmt = stmt.order_by(Lead.created_at.desc())
    elif sort == "warm":
        stmt = stmt.order_by((Lead.status == LeadStatus.WARM).desc(), Lead.next_followup.asc().nulls_last())

    # Total must reflect the same filters so pagination stays correct.
    total_count = db.execute(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ).scalar() or 0
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    leads = db.execute(stmt).scalars().all()
    return {
        "leads": [LeadOut.model_validate(l).model_dump(mode="json") for l in leads],
        "total": total_count,
        "page": page,
        "page_size": page_size,
    }

@router.get("/comps/status")
def comps_status(db: Session = Depends(get_db)):
    """Report the active comps provider (mock vs real) + key configured."""
    from app.services.comps import comps_provider_status
    return comps_provider_status(db)


@router.get("/{lead_id}", response_model=LeadOut)
def get_lead(lead_id: str, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Property not found")
    return lead


@router.post("", response_model=LeadOut, status_code=201)
def create_lead(payload: LeadCreate, db: Session = Depends(get_db)):
    from app.services.stacking import get_or_create_lead
    lead, created = get_or_create_lead(db, **payload.model_dump())
    db.commit()
    db.refresh(lead)
    return lead


@router.patch("/{lead_id}", response_model=LeadOut)
def update_lead(lead_id: str, payload: LeadUpdate, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Property not found")
    data = payload.model_dump(exclude_unset=True)
    if data.get("status"):
        try:
            lead.status = LeadStatus(data["status"])
        except ValueError:
            raise HTTPException(400, f"Invalid status: {data['status']}")
    if "notes" in data:
        lead.notes = data["notes"]
    if data.get("next_followup"):
        lead.next_followup = data["next_followup"]
    # Property / owner edits
    text_fields = ("property_address", "property_city", "property_zip", "owner_name", "owner_email",
                   "mailing_address", "mailing_city", "mailing_zip", "rehab_level")
    for f in text_fields:
        if f in data:
            v = (data[f] or "").strip() or None
            if f == "property_address" and v is None:
                raise HTTPException(400, "The property address can't be empty.")
            setattr(lead, f, v if v is not None or f != "owner_name" else "")   # owner_name is NOT NULL
    for f in ("property_state", "mailing_state"):
        if f in data:
            v = (data[f] or "").strip().upper() or None
            if v and len(v) != 2:
                raise HTTPException(400, "Use a 2-letter state code, e.g. TX.")
            setattr(lead, f, v)
    for f in ("beds", "baths", "sqft", "year_built", "assessed_value", "taxes_owed", "owner_is_llc"):
        if f in data:
            setattr(lead, f, data[f])
    if any(f in data for f in ("property_address", "mailing_address")):
        lead.absentee = bool(lead.mailing_address and lead.property_address
                             and lead.mailing_address.strip().lower() != lead.property_address.strip().lower())
    db.commit()
    db.refresh(lead)
    return lead


@router.post("/{lead_id}/archive", response_model=LeadOut)
def archive_lead(lead_id: str, payload: LeadArchive, request: Request, db: Session = Depends(get_db)):
    """Hide a property from Properties, the call queue, and the dashboard (restorable).
    Re-scans keep it archived instead of bringing it back."""
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Property not found")
    user = getattr(request.state, "user", None)
    lead.archived_at = utcnow()
    lead.archived_by = user.id if user else None
    lead.archive_reason = (payload.reason or "").strip()[:255] or None
    db.commit()
    db.refresh(lead)
    return lead


@router.post("/{lead_id}/restore", response_model=LeadOut)
def restore_lead(lead_id: str, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Property not found")
    lead.archived_at = lead.archived_by = lead.archive_reason = None
    db.commit()
    db.refresh(lead)
    return lead


@router.delete("/{lead_id}")
def delete_lead(lead_id: str, db: Session = Depends(get_db)):
    """Permanently delete an archived property with its phones, calls, comps and follow-ups.
    Properties with contracts can only be archived (deal history is kept)."""
    from app.models import AiCall, Call, Meeting, MessageLog
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Property not found")
    if lead.archived_at is None:
        raise HTTPException(409, "Archive the property first — only archived properties can be deleted.")
    if lead.contracts:
        raise HTTPException(409, "This property has contracts, so it stays archived instead of being deleted.")
    phone_ids = [p.id for p in lead.phones]
    for model in (MessageLog, Meeting):
        for row in db.execute(select(model).where(model.lead_id == lead.id)).scalars():
            row.lead_id = None
            if hasattr(row, "phone_id") and row.phone_id in phone_ids:
                row.phone_id = None
    for ai in db.execute(select(AiCall).where(or_(AiCall.lead_id == lead.id, AiCall.phone_id.in_(phone_ids or [""])))).scalars():
        ai.lead_id = None
        ai.phone_id = None
        ai.call_id = None
    for call in db.execute(select(Call).where(Call.lead_id == lead.id)).scalars():
        db.delete(call)
    db.flush()
    db.delete(lead)
    db.commit()
    return {"deleted": lead_id}


@router.post("/{lead_id}/followups", response_model=LeadOut)
def add_followup(lead_id: str, payload: FollowUpCreate, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Property not found")
    fu = FollowUp(lead_id=lead_id, due_at=payload.due_at, reason=payload.reason)
    db.add(fu)
    lead.next_followup = payload.due_at
    db.commit()
    db.refresh(lead)
    return lead


@router.post("/{lead_id}/skiptrace", response_model=LeadOut)
def skiptrace_lead(lead_id: str, db: Session = Depends(get_db)):
    """Skip trace the owner of record; numbers are DNC-scrubbed by the service."""
    from app.services.skiptrace import skip_trace_lead
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Property not found")
    skip_trace_lead(db, lead)
    db.commit()
    db.refresh(lead)
    return lead


@router.get("/{lead_id}/timeline")
def lead_timeline(lead_id: str, db: Session = Depends(get_db)):
    """Unified timeline: calls + followups + valuations + contracts in chrono order."""
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Property not found")
    events = []  # (sort key: naive-UTC datetime, event dict)
    for c in lead.calls:
        events.append((c.created_at, {"type": "call", "outcome": c.outcome.value,
                                      "attempt": c.attempt_count, "notes": c.notes}))
    for f in lead.followups:
        events.append((f.due_at, {"type": "followup", "reason": f.reason, "done": f.done}))
    for v in lead.valuations:
        events.append((v.created_at, {"type": "valuation",
                                      "arv": v.arv, "mao": v.mao, "repairs": v.repair_estimate}))
    for c in lead.contracts:
        events.append((c.signed_at or c.created_at, {"type": "contract",
                                                     "status": c.status, "price": c.contract_price}))
    events.sort(key=lambda e: e[0], reverse=True)
    return {"events": [{**ev, "at": iso_utc(at)} for at, ev in events]}


# ── Property data, call brief, photo, AI-call consent ────────────────────────

def _lead_or_404(db: Session, lead_id: str) -> Lead:
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Property not found")
    return lead


@router.get("/{lead_id}/brief")
def lead_brief(lead_id: str, db: Session = Depends(get_db)):
    """Everything the dialer / AI agent needs: value, tax, debt, MLS status, photo, number checks."""
    from app.services.property_data import brief
    return brief(db, _lead_or_404(db, lead_id))


@router.post("/{lead_id}/property-data")
def refresh_property_data(lead_id: str, db: Session = Depends(get_db)):
    """Pull public records, value estimate, and MLS listing from RentCast (up to 3 calls)."""
    from app.services.property_data import PropertyDataError, brief, enrich
    lead = _lead_or_404(db, lead_id)
    try:
        enrich(db, lead)
    except PropertyDataError as exc:
        db.commit()  # keep usage counter + recorded error
        raise HTTPException(502, str(exc))
    db.commit()
    db.refresh(lead)
    return brief(db, lead)


@router.get("/{lead_id}/photo")
def lead_photo(lead_id: str, db: Session = Depends(get_db)):
    """Street View photo of the house (satellite view if there's no Street View)."""
    from fastapi.responses import Response
    from app.services.property_data import PropertyDataError, photo
    lead = _lead_or_404(db, lead_id)
    try:
        data, media = photo(db, lead)
    except PropertyDataError as exc:
        raise HTTPException(404, str(exc))
    db.commit()
    return Response(content=data, media_type=media, headers={"Cache-Control": "private, max-age=86400"})


@router.put("/{lead_id}/ai-consent")
def set_ai_consent(lead_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    """Record (or remove) the owner's prior express written consent to AI-voice calls."""
    from app.timeutil import utcnow
    lead = _lead_or_404(db, lead_id)
    if payload.get("remove"):
        lead.ai_call_consent_at = lead.ai_call_consent_source = lead.ai_call_consent_by = None
    else:
        source = (payload.get("source") or "").strip()
        if len(source) < 10:
            raise HTTPException(400, "Describe where the written consent came from (form, date, wording) — at least 10 characters.")
        user = getattr(request.state, "user", None)
        lead.ai_call_consent_at = utcnow()
        lead.ai_call_consent_source = source[:500]
        lead.ai_call_consent_by = user.id if user else None
    db.commit()
    return {"ai_consent": {"at": iso_utc(lead.ai_call_consent_at), "source": lead.ai_call_consent_source}
            if lead.ai_call_consent_at else None}


@router.get("/property-data/status")
def property_data_status(check: bool = False, db: Session = Depends(get_db)):
    """RentCast budget/usage + whether keys are set; ``check=true`` validates the Maps key (free metadata call)."""
    import httpx
    from app.services.property_data import MAPS_BASE, maps_key, rentcast_key, usage
    out = {"rentcast": {"configured": bool(rentcast_key(db)), **usage(db)},
           "maps": {"configured": bool(maps_key(db)), "ok": None, "error": None}}
    if check and out["maps"]["configured"]:
        try:
            meta = httpx.get(f"{MAPS_BASE}/streetview/metadata", timeout=15,
                             params={"location": "Dallas, TX", "key": maps_key(db)}).json()
            out["maps"]["ok"] = meta.get("status") in ("OK", "ZERO_RESULTS")
            out["maps"]["error"] = None if out["maps"]["ok"] else f"{meta.get('status')}: {meta.get('error_message', '')}"
        except Exception as exc:
            out["maps"]["ok"], out["maps"]["error"] = False, str(exc)
    return out
