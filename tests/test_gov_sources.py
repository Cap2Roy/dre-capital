"""Parser tests for the government sale sources (app/services/gov_sources.py).

Fixtures in tests/fixtures/gov/ are trimmed copies of the real pages (Oct 2026). Each test
pins a parsing bug found against the live sites:
  - Treasury splits "ONLINE AUCTION DATE" across <b>/<strong> tags; broker sales have no page link
  - IRS notices sometimes leave the taxpayer blank in the "right, title, and interest of" boilerplate
  - USDA detail pages leave fields blank ("Lot Size: Real Estate Taxes: $1,260") and put the
    government's opening bid after "Sale Time:"
  - Tarrant's main-content marker sits mid-tag; attorney headings precede each table

The DB test (registration + bulk import) only runs when DATABASE_URL points at a throwaway DB:
    DATABASE_URL=sqlite:////tmp/gov_test.db .venv/bin/python tests/test_gov_sources.py

Run: python3 tests/test_gov_sources.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIX = Path(__file__).parent / "fixtures" / "gov"


def _read(name: str) -> str:
    return (FIX / name).read_text(errors="replace")


def test_treasury_list():
    from app.services.gov_sources import TREASURY_URL, parse_treasury_list
    recs = parse_treasury_list(_read("treasury_list.html"), TREASURY_URL)
    assert [r["case_number"] for r in recs] == ["27-66-104", "27-66-805", "27-66-830"], recs
    big_sandy, houston, winter_park = recs
    assert big_sandy["property_address"] == "6527 State Hwy 155 South"
    assert (big_sandy["property_city"], big_sandy["property_state"], big_sandy["property_zip"]) == ("Big Sandy", "TX", "75755")
    assert big_sandy["sale_date"] == "2026-11-05" and big_sandy["sale_type"] == "Online auction"
    assert big_sandy["source_url"].endswith("/rp/6527bigsandy.shtml")
    assert (houston["beds"], houston["baths"], houston["sqft"]) == (3, 3.5, 4160)  # "3.1 baths" = 3 full + 1 half
    assert winter_park["sale_type"] == "Broker sale" and winter_park["sale_date"] is None
    assert winter_park["source_url"] == TREASURY_URL  # "coming soon" rows have no page of their own
    print(f"  ✅ Treasury list: {len(recs)} records, dates/sale #/links parsed")


def test_treasury_detail():
    from app.services.gov_sources import parse_treasury_detail
    d = parse_treasury_detail(_read("treasury_detail.html"))
    assert d["minimum_bid"] == 176000.0 and d["deposit"] == 25000.0, d
    assert d["sale_date"] == "2026-11-06" and d["sale_time"].startswith("10:00am"), d
    assert (d["sqft"], d["year_built"], d["county"]) == (4100, 2003, "Harris"), d
    assert d["annual_taxes"] == 9664.44 and d["parcel_number"] == "124-374-001-0021", d
    print("  ✅ Treasury detail: starting bid, deposit, size, year, county, taxes, parcel")


def test_irs_notice():
    from app.services.gov_sources import parse_irs_detail
    d = parse_irs_detail(_read("irs_notice.html"))
    assert d["owner_name"] == "Willie J. Sessions", d  # not the blank "interest of in and to" boilerplate
    assert (d["property_address"], d["property_city"], d["property_state"], d["property_zip"]) == (
        "340 Cedar Grove Dr.", "Henderson", "NC", "27537"), d
    assert d["minimum_bid"] == 81480.0 and d["sale_date"] == "2026-10-28", d
    assert d["has_legal_description"]
    print("  ✅ IRS notice: taxpayer, street address, minimum bid, auction date")


def test_usda_detail():
    from app.services.gov_sources import parse_usda_detail
    d = parse_usda_detail(_read("usda_detail.html"))
    assert d["minimum_bid"] == 84461.0, d  # "Govt Bid: $84,461" — not a sale time
    assert "sale_time" not in d and "sale_date" not in d, d  # blank on the page
    assert "lot_size" not in d, d  # "Lot Size:" is blank — must not swallow "Real Estate Taxes"
    assert d["annual_taxes"] == 1260.0 and d["property_type"] == "Ranch-Brick", d
    print("  ✅ USDA detail: government bid, blank fields stay blank")


def test_tarrant_sale_page():
    from app.services.gov_sources import parse_tarrant_sale
    rows, writs = parse_tarrant_sale(_read("tarrant_sale.html"))
    assert len(rows) == 35, len(rows)
    for_sale = [r for r in rows if r["status"] == "For Sale"]
    assert len(for_sale) == 13, len(for_sale)
    assert {r["attorney"] for r in rows} == {"Linebarger, Goggan, Blair & Sampson",
                                             "Perdue, Brandon, Fielder, Collins & Mott, L.L.P."}
    assert any(r["case_number"] == "D39369-23" and r["account_number"] == "01460412" for r in rows)  # 7-digit → 8
    assert [w["case_number"] for w in writs] == ["JP01-10-JS00021659", "001-01852-2026", "067-372397-25"], writs
    w = writs[0]
    assert (w["property_address"], w["property_city"], w["property_zip"]) == ("6009 Team Court", "Arlington", "76018")
    assert w["owner_name"] == "Karlson T. Bessman" and w["lender"] == "Pallida LLC, assignee of Pharia LLC"
    print(f"  ✅ Tarrant sale page: {len(rows)} table rows ({len(for_sale)} for sale), {len(writs)} writs")


def test_tad_record_zip_rule():
    from app.services.gov_sources import tad_record
    base = {"SITUS_ADDR": "821 VICKSBURG LN   ", "CITY": "BURLESON   ", "OWNER_NAME": "ROBERTS, RALPH J",
            "OWNER_ADDR": "821 VICKSBURG LN   ", "OWNER_CITY": "BURLESON, TX", "OWNER_ZIP": "76028",
            "ZIPCODE": "     ", "YEAR_BUILT": 1991.0, "LIVING_ARE": 1276.0, "APPRAISEDV": 263530.0}
    r = tad_record(base)
    assert (r["property_address"], r["property_city"], r["property_zip"]) == ("821 VICKSBURG LN", "Burleson", "76028"), r
    absentee = tad_record({**base, "OWNER_ADDR": "6547 KINGSBURY DR", "OWNER_CITY": "DALLAS, TX", "OWNER_ZIP": "75231"})
    assert absentee["property_zip"] is None and absentee["mailing_city"] == "DALLAS", absentee
    print("  ✅ TAD record: situs ZIP only taken from an owner-occupied mailing address")


def test_analysis_format():
    from app.services.gov_sources import analyze_record
    text = analyze_record({"list_type": "tax_delinquent", "owner_name": "GREENE, ETHEL B EST",
                           "property_address": "1810 MC CURDY ST", "mailing_address": "1810 MC CURDY ST",
                           "year_built": 1953, "agency": "Tarrant County Constable Precinct 3"})
    assert text.startswith("Signals: delinquent-tax foreclosure sale") and "estate (probate)" in text, text
    assert "Lead score: " in text and "/100. Recommend: " in text, text
    print(f"  ✅ Analysis: {text}")


def test_registration_and_bulk_import():
    if not os.environ.get("DATABASE_URL"):
        print("  ⏭  skipped — set DATABASE_URL to a throwaway DB to run")
        return
    from app.database import SessionLocal, init_db
    from app.models import Lead, ScrapeJob, ScrapeJobStatus, ScrapeResult, ScrapeSource
    from app.services import gov_sources
    from app.timeutil import utcnow

    init_db()
    db = SessionLocal()
    try:
        first = gov_sources.ensure_government_sources(db)
        again = gov_sources.ensure_government_sources(db)
        assert again == [], again
        srcs = db.query(ScrapeSource).filter(ScrapeSource.source_type.in_(list(gov_sources.ADAPTERS))).all()
        assert len(srcs) == len(gov_sources.DIRECTORY) and all(s.active and s.auto_scrape for s in srcs)
        print(f"  ✅ ensure_government_sources: created {len(first)}, second call created 0")

        irs = next(s for s in srcs if s.source_type == "gov_irs")
        job = ScrapeJob(source_id=irs.id, status=ScrapeJobStatus.COMPLETED, completed_at=utcnow())
        db.add(job)
        db.commit()
        rec = {**gov_sources.parse_irs_detail(_read("irs_notice.html")), "list_type": "tax_delinquent",
               "program": "IRS auction (Seized)", "source_url": "https://www.irsauctions.gov/ad/half-interest-house-acreage"}
        reo = {"property_address": "19563 Executive Drive", "property_city": "Corning", "property_state": "CA",
               "property_zip": "96021", "list_price": 297000, "list_type": "government_sale", "program": "USDA FSA REO"}
        for r in (rec, reo):
            db.add(ScrapeResult(job_id=job.id, property_data=json.dumps(r), llm_analysis=gov_sources.analyze_record(r)))
        db.commit()
        out = gov_sources.import_all_new(db, [irs.id])
        assert out["imported"] + out["skipped"] == 2, out
        assert sorted(x["list_type"] for x in out["sources"][0]["lists"]) == ["government_sale", "tax_delinquent"], out
        lead = db.query(Lead).filter(Lead.property_address.like("340 Cedar Grove%")).one()
        assert "Minimum bid $81,480.00" in (lead.notes or "") and "Sale date 2026-10-28 12:00 PM" in lead.notes, lead.notes
        again = gov_sources.import_all_new(db, [irs.id])
        assert again["imported"] == 0 and again["skipped"] == 0, again
        print(f"  ✅ import_all_new: {out['imported']} imported into per-list-type lists; note: {lead.notes.splitlines()[-1][:110]}…")
    finally:
        db.close()


def main():
    print("=" * 70)
    print("  Government sale sources — parser tests")
    print("=" * 70)
    for t in (test_treasury_list, test_treasury_detail, test_irs_notice, test_usda_detail,
              test_tarrant_sale_page, test_tad_record_zip_rule, test_analysis_format,
              test_registration_and_bulk_import):
        print(t.__name__)
        t()
    print("=" * 70)
    print("  ✅ ALL TESTS PASSED")
    print("=" * 70)


if __name__ == "__main__":
    main()
