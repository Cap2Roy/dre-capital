"""FastAPI application factory + startup.

Serves the JSON API under /api/ and the web UI (Jinja2 + static) at /.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import init_db
from app.routers import (
    auth, buyers, calls, contracts, dashboard, importer, leads, settings, users, valuation,
)

# Paths that don't require authentication
PUBLIC_PATHS = {"/login", "/api/health", "/api/auth/login", "/api/auth/logout", "/api/auth/google/login", "/api/auth/google/callback", "/api/auth/google/config", "/static"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="DRE-Capital Property Pipeline",
    description="Wholesale real estate acquisitions pipeline for Direct Real Estate Capital.",
    version="2.0.0",
    lifespan=lifespan,
)

_base = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(_base / "static")), name="static")
templates = Jinja2Templates(directory=str(_base / "templates"))

# API routers
for r in (auth, users, settings, dashboard, leads, importer, valuation, calls, buyers, contracts):
    app.include_router(r.router)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "DRE-Capital", "version": "2.0.0"}


# ── Auth middleware (redirect to /login if no session) ─────────────────────
@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """Redirect unauthenticated requests to /login, except public paths."""
    path = request.url.path

    # Allow public paths
    if path in PUBLIC_PATHS or path.startswith("/static"):
        return await call_next(request)

    # Check session cookie
    from app.database import SessionLocal
    from app.models import User
    from app.services.auth import decode_session_token

    token = request.cookies.get("dre_session")
    if token:
        payload = decode_session_token(token)
        if payload:
            db = SessionLocal()
            try:
                user = db.get(User, payload.get("uid"))
                if user and user.active:
                    request.state.user = user
                    return await call_next(request)
            finally:
                db.close()

    # Not authenticated
    if path.startswith("/api/"):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Authentication required"}, status_code=401)
    return RedirectResponse("/login", status_code=302)


# ── Web UI (server-rendered pages) ──────────────────────────────────────────

def _ctx(request: Request) -> dict:
    """Common template context with current user."""
    user = getattr(request.state, "user", None)
    return {
        "request": request,
        "current_user": user,
        "user_name": user.name if user else "",
        "user_email": user.email if user else "",
        "user_initials": " ".join(w[0] for w in (user.name or "").split()[:2]).upper() if user else "",
        "user_role": user.role if user else "",
        "is_admin": user.role == "admin" if user else False,
    }


@app.get("/", response_class=HTMLResponse)
def page_dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {**_ctx(request), "page": "dashboard"})


@app.get("/leads", response_class=HTMLResponse)
def page_leads(request: Request):
    return templates.TemplateResponse("leads.html", {**_ctx(request), "page": "leads"})


@app.get("/leads/{lead_id}", response_class=HTMLResponse)
def page_lead_detail(request: Request, lead_id: str):
    return templates.TemplateResponse("lead_detail.html", {**_ctx(request), "page": "lead_detail", "lead_id": lead_id})


@app.get("/queue", response_class=HTMLResponse)
def page_queue(request: Request):
    return templates.TemplateResponse("queue.html", {**_ctx(request), "page": "queue"})


@app.get("/comps", response_class=HTMLResponse)
def page_comps(request: Request):
    return templates.TemplateResponse("comps.html", {**_ctx(request), "page": "comps"})


@app.get("/buyers", response_class=HTMLResponse)
def page_buyers(request: Request):
    return templates.TemplateResponse("buyers.html", {**_ctx(request), "page": "buyers"})


@app.get("/contracts", response_class=HTMLResponse)
def page_contracts(request: Request):
    return templates.TemplateResponse("contracts.html", {**_ctx(request), "page": "contracts"})


@app.get("/import", response_class=HTMLResponse)
def page_import(request: Request):
    return templates.TemplateResponse("import.html", {**_ctx(request), "page": "import"})


@app.get("/settings", response_class=HTMLResponse)
def page_settings(request: Request):
    return templates.TemplateResponse("settings.html", {**_ctx(request), "page": "settings"})


# ── Login page (public) ─────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
def page_login(request: Request):
    """Login page — public, no auth required."""
    from app.config import get_settings
    s = get_settings()
    return templates.TemplateResponse("login.html", {"request": request, "page": "login", "google_auth_enabled": s.google_auth_enabled})
