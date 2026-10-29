"""Scan property: run the enabled property data sources for one lead; manage the source catalog."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Lead
from app.services import property_scan as ps
from app.services.auth import require_admin

router = APIRouter(prefix="/api", tags=["property-scan"])


@router.post("/leads/{lead_id}/scan")
def scan_lead(lead_id: str, request: Request, db: Session = Depends(get_db)):
    """Run every enabled, keyed source for this property (Grok web research ~30 s); store and return the scan."""
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    user = getattr(request.state, "user", None)
    try:
        scan = ps.start_scan(db, lead, user.id if user else None)
    except ps.ScanError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.commit()  # visible as "running" while the sources work
    try:
        ps.run_scan(db, scan)
    except Exception as exc:
        db.rollback()
        ps.fail_scan(db, scan, f"{type(exc).__name__}: {exc}")
        db.commit()
        raise HTTPException(500, f"Scan failed: {exc}") from exc
    db.commit()
    return ps.serialize(scan)


@router.get("/leads/{lead_id}/scans")
def lead_scans(lead_id: str, limit: int = 10, db: Session = Depends(get_db)):
    """This property's scans, latest first."""
    if not db.get(Lead, lead_id):
        raise HTTPException(404, "Lead not found")
    return [ps.serialize(s) for s in ps.list_scans(db, lead_id, max(1, min(limit, 50)))]


@router.get("/property-sources")
def property_sources(db: Session = Depends(get_db)):
    """Source catalog: description, links, enabled, key status (keys are never returned)."""
    return ps.catalog_status(db)


class SourceUpdate(BaseModel):
    id: str
    enabled: Optional[bool] = None
    key: Optional[str] = None  # "" clears; all-bullets (masked echo) keeps the current key


class SourcesUpdate(BaseModel):
    sources: list[SourceUpdate] = []
    model: Optional[str] = None


@router.put("/property-sources")
def update_property_sources(body: SourcesUpdate, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    """Enable/disable sources, set their API keys, pick the Grok model. Admin only."""
    try:
        ps.update_sources(db, [s.model_dump() for s in body.sources], body.model)
    except ps.ScanError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.commit()
    return ps.catalog_status(db)


@router.post("/property-sources/{source_id}/test")
def test_property_source(source_id: str, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    """Check a source's key with its cheapest documented request. Admin only."""
    try:
        result = ps.test_source(db, source_id)
    except ps.ScanError as exc:
        raise HTTPException(404, str(exc)) from exc
    db.commit()  # RentCast test counts one call against the budget
    return result
