"""Property scraper service.

Finds properties from public data sources — county tax/property appraiser
sites, public records APIs, and HTML pages.  Uses an LLM to parse unstructured
scraped content into structured property + owner records.

Source types:
  - county_tax:  County property appraiser / tax collector sites (HTML scraping)
  - public_api:  REST APIs that return JSON property data
  - html:        Generic HTML page with property listings

The LLM is used to:
  1. Parse raw HTML/JSON into structured property records
  2. Analyze each property for motivated-seller signals
  3. Summarize the scrape results for the operator
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models import (
    Lead,
    ListType,
    ScrapeJob,
    ScrapeJobStatus,
    ScrapeResult,
    ScrapeSource,
    SourceList,
)
from app.services.stacking import (
    add_lead_to_list,
    get_or_create_lead,
    recompute_stack_depths,
)

# ── HTML parsing helpers (no external dependency) ─────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_TABLE_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_TABLE_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.DOTALL | re.IGNORECASE)
_LINK_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)


def strip_html(html: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    text = _TAG_RE.sub(" ", html)
    text = _WS_RE.sub(" ", text).strip()
    return text


def parse_html_tables(html: str) -> list[list[list[str]]]:
    """Parse all <table> elements into lists of rows of cell text.

    Lightweight parser — no BeautifulSoup dependency.  Handles nested
    tags inside cells by stripping them to text.
    """
    tables: list[list[list[str]]] = []
    for table_match in re.finditer(r"<table[^>]*>(.*?)</table>", html, re.DOTALL | re.IGNORECASE):
        table_html = table_match.group(1)
        rows: list[list[str]] = []
        for row_match in _TABLE_ROW_RE.finditer(table_html):
            row_html = row_match.group(1)
            cells = [strip_html(c) for c in _TABLE_CELL_RE.findall(row_html)]
            if any(c.strip() for c in cells):
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def find_property_data_in_html(html: str) -> list[dict[str, Any]]:
    """Extract property-like records from HTML tables.

    Looks for tables with columns that match property fields (address, owner,
    value, beds, baths, sqft, year built).  Returns a list of dicts.
    """
    # Order matters: more specific patterns first to avoid mis-matching
    # (e.g. "Delinquent Tax" must match taxes_owed, not assessed_value)
    FIELD_PATTERNS = [
        ("taxes_owed", re.compile(r"delinquent|tax_owed|taxes_owed|taxes_delinquent|tax_due|balance_owed|balance", re.I)),
        ("assessed_value", re.compile(r"assessed|appraised|apprais|^value\b|market_value", re.I)),
        ("property_address", re.compile(r"address|site_address|property_address|^\s*property\s*$", re.I)),
        ("owner_name", re.compile(r"owner|grantee|owner_of_record|owner_name", re.I)),
        ("property_city", re.compile(r"city|property_city|prop_city", re.I)),
        ("property_state", re.compile(r"state|property_state|prop_state", re.I)),
        ("property_zip", re.compile(r"zip|postal", re.I)),
        ("beds", re.compile(r"bed|bedrooms|br\b", re.I)),
        ("baths", re.compile(r"bath|bathrooms|ba\b", re.I)),
        ("sqft", re.compile(r"sqft|sq\.?\s*ft|square|gla|living", re.I)),
        ("year_built", re.compile(r"year_built|yr_built|yearbuilt|year\b", re.I)),
    ]

    results: list[dict[str, Any]] = []
    for table in parse_html_tables(html):
        if not table or len(table) < 2:
            continue
        header = table[0]
        col_map: dict[int, str] = {}
        for i, h in enumerate(header):
            for field, pat in FIELD_PATTERNS:
                if pat.search(h):
                    col_map[i] = field
                    break
        if "property_address" not in col_map.values():
            continue
        for row in table[1:]:
            record: dict[str, Any] = {}
            for i, val in enumerate(row):
                if i in col_map:
                    record[col_map[i]] = val
            if record.get("property_address"):
                results.append(record)
    return results


# ── LLM integration ────────────────────────────────────────────────────────────

LLM_SYSTEM_PROMPT = """You are a real estate data extraction assistant for DRE-Capital,
a wholesale real estate acquisitions firm.  You analyze raw property data from public sources
and extract structured information about properties and their owners.

