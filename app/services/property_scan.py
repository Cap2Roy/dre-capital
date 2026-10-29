"""Scan property: gather sourced facts about one lead's property from several data sources.

Sources (``CATALOG``; enabled per source in Settings → Property data sources):

- ``grok`` — xAI Responses API with the server-side ``web_search`` tool
  (https://docs.x.ai/developers/tools/web-search), two passes in parallel
  (official records; distress + listings). Grok searches and opens public
  pages (appraisal district / assessor, tax, deeds, court and sale notices,
  listing portals) and returns structured facts, each with the page URL and a
  verbatim quote. We then:
    1. drop facts without a source URL, absence-only values, and facts citing a
       URL the search tool never returned or opened (no invented sources);
    2. re-read each cited page ourselves (robots.txt-aware, own User-Agent,
       crawl-delay ≥ 1 s per host) and mark the fact ``confirmed`` when the
       quote / value is on the page, ``not_found`` when it isn't, ``unchecked``
       when the site doesn't let our bot read it.
- ``sheriff_sales`` — county sheriff-sale lists on CivilView (official, free):
  U.S. Census geocoder → county → that county's list → rows for this address.
- ``rentcast`` — the existing RentCast integration (property_data.enrich).
- Paid APIs — ATTOM, Regrid, BatchData, PropertyRadar, RealEstateAPI, Realie:
  adapters written from each provider's public API docs (endpoint, auth header,
  response fields). Estated is listed but not integrated (sunset, now ATTOM).

Facts from authoritative sources (official assessor / appraisal-district pages
confirmed on the page, RentCast public records) fill EMPTY lead fields only
(year built, sqft, beds, baths, assessed value) — user-entered data is never
overwritten, and conflicting authoritative values fill nothing.
"""
from __future__ import annotations

import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Lead, PropertyScan
from app.services.property_data import PropertyDataError, enrich, full_address
from app.timeutil import iso_utc, utcnow

XAI_BASE = "https://api.x.ai/v1"
DEFAULT_MODEL = "grok-4.3"
# Measured on real NJ/TX addresses (Oct 2026). A scan runs two research passes (records + signals).
MODEL_CHOICES = (
    ("grok-4.3", "Grok 4.3 — recommended (~37 s, $0.14–0.20 per scan)"),
    ("grok-4.20-non-reasoning", "Grok 4.20 fast — leaned on portal pages, missed the owner in tests"),
    ("grok-4.7", "Grok 4.7 — most thorough, ~4 min and ~$2 per pass"),
)
GROK_TIMEOUT = 360.0
GROK_MAX_TOOL_CALLS = 20
API_TIMEOUT = 25.0
VERIFY_MAX_PAGES = 15
VERIFY_MAX_PER_HOST = 4
STALE_RUNNING = timedelta(minutes=15)
USER_AGENT = "DRE-Capital-Agent/1.0 (+https://dre-capital.com; real estate research bot)"

CATEGORIES = (
    ("ownership", "Ownership"),
    ("property", "Property"),
    ("value_tax", "Value & tax"),
    ("sales", "Sales history"),
    ("distress", "Distress signals"),
    ("listing", "Listing"),
)
CATEGORY_IDS = {c for c, _ in CATEGORIES}

FIELD_LABELS = {
    "owner_name": "Owner of record", "owner_mailing_address": "Owner mailing address",
    "owner_occupied": "Owner occupied", "owner_type": "Owner type", "parcel_id": "Parcel / account #",
    "legal_description": "Legal description",
    "property_type": "Property type", "year_built": "Year built", "living_sqft": "Living area (sqft)",
    "lot_size": "Lot size", "beds": "Beds", "baths": "Baths",
    "assessed_value": "Assessed value", "land_value": "Land value", "improvement_value": "Improvement value",
    "market_value": "Market value", "tax_year": "Tax year", "annual_tax": "Annual tax", "tax_status": "Tax status",
    "taxes_owed": "Taxes owed", "tax_delinquent_year": "Tax delinquent year", "exemptions": "Exemptions",
    "last_sale_date": "Last sale date", "last_sale_price": "Last sale price", "sale_record": "Recorded sale / deed",
    "open_liens": "Open liens", "mortgage": "Mortgage",
    "foreclosure_notice": "Foreclosure", "sheriff_sale": "Sheriff sale", "tax_sale": "Tax sale",
    "tax_lien": "Tax lien", "lis_pendens": "Lis pendens", "code_violation": "Code violation", "probate": "Probate",
    "bankruptcy": "Bankruptcy", "vacancy": "Vacancy",
    "listing_status": "Listing status", "list_price": "List price", "listing_date": "Listed on",
    "days_on_market": "Days on market", "estimated_value": "Estimated value (AVM)", "listing_agent": "Listing agent",
}
# Fields that legitimately hold several values (one per record) — never a "conflict".
MULTI_FIELDS = {"sale_record", "mortgage", "foreclosure_notice", "sheriff_sale", "tax_sale", "tax_lien", "lis_pendens",
                "code_violation", "probate", "bankruptcy", "exemptions", "listing_agent", "estimated_value"}

# Lead columns filled (only when empty) from authoritative facts.
LEAD_FILL = {
    "year_built": ("year_built", int),
    "living_sqft": ("sqft", int),
    "beds": ("beds", int),
    "baths": ("baths", float),
    "assessed_value": ("assessed_value", float),
}

# Official, non-.gov appraisal / assessor domains (most are *.gov or *.us).
_OFFICIAL_HOSTS = {"tad.org", "dallascad.org", "hcad.org", "traviscad.org", "bcad.org", "collincad.org",
                   "dentoncad.com", "esearch.dentoncad.com", "fbcad.org", "wcad.org", "elpasocad.org",
                   "taxrecords-nj.com", "njactb.org",
                   "salesweb.civilview.com"}  # county sheriffs' official sale listings (NJ, PA, …)
_OFFICIAL_RE = re.compile(r"(\.gov|\.us|\.gov\.[a-z]{2})$|(^|\.)[a-z]*cad\.(org|com)$")


class ScanError(RuntimeError):
    """Scan can't start (no sources) or a source failed in a way worth showing as-is."""


# ── Catalog ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SourceDef:
    id: str
    name: str
    key_setting: Optional[str]
    description: str
    adds: str
    site_url: str
    docs_url: str
    pricing_url: str
    integrated: bool = True
    default_enabled: bool = False
    note: str = ""


CATALOG: tuple[SourceDef, ...] = (
    SourceDef(
        "grok", "Grok web research (xAI)", "xai_api_key",
        "Grok searches the public web for this exact address and opens the pages: appraisal district / assessor "
        "record, tax status, recorded sales, foreclosure / sheriff / tax-sale notices, code violations, listings "
        "and probate signals. Every fact cites its page; we re-read the page to confirm it.",
        "Owner of record, values, tax, sales, distress notices, listing status — with links",
        "https://x.ai/api", "https://docs.x.ai/developers/tools/web-search", "https://docs.x.ai/developers/models",
        default_enabled=True,
        note="Uses the xAI key from Calling & API Keys → agents default. Billed per token + $5 per 1,000 web searches.",
    ),
    SourceDef(
        "rentcast", "RentCast", "rentcast_api_key",
        "The Property Data integration above: public records, value estimate and MLS sale listing.",
        "Owner, assessed value, property tax, last sale, AVM, active listing",
        "https://www.rentcast.io/api", "https://developers.rentcast.io", "https://www.rentcast.io/api#api-pricing",
        default_enabled=True,
        note="Each scan uses up to 3 RentCast calls of the monthly budget.",
    ),
    SourceDef(
        "sheriff_sales", "County sheriff sales (CivilView)", None,
        "Official foreclosure sheriff-sale lists that NJ counties (and some DE, PA, OH, TX, IL … counties) publish on "
        "CivilView. The free U.S. Census geocoder finds the county; we look the address up on that county's list.",
        "Upcoming sheriff sale: date, sheriff #, plaintiff (lender), defendant (usually the owner)",
        "https://salesweb.civilview.com", "https://salesweb.civilview.com", "",
        default_enabled=True,
        note="Free public listings, no key. Covers only counties that use CivilView.",
    ),
    SourceDef(
        "attom", "ATTOM Data", "attom_api_key",
        "Nationwide assessor, recorder and AVM data (Property API, expanded profile).",
        "Owner + mailing address, assessed / market value, tax, year built, sqft, lot, last sale, first mortgage",
        "https://www.attomdata.com/solutions/property-data-api/", "https://api.developer.attomdata.com/docs",
        "https://api.developer.attomdata.com/home",
        note="No public price list — trial / sales contact. Billed per successful call.",
    ),
    SourceDef(
        "regrid", "Regrid (parcels)", "regrid_api_key",
        "Parcel records for every U.S. county (Parcel API v2, address lookup).",
        "Parcel ID, owner + mailing address, parcel value, tax, year built, USPS vacancy flag",
        "https://regrid.com/api", "https://support.regrid.com/docs/api-endpoint-overview", "https://regrid.com/api",
    ),
    SourceDef(
        "batchdata", "BatchData", "batchdata_api_key",
        "Property lookup with owner, liens, foreclosure, listing and vacancy datasets (BatchData also sells skip "
        "tracing).",
        "Owner, assessed / market value, tax delinquency, open liens, foreclosure status, vacancy, MLS status",
        "https://batchdata.io", "https://developer.batchdata.com/docs/batchdata/batchdata-v1",
        "https://batchdata.io/pricing",
    ),
    SourceDef(
        "propertyradar", "PropertyRadar", "propertyradar_api_key",
        "Property records with foreclosure, tax-delinquency and listing flags (API v1).",
        "Owner, assessed value, loan balance, foreclosure stage, tax delinquency, vacancy, listing",
        "https://www.propertyradar.com", "https://developers.propertyradar.com", "https://www.propertyradar.com/pricing",
        note="Needs a paid PropertyRadar subscription + API trial; each record returned is billed. Terms: end-user use only.",
    ),
    SourceDef(
        "realestateapi", "RealEstateAPI", "realestateapi_api_key",
        "PropertyDetail API: owner, tax, mortgages, pre-foreclosure / auction and MLS history.",
        "Owner, assessed / market value, tax, open mortgage balance, pre-foreclosure, vacancy, MLS status",
        "https://www.realestateapi.com", "https://developer.realestateapi.com/reference/property-detail-api-1",
        "https://www.realestateapi.com/pricing/",
    ),
    SourceDef(
        "realie", "Realie", "realie_api_key",
        "Property data API built from county records (address lookup).",
        "Owner, assessed / market value, tax, liens, foreclosure filing",
        "https://www.realie.ai", "https://docs.realie.ai/api-reference/v3/property/address-lookup",
        "https://docs.realie.ai/api-reference/pricing",
    ),
    SourceDef(
        "estated", "Estated", None,
        "Estated was acquired by ATTOM; its docs are being retired in 2026 and new signups / pricing pages are gone.",
        "Same data as ATTOM — use ATTOM instead",
        "https://estated.com", "https://estated.com/developers/docs/v4", "https://www.attomdata.com",
        integrated=False,
        note="Not integrated: no new API keys are issued.",
    ),
)
CATALOG_BY_ID = {s.id: s for s in CATALOG}


