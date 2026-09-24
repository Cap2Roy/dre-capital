"""Property scraper service.

Finds properties from public data sources — county tax/property appraiser
sites, public records APIs, and HTML pages.  Uses an LLM to parse unstructured
scraped content into structured property + owner records.

Source types:
  - rentcast:    RentCast API (JSON, real property data, 150M+ records)
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

# ── Source directory ──────────────────────────────────────────────────────────
# Pre-configured, known-working public property data sources that users can
# browse and add with one click.  These are real APIs/sites that return
# property data without requiring JavaScript rendering.

SOURCE_DIRECTORY: list[dict[str, str]] = [
    {
        "name": "RentCast — Properties by City/State",
        "source_type": "rentcast",
        "url": "https://api.rentcast.io/v1/properties",
        "description": "150M+ property records nationwide. Search by city+state. Returns owner name, mailing address, tax assessments, property details. Requires free RentCast API key (50 calls/mo free).",
        "search_hint": '{"city":"Houston","state":"TX","limit":50}',
        "state": "",
        "county": "",
    },
    {
        "name": "RentCast — Properties by ZIP Code",
        "source_type": "rentcast",
        "url": "https://api.rentcast.io/v1/properties",
        "description": "Search properties by ZIP code. Returns owner info, assessed value, tax history, sale history. Requires RentCast API key.",
        "search_hint": '{"zipCode":"77001","limit":50}',
        "state": "",
        "county": "",
    },
    {
        "name": "RentCast — Absentee Owners (non-owner-occupied)",
        "source_type": "rentcast",
        "url": "https://api.rentcast.io/v1/properties",
        "description": "Filter for absentee owners where mailing address differs from property address. Key motivated-seller signal. Requires RentCast API key.",
        "search_hint": '{"city":"Houston","state":"TX","ownerOccupied":false,"limit":50}',
        "state": "",
        "county": "",
    },
    {
        "name": "RentCast — Random Property Sample",
        "source_type": "rentcast",
        "url": "https://api.rentcast.io/v1/properties/random",
        "description": "Get up to 500 random property records — useful for testing or building a cold-call list. Requires RentCast API key.",
        "search_hint": '{"limit":50}',
        "state": "",
        "county": "",
    },
]


def get_source_directory() -> list[dict[str, str]]:
    """Return the pre-configured source directory."""
    return SOURCE_DIRECTORY


# ── HTML parsing helpers (no external dependency) ─────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_TABLE_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_TABLE_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.DOTALL | re.IGNORECASE)
_LINK_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)


def strip_html(html: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    text = _TAG_RE.sub("", html)
    text = _WS_RE.sub(" ", text)
    return text.strip()


def parse_html_tables(html: str) -> list[list[list[str]]]:
    """Parse all <table> elements into lists of rows of cell text."""
    tables: list[list[list[str]]] = []
    for tbl_match in re.finditer(r"<table[^>]*>(.*?)</table>", html, re.DOTALL | re.IGNORECASE):
        inner = tbl_match.group(1)
        rows: list[list[str]] = []
        for row_match in _TABLE_ROW_RE.finditer(inner):
            cells = _TABLE_CELL_RE.findall(row_match.group(1))
            rows.append([strip_html(c) for c in cells])
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
a real estate acquisitions company.  Your job is to extract property records
from raw HTML or JSON text and return them as a JSON array of objects.

Each property object should have these fields when available:
- property_address: street address
- property_city, property_state, property_zip
- owner_name: owner of record
- mailing_address, mailing_city, mailing_state, mailing_zip (if different)
- beds, baths, sqft, year_built
- assessed_value: current assessed/appraised value (number)
- taxes_owed: delinquent tax amount if any (number)
- last_sale_price, last_sale_date
- owner_is_llc: true if owner name contains LLC, Inc, Corp, etc.

Return ONLY the JSON array, no explanation.  If no properties found, return [].

Look for property data in HTML tables, JSON arrays, or structured text.  Each
record must have a property_address or at least a street number and name that looks like
an address.  If the data is unstructured text, parse it intelligently."""

