"""Phone numbers, marketplace listings and property data for properties ("Find phones & listings").

- One property: phone finder and listing finder run in parallel; with the ``data`` part, "Refresh
  property data" (property_data.refresh: fill empty fields from public records) runs first, so the
  finders see the owner / mailing address it filled.
- Many properties: ``/api/research/batch`` researches a bounded batch per request (so a
  request stays well inside Cloud Run's timeout); the UI calls it until nothing is left.
  ``parts`` picks the work (default phones + listings; add or use "data" to bulk-fill property data).
- Daily: ``/api/research/cron`` (Cloud Scheduler, X-Cron-Token) researches properties
  added since the last run.
"""
from __future__ import annotations

import hmac
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import and_, case, func, or_, select, true
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models import Lead, MarketplaceListing, Phone, PropertyRecord
from app.schemas import LeadOut

router = APIRouter(prefix="/api", tags=["research"])
log = logging.getLogger("uvicorn.error")

BATCH_MAX = 12          # properties per batch request
WORKERS = 4             # properties researched at once
CRON_BUDGET_S = 12 * 60
PARTS = ("data", "phones", "listings")
DEFAULT_PARTS = ("phones", "listings")


def _run_part(lead_id: str, part: str) -> dict:
    """Run one finder on its own DB session (thread-safe)."""
    db = SessionLocal()
    try:
        lead = db.get(Lead, lead_id)
        if lead is None:
            return {"error": "Property not found"}
        if part == "data":
            from app.services.property_data import PropertyDataError, refresh
            try:
                res = refresh(db, lead)
            except PropertyDataError as exc:  # every source failed: keep the recorded attempt, move on
                db.commit()
                return {"error": str(exc)[:300]}
        elif part == "phones":
            from app.services.owner_phones import find_owner_phones
            res = find_owner_phones(db, lead)
        else:
            from app.services.marketplace import find_listings
            res = find_listings(db, lead)
        db.commit()
        return res
    except Exception as exc:  # one property / one finder never breaks a batch
        db.rollback()
        log.exception("Research (%s) failed for %s", part, lead_id)
        # Record the attempt so batches / the daily run move on (re-run it with redo).
        from app.timeutil import utcnow
        lead = db.get(Lead, lead_id)
        if lead is not None:
            if part == "data":
                from app.services.property_data import _record
                rec = _record(db, lead)
                rec.fetched_at, rec.error = utcnow(), f"Refresh failed: {str(exc)[:300]}"
            else:
                setattr(lead, "contact_checked_at" if part == "phones" else "listings_checked_at", utcnow())
            lead.research_note = ((lead.research_note or "") + f"\n{part} failed: {str(exc)[:200]}").strip()[-2000:]
            db.commit()
        return {"error": str(exc)[:300]}
    finally:
        db.close()


def research_lead(lead_id: str, parts: tuple[str, ...] = DEFAULT_PARTS) -> dict:
    """The requested parts for one property: "data" first (it fills owner / mailing the finders use),
    then phones + listings in parallel. Returns a compact summary."""
    t0 = time.monotonic()
    results: dict[str, dict] = {}
    if "data" in parts:
        results["data"] = _run_part(lead_id, "data")
    rest = [p for p in parts if p != "data"]
    if rest:
        with ThreadPoolExecutor(max_workers=len(rest)) as ex:
            results.update(zip(rest, ex.map(lambda p: _run_part(lead_id, p), rest)))
    phones, listings, data = results.get("phones") or {}, results.get("listings") or {}, results.get("data") or {}
    return {
        "lead_id": lead_id,
        "parts": list(results),
        "phones_added": len(phones.get("added") or []),
        "listings_found": len(listings.get("listings") or []),
        "data_filled": [f"{a['field']}: {a['value']} ({a['source']})" for a in data.get("filled") or []],
        "data_missing": data.get("missing") or [],
        "errors": [f"{k}: {v['error']}" for k, v in results.items() if v.get("error")],
        "cost_usd": round(sum(r.get("cost_usd") or 0 for r in results.values()), 4),
        "seconds": round(time.monotonic() - t0, 1),
        "phones": phones, "listings": listings,
    }