def _settings():
    from app.services import settings as s
    return s


def _key(db: Session, src: SourceDef) -> str:
    return (_settings().get_setting(db, src.key_setting) or "").strip() if src.key_setting else ""


def _toggles(db: Session) -> dict[str, bool]:
    raw = _settings().get_setting(db, "property_scan_sources") or ""
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return {k: bool(v) for k, v in data.items()} if isinstance(data, dict) else {}


def scan_model(db: Session) -> str:
    return (_settings().get_setting(db, "property_scan_model") or "").strip() or DEFAULT_MODEL


def source_states(db: Session) -> list[dict]:
    """The catalog with enabled / key status for the Settings card (keys never included)."""
    toggles = _toggles(db)
    out = []
    for s in CATALOG:
        needs_key = bool(s.key_setting)
        key_set = bool(_key(db, s))
        enabled = s.integrated and toggles.get(s.id, s.default_enabled)
        out.append({
            "id": s.id, "name": s.name, "description": s.description, "adds": s.adds, "note": s.note,
            "site_url": s.site_url, "docs_url": s.docs_url, "pricing_url": s.pricing_url,
            "integrated": s.integrated, "key_setting": s.key_setting, "needs_key": needs_key,
            # Shared keys (xAI, RentCast) are edited in their own cards.
            "key_editable": needs_key and s.id not in ("grok", "rentcast"),
            "key_set": key_set, "key_masked": "••••" if key_set else "",
            "enabled": bool(enabled), "ready": bool(enabled and (key_set or not needs_key)),
        })
    return out


def catalog_status(db: Session) -> dict:
    return {
        "sources": source_states(db),
        "model": scan_model(db),
        "models": [{"id": m, "label": label} for m, label in MODEL_CHOICES],
    }


def update_sources(db: Session, items: list[dict], model: Optional[str] = None) -> None:
    """Admin update: enable/disable sources, set or clear their API keys, pick the Grok model."""
    s = _settings()
    toggles = _toggles(db)
    for item in items:
        src = CATALOG_BY_ID.get(item.get("id") or "")
        if src is None:
            raise ScanError(f"Unknown property data source: {item.get('id')!r}")
        if item.get("enabled") is not None:
            if item["enabled"] and not src.integrated:
                raise ScanError(f"{src.name} is not integrated and can't be enabled.")
            toggles[src.id] = bool(item["enabled"])
        key = item.get("key")
        if key is not None and src.key_setting and src.id not in ("grok", "rentcast"):
            key = key.strip()
            if key and set(key) == {"\u2022"}:
                continue  # masked echo = keep current
            s.set_setting(db, src.key_setting, key)
    s.set_setting(db, "property_scan_sources", json.dumps(toggles, sort_keys=True))
    if model is not None:
        s.set_setting(db, "property_scan_model", model.strip())


# ── Facts ───────────────────────────────────────────────────────────────────

def _fact(category: str, field_: str, value: Any, *, src: str, source_name: str, source_url: Optional[str],
          source_kind: str, as_of: Any = None, quote: Optional[str] = None, check: str = "api",
          official: bool = False) -> dict:
    return {
        "category": category, "field": field_, "label": FIELD_LABELS.get(field_, field_.replace("_", " ").capitalize()),
        "value": str(value).strip(), "as_of": str(as_of) if as_of not in (None, "") else None,
        "source": src, "source_name": source_name, "source_url": source_url, "source_kind": source_kind,
        "quote": quote, "check": check, "official": official,
    }


def _num(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(v))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _money(v: Any) -> Optional[str]:
    n = _num(v)
    if n is None:
        return None
    return f"${n:,.0f}" if n == int(n) else f"${n:,.2f}"


def _int_str(v: Any) -> Optional[str]:
    n = _num(v)
    return None if n is None else (f"{int(n):,}" if n == int(n) else f"{n:,}")


def _plain(v: Any) -> Optional[str]:
    if v is None or isinstance(v, (dict, list)):
        return None
    if isinstance(v, bool):
        return "Yes" if v else "No"
    s = " ".join(str(v).split())
    return s or None


def _date(v: Any) -> Optional[str]:
    s = _plain(v)
    return s[:10] if s and re.match(r"\d{4}-\d{2}-\d{2}", s) else s


def _pick(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if obj is None:
            return None
        if isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


# A mapping row: (category, field, json path or callable(record), formatter, as_of path or None)
Row = tuple[str, str, Any, Callable[[Any], Optional[str]], Optional[str]]


def _map_facts(record: dict, rows: list[Row], *, src: SourceDef, source_url: str) -> list[dict]:
    facts = []
    for category, fld, path, fmt, as_of_path in rows:
        raw = path(record) if callable(path) else _pick(record, path)
        value = fmt(raw)
        if value in (None, ""):
            continue
        as_of = _pick(record, as_of_path) if as_of_path else None
        facts.append(_fact(category, fld, value, src=src.id, source_name=src.name, source_url=source_url,
                           source_kind="data_provider", as_of=as_of))
    return facts


def _flag(v: Any) -> Optional[str]:
    """Only positive flags become facts (a False flag is absence, not a finding)."""
    return "Yes" if v is True or (isinstance(v, str) and v.strip().lower() in ("true", "y", "yes", "1")) else None


# ── HTTP helper for API providers ───────────────────────────────────────────

def _err_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200].strip() or resp.reason_phrase
    for path in ("message", "error.description", "error", "status.message", "Response.status.msg", "status.msg", "detail"):
        v = _pick(body, path)
        if isinstance(v, str) and v.strip():
            return v.strip()[:200]
    return json.dumps(body)[:200]