For each property you find, extract:
- property_address: street address
- property_city, property_state, property_zip
- owner_name: owner of record (person or LLC name)
- owner_is_llc: true if owner appears to be an LLC/entity
- mailing_address, mailing_city, mailing_state, mailing_zip (if different from property)
- beds, baths, sqft, year_built (if available)
- assessed_value (if available)
- taxes_owed (if available)
- motivation_signals: brief notes on motivated-seller indicators (tax delinquency, code violations, etc.)

Return a JSON array of property objects.  Only include properties you can identify with
an address.  If the data is unstructured text, parse it intelligently."""

LLM_ANALYSIS_PROMPT = """You are a real estate acquisitions analyst for DRE-Capital.
Analyze this property and provide a brief assessment (2-3 sentences):

1. Motivated-seller signals (tax delinquency, distressed condition, absentee owner, etc.)
2. Deal potential (equity spread, ARV vs assessed value, repair needs)
3. Recommended action (call immediately, mail, research further, skip)

Keep it concise and actionable."""


def get_llm_config() -> dict[str, str]:
    """Get LLM API configuration from env vars."""
    return {
        "api_key": os.getenv("LLM_API_KEY", ""),
        "base_url": os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
        "model": os.getenv("LLM_MODEL", "gpt-4o-mini"),
    }


def llm_is_configured() -> bool:
    """Check if LLM is configured."""
    c = get_llm_config()
    return bool(c["api_key"])


def _call_llm(system: str, user: str, timeout: int = 30) -> str:
    """Call the LLM API (OpenAI-compatible). Returns text response.

    Uses OpenAI-compatible chat completions endpoint.  Works with OpenAI,
    Azure OpenAI, Ollama, LM Studio, or any compatible provider.
    """
    cfg = get_llm_config()
    if not cfg["api_key"]:
        raise RuntimeError("LLM_API_KEY not configured")

    with httpx.Client(timeout=timeout) as client:
        resp = client.post(
            f"{cfg['base_url']}/chat/completions",
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
            },
            json={
                "model": cfg["model"],
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.3,
                "max_tokens": 2000,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


def llm_parse_raw_data(raw_text: str) -> list[dict[str, Any]]:
    """Use the LLM to parse raw scraped text/HTML into structured property records.

    Falls back to HTML table parser if LLM is not configured.
    """
    # Try HTML table parser first — it's free and instant
    html_results = find_property_data_in_html(raw_text)
    if html_results:
        return html_results

    # If no tables found and LLM is configured, use LLM to parse
    if llm_is_configured():
        try:
            # Truncate to avoid token limits
            truncated = raw_text[:8000]
            response = _call_llm(
                LLM_SYSTEM_PROMPT,
                f"Extract all property records from this data. Return ONLY a JSON array, "
                f"no other text:\n\n{truncated}",
            )
            # Extract JSON array from response
            response = response.strip()
            if response.startswith("```"):
                response = re.sub(r"^```(?:json)?\s*", "", response)
                response = re.sub(r"\s*```$", "", response)
            data = json.loads(response)
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, RuntimeError, httpx.HTTPError):
            pass

    # Last resort: try to find JSON in the raw text
    try:
        data = json.loads(raw_text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "results" in data:
            return data["results"]
        if isinstance(data, dict) and "properties" in data:
            return data["properties"]
    except (json.JSONDecodeError, TypeError):
        pass

    return []


def llm_analyze_property(property_data: dict[str, Any]) -> str:
    """Use the LLM to analyze a single property for motivated-seller signals."""
    if not llm_is_configured():
        # Provide a basic heuristic analysis without LLM
        signals: list[str] = []
        if property_data.get("taxes_owed") and float(property_data.get("taxes_owed", 0)) > 0:
            signals.append("tax delinquency")
        if property_data.get("owner_is_llc"):
            signals.append("LLC-owned (possible investor, ask if they want to sell)")
        addr = property_data.get("mailing_address", "")
        prop_addr = property_data.get("property_address", "")
        if addr and addr != prop_addr:
            signals.append("absentee owner")
        if not signals:
            return "No strong motivated-seller signals detected. Research further."
        return f"Signals: {', '.join(signals)}. Recommend: call to qualify."

    try:
        prop_json = json.dumps(property_data, indent=2)
        return _call_llm(LLM_ANALYSIS_PROMPT, f"Property data:\n{prop_json}")
    except (RuntimeError, httpx.HTTPError):
        return "LLM analysis unavailable. Review manually."


def llm_summarize_scrape(results: list[dict[str, Any]]) -> str:
    """Generate a summary of the scrape results using the LLM."""
    if not results:
        return "No properties found."

    if not llm_is_configured():
        return f"Found {len(results)} properties. Review and import the ones that fit your criteria."

    try:
        # Summarize top properties
        top = results[:20]
        summary_data = json.dumps(top, indent=2)
        return _call_llm(
            "You are a real estate acquisitions analyst. Summarize the key findings from "
            "this batch of scraped properties in 3-4 sentences. Highlight the best opportunities.",
            f"Scrape results ({len(results)} total, showing {len(top)}):\n{summary_data}",
        )
    except (RuntimeError, httpx.HTTPError):
        return f"Found {len(results)} properties. LLM summary unavailable."


# ── Source adapters ───────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": "DRE-Capital-Property-Research/1.0 (real estate acquisitions; contact: ops@dre-capital.com)",
    "Accept": "text/html,application/json,*/*",
}


def scrape_county_tax(url: str, search_params: dict[str, Any], api_key: str | None = None) -> str:
    """Scrape a county tax/property appraiser site.

    Many county sites have search forms that return HTML tables of results.
    We fetch the search results page and return the raw HTML for parsing.
    """
    params = {k: v for k, v in search_params.items() if v}
    with httpx.Client(timeout=30, follow_redirects=True, headers=HEADERS) as client:
        # Try POST first (many county search forms use POST)
        try:
            resp = client.post(url, data=params)
        except httpx.HTTPError:
            resp = client.get(url, params=params)
        resp.raise_for_status()
        return resp.text


def scrape_public_api(url: str, search_params: dict[str, Any], api_key: str | None = None) -> str:
    """Fetch data from a public records API that returns JSON."""
    headers = {**HEADERS}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    params = {k: v for k, v in search_params.items() if v}
    with httpx.Client(timeout=30, follow_redirects=True, headers=headers) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        return resp.text


def scrape_html(url: str, search_params: dict[str, Any], api_key: str | None = None) -> str:
    """Scrape a generic HTML page with property listings."""
    with httpx.Client(timeout=30, follow_redirects=True, headers=HEADERS) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.text


SOURCE_ADAPTERS = {
    "county_tax": scrape_county_tax,
    "public_api": scrape_public_api,
    "html": scrape_html,
}


# ── Job execution ──────────────────────────────────────────────────────────────

def run_scrape_job(db: Session, job: ScrapeJob) -> None:
    """Execute a scrape job: fetch data, parse with LLM, store results.

    This is a synchronous function — called from the API endpoint in a
    background thread or inline.  Updates the job in-place.
    """
    source = job.source
    job.status = ScrapeJobStatus.RUNNING
    job.started_at = datetime.utcnow()
    db.commit()

    try:
        # Parse search params
        params: dict[str, Any] = {}
        if source.search_params:
            try:
                params.update(json.loads(source.search_params))
            except json.JSONDecodeError:
                pass
        if job.search_params:
            try:
                params.update(json.loads(job.search_params))
            except json.JSONDecodeError:
                pass

        adapter = SOURCE_ADAPTERS.get(source.source_type, scrape_html)
        raw_text = adapter(source.url, params, source.api_key)

        # Parse raw data into structured records using LLM or HTML parser
        records = llm_parse_raw_data(raw_text)

        # Store raw results
        job.raw_results = json.dumps(records[:200])  # cap at 200 to avoid huge DB rows
        job.total_found = len(records)

        # Create ScrapeResult entries with LLM analysis
        for record in records[:200]:
            analysis = llm_analyze_property(record)
            result = ScrapeResult(
                job_id=job.id,
                property_data=json.dumps(record),
                llm_analysis=analysis,
            )
            db.add(result)

        # Generate LLM summary
        job.llm_summary = llm_summarize_scrape(records)

        job.status = ScrapeJobStatus.COMPLETED
        job.completed_at = datetime.utcnow()

    except Exception as exc:
        job.status = ScrapeJobStatus.FAILED
        job.error_message = str(exc)[:2000]
        job.completed_at = datetime.utcnow()

    db.commit()


def import_scrape_results(
    db: Session,
    result_ids: list[str],
    list_type: str,
    list_name: str | None = None,
) -> dict[str, Any]:
    """Import selected scrape results as leads into a new source list.

    Creates leads (deduped by address), adds them to a new SourceList,
    and recomputes stack depths.
    """
    try:
        lt = ListType(list_type)
    except ValueError:
        raise ValueError(f"Invalid list_type. Valid: {[e.value for e in ListType]}")

    results = db.query(ScrapeResult).filter(ScrapeResult.id.in_(result_ids)).all()
    if not results:
        raise ValueError("No results found to import.")

    # Create or reuse source list
    name = list_name or f"Scrape Import {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"
    source_list = SourceList(
        name=name,
        list_type=lt,
        county=None,
        state=None,
    )
    db.add(source_list)
    db.flush()

    added = 0
    skipped = 0
    affected_lead_ids: set[str] = set()

    for result in results:
        try:
            data = json.loads(result.property_data)
        except json.JSONDecodeError:
            skipped += 1
            continue

        addr = data.get("property_address", "").strip()
        if not addr:
            skipped += 1
            continue

        def _clean(val: Any) -> str | None:
            if val is None:
                return None
            s = str(val).strip()
            return s or None

        def _to_int(val: Any) -> int | None:
            if val is None:
                return None
            try:
                return int(float(str(val).replace(",", "").strip()))
            except (ValueError, TypeError):
                return None

        def _to_float(val: Any) -> float | None:
            if val is None:
                return None
            try:
                return float(str(val).replace(",", "").replace("$", "").strip())
            except (ValueError, TypeError):
                return None

        lead, created = get_or_create_lead(
            db,
            property_address=addr,
            property_zip=_clean(data.get("property_zip")),
            property_city=_clean(data.get("property_city")),
            property_state=_clean(data.get("property_state")),
            owner_name=_clean(data.get("owner_name")),
            mailing_address=_clean(data.get("mailing_address")),
            mailing_city=_clean(data.get("mailing_city")),
            mailing_state=_clean(data.get("mailing_state")),
            mailing_zip=_clean(data.get("mailing_zip")),
            beds=_to_int(data.get("beds")),
            baths=_to_float(data.get("baths")),
            sqft=_to_int(data.get("sqft")),
            year_built=_to_int(data.get("year_built")),
            assessed_value=_to_float(data.get("assessed_value")),
            taxes_owed=_to_float(data.get("taxes_owed")),
            owner_is_llc=bool(data.get("owner_is_llc", False)),
        )

        if add_lead_to_list(db, lead, source_list):
            added += 1
        else:
            skipped += 1

        result.imported = True
        result.imported_lead_id = lead.id
        affected_lead_ids.add(lead.id)

    source_list.record_count = added
    recompute_stack_depths(db, affected_lead_ids)
    db.commit()

    return {
        "source_list_id": source_list.id,
        "imported": added,
        "skipped": skipped,
    }