def _missing_any():
    """SQL: the property lacks at least one key field property_data.refresh can fill."""
    from app.services.property_data import KEY_FIELDS
    conds = []
    for c in KEY_FIELDS:
        col = getattr(Lead, c)
        conds.append(col.is_(None))
        if c in ("property_zip", "owner_name", "mailing_address"):
            conds.append(func.trim(col) == "")
    return or_(*conds)


def _data_pending(redo: bool = False):
    """SQL: properties "data" should run on — missing a key field and not refreshed from public records yet."""
    from app.services.property_data import DATA_SOURCE_LABEL
    if redo:
        return _missing_any()
    return and_(_missing_any(), or_(PropertyRecord.id.is_(None), PropertyRecord.fetched_at.is_(None),
                                    PropertyRecord.source.is_(None),
                                    PropertyRecord.source.notlike(f"%{DATA_SOURCE_LABEL}%")))


def _pending(db: Session, limit: int, redo: bool = False,
             parts: tuple[str, ...] = DEFAULT_PARTS) -> list[tuple[str, tuple[str, ...]]]:
    """(lead id, parts still to run on it) for the next ``limit`` properties needing any of ``parts``."""
    conds = {"phones": Lead.contact_checked_at.is_(None), "listings": Lead.listings_checked_at.is_(None),
             "data": _data_pending(redo)}
    if redo:
        conds["phones"] = conds["listings"] = true()
    cols = [case((conds[p], 1), else_=0).label(p) for p in parts]
    q = (select(Lead.id, *cols).outerjoin(PropertyRecord, PropertyRecord.lead_id == Lead.id)
         .where(Lead.archived_at.is_(None), or_(*(conds[p] for p in parts)))
         .order_by(Lead.created_at.desc()).limit(limit))
    return [(row[0], tuple(p for i, p in enumerate(parts) if row[i + 1])) for row in db.execute(q)]


def _pending_ids(db: Session, limit: int, redo: bool = False) -> list[str]:
    return [lid for lid, _ in _pending(db, limit, redo)]


@router.post("/leads/{lead_id}/find-contacts", response_model=None)
def find_contacts(lead_id: str, db: Session = Depends(get_db)):
    """Find the owner's phone numbers and where the property is listed (≈ 30–60 s)."""
    if db.get(Lead, lead_id) is None:
        raise HTTPException(404, "Property not found")
    summary = research_lead(lead_id)
    db.expire_all()
    lead = db.get(Lead, lead_id)
    return {"summary": {k: v for k, v in summary.items() if k not in ("phones", "listings")},
            "phones": summary["phones"], "listings": summary["listings"],
            "lead": LeadOut.model_validate(lead).model_dump(mode="json")}


class PhoneIn(BaseModel):
    number: str
    role: str = "owner"
    contact_name: Optional[str] = None
    note: Optional[str] = None


@router.post("/leads/{lead_id}/phones", status_code=201)
def add_phone(lead_id: str, body: PhoneIn, db: Session = Depends(get_db)):
    """A number the team got themselves (from the owner, a neighbor, the agent…)."""
    from app.services.phones import ROLES, upsert_phone
    lead = db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(404, "Property not found")
    if body.role not in ROLES:
        raise HTTPException(400, f"role must be one of {', '.join(ROLES)}")
    phone, created = upsert_phone(db, lead, body.number, role=body.role, source="manual", confidence="high",
                                  contact_name=(body.contact_name or "").strip() or None, note=body.note)
    if phone is None:
        raise HTTPException(400, "That isn't a valid US phone number.")
    db.commit()
    return {"id": phone.id, "number": phone.number, "created": created}


@router.get("/phone-sources")
def phone_sources(db: Session = Depends(get_db)):
    """Phone-finder sources: on/off + key status (keys never returned)."""
    from app.services.owner_phones import phone_source_states
    return {"sources": phone_source_states(db)}


@router.post("/phone-sources/{source_id}/test")
def test_phone_source(source_id: str, request: Request, db: Session = Depends(get_db)):
    """Admin: check a phone-finder source's key / reachability."""
    from app.services.owner_phones import test_phone_source as run_test
    user = getattr(request.state, "user", None)
    if user is None or user.role != "admin":
        raise HTTPException(403, "Admin only")
    return run_test(db, source_id)


class BatchIn(BaseModel):
    lead_ids: Optional[list[str]] = None   # omitted → next properties not researched yet
    limit: int = 8
    redo: bool = False                      # re-research properties already checked
    parts: list[str] = list(DEFAULT_PARTS)  # any of "data", "phones", "listings"


