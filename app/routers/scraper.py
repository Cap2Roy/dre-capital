"""Scraper router: manage data sources, run scrape jobs, review + import results."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ScrapeJob, ScrapeResult, ScrapeSource
from app.schemas import (
    ScrapeImportRequest,
    ScrapeJobCreate,
    ScrapeJobOut,
    ScrapeResultOut,
    ScrapeSourceCreate,
    ScrapeSourceOut,
)
from app.services.scraper import import_scrape_results, run_scrape_job

router = APIRouter(prefix="/api/scraper", tags=["scraper"])


# ── Sources ────────────────────────────────────────────────────────────────────

@router.get("/sources", response_model=list[ScrapeSourceOut])
def list_sources(db: Session = Depends(get_db)):
    """List all configured scrape sources."""
    return db.execute(
        select(ScrapeSource).order_by(ScrapeSource.created_at.desc())
    ).scalars().all()


@router.post("/sources", response_model=ScrapeSourceOut)
def create_source(body: ScrapeSourceCreate, db: Session = Depends(get_db)):
    """Create a new data source to scrape."""
    source = ScrapeSource(
        name=body.name,
        url=body.url,
        source_type=body.source_type,
        state=body.state,
        county=body.county,
        search_params=body.search_params,
        api_key=body.api_key,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


@router.put("/sources/{source_id}", response_model=ScrapeSourceOut)
def update_source(
    source_id: str,
    body: ScrapeSourceCreate,
    db: Session = Depends(get_db),
):
    """Update an existing scrape source."""
    source = db.get(ScrapeSource, source_id)
    if not source:
        raise HTTPException(404, "Source not found")
    source.name = body.name
    source.url = body.url
    source.source_type = body.source_type
    source.state = body.state
    source.county = body.county
    if body.search_params is not None:
        source.search_params = body.search_params
    if body.api_key is not None:
        source.api_key = body.api_key
    db.commit()
    db.refresh(source)
    return source


@router.delete("/sources/{source_id}")
def delete_source(source_id: str, db: Session = Depends(get_db)):
    """Delete a scrape source and all its jobs."""
    source = db.get(ScrapeSource, source_id)
    if not source:
        raise HTTPException(404, "Source not found")
    db.delete(source)
    db.commit()
    return {"ok": True}


# ── Jobs ──────────────────────────────────────────────────────────────────────

@router.post("/jobs", response_model=ScrapeJobOut)
def create_job(body: ScrapeJobCreate, db: Session = Depends(get_db)):
    """Create and immediately run a scrape job.

    The job runs synchronously — the request blocks until the scrape completes
    or fails.  For large sources, this may take 10-30 seconds.
    """
    source = db.get(ScrapeSource, body.source_id)
    if not source:
        raise HTTPException(404, "Source not found")
    if not source.active:
        raise HTTPException(400, "Source is inactive")

    job = ScrapeJob(
        source_id=source.id,
        search_params=body.search_params,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # Run synchronously (sources are small page scrapes, typically <30s)
    run_scrape_job(db, job)
    db.refresh(job)
    return job


@router.get("/jobs", response_model=list[ScrapeJobOut])
def list_jobs(source_id: str | None = None, db: Session = Depends(get_db)):
    """List scrape jobs, optionally filtered by source."""
    q = select(ScrapeJob).order_by(ScrapeJob.created_at.desc())
    if source_id:
        q = q.where(ScrapeJob.source_id == source_id)
    return db.execute(q).scalars().all()


@router.get("/jobs/{job_id}", response_model=ScrapeJobOut)
def get_job(job_id: str, db: Session = Depends(get_db)):
    """Get a single scrape job."""
    job = db.get(ScrapeJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


# ── Results ──────────────────────────────────────────────────────────────────

@router.get("/jobs/{job_id}/results", response_model=list[ScrapeResultOut])
def get_job_results(job_id: str, db: Session = Depends(get_db)):
    """Get all results from a scrape job."""
    job = db.get(ScrapeJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return db.execute(
        select(ScrapeResult).where(ScrapeResult.job_id == job_id)
    ).scalars().all()


@router.post("/import")
def import_results(
    body: ScrapeImportRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Import selected scrape results as leads into a new source list."""
    try:
        result = import_scrape_results(
            db,
            result_ids=body.result_ids,
            list_type=body.list_type,
            list_name=body.list_name,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return result


# ── LLM status ────────────────────────────────────────────────────────────────

@router.get("/llm-status")
def llm_status():
    """Check if the LLM is configured for scraping/parsing."""
    from app.services.scraper import llm_is_configured
    return {"configured": llm_is_configured()}