LLM_ANALYSIS_PROMPT = """You are a real estate acquisitions analyst for DRE-Capital.
Analyze this property for motivated-seller signals and assign a lead score (0-100).

Consider:
- Tax delinquency / high taxes relative to value
- Absentee owner (mailing address differs from property)
- LLC or corporate ownership (often tired landlords)
- Low equity / high leverage (if sale price < assessed value)
- Older property (pre-1980, likely needs repairs)
- Vacant or code violations
- Recent inheritance / probate indicators

Respond in 2-3 sentences with: signals detected, lead score (0-100), and recommended action.
Keep it concise and actionable."""


def get_llm_config() -> dict[str, str]:
    """Get LLM API configuration from env vars."""
    return {
        "api_key": os.environ.get("LLM_API_KEY", ""),
        "base_url": os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
        "model": os.environ.get("LLM_MODEL", "gpt-4o-mini"),
    }


def llm_is_configured() -> bool:
    """Check if LLM is configured."""
    c = get_llm_config()
    return bool(c["api_key"])


def _call_llm(system: str, user: str, timeout: int = 30) -> str:
    """Call the LLM API (OpenAI-compatible). Returns text response.

    Raises no exception — returns empty string on failure so the caller
    can fall back gracefully.
    """
    cfg = get_llm_config()
    if not cfg["api_key"]:
        return ""
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{cfg['base_url']}/chat/completions",
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
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
    except Exception:
        return ""


def llm_parse_raw_data(raw_text: str) -> list[dict[str, Any]]:
    """Use the LLM to parse raw scraped text/HTML into structured property records.

    Tries the HTML parser first (fast, no API cost), then falls back to LLM
    if no tables are found, then tries raw JSON parse.
    """
    # Try HTML table parser first
    records = find_property_data_in_html(raw_text)
    if records:
        return records

    # Try JSON parse (for API responses that returned JSON but not in
    # the expected format)
    try:
        data = json.loads(raw_text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "results" in data:
            return data["results"]
    except (json.JSONDecodeError, TypeError):
        pass

    # Fall back to LLM
    if llm_is_configured():
        text = _call_llm(LLM_SYSTEM_PROMPT, raw_text[:8000])
        if text:
            try:
                # Strip markdown code fences if present
                text = text.strip()
                if text.startswith("```"):
                    text = re.sub(r"^```(?:json)?\s*", "", text)
                    text = re.sub(r"\s*```$", "", text)
                data = json.loads(text)
                if isinstance(data, list):
                    return data
            except json.JSONDecodeError:
                pass

    return []


def llm_analyze_property(property_data: dict[str, Any]) -> str:
    """Use the LLM to analyze a single property for motivated-seller signals."""
    # Heuristic analysis as baseline (always works, even without LLM)
    signals: list[str] = []
    score = 30

    taxes_owed = property_data.get("taxes_owed")
    assessed_value = property_data.get("assessed_value")
    owner_name = (property_data.get("owner_name") or "").strip()
    mailing = (property_data.get("mailing_address") or "").strip()
    prop_addr = (property_data.get("property_address") or "").strip()

    if taxes_owed and float(taxes_owed or 0) > 0:
        signals.append("tax delinquency")
        score += 25

    if mailing and prop_addr and mailing.lower() != prop_addr.lower():
        signals.append("absentee owner")
        score += 20

    if any(kw in owner_name.upper() for kw in ("LLC", "INC", "CORP", "PARTNERSHIP", "TRUST")):
        signals.append("corporate/LLC ownership")
        score += 10

    year_built = property_data.get("year_built")
    if year_built and int(float(year_built)) < 1980:
        signals.append("older property (likely needs repairs)")
        score += 5

    last_sale = property_data.get("last_sale_price")
    if last_sale and assessed_value and float(last_sale) < float(assessed_value) * 0.7:
        signals.append("possible low equity / distress")
        score += 15

    if not signals:
        return "No strong motivated-seller signals detected. Research further."

    if llm_is_configured():
        text = _call_llm(
            LLM_ANALYSIS_PROMPT,
            json.dumps(property_data, default=str),
            timeout=15,
        )
        if text:
            return text.strip()

    action = "call to qualify" if score >= 50 else "research further"
    return f"Signals: {', '.join(signals)}. Lead score: {score}/100. Recommend: {action}."


def llm_summarize_scrape(results: list[dict[str, Any]]) -> str:
    """Generate a summary of the scrape results using the LLM."""
    if not results:
        return "No properties found in this scrape."

    if llm_is_configured():
        # Truncate to avoid token limits
        sample = json.dumps(results[:20], default=str)[:4000]
        text = _call_llm(
            "You are a real estate analyst. Summarize property scrape results in 2-3 sentences: total count, key patterns, top opportunities.",
            f"Scrape results ({len(results)} total, showing first 20):\n{sample}",
            timeout=15,
        )
        if text:
            return text.strip()

    # Heuristic summary
    absentee = sum(1 for r in results if (r.get("mailing_address") or "").strip()
                   and (r.get("mailing_address", "") or "").strip().lower() != (r.get("property_address", "") or "").strip().lower())
    llc_owners = sum(1 for r in results if any(kw in (r.get("owner_name") or "").upper()
                  for kw in ("LLC", "INC", "CORP")))
    tax_delinquent = sum(1 for r in results if float(r.get("taxes_owed") or 0) > 0)

    parts = [f"Found {len(results)} properties."]
    if absentee:
        parts.append(f"{absentee} absentee owners.")
    if llc_owners:
        parts.append(f"{llc_owners} LLC/corporate owners.")
    if tax_delinquent:
        parts.append(f"{tax_delinquent} with tax delinquency.")
    parts.append("Review and import the ones that fit your criteria.")
    return " ".join(parts)


# ── Source adapters ───────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": "DRE-Capital-Property-Research/1.0 (real estate acquisitions; contact: ops@dre-capital.com)",
    "Accept": "text/html,application/json,*/*",
}