def _status(db: Session) -> dict:
    from app.services.property_data import DATA_SOURCE_LABEL, KEY_FIELDS
    active = Lead.archived_at.is_(None)
    total = db.execute(select(func.count(Lead.id)).where(active)).scalar() or 0
    researched = db.execute(select(func.count(Lead.id)).where(
        active, Lead.contact_checked_at.isnot(None), Lead.listings_checked_at.isnot(None))).scalar() or 0
    with_owner_phone = db.execute(select(func.count(func.distinct(Phone.lead_id))).join(Lead, Lead.id == Phone.lead_id)
                                  .where(active, Phone.role == "owner")).scalar() or 0
    with_contact = db.execute(select(func.count(func.distinct(Phone.lead_id))).join(Lead, Lead.id == Phone.lead_id)
                              .where(active)).scalar() or 0
    listed = db.execute(select(func.count(func.distinct(MarketplaceListing.lead_id)))
                        .join(Lead, Lead.id == MarketplaceListing.lead_id)
                        .where(active, MarketplaceListing.source != "import")).scalar() or 0
    missing = {}
    for c in KEY_FIELDS:
        col = getattr(Lead, c)
        empty = or_(col.is_(None), func.trim(col) == "") if c in ("property_zip", "owner_name", "mailing_address") else col.is_(None)
        missing[c] = db.execute(select(func.count(Lead.id)).where(active, empty)).scalar() or 0
    with_record = select(func.count(Lead.id)).outerjoin(PropertyRecord, PropertyRecord.lead_id == Lead.id)
    data = {
        "refreshed": db.execute(with_record.where(active, PropertyRecord.source.like(f"%{DATA_SOURCE_LABEL}%"))).scalar() or 0,
        "remaining": db.execute(with_record.where(active, _data_pending())).scalar() or 0,
        "missing_any": db.execute(select(func.count(Lead.id)).where(active, _missing_any())).scalar() or 0,
        "missing": missing,
    }
    return {"total": total, "researched": researched, "remaining": total - researched,
            "with_owner_phone": with_owner_phone, "with_any_phone": with_contact, "listed_on_marketplace": listed,
            "data": data}


@router.get("/research/status")
def research_status(db: Session = Depends(get_db)):
    return _status(db)


@router.post("/research/batch")
def research_batch(body: BatchIn, db: Session = Depends(get_db)):
    """Research up to ``limit`` properties (max 12) in parallel; call again until nothing is left —
    ``remaining`` for phones/listings, ``data.remaining`` for the "data" part (property data fill)."""
    parts = tuple(p for p in PARTS if p in body.parts)
    unknown = set(body.parts) - set(PARTS)
    if unknown or not parts:
        raise HTTPException(400, f"parts must be some of {', '.join(PARTS)}")
    limit = max(1, min(body.limit, BATCH_MAX))
    jobs = [(lid, parts) for lid in body.lead_ids[:limit]] if body.lead_ids else _pending(db, limit, body.redo, parts)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        done = list(ex.map(lambda j: research_lead(*j), jobs))
    db.expire_all()
    return {"processed": [{k: v for k, v in d.items() if k not in ("phones", "listings")} for d in done],
            "cost_usd": round(sum(d["cost_usd"] for d in done), 4), **_status(db)}


@router.post("/research/cron")
def research_cron(request: Request, db: Session = Depends(get_db)):
    """Daily: fill data, phones and listings for properties added since the last run (Cloud Scheduler; token-checked)."""
    expected = os.environ.get("CRON_TOKEN", "")
    if not expected or not hmac.compare_digest(request.headers.get("X-Cron-Token", ""), expected):
        raise HTTPException(403, "Invalid cron token")
    t0, n, cost = time.monotonic(), 0, 0.0
    while time.monotonic() - t0 < CRON_BUDGET_S:
        # New properties get everything: property data first, then phones + listings.
        jobs = _pending(db, WORKERS, parts=("data", "phones", "listings"))
        if not jobs:
            break
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            done = list(ex.map(lambda j: research_lead(*j), jobs))
        n += len(done)
        cost += sum(d["cost_usd"] for d in done)
        db.expire_all()
        if all(d["errors"] for d in done):   # every property failed (e.g. no key): stop, don't loop
            break
    return {"researched": n, "cost_usd": round(cost, 4), **_status(db)}
