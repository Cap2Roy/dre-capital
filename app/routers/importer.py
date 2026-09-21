"""Import router: upload county-record CSVs, list management."""
from __future__ import annotations

import io

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ListType, SourceList
from app.schemas import ImportResult, SourceListOut
from app.services.importer import import_csv
from app.services.stacking import recompute_stack_depths

router = APIRouter(prefix="/api/import", tags=["import"])


@router.post("/csv", response_model=ImportResult)
async def import_csv_file(
    db: Session = Depends(get_db),
    list_name: str = Form(...),
    list_type: str = Form(...),
    county: str = Form(None),
    state: str = Form(None),
    file: UploadFile = File(...),
):
    """Upload a county-records CSV and import leads into a new source list."""
    try:
        lt = ListType(list_type)
    except ValueError:
        raise HTTPException(400, f"Invalid list_type. Valid: {[e.value for e in ListType]}")
    content = await file.read()
    text = content.decode("utf-8-sig")
    try:
        sl = import_csv(db, csv_text=text, list_name=list_name, list_type=lt,
                        county=county, state=state)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return ImportResult(source_list_id=sl.id, name=sl.name,
                        list_type=sl.list_type.value, record_count=sl.record_count)


@router.get("/lists", response_model=list[SourceListOut])
def list_source_lists(db: Session = Depends(get_db)):
    return db.execute(select(SourceList).order_by(SourceList.imported_at.desc())).scalars().all()


@router.post("/recompute-stacks")
def recompute_stacks(db: Session = Depends(get_db)):
    """Recompute all stack depths (maintenance endpoint)."""
    depths = recompute_stack_depths(db)
    db.commit()
    return {"updated": len(depths)}
