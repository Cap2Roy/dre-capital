"""Scraper router: manage data sources, run scrape jobs, review + import results."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Agent, ScrapeJob, ScrapeResult, ScrapeSource
from app.schemas import (
    ScrapeAgentRunRequest,
    ScrapeImportRequest,
    ScrapeJobCreate,
    ScrapeJobOut,
    ScrapeResultOut,
    ScrapeSourceCreate,
    ScrapeSourceOut,
    ScrapeSourceUpdate,
)
from app.services import gov_sources
from app.services.agents import default_agent
from app.services.scraper import get_source_directory, import_scrape_results, run_scrape_job
from app.services.scraping_agent import ScrapingError, check_scraping_agent, validate_url

router = APIRouter(prefix="/api/scraper", tags=["scraper"])


def _scraping_agent_or_400(db: Session, agent_id: str | None) -> Agent:
    """The requested scraping agent (or the default one), else a clear 400."""
    agent = db.get(Agent, agent_id) if agent_id else default_agent(db, "scraping")
    if agent_id and agent is None:
        raise HTTPException(400, "Scraping agent not found — it may have been deleted.")
    try:
        return check_scraping_agent(agent)
    except ScrapingError as exc:
        raise HTTPException(400, str(exc))


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
    agent_id = body.agent_id or None
    if agent_id or body.source_type == "agent":
        agent_id = _scraping_agent_or_400(db, agent_id).id
    source = ScrapeSource(
        name=body.name,
        url=body.url,
        source_type=body.source_type,
        state=body.state,
        county=body.county,
        search_params=body.search_params,
        api_key=body.api_key,
        auto_scrape=body.auto_scrape,
        agent_id=agent_id,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


@router.put("/sources/{source_id}", response_model=ScrapeSourceOut)
def update_source(
    source_id: str,
    body: ScrapeSourceUpdate,
    db: Session = Depends(get_db),
):
    """Update an existing scrape source — only fields present in the body are applied."""
    source = db.get(ScrapeSource, source_id)
    if not source:
        raise HTTPException(404, "Source not found")
    # Non-nullable columns + secrets: an explicit null means "keep current".
    keep_on_null = {"name", "url", "source_type", "auto_scrape", "search_params", "api_key"}
    data = body.model_dump(exclude_unset=True)
    if "agent_id" in data:
        data["agent_id"] = _scraping_agent_or_400(db, data["agent_id"]).id if data["agent_id"] else None
    for field, value in data.items():
        if value is None and field in keep_on_null:
            continue
        setattr(source, field, value)
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


@router.post("/agent-run", response_model=ScrapeJobOut)
def agent_run(body: ScrapeAgentRunRequest, db: Session = Depends(get_db)):
    """Let a scraping agent crawl from ``url`` (up to its page limit) and return the job.

    Runs synchronously — bounded by the agent's ``max_pages``.  The run is saved
    as an ``agent`` source (reused per agent + URL) so it can be re-run later
    from the Sources list.
    """
    agent = _scraping_agent_or_400(db, body.agent_id)
    try:
        url = validate_url(body.url)
    except ScrapingError as exc:
        raise HTTPException(400, str(exc))
    goal = (body.goal or "").strip()
    params = json.dumps({"goal": goal}) if goal else None

    source = db.execute(
        select(ScrapeSource)
        .where(ScrapeSource.source_type == "agent", ScrapeSource.agent_id == agent.id, ScrapeSource.url == url)
        .order_by(ScrapeSource.created_at)
        .limit(1)
    ).scalars().first()
    if source is None:
        source = ScrapeSource(name=f"{agent.name} · {urlsplit(url).hostname}"[:255], url=url,
                              source_type="agent", agent_id=agent.id)
        db.add(source)
    source.search_params = params  # the latest goal is what "Scrape" re-runs
    db.flush()

    job = ScrapeJob(source_id=source.id, search_params=params)
    db.add(job)
    db.commit()
    db.refresh(job)
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

# ── Source directory ──────────────────────────────────────────────────────────

@router.get("/directory")
def list_directory():
    """List pre-configured, known-working data sources that can be added with one click."""
    return get_source_directory()


# ── Government sale sources ───────────────────────────────────────────────────

@router.post("/government/ensure")
def ensure_government_sources(db: Session = Depends(get_db)):
    """Add any missing government sale sources (active, daily auto-scrape). Idempotent."""
    return {"created": gov_sources.ensure_government_sources(db)}


@router.post("/government/import-new")
def import_new_government_results(
    source_ids: list[str] | None = Body(None, embed=True),
    db: Session = Depends(get_db),
):
    """Import every not-yet-imported result from each government source's latest completed job."""
    return gov_sources.import_all_new(db, source_ids=source_ids or None)


# ── LLM status ────────────────────────────────────────────────────────────────

@router.get("/llm-status")
def llm_status():
    """Check if the LLM is configured for scraping/parsing."""
    from app.services.scraper import llm_is_configured
    return {"configured": llm_is_configured()}


# ── Auto-scrape (daily scheduler) ─────────────────────────────────────────────

@router.post("/auto/run")
def run_auto_scrape_now(db: Session = Depends(get_db)):
    """Manually trigger the daily auto-scrape for all active auto_scrape sources.

    Runs synchronously (same as a single on-demand job per source).  Returns a
    summary of how many sources ran / succeeded / failed.
    """
    from app.services.scheduler import run_daily_auto_scrape
    return run_daily_auto_scrape(db)


@router.post("/auto/cron")
def auto_scrape_cron(request: Request, db: Session = Depends(get_db)):
    """Daily scan, called by Cloud Scheduler with the X-Cron-Token header (public path; token-checked).
    Scrapes every active auto-scrape source, then imports new government-sale properties."""
    import hmac
    import os
    from app.services.scheduler import _auto_scrape_enabled, run_daily_auto_scrape
    from app.services.settings import set_setting
    from app.timeutil import utcnow
    expected = os.environ.get("CRON_TOKEN", "")
    if not expected or not hmac.compare_digest(request.headers.get("X-Cron-Token", ""), expected):
        raise HTTPException(403, "Invalid cron token")
    if not _auto_scrape_enabled(db):
        return {"skipped": "Daily scan is turned off (Settings)."}
    result = run_daily_auto_scrape(db)
    set_setting(db, "last_auto_scrape_date", utcnow().strftime("%Y-%m-%d"))
    db.commit()
    return result


@router.get("/auto/status")
def auto_scrape_status(db: Session = Depends(get_db)):
    """Report whether the daily auto-scrape is enabled and how many sources are opted in."""
    from app.services.scheduler import _auto_scrape_enabled, _auto_scrape_hour
    sources = db.execute(
        select(ScrapeSource).where(
            ScrapeSource.active == True,
            ScrapeSource.auto_scrape == True,
        )
    ).scalars().all()
    return {
        "enabled": _auto_scrape_enabled(db),
        "hour": _auto_scrape_hour(db),
        "opted_in_sources": len(sources),
        "sources": [{"id": s.id, "name": s.name} for s in sources],
    }
