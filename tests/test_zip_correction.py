"""Census ZIP correction: only on an exact street match, and re-imports with the old ZIP don't duplicate.

Run: DATABASE_URL=sqlite:////tmp/zipfix.db .venv/bin/python tests/test_zip_correction.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/zipfix.db")

from app.database import Base, SessionLocal, engine  # noqa: E402
from app import models  # noqa: E402,F401
from app.models import Lead  # noqa: E402
from app.services import property_scan as ps  # noqa: E402
from app.services.property_data import census_zip_correction  # noqa: E402
from app.services.stacking import get_or_create_lead  # noqa: E402

fails = 0


def check(name: str, ok: bool) -> None:
    global fails
    print(("  ✓ " if ok else "  ✗ ") + name)
    fails += not ok


def main() -> int:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()

    def lead(addr: str, city: str, st: str, zip_: str) -> Lead:
        l, _ = get_or_create_lead(db, property_address=addr, property_city=city, property_state=st, property_zip=zip_)
        db.flush()
        return l

    # Exact street (suffix spelled out on our side): corrected, old ZIP kept.
    a = lead("1385 Peabody Street NW", "Washington", "DC", "20002")
    got = census_zip_correction(a, ps._target(a), {"zip": "20011", "matched_address": "1385 PEABODY ST NW, WASHINGTON, DC, 20011"})
    check("exact street → ZIP corrected", a.property_zip == "20011" and a.zip_corrected_from == "20002"
          and got and got["corrected_from"] == "20002")

    # Different directional (NW vs NE) is a different street: untouched.
    b = lead("1385 Peabody Street NE", "Washington", "DC", "20002")
    check("different directional → not corrected",
          census_zip_correction(b, ps._target(b), {"zip": "20011", "matched_address": "1385 PEABODY ST NW, WASHINGTON, DC, 20011"}) is None
          and b.property_zip == "20002")

    # Different suffix (Dr vs Ave) — the geocoder's fuzzy match: untouched.
    c = lead("125 Janie Bird Dr", "Moscow", "TN", "38057")
    check("different suffix → not corrected",
          census_zip_correction(c, ps._target(c), {"zip": "38076", "matched_address": "125 JANIE BIRD AVE, MOSCOW, TN, 38076"}) is None
          and c.property_zip == "38057")

    # A second correction keeps the ZIP the source originally gave.
    census_zip_correction(a, ps._target(a), {"zip": "20010", "matched_address": "1385 PEABODY ST NW, WASHINGTON, DC, 20010"})
    check("second correction keeps the source's ZIP", a.property_zip == "20010" and a.zip_corrected_from == "20002")
    a.property_zip = "20011"
    db.flush()

    # The daily scrape re-imports the property with the source's (wrong) ZIP: same row, ZIP stays corrected.
    again, created = get_or_create_lead(db, property_address="1385 Peabody Street NW", property_city="Washington",
                                        property_state="DC", property_zip="20002")
    check("re-import with old ZIP finds the property (no duplicate)", not created and again.id == a.id and again.property_zip == "20011")
    again2, created2 = get_or_create_lead(db, property_address="1385 PEABODY ST NW", property_state="DC", property_zip="20002")
    check("re-import with old ZIP + different spelling finds it", not created2 and again2.id == a.id)
    db.close()
    print("  ✅ ALL TESTS PASSED" if not fails else f"  ❌ {fails} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
