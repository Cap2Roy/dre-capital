"""Comps provider dispatcher.

``get_comps_for_lead`` routes to the configured comps provider.  Today only
the ``mock`` provider is implemented (synthesizes plausible sold comps from
the lead's assessed value); a real provider (e.g. RentCast / MLS) plugs in by
adding a branch here and reading ``comps_api_key`` from settings — no caller
changes required.

The mock is intentionally retained by user decision: real comps provider wiring
is deferred.  This module is the seam.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.models import Comp, Lead
from app.services.settings import get_setting
from app.services.valuation import mock_comps_for_lead


def _provider_name(db: Session) -> str:
    return (get_setting(db, "comps_provider") or "mock").strip().lower()


def get_comps_for_lead(
    db: Session, lead: Lead, count: int = 6
) -> tuple[list[Comp], str]:
    """Return (comps, provider_name) for a lead per the configured provider.

    Real providers (RentCast, etc.) would be added as ``elif`` branches here and
    built from ``comps_api_key``.  Until then every unconfigured / unknown
    provider name falls back to ``mock`` so the pipeline keeps working.
    """
    provider = _provider_name(db)
    if provider == "mock" or not provider:
        return mock_comps_for_lead(lead, count=count), "mock"
    # ── Real provider seam ───────────────────────────────────────────
    # Example (not wired — no live key in this env):
    #   if provider == "rentcast":
    #       return _rentcast_comps(db, lead, count), "rentcast"
    # Unknown provider → honest mock with a marker so callers can see it.
    return mock_comps_for_lead(lead, count=count), f"mock:{provider}"


def comps_provider_status(db: Session) -> dict:
    """Report the active comps provider + whether a real key is configured."""
    provider = _provider_name(db)
    key = get_setting(db, "comps_api_key") or ""
    return {
        "provider": provider or "mock",
        "real_key_configured": bool(key.strip()),
        "active": "mock" if provider in ("", "mock") else provider,
        "note": (
            "Using synthetic mock comps. Set comps_provider + comps_api_key in "
            "Settings to wire a real provider (RentCast/MLS)."
            if not key.strip() else "A comps API key is set, but the real provider branch is not yet wired."
        ),
    }