def _friendly_error(exc: Exception, source_type: str, url: str) -> str:
    """Convert httpx exceptions into actionable error messages."""
    url_display = url[:80] + "…" if len(url) > 80 else url

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 401 or code == 403:
            if source_type == "rentcast":
                return f"API key required (HTTP {code}). Get a free RentCast API key at https://app.rentcast.io/api — then add it to the source's API Key field or set RENTCAST_API_KEY env var."
            return f"Authentication failed (HTTP {code}). Check the API key for this source."
        if code == 404:
            return f"Page not found (HTTP 404) at {url_display}. The URL may be wrong or the page has moved. Check the source URL."
        if code == 429:
            return f"Rate limit exceeded (HTTP 429). Wait a moment and try again, or reduce the limit in search params."
        if code >= 500:
            return f"Server error (HTTP {code}) at {url_display}. The source site may be temporarily down. Try again later."
        return f"HTTP {code} error at {url_display}. Check the source URL and parameters."

    if isinstance(exc, httpx.ConnectError):
        return f"Cannot connect to {url_display}. The site may be down, blocking automated requests, or the URL is incorrect."
    if isinstance(exc, httpx.TimeoutException):
        return f"Request timed out after 30s for {url_display}. The site may be slow or unresponsive. Try again or use a different source."
    if isinstance(exc, httpx.TooManyRedirects):
        return f"Too many redirects at {url_display}. The site may require JavaScript or a login. Try a different source type."

    return f"Scrape error: {str(exc)[:200]}"


def scrape_rentcast(url: str, search_params: dict[str, Any], api_key: str | None = None) -> str:
    """Fetch property data from the RentCast API.

    RentCast returns JSON property records with owner info, tax assessments,
    and property details.  Requires an API key (free tier: 50 calls/mo).

    API key resolution order:
      1. api_key parameter (from source's api_key field)
      2. RENTCAST_API_KEY env var
      3. LLM_API_KEY env var (shared key fallback)

    Search params are passed as query parameters:
      city, state, zipCode, limit, ownerOccupied, propertyType, etc.
    See https://developers.rentcast.io/reference/search-queries
    """
    key = api_key or os.environ.get("RENTCAST_API_KEY", "") or os.environ.get("LLM_API_KEY", "")
    if not key:
        raise httpx.HTTPStatusError(
            "RentCast API key required",
            request=httpx.Request("GET", url),
            response=httpx.Response(401),
        )

    # Build query params — only pass non-empty values
    params = {k: str(v) for k, v in search_params.items() if v is not None and v != ""}

    headers = {"X-Api-Key": key, "Accept": "application/json"}
    with httpx.Client(timeout=30, follow_redirects=True, headers=headers) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        return resp.text


