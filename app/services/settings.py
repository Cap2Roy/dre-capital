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
    "twilio_sms_number": {
        "label": "Twilio SMS Number",
        "category": "calling",
        "is_secret": False,
        "env": "TWILIO_SMS_NUMBER",
        "description": "Optional dedicated SMS number (E.164). If unset, the From Number above is used.",
    },
    "operator_number": {
        "label": "Default Operator Number",
        "category": "calling",
        "is_secret": False,
        "env": "",
        "description": "Default phone number for the acquisitions operator (E.164). Used if not prompted per-call.",
    },
    # ── Auto-scrape ────────────────────────────────────────────────
    "auto_scrape_enabled": {
        "label": "Daily Auto-Scrape Enabled",
        "category": "scraper",
        "is_secret": False,
        "env": "AUTO_SCRAPE_ENABLED",
        "description": "If '1'/'true', the in-process scheduler scrapes every active source flagged auto_scrape once a day.",
    },
    "auto_scrape_hour": {
        "label": "Auto-Scrape Hour (0-23)",
        "category": "scraper",
        "is_secret": False,
        "env": "AUTO_SCRAPE_HOUR",
        "description": "Local hour (0-23) to run the daily auto-scrape. Default 6 (6 AM).",
    },
    # ── Area focus ───────────────────────────────────────────────────
    "focus_state": {
        "label": "Primary Focus State",
        "category": "general",
        "is_secret": False,
        "env": "FOCUS_STATE",
        "description": "Two-letter state the dialer/queue defaults to (e.g. MI). Leave blank for no default.",
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
    # ── Company / Email delivery ─────────────────────────────────
    "company_name": {
        "label": "Company Name",
        "category": "general",
        "is_secret": False,
        "env": "COMPANY_NAME",
        "description": "Legal entity name shown on contracts and emails (e.g. Direct Real Estate Capital LLC).",
    },
    "smtp_host": {
        "label": "SMTP Host",
        "category": "email",
        "is_secret": False,
        "env": "SMTP_HOST",
        "description": "SMTP server for sending contract/email templates. Leave empty to disable email delivery.",
    },
    "smtp_port": {
        "label": "SMTP Port",
        "category": "email",
        "is_secret": False,
        "env": "SMTP_PORT",
        "description": "SMTP port (default 587).",
    },
    "smtp_user": {
        "label": "SMTP Username",
        "category": "email",
        "is_secret": True,
        "env": "SMTP_USER",
        "description": "SMTP username / API key for the configured provider.",
    },
    "smtp_password": {
        "label": "SMTP Password",
        "category": "email",
        "is_secret": True,
        "env": "SMTP_PASSWORD",
        "description": "SMTP password / secret for the configured provider.",
    },
    "smtp_from": {
        "label": "SMTP From Address",
        "category": "email",
        "is_secret": False,
        "env": "SMTP_FROM",
        "description": "Sender email address for outbound email (contracts, templates).",
    },
    # ── LLM / Scraper ──────────────────────────────────────────────
    "llm_api_key": {
        "label": "LLM API Key",
        "category": "llm",
        "is_secret": True,
        "env": "LLM_API_KEY",
        "description": "API key for the LLM used to parse and analyze scraped property data (OpenAI-compatible).",
    },
    "llm_base_url": {
        "label": "LLM Base URL",
        "category": "llm",
        "is_secret": False,
        "env": "LLM_BASE_URL",
        "description": "Base URL for the LLM API (default: https://api.openai.com/v1).",
    },
    "llm_model": {
        "label": "LLM Model",
        "category": "llm",
        "is_secret": False,
        "env": "LLM_MODEL",
        "description": "Model name for the LLM (default: gpt-4o-mini).",
    },
    "rentcast_api_key": {
        "label": "RentCast API Key",
        "category": "llm",
        "is_secret": True,
        "env": "RENTCAST_API_KEY",
        "description": "API key for RentCast property data API (free tier: 50 calls/mo). Get one at https://app.rentcast.io/api",
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
            # NOTE: Setting model has no `description` column — omitted.

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
    """Mask a secret for API output.

    Returns a fixed all-bullets sentinel for any non-empty value.  This keeps the
    router's keep-current guard (``set(value) == {"\u2022"}``) working: a client
    that echoes the masked value back on PUT is treated as "keep current" instead
    of having the mask written over the real secret.  Empty/unset returns ''.
    """
    if not value:
        return ""
    return "••••"


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
