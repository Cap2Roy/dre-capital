"""Settings service: key-value configuration stored in DB.

Provides get/set helpers for API keys (Twilio, skip trace) and call follow-up
cadence.  Secrets are masked when returned via API.  Values fall back to
environment variables when not set in the DB.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Setting

# Keys that are treated as secrets (masked in API output)
_SECRET_SUFFIXES = ("_key", "_token", "_sid", "_password")

# Default call follow-up cadence (days after call)
DEFAULT_CADENCE: dict[str, int] = {
    "cadence_no_answer": 3,
    "cadence_voicemail": 5,
    "cadence_not_interested": 90,
    "cadence_callback_requested": 7,
    "cadence_warm": 1,
    "cadence_offer_sent": 3,
    "cadence_dnc_request": 0,
    "cadence_default": 14,
}

# Known setting keys, their labels, categories, and env-var fallbacks
SETTING_DEFS: dict[str, dict] = {
    # ── Calling (Twilio) ──────────────────────────────────────────
    "twilio_account_sid": {
        "label": "Twilio Account SID",
        "category": "calling",
        "is_secret": True,
        "env": "TWILIO_ACCOUNT_SID",
        "description": "Your Twilio account SID (found in Twilio Console).",
    },
    "twilio_auth_token": {
        "label": "Twilio Auth Token",
        "category": "calling",
        "is_secret": True,
        "env": "TWILIO_AUTH_TOKEN",
        "description": "Your Twilio auth token (found in Twilio Console).",
    },
    "twilio_from_number": {
        "label": "Twilio From Number",
        "category": "calling",
        "is_secret": False,
        "env": "TWILIO_FROM_NUMBER",
        "description": "Your Twilio phone number (E.164 format, e.g. +12125551234).",
    },
    "operator_number": {
        "label": "Default Operator Number",
        "category": "calling",
        "is_secret": False,
        "env": "",
        "description": "Default phone number for the acquisitions operator (E.164). Used if not prompted per-call.",
    },
    # ── Skip Tracing ──────────────────────────────────────────────
    "skiptrace_provider": {
        "label": "Skip Trace Provider",
        "category": "skiptrace",
        "is_secret": False,
        "env": "SKIPTRACE_PROVIDER",
        "description": "Provider: 'mock' (dev), or your skip-trace service name.",
    },
    "skiptrace_api_key": {
        "label": "Skip Trace API Key",
        "category": "skiptrace",
        "is_secret": True,
        "env": "SKIPTRACE_API_KEY",
        "description": "API key for your skip-trace provider.",
    },
    # ── Comps ──────────────────────────────────────────────────────
    "comps_provider": {
        "label": "Comps Provider",
        "category": "comps",
        "is_secret": False,
        "env": "COMPS_PROVIDER",
        "description": "Provider: 'mock' (dev), or your comps API service name.",
    },
    "comps_api_key": {
        "label": "Comps API Key",
        "category": "comps",
        "is_secret": True,
        "env": "COMPS_API_KEY",
        "description": "API key for your comps/valuation provider.",
    },
    # ── Call Follow-up Cadence ────────────────────────────────────
    "cadence_no_answer": {
        "label": "No Answer — follow-up in (days)",
        "category": "cadence",
        "is_secret": False,
        "env": "",
        "description": "Days before next call attempt after no answer.",
    },
    "cadence_voicemail": {
        "label": "Voicemail — follow-up in (days)",
        "category": "cadence",
        "is_secret": False,
        "env": "",
        "description": "Days before next call attempt after leaving voicemail.",
    },
    "cadence_not_interested": {
        "label": "Not Interested — follow-up in (days)",
        "category": "cadence",
        "is_secret": False,
        "env": "",
        "description": "Days before re-contacting (long cool-down).",
    },
    "cadence_callback_requested": {
        "label": "Callback Requested — follow-up in (days)",
        "category": "cadence",
        "is_secret": False,
        "env": "",
        "description": "Days before calling back when they requested a callback.",
    },
    "cadence_warm": {
        "label": "Warm Lead — follow-up in (days)",
        "category": "cadence",
        "is_secret": False,
        "env": "",
        "description": "Days before following up on a warm lead.",
    },
    "cadence_offer_sent": {
        "label": "Offer Sent — follow-up in (days)",
        "category": "cadence",
        "is_secret": False,
        "env": "",
        "description": "Days before following up after an offer is sent.",
    },
    "cadence_dnc_request": {
        "label": "DNC Request — follow-up (days, 0=no follow-up)",
        "category": "cadence",
        "is_secret": False,
        "env": "",
        "description": "Days for DNC-request follow-up (0 = no follow-up scheduled).",
    },
    "cadence_default": {
        "label": "Default — follow-up in (days)",
        "category": "cadence",
        "is_secret": False,
        "env": "",
        "description": "Fallback follow-up days for unrecognized outcomes.",
    },
}


def _is_secret_key(key: str) -> bool:
    """Check if a key should be treated as a secret."""
    # Check explicit definition first
    definition = SETTING_DEFS.get(key)
    if definition:
        return definition.get("is_secret", False)
    # Fall back to suffix detection
    return any(key.endswith(suffix) for suffix in _SECRET_SUFFIXES)


def get_setting(db: Session, key: str) -> Optional[str]:
    """Get a setting value, falling back to env var if not in DB."""
    row = db.get(Setting, key)
    if row is not None and row.value is not None:
        return row.value
    # Fall back to env var
    definition = SETTING_DEFS.get(key)
    if definition and definition.get("env"):
        return os.getenv(definition["env"], "")
    return None


def get_setting_int(db: Session, key: str, default: int) -> int:
    """Get a setting as int, with default."""
    val = get_setting(db, key)
    if val is None or val == "":
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def set_setting(db: Session, key: str, value: str) -> Setting:
    """Set a setting value, creating or updating the row."""
    row = db.get(Setting, key)
    if row is None:
        row = Setting(
            key=key,
            value=value,
            is_secret=_is_secret_key(key),
            category=SETTING_DEFS.get(key, {}).get("category", "general"),
            label=SETTING_DEFS.get(key, {}).get("label", key),
            description=SETTING_DEFS.get(key, {}).get("description"),
        )
    else:
        row.value = value
    db.add(row)
    db.flush()
    return row


def get_all_settings(db: Session) -> list[dict]:
    """Get all known settings, masked for API output."""
    results: list[dict] = []
    for key, definition in SETTING_DEFS.items():
        row = db.get(Setting, key)
        if row is not None and row.value is not None:
            raw_value: Optional[str] = row.value
        elif definition.get("env"):
            raw_value = os.getenv(definition["env"], "")
        else:
            raw_value = ""
        is_secret = definition.get("is_secret", False)
        masked_value = _mask_value(raw_value) if is_secret else raw_value
        results.append({
            "key": key,
            "label": definition.get("label", key),
            "category": definition.get("category", "general"),
            "value": masked_value,
            "is_set": bool(raw_value),
            "is_secret": is_secret,
            "description": definition.get("description", ""),
        })
    return results


def _mask_value(value: Optional[str]) -> str:
    """Mask a secret value for display, showing only the last 4 chars."""
    if not value:
        return ""
    if len(value) <= 4:
        return "••••"
    return "•" * (len(value) - 4) + value[-4:]


def get_twilio_config(db: Session) -> dict:
    """Get Twilio configuration for the calling service."""
    return {
        "account_sid": get_setting(db, "twilio_account_sid") or "",
        "auth_token": get_setting(db, "twilio_auth_token") or "",
        "from_number": get_setting(db, "twilio_from_number") or "",
    }


def get_call_cadence(db: Session) -> dict[str, int]:
    """Get the call follow-up cadence (days per outcome)."""
    return {
        "no_answer": get_setting_int(db, "cadence_no_answer", DEFAULT_CADENCE["cadence_no_answer"]),
        "voicemail": get_setting_int(db, "cadence_voicemail", DEFAULT_CADENCE["cadence_voicemail"]),
        "not_interested": get_setting_int(db, "cadence_not_interested", DEFAULT_CADENCE["cadence_not_interested"]),
        "callback_requested": get_setting_int(db, "cadence_callback_requested", DEFAULT_CADENCE["cadence_callback_requested"]),
        "warm": get_setting_int(db, "cadence_warm", DEFAULT_CADENCE["cadence_warm"]),
        "offer_sent": get_setting_int(db, "cadence_offer_sent", DEFAULT_CADENCE["cadence_offer_sent"]),
        "dnc_request": get_setting_int(db, "cadence_dnc_request", DEFAULT_CADENCE["cadence_dnc_request"]),
        "default": get_setting_int(db, "cadence_default", DEFAULT_CADENCE["cadence_default"]),
    }
