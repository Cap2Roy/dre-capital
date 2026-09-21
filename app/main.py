"""FastAPI application factory + startup.

Serves the JSON API under /api/ and the web UI (Jinja2 + static) at /.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.database import init_db
from app.routers import (
    buyers, calls, contracts, dashboard, importer, leads, valuation,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="DRE-Capital Property Pipeline",
    description="Wholesale real estate acquisitions pipeline for Direct Real Estate Capital.",
    version="1.0.0",
    lifespan=lifespan,
)

_base = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(_base / "static")), name="static")
templates = Jinja2Templates(directory=str(_base / "templates"))

# API routers
for r in (dashboard, leads, importer, valuation, calls, buyers, contracts):
    app.include_router(r.router)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "DRE-Capital", "version": "1.0.0"}


# ── Web UI (server-rendered pages) ──────────────────────────────────────────
# Each page renders a shell + loads data via fetch() from the API.

@app.get("/", response_class=HTMLResponse)
def page_dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request, "page": "dashboard"})


@app.get("/leads", response_class=HTMLResponse)
def page_leads(request: Request):
    return templates.TemplateResponse("leads.html", {"request": request, "page": "leads"})


@app.get("/leads/{lead_id}", response_class=HTMLResponse)
def page_lead_detail(request: Request, lead_id: str):
    return templates.TemplateResponse("lead_detail.html", {"request": request, "page": "lead_detail", "lead_id": lead_id})


@app.get("/queue", response_class=HTMLResponse)
def page_queue(request: Request):
    return templates.TemplateResponse("queue.html", {"request": request, "page": "queue"})


@app.get("/comps", response_class=HTMLResponse)
def page_comps(request: Request):
    return templates.TemplateResponse("comps.html", {"request": request, "page": "comps"})


@app.get("/buyers", response_class=HTMLResponse)
def page_buyers(request: Request):
    return templates.TemplateResponse("buyers.html", {"request": request, "page": "buyers"})


@app.get("/contracts", response_class=HTMLResponse)
def page_contracts(request: Request):
    return templates.TemplateResponse("contracts.html", {"request": request, "page": "contracts"})


@app.get("/import", response_class=HTMLResponse)
def page_import(request: Request):
    return templates.TemplateResponse("import.html", {"request": request, "page": "import"})
