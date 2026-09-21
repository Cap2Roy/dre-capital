"""End-to-end test for the property scraper.

Tests:
  1. HTML table parser extracts property data from raw HTML
  2. Live HTTP fetch against a local fixture server → parse → structured records
  3. Scrape job creates ScrapeResult entries with property data
  4. Import scrape results creates real Lead + SourceList entries
  5. LLM analysis function works (with heuristic fallback when no API key)

Run: python3 tests/test_scraper_e2e.py
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import sys
import threading
from pathlib import Path

# Ensure the app is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── Test 1: HTML table parser ──────────────────────────────────────────────────

def test_html_parser():
    """Verify the HTML table parser extracts property records from raw HTML."""
    from app.services.scraper import find_property_data_in_html

    fixture = Path(__file__).parent / "fixtures" / "property_search_results.html"
    html = fixture.read_text()
    records = find_property_data_in_html(html)

    assert len(records) == 5, f"Expected 5 records, got {len(records)}: {records}"
    first = records[0]
    assert "123 Main St" in first.get("property_address", ""), f"Bad address: {first}"
    assert "John Smith" in first.get("owner_name", ""), f"Bad owner: {first}"
    assert "Dallas" in first.get("property_city", ""), f"Bad city: {first}"
    assert "75201" in first.get("property_zip", ""), f"Bad zip: {first}"
    assert "185000" in first.get("assessed_value", ""), f"Bad value: {first}"
    print(f"  ✅ HTML parser extracted {len(records)} property records")
    print(f"     First: {first}")
    return records


# ── Test 2: Live HTTP fetch + parse (local fixture server) ────────────────────

def start_local_server(html_content: str) -> tuple[str, threading.Thread, http.server.HTTPServer]:
    """Start a local HTTP server serving the fixture HTML on a random port."""
    handler = type("FixtureHandler", (http.server.BaseHTTPRequestHandler,), {
        "do_GET": lambda self: (
            setattr(self, "protocol_version", "HTTP/1.0"),
            self.send_response(200),
            self.send_header("Content-Type", "text/html"),
            self.end_headers(),
            self.wfile.write(html_content.encode()),
        ),
        "log_message": lambda *args: None,  # suppress logs
    })
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{port}/", thread, server


def test_live_fetch_and_parse():
    """Verify the scraper fetches HTML via httpx and parses it into records."""
    from app.services.scraper import scrape_html, llm_parse_raw_data

    fixture = Path(__file__).parent / "fixtures" / "property_search_results.html"
    html = fixture.read_text()
    url, thread, server = start_local_server(html)

    try:
        # Fetch via the real httpx adapter
        raw = scrape_html(url, {}, None)
        assert "Property Search Results" in raw, "Fetch returned wrong content"
        print(f"  ✅ HTTP fetch: got {len(raw)} bytes from local server")

        # Parse the fetched data
        records = llm_parse_raw_data(raw)
        assert len(records) == 5, f"Expected 5 records, got {len(records)}"
        assert "123 Main St" in records[0].get("property_address", "")
        print(f"  ✅ Parsed {len(records)} property records from fetched HTML")
        print(f"     Sample: {records[0].get('property_address')} — {records[0].get('owner_name')}")
    finally:
        server.shutdown()
    return records


# ── Test 3: Scrape job end-to-end (DB) ─────────────────────────────────────────

def test_scrape_job_e2e(records):
    """Create a source, run a job, verify ScrapeResult entries are created."""
    from app.database import SessionLocal, init_db, engine
    from app.models import ScrapeJob, ScrapeResult, ScrapeSource
    from app.services.scraper import run_scrape_job

    init_db()
    db = SessionLocal()

    fixture = Path(__file__).parent / "fixtures" / "property_search_results.html"
    html = fixture.read_text()
    url, thread, server = start_local_server(html)

    try:
        # Create a source pointing at local server
        source = ScrapeSource(
            name="Test Local Source",
            url=url,
            source_type="html",
            state="TX",
            county="Dallas",
        )
        db.add(source)
        db.commit()
        db.refresh(source)

        # Create and run a job
        job = ScrapeJob(source_id=source.id)
        db.add(job)
        db.commit()
        db.refresh(job)

        run_scrape_job(db, job)
        db.refresh(job)

        assert job.status.value == "completed", f"Job failed: {job.error_message}"
        assert job.total_found == 5, f"Expected 5, got {job.total_found}"
        print(f"  ✅ Scrape job completed: found {job.total_found} properties")

        # Check results
        results = db.query(ScrapeResult).filter(ScrapeResult.job_id == job.id).all()
        assert len(results) == 5, f"Expected 5 results, got {len(results)}"
        first_data = json.loads(results[0].property_data)
        assert "123 Main St" in first_data.get("property_address", "")
        print(f"  ✅ {len(results)} ScrapeResult entries created with property data")

        # Check LLM analysis (heuristic fallback)
        if results[0].llm_analysis:
            print(f"     LLM analysis sample: {results[0].llm_analysis[:80]}...")

        # Check LLM summary
        if job.llm_summary:
            print(f"     LLM summary: {job.llm_summary[:80]}...")

        return db, source, job, results
    finally:
        server.shutdown()


# ── Test 4: Import scrape results into leads ───────────────────────────────────

def test_import_results(db, source, job, results):
    """Import scrape results as leads and verify they enter the pipeline."""
    from app.models import Lead, LeadListMembership, SourceList
    from app.services.scraper import import_scrape_results

    # Count leads before
    before = db.query(Lead).count()

    result_ids = [r.id for r in results]
    import_result = import_scrape_results(
        db,
        result_ids=result_ids,
        list_type="tax_delinquent",
        list_name="Test Scrape Import",
    )

    assert import_result["imported"] == 5, f"Expected 5 imported, got {import_result['imported']}"
    print(f"  ✅ Imported {import_result['imported']} leads, skipped {import_result['skipped']}")

    # Verify leads were created
    after = db.query(Lead).count()
    assert after >= before + 5, f"Lead count didn't increase: {before} → {after}"
    print(f"  ✅ Lead count: {before} → {after} (+{after - before})")

    # Verify source list
    sl = db.query(SourceList).filter(SourceList.name == "Test Scrape Import").first()
    assert sl is not None, "SourceList not created"
    assert sl.record_count == 5, f"Expected 5 records, got {sl.record_count}"
    print(f"  ✅ SourceList '{sl.name}' created with {sl.record_count} records")

    # Verify memberships
    memberships = db.query(LeadListMembership).filter(
        LeadListMembership.source_list_id == sl.id
    ).all()
    assert len(memberships) == 5, f"Expected 5 memberships, got {len(memberships)}"
    print(f"  ✅ {len(memberships)} LeadListMembership entries created")

    # Verify the first lead has correct data
    first_lead = db.query(Lead).filter(Lead.property_address.like("%123 Main St%")).first()
    assert first_lead is not None, "First lead not found"
    assert first_lead.owner_name == "John Smith", f"Bad owner: {first_lead.owner_name}"
    assert first_lead.property_city == "Dallas", f"Bad city: {first_lead.property_city}"
    assert first_lead.beds == 3, f"Bad beds: {first_lead.beds}"
    assert first_lead.assessed_value == 185000, f"Bad value: {first_lead.assessed_value}"
    print(f"  ✅ Lead data verified: {first_lead.property_address}, {first_lead.owner_name}")
    print(f"     beds={first_lead.beds}, sqft={first_lead.sqft}, value={first_lead.assessed_value}")


# ── Test 5: LLM analysis (heuristic fallback) ──────────────────────────────────

def test_llm_analysis():
    """Test LLM property analysis with heuristic fallback (no API key)."""
    from app.services.scraper import llm_analyze_property, llm_is_configured

    assert not llm_is_configured(), "LLM should not be configured in test env"

    # Test with tax delinquency signal
    data = {
        "property_address": "789 Elm St",
        "owner_name": "Mary J Smith",
        "taxes_owed": "12200",
        "mailing_address": "PO Box 123, Plano, TX 75074",
    }
    analysis = llm_analyze_property(data)
    assert "tax" in analysis.lower(), f"Expected tax signal in: {analysis}"
    assert "absentee" in analysis.lower(), f"Expected absentee signal in: {analysis}"
    print(f"  ✅ Heuristic analysis detected tax + absentee signals")
    print(f"     '{analysis}'")

    # Test with LLC signal
    data2 = {"property_address": "456 Oak Ave", "owner_name": "Smith Properties LLC", "owner_is_llc": True}
    analysis2 = llm_analyze_property(data2)
    assert "llc" in analysis2.lower(), f"Expected LLC signal in: {analysis2}"
    print(f"  ✅ Heuristic analysis detected LLC signal: '{analysis2}'")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("  DRE-Capital Property Scraper — End-to-End Test")
    print("=" * 70)
    print()

    print("Test 1: HTML table parser")
    records = test_html_parser()
    print()

    print("Test 2: Live HTTP fetch + parse")
    test_live_fetch_and_parse()
    print()

    print("Test 3: Scrape job end-to-end (DB)")
    db, source, job, results = test_scrape_job_e2e(records)
    print()

    print("Test 4: Import scrape results into leads")
    test_import_results(db, source, job, results)
    print()

    print("Test 5: LLM analysis (heuristic fallback)")
    test_llm_analysis()
    print()

    # Cleanup
    from app.models import ScrapeJob, ScrapeResult, ScrapeSource
    db.query(ScrapeResult).filter(ScrapeResult.job_id == job.id).delete()
    db.query(ScrapeJob).filter(ScrapeJob.id == job.id).delete()
    db.query(ScrapeSource).filter(ScrapeSource.id == source.id).delete()
    db.commit()
    db.close()

    print("=" * 70)
    print("  ✅ ALL TESTS PASSED — scraper is fully functional")
    print("=" * 70)


if __name__ == "__main__":
    main()
