"""Timezone rules for the whole app.

* Storage: every datetime written to the DB is **naive UTC** (SQLite's
  ``func.now()`` server defaults are UTC too, so both sources agree).
* API output: datetimes are serialized as ISO-8601 with an explicit ``Z``.
* API input: client datetimes are normalized with :func:`to_naive_utc`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def utcnow() -> datetime:
    """Current time as a naive UTC datetime (the storage convention)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Aware → converted to UTC with tzinfo dropped; naive → assumed UTC, unchanged."""
    if dt is None or dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """ISO-8601 string with an explicit UTC ``Z`` (naive input is assumed UTC)."""
    if dt is None:
        return None
    return to_naive_utc(dt).isoformat() + "Z"
