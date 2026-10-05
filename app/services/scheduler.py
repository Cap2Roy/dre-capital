"""Daily auto-scrape scheduler.

Runs a scrape job for every active source flagged ``auto_scrape=True`` once a
day, gated by the ``auto_scrape_enabled`` setting.  Lives in-process (an
``asyncio`` background task started from ``app.main.lifespan``) so it needs no
extra infrastructure — but it only fires while the app is running, which is the
common case for this single-instance deployment.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select

from app.database import SessionLocal
from app.models import ScrapeJob, ScrapeSource, ScrapeJobStatus
from app.services.scraper import run_scrape_job
from app.services.settings import get_setting

log = logging.getLogger("dre_capital.scheduler")

# How often the background loop wakes to check whether it's time to run.
_CHECK_INTERVAL_SECONDS = 60 * 15  # 15 min — coarse, low overhead.


def _auto_scrape_hour(db) -> int:
    raw = get_setting(db, "auto_scrape_hour") or "6"
    try:
        h = int(raw)
        return h if 0 <= h <= 23 else 6
    except (TypeError, ValueError):
        return 6


def _auto_scrape_enabled(db) -> bool:
    raw = (get_setting(db, "auto_scrape_enabled") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def run_daily_auto_scrape(db=None) -> dict:
    """Run a scrape job for every active ``auto_scrape`` source now.

    Returns a summary ``{"ran": N, "succeeded": N, "failed": N, "skipped": N}``.
    Safe to call manually (the ``/api/scraper/auto/run`` endpoint does so).
    """
    own = db is None
    if own:
        db = SessionLocal()
    try:
        sources = db.execute(
            select(ScrapeSource).where(
                ScrapeSource.active == True,
                ScrapeSource.auto_scrape == True,
            )
        ).scalars().all()
        ran = succeeded = failed = 0
        for src in sources:
            job = ScrapeJob(source_id=src.id, search_params=src.search_params)
            db.add(job)
            db.commit()
            db.refresh(job)
            try:
                run_scrape_job(db, job)
                db.refresh(job)
                if job.status == ScrapeJobStatus.COMPLETED:
                    succeeded += 1
                else:
                    failed += 1
            except Exception as exc:  # noqa: BLE001 — one bad source can't stop the rest
                log.exception("Auto-scrape job for %s failed: %s", src.name, exc)
                job.status = ScrapeJobStatus.FAILED
                job.error = str(exc)[:500]
                db.commit()
                failed += 1
            ran += 1
        log.info("Auto-scrape: ran=%d succeeded=%d failed=%d", ran, succeeded, failed)
        return {"ran": ran, "succeeded": succeeded, "failed": failed}
    finally:
        if own:
            db.close()


async def scheduler_loop() -> None:
    """Background loop: at the configured hour each day, run the auto-scrape.

    Persists ``last_auto_scrape_at`` in the in-memory module state so a restart
    doesn't double-run on the same day more than once per boot.
    """
    last_run_date: Optional[str] = None
    while True:
        try:
            await asyncio.sleep(_CHECK_INTERVAL_SECONDS)
            db = SessionLocal()
            try:
                # Meeting reminders fire on every wake (independent of the
                # auto-scrape toggle) so a near meeting isn't missed.
                try:
                    from app.services.meetings import process_due_reminders
                    process_due_reminders(db)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Meeting reminder check failed: %s", exc)

                if not _auto_scrape_enabled(db):
                    continue
                now = datetime.now()
                target_hour = _auto_scrape_hour(db)
                today_key = now.strftime("%Y-%m-%d")
                # Only run once per day, and only at/after the configured hour.
                if last_run_date == today_key:
                    continue
                if now.hour < target_hour:
                    continue
                log.info("Starting daily auto-scrape at %s", now.isoformat())
                run_daily_auto_scrape(db)
                last_run_date = today_key
            finally:
                db.close()
        except asyncio.CancelledError:  # app shutdown
            return
        except Exception as exc:  # noqa: BLE001 — loop must not die
            log.exception("Scheduler loop error: %s", exc)
            await asyncio.sleep(60)