def _request(src: SourceDef, method: str, url: str, **kw) -> Optional[Any]:
    """JSON from a provider; None on a documented no-match (404). Raises ScanError with the provider's message."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **kw.pop("headers", {})}
    try:
        resp = httpx.request(method, url, headers=headers, timeout=API_TIMEOUT, follow_redirects=False, **kw)
    except httpx.HTTPError as exc:
        raise ScanError(f"{src.name} unreachable: {type(exc).__name__}") from exc
    if resp.status_code in (401, 403):
        raise ScanError(f"{src.name} rejected the API key (HTTP {resp.status_code}): {_err_text(resp)}")
    if resp.status_code == 404:
        return None
    if resp.status_code == 429:
        raise ScanError(f"{src.name} rate limit / quota reached: {_err_text(resp)}")
    if resp.status_code >= 300:
        raise ScanError(f"{src.name} error HTTP {resp.status_code}: {_err_text(resp)}")
    try:
        return resp.json()
    except ValueError as exc:
        raise ScanError(f"{src.name} returned non-JSON ({resp.headers.get('content-type')})") from exc


@dataclass
class Target:
    """The property being scanned (plain values — safe to hand to worker threads)."""
    address: str          # one line
    street: str
    city: str
    state: str
    zip: str
    county: str
    owner: str


# ── Paid API adapters (from each provider's public docs) ────────────────────

def _attom(src: SourceDef, key: str, t: Target) -> list[dict]:
    # https://api.developer.attomdata.com/docs — header "apikey", envelope {status:{msg}, property:[...]}
    line2 = ", ".join(x for x in (t.city, f"{t.state} {t.zip}".strip()) if x)
    data = _request(src, "GET", "https://api.gateway.attomdata.com/propertyapi/v1.0.0/property/expandedprofile",
                    params={"address1": t.street, "address2": line2}, headers={"apikey": key})
    if not data:
        return []
    msg = str(_pick(data, "status.msg") or "")
    if msg == "SuccessWithoutResult" or not data.get("property"):
        return []
    if not msg.startswith("Success"):
        raise ScanError(f"ATTOM: {msg or 'unexpected response'}")
    p = data["property"][0]

    def owner(r):
        names = []
        for i in range(1, 5):
            o = _pick(r, f"assessment.owner.owner{i}") or {}
            n = " ".join(x for x in (o.get("firstNameAndMi") or o.get("firstnameandmi"),
                                    o.get("lastName") or o.get("lastname")) if x)
            if n.strip():
                names.append(n.strip())
        return "; ".join(names) or None

    def mortgage(r):
        m = _pick(r, "assessment.mortgage.FirstConcurrent") or {}
        if not m.get("amount"):
            return None
        bits = [_money(m["amount"]), m.get("date"), m.get("LenderLastName") or m.get("lenderLastName")]
        return " · ".join(str(b) for b in bits if b)

    rows: list[Row] = [
        ("ownership", "owner_name", owner, _plain, None),
        ("ownership", "owner_mailing_address", "assessment.owner.mailingAddressOneline", _plain, None),
        ("ownership", "parcel_id", "identifier.apn", _plain, None),
        ("property", "property_type", "summary.propertyType", _plain, None),
        ("property", "year_built", "summary.yearBuilt", _plain, None),
        ("property", "living_sqft", "building.size.livingSize", _int_str, None),
        ("property", "lot_size", lambda r: f"{_int_str(_pick(r, 'lot.lotSize2'))} sqft" if _pick(r, "lot.lotSize2") else None, _plain, None),
        ("property", "beds", "building.rooms.beds", _plain, None),
        ("property", "baths", "building.rooms.bathsTotal", _plain, None),
        ("value_tax", "assessed_value", "assessment.assessed.assdTtlValue", _money, "assessment.tax.taxYear"),
        ("value_tax", "market_value", "assessment.market.mktTtlValue", _money, "assessment.tax.taxYear"),
        ("value_tax", "annual_tax", "assessment.tax.taxAmt", _money, "assessment.tax.taxYear"),
        ("sales", "last_sale_date", "sale.saleTransDate", _date, None),
        ("sales", "last_sale_price", "sale.amount.saleAmt", _money, "sale.saleTransDate"),
        ("sales", "mortgage", mortgage, _plain, None),
        ("ownership", "owner_occupied", lambda r: {"O": "Yes", "A": "No (absentee)"}.get(str(_pick(r, "summary.absenteeInd") or "")[:1]), _plain, None),
    ]
    return _map_facts(p, rows, src=src, source_url="https://api.developer.attomdata.com/docs")


def _regrid(src: SourceDef, key: str, t: Target) -> list[dict]:
    # https://support.regrid.com/reference/get_parcels-address-1 — header x-regrid-token
    params = {"query": t.address, "limit": 1}
    if t.state and t.county:
        params["path"] = f"/us/{t.state.lower()}/{re.sub(r'[^a-z]+', '-', t.county.lower().replace(' county', '')).strip('-')}"
    data = _request(src, "GET", "https://app.regrid.com/api/v2/parcels/address", params=params,
                    headers={"x-regrid-token": key})
    feats = _pick(data, "parcels.features") or []
    if not feats:
        return []
    f = _pick(feats[0], "properties.fields") or {}

    def mail(r):
        parts = [r.get("mailadd"), r.get("mail_city"), " ".join(x for x in (r.get("mail_state2"), r.get("mail_zip")) if x)]
        return ", ".join(p for p in parts if p) or None

    def owners(r):
        return "; ".join(r[k] for k in ("owner", "owner2", "owner3", "owner4") if r.get(k)) or None

    def vacancy(r):
        v = str(r.get("usps_vacancy") or "").upper()
        return f"USPS vacant{(' since ' + r['usps_vacancy_date']) if r.get('usps_vacancy_date') else ''}" if v == "Y" else None

    rows: list[Row] = [
        ("ownership", "owner_name", owners, _plain, None),
        ("ownership", "owner_mailing_address", mail, _plain, None),
        ("ownership", "parcel_id", "parcelnumb", _plain, None),
        ("property", "year_built", "yearbuilt", _plain, None),
        ("property", "living_sqft", "area_building", _int_str, None),
        ("property", "lot_size", lambda r: f"{r['ll_gisacre']} acres" if r.get("ll_gisacre") else None, _plain, None),
        ("property", "beds", "num_bedrooms", _plain, None),
        ("property", "baths", "num_bath", _plain, None),
        ("value_tax", "assessed_value", lambda r: r.get("parval") if "assess" in str(r.get("parvaltype") or "").lower() else None, _money, "taxyear"),
        ("value_tax", "market_value", lambda r: r.get("parval") if "market" in str(r.get("parvaltype") or "").lower() else None, _money, "taxyear"),
        ("value_tax", "land_value", "landval", _money, "taxyear"),
        ("value_tax", "improvement_value", "improvval", _money, "taxyear"),
        ("value_tax", "annual_tax", "taxamt", _money, "taxyear"),
        ("sales", "last_sale_date", "saledate", _date, None),
        ("sales", "last_sale_price", "saleprice", _money, "saledate"),
        ("distress", "vacancy", vacancy, _plain, None),
    ]
    return _map_facts(f, rows, src=src, source_url="https://app.regrid.com" + (feats[0].get("properties", {}).get("path") or ""))


def _batchdata(src: SourceDef, key: str, t: Target) -> list[dict]:
    # https://developer.batchdata.com/docs/batchdata/batchdata-v1/operations/create-a-property-lookup-all-attribute
    body = {"requests": [{"address": {"street": t.street, "city": t.city, "state": t.state, "zip": t.zip}}],
            "options": {"skipTrace": False,
                        "datasets": ["core", "owner", "mortgage-liens", "foreclosure", "listing", "valuation", "quicklist"]}}
    data = _request(src, "POST", "https://api.batchdata.com/api/v1/property/lookup/all-attributes", json=body,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    props = _pick(data, "results.properties") or []
    if not props:
        return []
    p = props[0]

    def mail(r):
        m = _pick(r, "owner.mailingAddress") or {}
        return ", ".join(x for x in (m.get("street"), m.get("city"), " ".join(y for y in (m.get("state"), m.get("zip")) if y)) if x) or None

    def foreclosure(r):
        fc = r.get("foreclosure") or {}
        if not (fc.get("status") or fc.get("filingDate") or fc.get("auctionDate")):
            return None
        bits = [fc.get("status"), f"filed {fc['filingDate']}" if fc.get("filingDate") else None,
                f"auction {fc['auctionDate']}" if fc.get("auctionDate") else None,
                f"unpaid {_money(fc['unpaidBalance'])}" if fc.get("unpaidBalance") else None]
        return " · ".join(b for b in bits if b)

    def liens(r):
        bal = _pick(r, "openLien.totalOpenLienBalance")
        cnt = _pick(r, "openLien.totalOpenLienCount")
        if not bal and not cnt:
            return None
        return " · ".join(x for x in (f"{cnt} open" if cnt else None, _money(bal)) if x)

    def listing(r):
        st = _pick(r, "listing.status")
        if not st:
            return None
        return " · ".join(x for x in (st, _money(_pick(r, "listing.price")),
                                      f"{_pick(r, 'listing.daysOnMarket')} days" if _pick(r, "listing.daysOnMarket") else None) if x)

    rows: list[Row] = [
        ("ownership", "owner_name", "owner.fullName", _plain, None),
        ("ownership", "owner_mailing_address", mail, _plain, None),
        ("property", "year_built", "building.yearBuilt", _plain, None),
        ("property", "living_sqft", "building.livingAreaSquareFeet", _int_str, None),
        ("property", "lot_size", lambda r: f"{_int_str(_pick(r, 'lot.lotSizeSquareFeet'))} sqft" if _pick(r, "lot.lotSizeSquareFeet") else None, _plain, None),
        ("property", "beds", "building.bedroomCount", _plain, None),
        ("property", "baths", "building.bathroomCount", _plain, None),
        ("value_tax", "assessed_value", "assessment.totalAssessedValue", _money, "assessment.assessmentYear"),
        ("value_tax", "market_value", "assessment.totalMarketValue", _money, "assessment.marketValueYear"),
        ("value_tax", "annual_tax", "tax.taxAmount", _money, "tax.taxYear"),
        ("value_tax", "tax_delinquent_year", "tax.taxDelinquentYear", _plain, None),
        ("sales", "last_sale_date", "sale.lastSale.saleDate", _date, None),
        ("sales", "last_sale_price", "sale.lastSale.price", _money, "sale.lastSale.saleDate"),
        ("sales", "open_liens", liens, _plain, None),
        ("distress", "foreclosure_notice", foreclosure, _plain, None),
        ("distress", "tax_lien", lambda r: _flag(_pick(r, "quickLists.taxDefault")) and "Tax default (BatchData quick list)", _plain, None),
        ("distress", "lis_pendens", lambda r: _flag(_pick(r, "quickLists.noticeOfLisPendens")) and "Notice of lis pendens", _plain, None),
        ("distress", "vacancy", lambda r: _flag(_pick(r, "general.vacant") if _pick(r, "general.vacant") is not None else _pick(r, "quickLists.vacant")) and "Vacant", _plain, None),
        ("listing", "listing_status", listing, _plain, None),
        ("listing", "estimated_value", "valuation.estimatedValue", _money, None),
    ]
    return _map_facts(p, rows, src=src, source_url="https://app.batchdata.com")


def _propertyradar(src: SourceDef, key: str, t: Target) -> list[dict]:
    # https://developers.propertyradar.com/api/properties/post/properties — Purchase=1 bills this one record.
    criteria = [{"name": "Address", "value": [t.street]}, {"name": "State", "value": [t.state]}]
    if t.city:
        criteria.append({"name": "City", "value": [t.city]})
    if t.zip:
        criteria.append({"name": "ZipFive", "value": [t.zip[:5]]})
    data = _request(src, "POST", "https://api.propertyradar.com/v1/properties",
                    params={"Purchase": 1, "Limit": 1, "Fields": "Overview"}, json={"Criteria": criteria},
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    results = (data or {}).get("results") or []
    if not results:
        return []
    p = results[0]

    def mail(r):
        return ", ".join(x for x in (r.get("OwnerAddress"), r.get("OwnerCity"),
                                     " ".join(y for y in (r.get("OwnerState"), r.get("OwnerZipFive")) if y)) if x) or None

    def foreclosure(r):
        if _flag(r.get("inForeclosure")) or _flag(r.get("isPreforeclosure")):
            return r.get("ForeclosureStage") or ("Pre-foreclosure" if _flag(r.get("isPreforeclosure")) else "In foreclosure")
        return None

    def delinquent(r):
        if not _flag(r.get("inTaxDelinquency")):
            return None
        return " · ".join(x for x in ("Tax delinquent", _money(r.get("DelinquentAmount")),
                                      f"since {r['DelinquentYear']}" if r.get("DelinquentYear") else None) if x)

    def vacancy(r):
        bits = [x for x, k in (("site vacant", "isSiteVacant"), ("mail vacant", "isMailVacant")) if _flag(r.get(k))]
        return ", ".join(bits).capitalize() or None

    def listed(r):
        return " · ".join(x for x in ("Listed for sale", _money(r.get("ListingPrice"))) if x) if _flag(r.get("isListedForSale")) else None

    rows: list[Row] = [
        ("ownership", "owner_name", "Owner", _plain, None),
        ("ownership", "owner_mailing_address", mail, _plain, None),
        ("property", "year_built", "YearBuilt", _plain, None),
        ("property", "living_sqft", "SqFt", _int_str, None),
        ("property", "lot_size", "LotSize", _plain, None),
        ("property", "beds", "Beds", _plain, None),
        ("property", "baths", "Baths", _plain, None),
        ("value_tax", "assessed_value", "AssessedValue", _money, "AssessedYear"),
        ("value_tax", "annual_tax", "AnnualTaxes", _money, None),
        ("value_tax", "taxes_owed", delinquent, _plain, None),
        ("sales", "last_sale_date", "LastTransferRecDate", _date, None),
        ("sales", "last_sale_price", "LastTransferValue", _money, "LastTransferRecDate"),
        ("sales", "open_liens", lambda r: _money(r.get("TotalLoanBalance")) and f"{_money(r.get('TotalLoanBalance'))} total loan balance ({r.get('NumberLoans') or '?'} loans)", _plain, None),
        ("distress", "foreclosure_notice", foreclosure, _plain, None),
        ("distress", "vacancy", vacancy, _plain, None),
        ("listing", "listing_status", listed, _plain, None),
        ("listing", "estimated_value", "AVM", _money, None),
    ]
    rid = p.get("RadarID")
    return _map_facts(p, rows, src=src, source_url=f"https://app.propertyradar.com/#!/property/{rid}" if rid else src.site_url)


def _realestateapi(src: SourceDef, key: str, t: Target) -> list[dict]:
    # https://developer.realestateapi.com/reference/property-detail-api-1 — header x-api-key; 404 = no match
    data = _request(src, "POST", "https://api.realestateapi.com/v2/PropertyDetail", json={"address": t.address},
                    headers={"x-api-key": key, "Content-Type": "application/json"})
    p = (data or {}).get("data") or {}
    if not p:
        return []

    def mortgages(r):
        bal = r.get("openMortgageBalance")
        return f"{_money(bal)} open mortgage balance" if _num(bal) else None

    def mls(r):
        for k, label in (("mlsActive", "Active"), ("mlsPending", "Pending"), ("mlsSold", "Sold"), ("mlsCancelled", "Cancelled"), ("mlsFailed", "Failed / expired")):
            if r.get(k) is True:
                return f"MLS: {label}"
        return None

    rows: list[Row] = [
        ("ownership", "owner_name", "ownerInfo.owner1FullName", _plain, None),
        ("ownership", "owner_mailing_address", "ownerInfo.mailAddress.label", _plain, None),
        ("property", "year_built", "propertyInfo.yearBuilt", _plain, None),
        ("property", "living_sqft", "propertyInfo.livingSquareFeet", _int_str, None),
        ("property", "lot_size", lambda r: f"{_int_str(_pick(r, 'lotInfo.lotSquareFeet'))} sqft" if _pick(r, "lotInfo.lotSquareFeet") else None, _plain, None),
        ("property", "beds", "propertyInfo.bedrooms", _plain, None),
        ("property", "baths", "propertyInfo.bathrooms", _plain, None),
        ("value_tax", "assessed_value", "taxInfo.assessedValue", _money, "taxInfo.year"),
        ("value_tax", "market_value", "taxInfo.marketValue", _money, "taxInfo.year"),
        ("value_tax", "annual_tax", "taxInfo.taxAmount", _money, "taxInfo.year"),
        ("sales", "last_sale_date", "lastSaleDate", _date, None),
        ("sales", "last_sale_price", "lastSalePrice", _money, "lastSaleDate"),
        ("sales", "open_liens", mortgages, _plain, None),
        ("distress", "foreclosure_notice", lambda r: _flag(r.get("preForeclosure")) and "Pre-foreclosure", _plain, None),
        ("distress", "sheriff_sale", lambda r: _flag(r.get("auction")) and "Auction scheduled", _plain, None),
        ("distress", "tax_lien", lambda r: _flag(r.get("taxLien")) and "Tax lien", _plain, None),
        ("distress", "vacancy", lambda r: _flag(r.get("vacant")) and "Vacant", _plain, None),
        ("listing", "listing_status", mls, _plain, None),
        ("listing", "estimated_value", "estimatedValue", _money, None),
    ]
    return _map_facts(p, rows, src=src, source_url="https://www.realestateapi.com")


def _realie(src: SourceDef, key: str, t: Target) -> list[dict]:
    # https://docs.realie.ai/api-reference/v3/property/address-lookup — "Authorization: <key>" (no Bearer)
    params = {"state": t.state, "address": t.street}
    if t.city and t.county:  # county is required when city is given
        params.update(city=t.city, county=re.sub(r"\s+county$", "", t.county, flags=re.I))
    data = _request(src, "GET", "https://app.realie.ai/api/public/property/address/", params=params,
                    headers={"Authorization": key})
    p = (data or {}).get("property") or {}
    if not p:
        return []
    rows: list[Row] = [
        ("ownership", "owner_name", lambda r: "; ".join(x for x in (r.get("ownerName"), r.get("ownerName2")) if x) or None, _plain, None),
        ("ownership", "owner_mailing_address", "ownerAddressFull", _plain, None),
        ("property", "year_built", "yearBuilt", _plain, None),
        ("property", "living_sqft", "buildingArea", _int_str, None),
        ("property", "lot_size", lambda r: f"{r['acres']} acres" if r.get("acres") else None, _plain, None),
        ("property", "beds", "totalBedrooms", _plain, None),
        ("property", "baths", "totalBathrooms", _plain, None),
        ("value_tax", "assessed_value", "totalAssessedValue", _money, "assessedYear"),
        ("value_tax", "market_value", "totalMarketValue", _money, "marketValueYear"),
        ("value_tax", "annual_tax", "taxValue", _money, "taxYear"),
        ("sales", "last_sale_date", "transferDate", _date, None),
        ("sales", "last_sale_price", "transferPrice", _money, "transferDate"),
        ("sales", "open_liens", lambda r: _money(r.get("totalLienBalance")) and f"{_money(r.get('totalLienBalance'))} ({r.get('totalLienCount') or '?'} liens)", _plain, None),
        ("distress", "foreclosure_notice", lambda r: (r.get("forecloseCode") or r.get("forecloseFileDate")) and " · ".join(str(x) for x in (r.get("forecloseCode"), r.get("forecloseFileDate") and f"filed {r['forecloseFileDate']}", r.get("auctionDate") and f"auction {r['auctionDate']}") if x), _plain, None),
        ("listing", "estimated_value", "modelValue", _money, None),
    ]
    return _map_facts(p, rows, src=src, source_url="https://app.realie.ai")


# ── County sheriff sales on CivilView (free, official) ──────────────────────

CIVILVIEW = "https://salesweb.civilview.com"
CENSUS_GEOCODER = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
_DIRECTIONALS = {"n", "s", "e", "w", "ne", "nw", "se", "sw", "north", "south", "east", "west"}
_cv_cache: dict[str, Any] = {}
CV_CACHE_SECONDS = 12 * 3600


def census_county(t: Target) -> Optional[tuple[str, str]]:
    """(county name, state) from the U.S. Census geocoder, e.g. ("Camden County", "NJ"); None when unmatched."""
    try:
        r = httpx.get(CENSUS_GEOCODER, timeout=API_TIMEOUT, headers={"User-Agent": USER_AGENT},
                      params={"address": t.address, "benchmark": "Public_AR_Current", "vintage": "Current_Current",
                              "layers": "Counties", "format": "json"})
        r.raise_for_status()
        match = (_pick(r.json(), "result.addressMatches") or [None])[0]
    except (httpx.HTTPError, ValueError) as exc:
        raise ScanError(f"U.S. Census geocoder unavailable: {type(exc).__name__}") from exc
    county = _pick(match, "geographies.Counties.0.NAME") if match else None
    state = _pick(match, "addressComponents.state") if match else None
    return (county, state.upper()) if county and state else None


def _cell(s: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s)).split())


def _cv_fetch(path: str, robots) -> str:
    from app.services.scraping_agent import ScrapingError, fetch_page
    try:
        return fetch_page(CIVILVIEW + path, robots).body
    except ScrapingError as exc:
        raise ScanError(f"CivilView: {exc}") from exc


def civilview_counties(robots) -> dict[str, list[tuple[str, str]]]:
    """"camden county, nj" → [(countyId, "Camden County, NJ")] (TX counties list one per constable precinct)."""
    cached = _cv_cache.get("counties")
    if cached and time.time() - cached[0] < CV_CACHE_SECONDS:
        return cached[1]
    out: dict[str, list[tuple[str, str]]] = {}
    for cid, label in re.findall(r'href="[^"]*SalesSearch\?countyId=(\d+)"[^>]*>([^<]+)<', _cv_fetch("/", robots)):
        label = " ".join(html.unescape(label).split())
        parts = [p.strip().lower() for p in label.split(",")]
        if len(parts) >= 2:
            out.setdefault(f"{parts[0]}, {parts[1]}", []).append((cid, label))
    if not out:
        raise ScanError("CivilView county list is empty or its layout changed.")
    _cv_cache["counties"] = (time.time(), out)
    return out


def _street_key(addr: str) -> Optional[tuple[str, str]]:
    """("128", "oak") from "128 Oak Street …"; directionals skipped."""
    words = _norm(addr).split()
    if not words or not words[0].isdigit():
        return None
    rest = [w for w in words[1:] if w not in _DIRECTIONALS]
    return (words[0].lstrip("0") or "0", rest[0]) if rest else None


def civilview_rows(body: str) -> list[dict]:
    """Sale rows of a CivilView county list, keyed by the column headers of the table's header row."""
    headers: list[str] = []
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S):
        ths = re.findall(r"<th(?:\s[^>]*)?>(.*?)</th>", tr, re.S)
        if ths:
            headers = [_cell(h).lower() for h in ths]
            continue
        cells = [_cell(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if not cells or len(cells) != len(headers):
            continue
        rows.append(dict(zip(headers, cells)))
    return rows


def _col(row: dict, *names: str) -> str:
    for k, v in row.items():
        if any(n in k for n in names):
            return v
    return ""


def _sheriff_sales(src: SourceDef, key: str, t: Target) -> dict:
    from app.services.scraping_agent import RobotsCache
    loc = census_county(t)
    if not loc:
        return {"facts": [], "note": "The U.S. Census geocoder couldn't match this address to a county."}
    county, state = loc
    robots = RobotsCache()
    lists = civilview_counties(robots).get(f"{county.lower()}, {state.lower()}")
    if not lists:
        return {"facts": [], "note": f"{county}, {state} doesn't publish sheriff sales on CivilView."}
    want = _street_key(t.street)
    if not want:
        return {"facts": [], "note": "Street address has no house number to match."}
    town = (_norm(t.city).split() or [""])[0]
    facts = []
    for i, (cid, label) in enumerate(lists):
        if i:
            time.sleep(robots.delay(CIVILVIEW + "/"))
        for row in civilview_rows(_cv_fetch(f"/Sales/SalesSearch?countyId={cid}", robots)):
            if _street_key(_col(row, "address")) != want:
                continue
            m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", _col(row, "sale"))
            when = f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else _col(row, "sale")
            parts = [f"Sheriff sale {when}", _col(row, "sheriff", "case"), _col(row, "status"),
                     " v. ".join(x for x in (_col(row, "plaintiff"), _col(row, "defendant")) if x),
                     f"listed address: {_col(row, 'address')}"]
            listed = _norm(_col(row, "address"))
            same_town = not town or bool(re.search(rf"\b{re.escape(town)}\b", listed))
            facts.append(_fact("distress", "sheriff_sale", " · ".join(p for p in parts if p)[:400], src=src.id,
                               source_name=f"{label} sheriff sales (CivilView)",
                               source_url=f"{CIVILVIEW}/Sales/SalesSearch?countyId={cid}",
                               source_kind="court", as_of=when if m else None,
                               check="confirmed" if same_town else "unchecked", official=True))
            facts[-1]["check_note"] = ("read directly from the county's official sheriff sale list" if same_town else
                                       "same street address listed under another town name — confirm it's this property")
    note = None if facts else f"Not on the {county}, {state} sheriff sale list ({len(lists)} list{'s' if len(lists) > 1 else ''} checked)."
    return {"facts": facts, "note": note}


API_ADAPTERS: dict[str, Callable[[SourceDef, str, Target], Any]] = {
    "sheriff_sales": _sheriff_sales,
    "attom": _attom, "regrid": _regrid, "batchdata": _batchdata, "propertyradar": _propertyradar,
    "realestateapi": _realestateapi, "realie": _realie,
}


# ── Grok web research ───────────────────────────────────────────────────────

_GROK_FIELDS = """ownership: owner_name, owner_mailing_address, owner_occupied, parcel_id, legal_description
property: property_type, year_built, living_sqft, lot_size, beds, baths
value_tax: assessed_value, land_value, improvement_value, market_value, tax_year, annual_tax, tax_status, taxes_owed, exemptions
sales: last_sale_date, last_sale_price, sale_record (one fact per recorded sale/deed: date, price, grantor → grantee, deed book/page or instrument)
distress: foreclosure_notice, sheriff_sale, tax_sale, tax_lien, lis_pendens, code_violation, probate, bankruptcy, vacancy
listing: listing_status, list_price, listing_date, days_on_market, estimated_value (portal estimate such as Zestimate / Redfin Estimate), listing_agent"""

GROK_INSTRUCTIONS = f"""You research ONE U.S. residential property for a real-estate acquisitions team, using web search and by opening pages.

Look for: the county appraisal district / assessor / tax-assessment record (owner of record, assessed and market value, year built, living area, lot size), property-tax status and any delinquency, recorded sales/deeds, foreclosure / sheriff-sale / tax-sale / lis pendens notices, code violations, current or past listings (Zillow, Redfin, Realtor.com, HAR and similar public pages), and probate signals for the owner.

Rules — data validity matters more than coverage:
- Report only what a page you actually found states. Never infer, estimate, compute, or fill from memory.
- Every fact needs source_url: the exact page where the value appears (not a search page or a home page), and quote: the verbatim text from that page that contains the value (max 200 characters).
- The page must be about this exact property (same house number, street and city). Ignore neighbours, other units, and same-name streets elsewhere.
- Prefer official sources (appraisal district, assessor, tax collector, county clerk/recorder, courts, sheriff) over aggregators; when both are available report both. Open the county appraisal district / assessor property page itself (search it by address, then by the parcel / account number an aggregator shows) rather than relying on portal summaries.
- If a value is redacted on the source (e.g. New Jersey Daniel's Law hides owner names), report that as the value with its source.
- Money as it appears on the page (e.g. "$185,000"). Dates as YYYY-MM-DD when the page gives a full date.
- One fact per field per source; a sale_record fact per recorded sale.
- The owner name we were given is only a lead for probate / court searches — report an owner only when a page states it.
- Never put absence in facts (no "none found" / "not listed" values). In "checked", list what you searched for and did not find (e.g. "foreclosure notices", "code violations") — absence is not proof.

Field names by category (use these; use a short snake_case name only when none fits):
{_GROK_FIELDS}

source_kind: assessor (appraisal district / assessor / tax assessment), tax_collector, recorder (county clerk / deeds), court (incl. sheriff and probate), government (other official), listing (MLS portals: Zillow, Redfin, Realtor.com, HAR…), aggregator (third-party records sites), news, other."""

GROK_SCHEMA = {
    "type": "object",
    "properties": {
        "matched_address": {"type": ["string", "null"], "description": "Address as written on the best source, or null if not found"},
        "facts": {"type": "array", "items": {"type": "object", "properties": {
            "category": {"type": "string", "enum": [c for c, _ in CATEGORIES]},
            "field": {"type": "string"},
            "value": {"type": "string"},
            "as_of": {"type": ["string", "null"], "description": "Date or year the value applies to, if stated"},
            "source_url": {"type": "string"},
            "source_name": {"type": "string"},
            "source_kind": {"type": "string", "enum": ["assessor", "tax_collector", "recorder", "court", "government",
                                                       "listing", "aggregator", "news", "other"]},
            "quote": {"type": "string"},
        }, "required": ["category", "field", "value", "as_of", "source_url", "source_name", "source_kind", "quote"],
            "additionalProperties": False}},
        "checked": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string", "description": "2-3 sentences for an acquisitions rep: ownership, value, distress, listing"},
    },
    "required": ["matched_address", "facts", "checked", "summary"],
    "additionalProperties": False,
}

_ABSENCE_RE = re.compile(r"^\s*(none|n/?a|unknown|not (found|listed|available|specified|shown|stated)|no (record|data|information|results?)[^,;]*)\s*\.?\s*$", re.I)


def _url_key(url: str) -> tuple[str, str, str]:
    p = urlsplit((url or "").strip())
    host = (p.hostname or "").lower().removeprefix("www.")
    return host, (p.path or "/").rstrip("/").lower() or "/", p.query


def is_official(url: Optional[str]) -> bool:
    host = urlsplit(url or "").hostname or ""
    host = host.lower().removeprefix("www.")
    return host in _OFFICIAL_HOSTS or bool(_OFFICIAL_RE.search(host))


def _touched_urls(resp: dict) -> set[tuple[str, str, str]]:
    """Every URL the search tool returned or opened, plus the model's url_citation annotations."""
    urls: set[str] = set()
    for item in resp.get("output") or []:
        if item.get("type") == "web_search_call":
            action = item.get("action") or {}
            if action.get("url"):
                urls.add(action["url"])
            for s in action.get("sources") or []:
                if s.get("url"):
                    urls.add(s["url"])
        elif item.get("type") == "message":
            for c in item.get("content") or []:
                for a in c.get("annotations") or []:
                    if a.get("url"):
                        urls.add(a["url"])
    return {_url_key(u) for u in urls}


def _was_touched(url: str, touched: set[tuple[str, str, str]]) -> bool:
    host, path, query = _url_key(url)
    for h, p, q in touched:
        if h == host and p == path and (q == query or not q or not query):
            return True
    return False


def _message_text(resp: dict) -> str:
    for item in resp.get("output") or []:
        if item.get("type") == "message":
            for c in item.get("content") or []:
                if c.get("type") == "output_text" and c.get("text"):
                    return c["text"]
    return ""


def _clean_field(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")[:40]


def _xai_error(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        msg = body.get("error") if isinstance(body.get("error"), str) else _pick(body, "error.message") or body.get("code")
    except ValueError:
        msg = resp.text[:200]
    return f"xAI HTTP {resp.status_code}: {msg or resp.reason_phrase}"


# Two focused calls run in parallel: one pass covering everything let distress notices slip
# (a live NJ sheriff sale was missed in testing), so records and signals each get a full search budget.
GROK_FOCUS = {
    "records": "Focus of this pass: the official record — the appraisal district / assessor property page (owner of "
               "record, mailing address, parcel / account number, values, exemptions, year built, living area, lot), the "
               "tax collector (tax amount, paid or delinquent, amount owed) and recorded sales / deeds. Report categories "
               "ownership, property, value_tax and sales. Summary: one or two sentences on these records only — do not "
               "mention distress, foreclosure or listings at all (another pass covers them).",
    "signals": "Focus of this pass: distress and market signals — foreclosure filings and notices of sale, sheriff's sale "
               "lists (many NJ and PA counties publish theirs on salesweb.civilview.com; search the address there and on "
               "the county sheriff's site), tax sales / tax lien certificates, lis pendens, code-enforcement cases, "
               "probate or estate filings and obituaries tied to the owner, bankruptcy, vacancy; and current or past "
               "listings and price history on Zillow, Redfin, Realtor.com, HAR and similar portals. Report categories "
               "distress and listing (a sales fact only when a notice states a sale). Summary: one or two sentences on "
               "these signals only (say plainly if none were found).",
}


def _grok_call(key: str, model: str, t: Target, focus: str) -> tuple[dict, dict]:
    """One Responses API call with web_search → (parsed JSON output, raw response)."""
    user = f"Property: {t.address}" + (f"\nOwner on our list: {t.owner}" if t.owner else "")
    body = {
        "model": model,
        "instructions": GROK_INSTRUCTIONS + "\n\n" + GROK_FOCUS[focus],
        "input": [{"role": "user", "content": user}],
        "tools": [{"type": "web_search"}],
        "max_tool_calls": GROK_MAX_TOOL_CALLS,
        "store": False,
        "text": {"format": {"type": "json_schema", "name": "property_research", "schema": GROK_SCHEMA, "strict": True}},
    }
    try:
        r = httpx.post(f"{XAI_BASE}/responses", json=body, timeout=GROK_TIMEOUT,
                       headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    except httpx.TimeoutException as exc:
        raise ScanError(f"Grok {focus} research timed out after {GROK_TIMEOUT:.0f}s ({model}).") from exc
    except httpx.HTTPError as exc:
        raise ScanError(f"xAI unreachable: {type(exc).__name__}") from exc
    if r.status_code >= 400:
        raise ScanError(_xai_error(r))
    resp = r.json()
    if resp.get("status") != "completed":
        raise ScanError(f"Grok {focus} research did not complete ({resp.get('status')}: "
                        f"{resp.get('incomplete_details') or resp.get('error')})")
    try:
        return json.loads(_message_text(resp)), resp
    except ValueError as exc:
        raise ScanError(f"Grok {focus} research returned no structured result.") from exc


def grok_research(key: str, model: str, t: Target) -> dict:
    """Records + signals research in parallel; facts validated against the URLs the search tool
    actually returned/opened, then re-checked on the cited pages. Raises ScanError if both passes fail."""
    with ThreadPoolExecutor(max_workers=len(GROK_FOCUS)) as ex:
        futures = {focus: ex.submit(_grok_call, key, model, t, focus) for focus in GROK_FOCUS}
    facts: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    dropped = {"no_source": 0, "unvisited_source": 0, "absence": 0, "bad_field": 0}
    summaries, checked, errors = [], [], []
    cost, searches, tok_in, tok_out, model_used, matched = 0.0, 0, 0, 0, model, None
    for focus, fut in futures.items():
        try:
            out, resp = fut.result()
        except ScanError as exc:
            errors.append(str(exc))
            continue
        usage = resp.get("usage") or {}
        cost += (usage.get("cost_in_usd_ticks") or 0) / 1e10
        searches += _pick(usage, "server_side_tool_usage_details.web_search_calls") or 0
        tok_in += usage.get("input_tokens") or 0
        tok_out += usage.get("output_tokens") or 0
        model_used = resp.get("model") or model_used
        matched = matched or out.get("matched_address")
        if (out.get("summary") or "").strip():
            summaries.append(out["summary"].strip())
        checked += [str(c) for c in out.get("checked") or [] if str(c) not in checked]
        touched = _touched_urls(resp)
        for f in out.get("facts") or []:
            if not isinstance(f, dict):
                continue
            value = " ".join(str(f.get("value") or "").split())
            url = (f.get("source_url") or "").strip()
            category = f.get("category")
            fld = _clean_field(f.get("field") or "")
            if category not in CATEGORY_IDS or not fld or not value:
                dropped["bad_field"] += 1
            elif not url.lower().startswith(("http://", "https://")):
                dropped["no_source"] += 1
            elif _ABSENCE_RE.match(value):
                dropped["absence"] += 1
            elif not _was_touched(url, touched):
                dropped["unvisited_source"] += 1
            elif (fld, _norm(value), _url_key(url)[0]) not in seen:  # same value from the same site = one fact
                seen.add((fld, _norm(value), _url_key(url)[0]))
                facts.append(_fact(category, fld, value[:300], src="grok",
                                   source_name=(f.get("source_name") or "")[:120] or urlsplit(url).hostname,
                                   source_url=url, source_kind=f.get("source_kind") or "other", as_of=f.get("as_of"),
                                   quote=(f.get("quote") or "")[:300] or None, check="unchecked", official=is_official(url)))
    if len(errors) == len(GROK_FOCUS):
        raise ScanError("; ".join(errors))
    verify_on_pages(facts, t)
    return {
        "facts": facts, "summary": " ".join(summaries), "checked": checked[:30], "matched_address": matched,
        "dropped": {k: v for k, v in dropped.items() if v}, "partial_error": "; ".join(errors) or None,
        "model": model_used, "cost_usd": round(cost, 4), "web_searches": searches,
        "tokens": {"input": tok_in, "output": tok_out},
    }


# ── Page check: is the cited value actually on the cited page? ──────────────

def _norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"(?<=\d),(?=\d{3})", "", s)
    s = re.sub(r"[^a-z0-9.$/\- ]+", " ", s)
    return " ".join(s.split())


_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_STOP = {"and", "the", "for", "with", "from", "per", "sqft", "acres", "sq", "ft", "page", "book", "instrument",
         "recorded", "deed", "sale", "sold", "price", "value", "year", "annual", "tax", "taxes", "owner"}


def _date_variants(y: str, m: str, d: str) -> list[str]:
    mi, di = int(m), int(d)
    mon = _MONTHS[mi - 1] if 1 <= mi <= 12 else ""
    return [f"{y}-{m}-{d}", f"{m}/{d}/{y}", f"{mi}/{di}/{y}", f"{mon} {di} {y}", f"{mon} {d} {y}", f"{di} {mon} {y}"]


_WINDOW = 600  # chars: every part of a value must appear this close together (list pages hold many records)


def _patterns(value: str) -> Optional[list[str]]:
    """Regexes that must all match near each other for ``value`` to count as on the page; None if untestable."""
    pats = ["|".join(re.escape(v) for v in _date_variants(y, m, d)) for y, m, d in _ISO_DATE.findall(value)]
    rest = _norm(_ISO_DATE.sub(" ", value))
    numbers = [n.rstrip(".") for n in re.findall(r"\d+(?:\.\d+)?", rest)]
    numbers = [n[:-3] if n.endswith(".00") else n for n in numbers]
    pats += [rf"(?<![\d.])0*{re.escape(n.lstrip('0') or '0')}(?!\d)" if "." not in n else rf"(?<![\d.]){re.escape(n)}(?!\d)"
             for n in dict.fromkeys(numbers)]
    if not numbers:
        words = [w for w in re.findall(r"[a-z]{3,}", rest) if w not in _STOP]
        pats += [re.escape(w) for w in dict.fromkeys(words)][:6]
    return pats or None


def _near(page: str, pats: list[str], need: int) -> bool:
    """True when at least ``need`` of ``pats`` (always the first) match within one window of the page."""
    for m in re.finditer(pats[0], page):
        lo, hi = max(0, m.start() - _WINDOW), m.end() + _WINDOW
        chunk = page[lo:hi]
        if 1 + sum(1 for p in pats[1:] if re.search(p, chunk)) >= need:
            return True
    return False


def address_patterns(t: Target) -> list[str]:
    """House number + first street word, e.g. ['(?<!\\d)128(?!\\d)', 'oak']."""
    m = re.match(r"\s*(\d+)\s+(\S+)", _norm(t.street))
    return [rf"(?<![\d.]){m.group(1)}(?!\d)", re.escape(m.group(2))] if m else []


def value_on_page(fact: dict, page: str, addr: Optional[list[str]] = None) -> bool:
    """True when the verbatim quote is on the page, or every number/date (else ~2/3 of the words) of the
    value appears within one window. Distress facts (usually from multi-record lists) also need the
    property's house number + street in that window."""
    quote = _norm(fact.get("quote") or "")
    if len(quote) >= 12 and quote in page and not (fact["category"] == "distress" and addr):
        return True
    pats = _patterns(fact["value"])
    if not pats:
        return False
    has_numbers = any(p.startswith("(?<!") for p in pats) or bool(_ISO_DATE.search(fact["value"]))
    need = len(pats) if has_numbers else max(1, round(len(pats) * 2 / 3))
    if fact["category"] == "distress" and addr:
        return _near(page, pats + addr, need + len(addr)) or _near(page, addr + pats, need + len(addr))
    return _near(page, pats, need) or (not has_numbers and _near(page, pats[1:] + pats[:1], need))


def _page_text(body: str) -> str:
    """All text of a page, script/noscript content included (many record sites print values
    from inline scripts or embed them as JSON), normalized like the facts."""
    body = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
    return _norm(html.unescape(re.sub(r"<[^>]+>", " ", body)))


def _read_pages(urls: list[str]) -> dict[str, tuple[Optional[str], Optional[str]]]:
    """url → (normalized page text, None) or (None, reason). Same host: sequential with crawl delay."""
    from app.services.scraping_agent import RobotsCache, ScrapingError, fetch_page
    robots = RobotsCache()
    out: dict[str, tuple[Optional[str], Optional[str]]] = {}
    for i, url in enumerate(urls):
        if i:
            try:
                time.sleep(robots.delay(url))
            except Exception:  # robots lookup failure is handled by fetch_page below
                time.sleep(1.0)
        try:
            text = _page_text(fetch_page(url, robots).body)
            out[url] = (text, None) if len(text) >= 200 else (None, "page has almost no text (blocked or built by JavaScript)")
        except ScrapingError as exc:
            out[url] = (None, str(exc))
        except Exception as exc:  # never let a page check fail the scan
            out[url] = (None, f"{type(exc).__name__}")
    return out


def verify_on_pages(facts: list[dict], t: Optional[Target] = None) -> None:
    """Set each Grok fact's ``check``: confirmed | not_found | unchecked (+ ``check_note``).
    A page that doesn't show the property's house number + street (bot-check page, different
    property) can't confirm or refute anything → unchecked."""
    addr = address_patterns(t) if t else []
    by_host: dict[str, list[str]] = {}
    for f in facts:
        url = f["source_url"]
        host = urlsplit(url).hostname or ""
        lst = by_host.setdefault(host, [])
        if url not in lst and len(lst) < VERIFY_MAX_PER_HOST:
            lst.append(url)
    # Cap total pages, keeping official sites first.
    hosts = sorted(by_host, key=lambda h: (not is_official("https://" + h), h))
    budget, plan = VERIFY_MAX_PAGES, []
    for h in hosts:
        take = by_host[h][:budget]
        if take:
            plan.append(take)
            budget -= len(take)
    pages: dict[str, tuple[Optional[str], Optional[str]]] = {}
    if plan:
        with ThreadPoolExecutor(max_workers=min(6, len(plan))) as ex:
            for res in ex.map(_read_pages, plan):
                pages.update(res)
    for f in facts:
        text, reason = pages.get(f["source_url"], (None, "not re-read (page limit)"))
        if text is None:
            f["check"], f["check_note"] = "unchecked", reason
        elif addr and not _near(text, addr, len(addr)):
            f["check"], f["check_note"] = "unchecked", "the page we received doesn't show this address (bot check or a different page)"
        elif value_on_page(f, text, addr):
            f["check"], f["check_note"] = "confirmed", "value found on the cited page"
        else:
            f["check"], f["check_note"] = "not_found", "value not found in the page text we could read"


# ── RentCast → facts ────────────────────────────────────────────────────────

def rentcast_facts(raw: dict) -> list[dict]:
    src = CATALOG_BY_ID["rentcast"]
    url = "https://www.rentcast.io/api"
    facts: list[dict] = []
    p = raw.get("property") or {}
    if p:
        owner = p.get("owner") or {}
        taxes = p.get("propertyTaxes") or {}
        assess = p.get("taxAssessments") or {}
        ty = max(taxes, key=lambda y: int(y)) if taxes else None
        ay = max(assess, key=lambda y: int(y)) if assess else None
        rows: list[Row] = [
            ("ownership", "owner_name", lambda r: "; ".join(owner.get("names") or []) or None, _plain, None),
            ("ownership", "owner_mailing_address", lambda r: _pick(owner, "mailingAddress.formattedAddress"), _plain, None),
            ("ownership", "owner_type", lambda r: owner.get("type"), _plain, None),
            ("ownership", "owner_occupied", "ownerOccupied", _plain, None),
            ("ownership", "parcel_id", "assessorID", _plain, None),
            ("ownership", "legal_description", "legalDescription", _plain, None),
            ("property", "property_type", "propertyType", _plain, None),
            ("property", "year_built", "yearBuilt", _plain, None),
            ("property", "living_sqft", "squareFootage", _int_str, None),
            ("property", "lot_size", lambda r: f"{_int_str(r.get('lotSize'))} sqft" if r.get("lotSize") else None, _plain, None),
            ("property", "beds", "bedrooms", _plain, None),
            ("property", "baths", "bathrooms", _plain, None),
            ("sales", "last_sale_date", "lastSaleDate", _date, None),
            ("sales", "last_sale_price", "lastSalePrice", _money, "lastSaleDate"),
        ]
        facts += _map_facts(p, rows, src=src, source_url=url)
        if ay is not None and _pick(assess, f"{ay}.value") is not None:
            facts.append(_fact("value_tax", "assessed_value", _money(assess[ay]["value"]), src="rentcast", source_name=src.name,
                               source_url=url, source_kind="data_provider", as_of=ay))
        if ty is not None and _pick(taxes, f"{ty}.total") is not None:
            facts.append(_fact("value_tax", "annual_tax", _money(taxes[ty]["total"]), src="rentcast", source_name=src.name,
                               source_url=url, source_kind="data_provider", as_of=ty))
        for ev in sorted((p.get("history") or {}).values(), key=lambda e: str(e.get("date") or ""), reverse=True):
            if ev.get("event") == "Sale":
                val = " · ".join(x for x in (_date(ev.get("date")), _money(ev.get("price"))) if x)
                if val:
                    facts.append(_fact("sales", "sale_record", val, src="rentcast", source_name=src.name, source_url=url,
                                       source_kind="data_provider"))
    avm = raw.get("avm") or {}
    if avm.get("price"):
        rng = f" (range {_money(avm.get('priceRangeLow'))} – {_money(avm.get('priceRangeHigh'))})" if avm.get("priceRangeLow") else ""
        facts.append(_fact("listing", "estimated_value", f"{_money(avm['price'])}{rng}", src="rentcast", source_name="RentCast AVM",
                           source_url=url, source_kind="data_provider"))
    x = raw.get("listing") or {}
    if x.get("status"):
        val = " · ".join(v for v in (x.get("status"), _money(x.get("price")),
                                     f"{x['daysOnMarket']} days" if x.get("daysOnMarket") is not None else None,
                                     " #".join(str(v) for v in (x.get("mlsName"), x.get("mlsNumber")) if v) or None) if v)
        facts.append(_fact("listing", "listing_status", val, src="rentcast", source_name="RentCast MLS listings",
                           source_url=url, source_kind="data_provider", as_of=_date(x.get("listedDate"))))
    return facts


# ── Merge / findings view ───────────────────────────────────────────────────

def _year(as_of: Optional[str]) -> Optional[str]:
    m = re.search(r"\b(19|20)\d{2}\b", as_of or "")
    return m.group(0) if m else None


_NUMERIC_FIELDS = {"year_built", "living_sqft", "lot_size", "beds", "baths", "assessed_value", "land_value",
                   "improvement_value", "market_value", "tax_year", "annual_tax", "taxes_owed", "last_sale_price"}
_NAME_NOISE = {"and", "the", "of", "etal", "et", "al", "jr", "sr", "llc", "inc", "trust", "trustee"}


def _lot_sqft(v: str) -> Optional[float]:
    """Lot size in sqft from "6,250 sqft (0.1435 acres)", "0.2283 acres", "6250" or "0.13" (small bare numbers are acres)."""
    low = v.lower()
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(sq|square|ft|sf\b)", low)
    if m:
        return float(m.group(1).replace(",", ""))
    n = _num(v)
    if n is None:
        return None
    return n * 43560 if "acre" in low or n < 100 else n


def _comparable(fact: dict) -> Any:
    """A value normalized for comparison: float for numeric fields, ISO date, else a set of words."""
    v = fact["value"]
    fld = fact["field"]
    if fld == "lot_size":
        n = _lot_sqft(v)
        if n is not None:
            return n
    elif fld == "parcel_id":  # "Block 229, Lot 1" vs "0414/229/1": compare digit groups
        return frozenset(g.lstrip("0") or "0" for g in re.findall(r"\d+", v))
    elif fld in _NUMERIC_FIELDS:
        n = _num(v)
        if n is not None:
            return n
    d = _ISO_DATE.search(v)
    if d:
        return d.group(0)
    return frozenset(w for w in re.findall(r"[a-z0-9]+", v.lower()) if w not in _NAME_NOISE)


def _same(a: Any, b: Any, fld: str) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        # Lots: 1%, or ~0.006 acre — portals round acres to 2 decimals (0.14 ac vs 6,250 sqft).
        tol = max(0.01 * max(abs(a), abs(b)), 261.0) if fld == "lot_size" else 0.5
        return abs(a - b) <= tol
    if isinstance(a, frozenset) and isinstance(b, frozenset):
        # Same words in another order / one source adds words (e.g. "Ln" vs "Lane" aside).
        return a <= b or b <= a or len(a & b) >= 0.6 * max(len(a), len(b))
    return a == b


# Only facts that describe one current state can conflict (listings, notices and sales are histories).
_CONFLICT_CATEGORIES = {"ownership", "property", "value_tax", "sales"}
_NO_CONFLICT_FIELDS = MULTI_FIELDS | {"property_type", "owner_type", "legal_description", "tax_status"}


def _conflict(values: list[dict], fld: str) -> bool:
    buckets: dict[Optional[str], list[Any]] = {}
    for f in values:
        buckets.setdefault(_year(f.get("as_of")), []).append(_comparable(f))
    undated = buckets.pop(None, [])
    groups = [vals + undated for vals in buckets.values()] or [undated]
    return any(not _same(a, b, fld) for g in groups for i, a in enumerate(g) for b in g[i + 1:])


def findings(facts: list[dict]) -> list[dict]:
    """Facts grouped by category → field; values from several sources side by side, conflicts flagged."""
    order = {f: i for i, f in enumerate(FIELD_LABELS)}
    groups = []
    for cat, cat_label in CATEGORIES:
        fields: dict[str, dict] = {}
        for f in facts:
            if f["category"] == cat:
                entry = fields.setdefault(f["field"], {"field": f["field"], "label": f["label"], "values": [], "conflict": False})
                entry["values"].append(f)
        for entry in fields.values():
            if cat in _CONFLICT_CATEGORIES and entry["field"] not in _NO_CONFLICT_FIELDS and len(entry["values"]) > 1:
                entry["conflict"] = _conflict(entry["values"], entry["field"])
        if fields:
            ordered = sorted(fields.values(), key=lambda e: (order.get(e["field"], 999), e["field"]))
            groups.append({"category": cat, "label": cat_label, "fields": ordered,
                           "conflicts": sum(1 for e in ordered if e["conflict"])})
    return groups


def _authoritative(f: dict) -> bool:
    return f["source"] == "rentcast" or (f["source"] == "grok" and f.get("official") and f.get("check") == "confirmed")


def apply_to_lead(lead: Lead, facts: list[dict]) -> list[dict]:
    """Fill EMPTY lead fields from authoritative facts; skip a field when authoritative values disagree."""
    applied = []
    for fld, (attr, cast) in LEAD_FILL.items():
        if getattr(lead, attr) not in (None, ""):
            continue
        cands = [f for f in facts if f["field"] == fld and _authoritative(f) and _num(f["value"]) is not None]
        values = {_num(f["value"]) for f in cands}
        if len(values) != 1:
            continue
        val = cast(values.pop())
        setattr(lead, attr, val)
        applied.append({"field": attr, "value": val, "source": cands[0]["source_name"], "source_url": cands[0]["source_url"]})
    return applied


# ── Run ─────────────────────────────────────────────────────────────────────

def _target(lead: Lead) -> Target:
    rec = lead.property_record
    return Target(
        address=full_address(lead), street=(lead.property_address or "").strip(), city=(lead.property_city or "").strip(),
        state=(lead.property_state or "").strip().upper(), zip=(lead.property_zip or "").strip(),
        county=((rec.county if rec else None) or "").strip(), owner=(lead.owner_name or "").strip(),
    )


def start_scan(db: Session, lead: Lead, user_id: Optional[str] = None) -> PropertyScan:
    """Create the scan row (status running). Raises ScanError when no source is ready."""
    if not [s for s in source_states(db) if s["ready"]]:
        raise ScanError("No property data source is ready — enable one and add its key in Settings → "
                        "Calling & API Keys → Property data sources (Grok needs the xAI key).")
    scan = PropertyScan(lead_id=lead.id, status="running", address=full_address(lead), created_by=user_id)
    db.add(scan)
    db.flush()
    return scan


def run_scan(db: Session, scan: PropertyScan) -> PropertyScan:
    """Run every ready source for the scan's lead (in parallel), store sourced facts and the outcome."""
    started = time.monotonic()
    lead = db.get(Lead, scan.lead_id)
    t = _target(lead)
    states = {s["id"]: s for s in source_states(db)}
    runs: list[dict] = []
    facts: list[dict] = []
    summary = None

    def timed(fn: Callable[[], Any]) -> tuple[Any, Optional[str], int]:
        t0 = time.monotonic()
        try:
            return fn(), None, int((time.monotonic() - t0) * 1000)
        except ScanError as exc:
            return None, str(exc), int((time.monotonic() - t0) * 1000)
        except Exception as exc:  # a broken adapter must not sink the other sources
            return None, f"{type(exc).__name__}: {exc}", int((time.monotonic() - t0) * 1000)

    jobs: dict[str, Callable[[], Any]] = {}
    for sid, st in states.items():
        if not st["enabled"] or sid == "rentcast":
            continue
        src = CATALOG_BY_ID[sid]
        if st["needs_key"] and not st["key_set"]:
            runs.append({"id": sid, "name": src.name, "status": "skipped", "note": "On, but no API key set.", "facts": 0})
            continue
        key = _key(db, src)
        if sid == "grok":
            model = scan_model(db)
            jobs[sid] = lambda key=key, model=model: grok_research(key, model, t)
        elif sid in API_ADAPTERS:
            jobs[sid] = lambda src=src, key=key, fn=API_ADAPTERS[sid]: fn(src, key, t)

    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as ex:
        futures = {sid: ex.submit(timed, fn) for sid, fn in jobs.items()}
        for sid, fut in futures.items():
            src = CATALOG_BY_ID[sid]
            got, err, ms = fut.result()
            run = {"id": sid, "name": src.name, "latency_ms": ms, "error": err}
            note = None
            if sid == "grok" and got:
                summary = got["summary"] or None
                run.update({k: got[k] for k in ("model", "cost_usd", "web_searches", "tokens", "checked", "dropped", "matched_address")})
                run["error"] = got["partial_error"]
                got = got["facts"]
            elif isinstance(got, dict):  # adapters may explain an empty result
                note, got = got.get("note"), got.get("facts")
            got = got or []
            facts += got
            run.update(status="error" if err else ("ok" if got else "empty"), facts=len(got))
            if not err and not got:
                run["note"] = note or "No match for this address."
            runs.append(run)

    # RentCast writes through the DB session (budget counter, PropertyRecord), so it runs on this
    # thread AFTER the slow sources: the write transaction stays seconds long, not minutes.
    if states["rentcast"]["enabled"]:
        if not states["rentcast"]["key_set"]:
            runs.append({"id": "rentcast", "name": "RentCast", "status": "skipped", "note": "On, but no RentCast API key set.", "facts": 0})
        else:
            raw: dict = {}

            def rc():
                try:
                    enrich(db, lead, raw)
                except PropertyDataError as exc:
                    if not raw:
                        raise ScanError(str(exc)) from exc
                return rentcast_facts(raw)

            got, err, ms = timed(rc)
            got = got or []
            facts += got
            runs.append({"id": "rentcast", "name": "RentCast", "status": "error" if err else ("ok" if got else "empty"),
                         "error": err, "facts": len(got), "latency_ms": ms,
                         "note": None if got or err else "RentCast has no record for this address."})

    order = [s.id for s in CATALOG]
    runs.sort(key=lambda r: order.index(r["id"]))
    scan.facts = json.dumps(facts)
    scan.sources = json.dumps(runs)
    scan.summary = summary
    scan.cost_usd = round(sum(r.get("cost_usd") or 0 for r in runs), 4)
    scan.applied = json.dumps(apply_to_lead(lead, facts))
    errors = [f"{r['name']}: {r['error']}" for r in runs if r.get("error")]
    ok = [r for r in runs if r["status"] in ("ok", "empty")]
    scan.status = "failed" if not ok else ("partial" if errors else "done")
    scan.error = "; ".join(errors) or None
    scan.duration_ms = int((time.monotonic() - started) * 1000)
    db.flush()
    return scan


def fail_scan(db: Session, scan: PropertyScan, message: str) -> None:
    scan.status = "failed"
    scan.error = message[:2000]
    db.flush()


def serialize(scan: PropertyScan) -> dict:
    facts = json.loads(scan.facts) if scan.facts else []
    status = scan.status
    if status == "running" and scan.created_at and utcnow() - scan.created_at > STALE_RUNNING:
        status = "interrupted"
    return {
        "id": scan.id, "lead_id": scan.lead_id, "created_at": iso_utc(scan.created_at), "status": status,
        "address": scan.address, "summary": scan.summary, "error": scan.error, "cost_usd": scan.cost_usd,
        "duration_ms": scan.duration_ms,
        "sources": json.loads(scan.sources) if scan.sources else [],
        "applied": json.loads(scan.applied) if scan.applied else [],
        "facts": facts, "groups": findings(facts),
    }


def list_scans(db: Session, lead_id: str, limit: int = 10) -> list[PropertyScan]:
    return list(db.execute(select(PropertyScan).where(PropertyScan.lead_id == lead_id)
                           .order_by(PropertyScan.created_at.desc(), PropertyScan.id.desc()).limit(limit)).scalars())


# ── Source tests (Settings → Test) ──────────────────────────────────────────

_PROBE = Target(address="1 Nonexistent Test Road, Nowhere, NJ 08000", street="1 Nonexistent Test Road", city="Nowhere",
                state="NJ", zip="08000", county="", owner="")


def test_source(db: Session, source_id: str) -> dict:
    """Prove a source's key works with its cheapest documented request. Returns {ok, message}."""
    src = CATALOG_BY_ID.get(source_id)
    if src is None:
        raise ScanError(f"Unknown property data source: {source_id!r}")
    if not src.integrated:
        return {"ok": False, "message": f"{src.name} is not integrated: {src.note}"}
    key = _key(db, src)
    if src.key_setting and not key:
        return {"ok": False, "message": "No API key set."}
    try:
        if src.id == "sheriff_sales":
            from app.services.scraping_agent import RobotsCache
            counties = civilview_counties(RobotsCache())
            nj = sum(1 for k in counties if k.endswith(", nj"))
            return {"ok": True, "message": f"CivilView reachable: {len(counties)} counties publish sheriff sales "
                                           f"({nj} in NJ). No key needed."}
        if src.id == "grok":
            r = httpx.get(f"{XAI_BASE}/api-key", headers={"Authorization": f"Bearer {key}"}, timeout=API_TIMEOUT)
            if r.status_code >= 400:
                return {"ok": False, "message": _xai_error(r)}
            info = r.json()
            if info.get("api_key_blocked") or info.get("api_key_disabled") or info.get("team_blocked"):
                return {"ok": False, "message": "xAI key is blocked or disabled."}
            return {"ok": True, "message": f"xAI key valid ({info.get('name') or 'unnamed'}); scan model {scan_model(db)}."}
        if src.id == "rentcast":
            from app.services.property_data import _rentcast
            _rentcast(db, "/properties", {"city": "Austin", "state": "TX", "limit": 1})
            return {"ok": True, "message": "RentCast key works (used 1 call of the monthly budget)."}
        if src.id == "regrid":
            data = _request(src, "GET", "https://app.regrid.com/api/v2/usage", headers={"x-regrid-token": key})
            return {"ok": True, "message": f"Regrid token works. Usage: {json.dumps(data)[:160]}"}
        if src.id == "batchdata":
            data = _request(src, "GET", "https://api.batchdata.com/api/v1/wallet/balance", headers={"Authorization": f"Bearer {key}"})
            bal = _pick(data, "results.balance")
            return {"ok": True, "message": f"BatchData token works. Wallet balance: {bal} {_pick(data, 'results.currency') or ''}".strip()}
        if src.id == "propertyradar":
            _request(src, "GET", "https://api.propertyradar.com/v1/lists", headers={"Authorization": f"Bearer {key}"})
            return {"ok": True, "message": "PropertyRadar token works (lists endpoint, no charge)."}
        # ATTOM, RealEstateAPI, Realie: no test endpoint — a no-match lookup proves auth without a billable result.
        API_ADAPTERS[src.id](src, key, _PROBE)
        return {"ok": True, "message": f"{src.name} accepted the key (test lookup of a non-existent address)."}
    except (ScanError, PropertyDataError) as exc:
        return {"ok": False, "message": str(exc)}