def scrape_county_tax(url: str, search_params: dict[str, Any], api_key: str | None = None) -> str:
    """Scrape a county tax/property appraiser site.

    Many county sites have search forms that return HTML tables of results.
    We fetch the search results page and return the raw HTML for parsing.
    Uses a browser-like User-Agent to avoid being blocked.
    """
    params = {k: v for k, v in search_params.items() if v}
    browser_headers = {
        **HEADERS,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    with httpx.Client(timeout=30, follow_redirects=True, headers=browser_headers) as client:
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
    browser_headers = {
        **HEADERS,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    with httpx.Client(timeout=30, follow_redirects=True, headers=browser_headers) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.text


SOURCE_ADAPTERS = {
    "rentcast": scrape_rentcast,
    "county_tax": scrape_county_tax,
    "public_api": scrape_public_api,
    "html": scrape_html,
}


# ── Job execution ──────────────────────────────────────────────────────────────

def run_scrape_job(db: Session, job: ScrapeJob) -> None:
    """Execute a scrape job: fetch data, parse with LLM, store results.

    This is a synchronous function — called from the API endpoint inline.
    Updates the job in-place.
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

        # Normalize RentCast records to our schema
        if source.source_type == "rentcast":
            records = [_normalize_rentcast(r) for r in records if isinstance(r, dict)]

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
        job.error_message = _friendly_error(exc, source.source_type, source.url)
        job.completed_at = datetime.utcnow()

    db.commit()


def _normalize_rentcast(r: dict[str, Any]) -> dict[str, Any]:
    """Normalize a RentCast property record to our internal schema."""
    record: dict[str, Any] = {
        "property_address": r.get("formattedAddress") or r.get("addressLine1"),
        "property_city": r.get("city"),
        "property_state": r.get("state"),
        "property_zip": r.get("zipCode"),
        "property_type": r.get("propertyType"),
        "beds": r.get("bedrooms"),
        "baths": r.get("bathrooms"),
        "sqft": r.get("squareFootage"),
        "year_built": r.get("yearBuilt"),
        "assessed_value": _latest_assessment(r),
        "taxes_owed": None,  # RentCast doesn't expose delinquent tax directly
        "last_sale_price": r.get("lastSalePrice"),
        "last_sale_date": r.get("lastSaleDate"),
        "owner_name": _owner_name(r),
        "owner_occupied": r.get("ownerOccupied"),
        "owner_is_llc": _is_llc(_owner_name(r)),
    }

    # Mailing address (for absentee owner detection)
    owner = r.get("owner") or {}
    mail = owner.get("mailingAddress") or {}
    if mail:
        record["mailing_address"] = mail.get("formattedAddress") or mail.get("addressLine1")
        record["mailing_city"] = mail.get("city")
        record["mailing_state"] = mail.get("state")
        record["mailing_zip"] = mail.get("zipCode")

    return record


def _latest_assessment(r: dict[str, Any]) -> float | None:
    """Extract the most recent assessed value from RentCast tax assessments."""
    assessments = r.get("taxAssessments") or {}
    if not assessments:
        return None
    # Keys are years as strings — find the latest
    latest_year = max(assessments.keys()) if assessments else None
    if latest_year:
        return assessments[latest_year].get("value")
    return None


def _owner_name(r: dict[str, Any]) -> str:
    """Extract owner name from RentCast record."""
    owner = r.get("owner") or {}
    names = owner.get("names") or []
    return ", ".join(names) if names else ""


def _is_llc(name: str) -> bool:
    """Check if owner name indicates LLC/corporate ownership."""
    if not name:
        return False
    upper = name.upper()
    return any(kw in upper for kw in ("LLC", "INC", "CORP", "PARTNERSHIP", "TRUST", "CO."))


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
