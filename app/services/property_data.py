"""Property data on every lead: estimated value, tax, MLS listing, debt, photo.

Sources
- RentCast (https://developers.rentcast.io) — public records (owner of record,
  tax assessments + annual property tax, sale history, coordinates), AVM value
  estimate with range, and MLS sale listings (status, price, days on market,
  MLS name/number, listing agent). Every request counts against a monthly
  budget (``rentcast_monthly_limit``; the free plan is 50/month).
- Google Street View Static API — a street-level photo of the house (falls back
  to a satellite map when there's no Street View imagery). Fetched server-side
  so the Maps key never reaches the browser; cached on disk.

Debt: we have no lien data source. What we show is labeled for what it is —
taxes owed from county lists, pre-foreclosure / tax-delinquent list filings,
what the seller said on a call, and an *estimated* mortgage balance from the
last sale (80% LTV, 30-yr fixed at that year's average rate).
"""
from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from sqlalchemy.orm import Session

from app.models import CallOutcome, Lead, ListType, PropertyRecord
from app.timeutil import iso_utc, to_naive_utc, utcnow

RENTCAST_BASE = "https://api.rentcast.io/v1"
MAPS_BASE = "https://maps.googleapis.com/maps/api"
DEFAULT_MONTHLY_LIMIT = 50
PHOTOS_DIR = Path(os.getenv("PHOTOS_DIR", "photos"))
_TIMEOUT = 20.0

# Freddie Mac PMMS 30-year fixed, annual average (%). Used only for the estimated
# mortgage balance; years outside the table use the nearest year.
_RATE_BY_YEAR = {
    1995: 7.93, 1996: 7.81, 1997: 7.60, 1998: 6.94, 1999: 7.44, 2000: 8.05, 2001: 6.97,
    2002: 6.54, 2003: 5.83, 2004: 5.84, 2005: 5.87, 2006: 6.41, 2007: 6.34, 2008: 6.03,
    2009: 5.04, 2010: 4.69, 2011: 4.45, 2012: 3.66, 2013: 3.98, 2014: 4.17, 2015: 3.85,
    2016: 3.65, 2017: 3.99, 2018: 4.54, 2019: 3.94, 2020: 3.11, 2021: 2.96, 2022: 5.34,
    2023: 6.81, 2024: 6.72, 2025: 6.60,
}
LTV = 0.80
TERM_MONTHS = 360


class PropertyDataError(RuntimeError):
    """Provider unconfigured, out of budget, unreachable, or rejected the request."""


# ── RentCast with a monthly budget ──────────────────────────────────────────

def rentcast_key(db: Session) -> str:
    from app.services.settings import get_setting
    return (get_setting(db, "rentcast_api_key") or "").strip()


def monthly_limit(db: Session) -> int:
    from app.services.settings import get_setting_int
    return get_setting_int(db, "rentcast_monthly_limit", DEFAULT_MONTHLY_LIMIT)


def usage(db: Session) -> dict:
    from app.services.settings import get_setting
    month = utcnow().strftime("%Y-%m")
    try:
        data = json.loads(get_setting(db, "rentcast_usage") or "{}")
    except ValueError:
        data = {}
    calls = data.get("calls", 0) if data.get("month") == month else 0
    return {"month": month, "calls": calls, "limit": monthly_limit(db)}


def record_usage(db: Session, n: int = 1) -> None:
    """Count RentCast requests (also called by the scraper)."""
    from app.services.settings import set_setting
    u = usage(db)
    set_setting(db, "rentcast_usage", json.dumps({"month": u["month"], "calls": u["calls"] + n}))
    db.flush()


def _rentcast(db: Session, path: str, params: dict) -> Any:
    key = rentcast_key(db)
    if not key:
        raise PropertyDataError("No RentCast API key — add it in Settings → Property Data.")
    u = usage(db)
    if u["calls"] >= u["limit"]:
        raise PropertyDataError(f"RentCast monthly budget reached ({u['calls']}/{u['limit']} calls in {u['month']}). "
                                "Raise it in Settings → Property Data.")
    try:
        resp = httpx.get(RENTCAST_BASE + path, params=params, timeout=_TIMEOUT,
                         headers={"X-Api-Key": key, "Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise PropertyDataError(f"RentCast unreachable: {exc}") from exc
    record_usage(db)
    if resp.status_code == 404:
        return None
    if resp.status_code in (401, 403):
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if body.get("error") == "billing/subscription-inactive":
            raise PropertyDataError("RentCast subscription is inactive — reactivate it at app.rentcast.io/app/api.")
        raise PropertyDataError(f"RentCast rejected the API key: {body.get('message') or resp.status_code}")
    if resp.status_code == 429:
        raise PropertyDataError("RentCast rate limit / plan quota reached.")
    if resp.status_code >= 400:
        raise PropertyDataError(f"RentCast error {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def full_address(lead: Lead) -> str:
    parts = [lead.property_address]
    city_state = ", ".join(x for x in (lead.property_city, lead.property_state) if x)
    if city_state:
        parts.append(city_state + (f" {lead.property_zip}" if lead.property_zip else ""))
    return ", ".join(parts)


def _ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return to_naive_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


# ── Debt estimate ───────────────────────────────────────────────────────────

def estimate_mortgage(sale_price: Optional[float], sale_date: Optional[datetime], now: Optional[datetime] = None) -> tuple[Optional[float], Optional[str]]:
    """Remaining balance of an assumed purchase loan, or (None, None) without a sale."""
    if not sale_price or not sale_date:
        return None, None
    now = now or utcnow()
    months = (now.year - sale_date.year) * 12 + (now.month - sale_date.month)
    year = min(max(sale_date.year, min(_RATE_BY_YEAR)), max(_RATE_BY_YEAR))
    rate = _RATE_BY_YEAR[year]
    basis = (f"Estimate: {int(LTV * 100)}% loan on the {sale_date.year} sale (${sale_price:,.0f}), "
             f"30-yr fixed at {rate:.2f}% — not a recorded lien")
    if months >= TERM_MONTHS:
        return 0.0, basis + "; term elapsed, likely paid off"
    if months < 0:
        months = 0
    principal = sale_price * LTV
    r = rate / 100 / 12
    growth_n, growth_k = (1 + r) ** TERM_MONTHS, (1 + r) ** months
    balance = principal * (growth_n - growth_k) / (growth_n - 1)
    return round(balance, -2), basis


# ── Enrichment ──────────────────────────────────────────────────────────────

def _record(db: Session, lead: Lead) -> PropertyRecord:
    rec = lead.property_record
    if rec is None:
        rec = PropertyRecord(lead_id=lead.id)
        db.add(rec)
        lead.property_record = rec
    return rec


def apply_property_record(rec: PropertyRecord, r: dict) -> None:
    rec.matched_address = r.get("formattedAddress") or rec.matched_address
    rec.latitude = r.get("latitude") or rec.latitude
    rec.longitude = r.get("longitude") or rec.longitude
    rec.county = r.get("county") or rec.county
    rec.property_type = r.get("propertyType") or rec.property_type
    owner = r.get("owner") or {}
    if owner.get("names"):
        rec.owner_names = json.dumps(owner["names"])
    if r.get("ownerOccupied") is not None:
        rec.owner_occupied = bool(r["ownerOccupied"])
    history: dict[str, dict] = {}
    for y, t in (r.get("propertyTaxes") or {}).items():
        history.setdefault(str(y), {})["tax"] = t.get("total")
    for y, a in (r.get("taxAssessments") or {}).items():
        history.setdefault(str(y), {})["assessed"] = a.get("value")
    if history:
        rec.tax_history = json.dumps(dict(sorted(history.items())))
        taxed = [int(y) for y, v in history.items() if v.get("tax") is not None]
        assessed = [int(y) for y, v in history.items() if v.get("assessed") is not None]
        if taxed:
            rec.tax_year = max(taxed)
            rec.tax_annual = float(history[str(rec.tax_year)]["tax"])
        if assessed:
            rec.tax_assessed_value = float(history[str(max(assessed))]["assessed"])
    if r.get("lastSalePrice"):
        rec.last_sale_price = float(r["lastSalePrice"])
        rec.last_sale_date = _ts(r.get("lastSaleDate"))


def apply_listing(rec: PropertyRecord, x: Optional[dict]) -> None:
    if not x:
        rec.listing_status = None
        rec.listing_price = rec.listing_days_on_market = rec.listing_date = None
        rec.listing_mls = rec.listing_agent = None
        return
    rec.listing_status = x.get("status")
    rec.listing_price = x.get("price")
    rec.listing_days_on_market = x.get("daysOnMarket")
    rec.listing_date = _ts(x.get("listedDate"))
    mls = " #".join(str(v) for v in (x.get("mlsName"), x.get("mlsNumber")) if v)
    rec.listing_mls = mls or None
    agent = x.get("listingAgent") or {}
    office = x.get("listingOffice") or {}
    if agent or office:
        rec.listing_agent = json.dumps({
            "name": agent.get("name"), "phone": agent.get("phone"), "email": agent.get("email"),
            "office": office.get("name"), "office_phone": office.get("phone"),
        })
    rec.latitude = rec.latitude or x.get("latitude")
    rec.longitude = rec.longitude or x.get("longitude")


def enrich(db: Session, lead: Lead, raw: Optional[dict] = None) -> PropertyRecord:
    """Refresh a lead's property data from RentCast (up to 3 requests).

    Partial results are kept: e.g. a property with public records but no AVM
    still gets its tax data. Raises only when nothing could be fetched.
    ``raw`` (optional) receives the provider responses under "property", "avm"
    and "listing" — the property scan turns them into sourced facts.
    """
    raw = {} if raw is None else raw
    rec = _record(db, lead)
    address = full_address(lead)
    errors: list[str] = []
    got_any = False

    try:
        records = _rentcast(db, "/properties", {"address": address})
        if records:
            raw["property"] = records[0] if isinstance(records, list) else records
            apply_property_record(rec, raw["property"])
            got_any = True
    except PropertyDataError as exc:
        errors.append(str(exc))
        if any(s in str(exc) for s in ("budget", "API key", "inactive")):  # the other 2 calls would fail the same way
            rec.error = str(exc)
            db.flush()
            raise

    try:
        avm = _rentcast(db, "/avm/value", {"address": address, "compCount": 5})
        raw["avm"] = avm
        if avm and avm.get("price"):
            rec.est_value = float(avm["price"])
            rec.est_value_low = avm.get("priceRangeLow")
            rec.est_value_high = avm.get("priceRangeHigh")
            sp = avm.get("subjectProperty") or {}
            rec.latitude = rec.latitude or sp.get("latitude")
            rec.longitude = rec.longitude or sp.get("longitude")
            if not rec.last_sale_price and sp.get("lastSalePrice"):
                rec.last_sale_price = float(sp["lastSalePrice"])
                rec.last_sale_date = _ts(sp.get("lastSaleDate"))
            got_any = True
    except PropertyDataError as exc:
        errors.append(str(exc))

    try:
        listings = _rentcast(db, "/listings/sale", {"address": address, "limit": 1})
        raw["listing"] = listings[0] if listings else None
        apply_listing(rec, raw["listing"])
        got_any = got_any or bool(listings)
    except PropertyDataError as exc:
        errors.append(str(exc))

    rec.est_mortgage_balance, rec.debt_basis = estimate_mortgage(rec.last_sale_price, rec.last_sale_date)
    rec.source = "rentcast"
    rec.fetched_at = utcnow()
    rec.error = "; ".join(errors) or (None if got_any else "RentCast has no record for this address.")
    rec.photo_available = None  # coordinates may have changed — re-check imagery
    db.flush()
    if errors and not got_any:
        raise PropertyDataError(errors[0])
    return rec


# ── Refresh: RentCast when active + free public sources ─────────────────────
#
# "Refresh property data" fills EMPTY lead fields only, each from a source recorded with the value
# (a PropertyScan row: the property page shows it under Property research, with links):
#   1. U.S. Census geocoder (free, official): county, coordinates, ZIP of the matched address.
#   2. The county's parcel layer (free ArcGIS REST, appraisal-district data) when we cover the county:
#      owner, mailing address, appraised value, year built, living area; matched by account or address.
#   3. The government sale page in the lead's notes (Treasury, IRS, USDA, FDIC): its own figures.
#   4. Grok research on the official record, for fields still empty (one pass, ~$0.05–0.10).
#   5. RentCast, when a key is set and the subscription is active (after the slow sources, so its
#      budget-counter write doesn't hold a transaction open during the web research).
# Which fact may fill which field: property_scan.LEAD_FILL / fill_tier.

KEY_FIELDS = ("property_zip", "owner_name", "mailing_address", "beds", "baths", "sqft", "year_built", "assessed_value")
_FACT_OF = {"property_zip": "property_zip", "owner_name": "owner_name", "mailing_address": "owner_mailing_address",
            "beds": "beds", "baths": "baths", "sqft": "living_sqft", "year_built": "year_built",
            "assessed_value": "assessed_value"}
_PHYSICAL = ("beds", "baths", "living_sqft", "year_built")
DATA_SOURCE_LABEL = "public records"
_SOURCE_URL_RE = re.compile(r"\(source:\s*(https?://[^\s)]+)\)")
_ACCOUNT_RE = re.compile(r"\bAccount (\d{5,8})\b")
RENTCAST_RETRY_S = 3600.0
_rentcast_down: dict[str, Any] = {}   # {"until": monotonic deadline, "error": message} after an inactive/key error


def missing_fields(lead: Lead) -> list[str]:
    return [c for c in KEY_FIELDS if getattr(lead, c) is None or (isinstance(getattr(lead, c), str) and not getattr(lead, c).strip())]


def _county_key(name: Optional[str]) -> str:
    return re.sub(r"\s+(county|parish|borough)$", "", (name or "").strip().lower())


# County parcel layers (ArcGIS REST, no key). ``snapshot`` = tax year of a statewide snapshot copy; owner /
# mailing / value from a snapshot more than 2 years old aren't used (the owner may have changed since).
@dataclass(frozen=True)
class ParcelLayer:
    county: str
    state: str
    name: str
    url: str                         # layer endpoint …/FeatureServer/0
    situs: str                       # situs street field, e.g. "5555 BONANZA DR"
    city: Optional[str]
    record: Callable[[dict], dict]   # attributes → our fields (see _tad_fields)
    snapshot: Optional[int] = None
    account: Optional[str] = None    # account-number field (lookups by the account on the sale list)


def _s(v: Any) -> Optional[str]:
    s = " ".join(str(v).split()) if v not in (None, "") else ""
    return s or None


def _pos(v: Any) -> Optional[float]:
    try:
        n = float(str(v).replace(",", "")) if v not in (None, "") else None
    except ValueError:
        return None
    return n if n and n > 0 else None


def _tad_fields(a: dict) -> dict:
    from app.services.gov_sources import tad_record
    r = tad_record(a)
    appraised = a.get("APPRAISAL_")
    return {
        "parcel_id": _s(a.get("ACCOUNT")), "owner": r["owner_name"], "mail_street": r["mailing_address"],
        "mail_city": r["mailing_city"], "mail_state": r["mailing_state"], "mail_zip": r["mailing_zip"],
        "situs_zip": r["property_zip"], "assessed": _pos(a.get("APPRAISEDV")), "assessed_label": "APPRAISEDV",
        "as_of": str(datetime.fromtimestamp(appraised / 1000, timezone.utc).year) if isinstance(appraised, (int, float)) else None,
        "year_built": r["year_built"], "sqft": r["sqft"], "beds": r["beds"], "baths": r["baths"],
        "improvement_value": a.get("IMPR_VALUE"),
    }


def _fields(owner, mail_street, mail_city, mail_state, mail_zip, situs_zip, assessed=None, year=None, sqft=None,
            as_of=None, parcel=None, impr=None) -> Callable[[dict], dict]:
    def rec(a: dict) -> dict:
        from app.services.gov_sources import state_code
        y = _pos(a.get(year)) if year else None
        return {"parcel_id": _s(a.get(parcel)) if parcel else None, "owner": _s(a.get(owner)),
                "mail_street": _s(a.get(mail_street)), "mail_city": _s(a.get(mail_city)),
                "mail_state": state_code(_s(a.get(mail_state))), "mail_zip": (_s(a.get(mail_zip)) or "")[:5] or None,
                "situs_zip": (_s(a.get(situs_zip)) or "")[:5] or None,
                "assessed": _pos(a.get(assessed)) if assessed else None, "assessed_label": assessed,
                "as_of": _s(a.get(as_of)) if as_of else None,
                "year_built": int(y) if y and y > 1700 else None, "sqft": int(_pos(a.get(sqft)) or 0) or None if sqft else None,
                "beds": None, "baths": None, "improvement_value": a.get(impr) if impr else None}
    return rec


# Same parcel copies the scraper's source directory uses (Harris's is a 2022 snapshot without building
# data — nothing it could fill — so it isn't listed).
_MIRROR = "https://services6.arcgis.com/2yF1BNcZtu43QAOt/ArcGIS/rest/services/"
PARCEL_LAYERS: tuple[ParcelLayer, ...] = (
    ParcelLayer("tarrant", "TX", "Tarrant Appraisal District parcels (Tarrant County GIS)",
                "https://mapit.tarrantcounty.com/arcgis/rest/services/Dynamic/TADParcels/FeatureServer/0",
                "SITUS_ADDR", "CITY", _tad_fields, account="ACCOUNT"),
    ParcelLayer("dallas", "TX", "Dallas CAD parcels (TxGIO 2024 snapshot)", _MIRROR + "Dallas_County_Parcel/FeatureServer/0",
                "SITUS_ADDR", "SITUS_CITY",
                _fields("OWNER_NAME", "MAIL_LINE1", "MAIL_CITY", "MAIL_STAT", "MAIL_ZIP", "SITUS_ZIP", year="YEAR_BUILT",
                        as_of="TAX_YEAR", parcel="Prop_ID", impr="IMP_VALUE"), snapshot=2024),
    ParcelLayer("maricopa", "AZ", "Maricopa County Assessor parcels (2025 copy)", _MIRROR + "Maricopa_Parcels/FeatureServer/0",
                "Situs_Addr", "Property_C",
                _fields("Owner_Name", "Mailing_Ad", "Mailing__2", "Mailing__3", "Mailing__5", "Property_Z",
                        year="Constructi", sqft="Living_Spa", parcel="Parcel_Num"), snapshot=2025),
    ParcelLayer("mohave", "AZ", "Mohave County Assessor parcels (2025 copy)",
                _MIRROR + "mohavecountyarizonaparcels_ExportFeatures/FeatureServer/0", "SITE_ADDRE", None,
                _fields("OWNER", "MAILING_AD", "CITY", "STATE", "ZIP", "_none_", as_of="TAX_YEAR", parcel="PARCEL"),
                snapshot=2025),
    ParcelLayer("coconino", "AZ", "Coconino County Assessor parcels (2025 copy)",
                _MIRROR + "coconino_county_arizona_parcels/FeatureServer/0", "SITUS", "SITUS_CITY",
                _fields("OWNERNAME", "OWNERADD_1", "OWNERADD_3", "OWNERADD_4", "OWNERADD_5", "SITUS_ZIP", parcel="APN",
                        impr="IMPROVEMEN"), snapshot=2025),
)


def parcel_layer(county: Optional[str], state: Optional[str]) -> Optional[ParcelLayer]:
    key, st = _county_key(county), (state or "").upper()
    return next((p for p in PARCEL_LAYERS if p.county == key and p.state == st), None)


def _arcgis_query(layer: ParcelLayer, **params: Any) -> list[dict]:
    try:
        r = httpx.get(layer.url + "/query", timeout=_TIMEOUT, params={
            "outFields": "*", "returnGeometry": "false", "f": "json", "resultRecordCount": 50, **params})
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise PropertyDataError(f"{layer.name} unreachable: {type(exc).__name__}") from exc
    if data.get("error"):
        raise PropertyDataError(f"{layer.name}: {data['error'].get('message')}")
    return [f.get("attributes") or {} for f in data.get("features") or []]


def _object_id(a: dict) -> Optional[int]:
    v = next((a[k] for k in ("OBJECTID", "objectid", "FID", "ObjectId") if a.get(k) is not None), None)
    return int(v) if isinstance(v, (int, float)) else None


def parcel_facts(layer: ParcelLayer, t, accounts: list[str], point: Optional[tuple[float, float]] = None) -> dict:
    """Facts from the county parcel layer for this property: by account number when the sale list gives
    one; else the parcels within 150 m of the geocoded point (``point`` = lon, lat — spatially indexed, fast
    on large layers), then a situs-address search; either way only parcels whose situs has this house
    number + street (and city) count."""
    from app.services import property_scan as ps
    from urllib.parse import urlencode
    want = ps._street_key(t.street)
    if layer.account and accounts:
        rows = _arcgis_query(layer, where=f"{layer.account} IN (" + ",".join(f"'{a.zfill(8)}'" for a in accounts) + ")")
    else:
        if not want:
            return {"facts": [], "note": "Street address has no house number to match."}

        def same(found: list[dict]) -> list[dict]:
            found = [a for a in found if ps._street_key(str(a.get(layer.situs) or "")) == want]
            town = ps._norm(t.city)
            if layer.city and town and not town.endswith(" county"):
                found = [a for a in found if ps._norm(str(a.get(layer.city) or "")) in ("", town)]
            return found

        rows = same(_arcgis_query(layer, geometry=f"{point[0]},{point[1]}", geometryType="esriGeometryPoint", inSR=4326,
                                  spatialRel="esriSpatialRelIntersects", distance=150, units="esriSRUnit_Meter",
                                  resultRecordCount=300)) if point else []
        if not rows:
            num, word = want
            word = re.sub(r"[^a-z0-9]", "", word).upper()
            rows = same(_arcgis_query(layer, where=f"UPPER({layer.situs}) LIKE '{num} %{word}%'"))
    if not rows:
        return {"facts": [], "note": f"No parcel at this address in {layer.name}."}
    ids = [i for i in map(_object_id, rows) if i is not None]
    link = layer.url + "/query?" + urlencode({"objectIds": ",".join(map(str, ids)), "outFields": "*",
                                              "returnGeometry": "false", "f": "html"})
    stale = bool(layer.snapshot and layer.snapshot < utcnow().year - 2)
    facts: list[dict] = []

    def add(cat: str, fld: str, value: Any, quote: str, as_of: Optional[str] = None, **extra) -> None:
        f = ps._fact(cat, fld, value, src="county_parcels", source_name=layer.name, source_url=link,
                     source_kind="assessor", as_of=as_of, quote=quote, check="api", official=True)
        f.update(extra)
        facts.append(f)

    no_building = 0
    for a in rows:
        r = layer.record(a)
        acct = f"parcel {r['parcel_id']}: " if r.get("parcel_id") else ""
        as_of = r.get("as_of") or (str(layer.snapshot) if layer.snapshot else None)
        if r.get("parcel_id"):
            add("ownership", "parcel_id", r["parcel_id"], f"{acct.rstrip(': ')}")
        if not stale:
            if r.get("owner"):
                add("ownership", "owner_name", r["owner"], f"{acct}owner {r['owner']}", as_of)
            if r.get("mail_street") and r.get("mail_state"):
                parts = {"street": r["mail_street"], "city": r.get("mail_city"), "state": r["mail_state"], "zip": r.get("mail_zip")}
                line = ", ".join(x for x in (parts["street"], parts["city"], " ".join(y for y in (parts["state"], parts["zip"]) if y)) if x)
                add("ownership", "owner_mailing_address", line, f"{acct}mailing {line}", as_of, parts=parts)
            if r.get("assessed"):
                add("value_tax", "assessed_value", f"${r['assessed']:,.0f}", f"{acct}{r['assessed_label']} {r['assessed']:,.0f}", as_of)
        if r.get("situs_zip") and re.fullmatch(r"\d{5}", r["situs_zip"]):
            add("property", "property_zip", r["situs_zip"], f"{acct}situs ZIP {r['situs_zip']}")
        for key, fld in (("year_built", "year_built"), ("sqft", "living_sqft"), ("beds", "beds"), ("baths", "baths")):
            if r.get(key):
                add("property", fld, r[key], f"{acct}{fld} {r[key]}")
        impr = _pos(r.get("improvement_value"))
        if r.get("improvement_value") is not None and not impr and not r.get("sqft"):
            no_building += 1
            add("property", "improvements", "No building on record (improvement value $0)", f"{acct}improvement value 0")
    note = f"{len(rows)} parcel{'s' if len(rows) > 1 else ''} matched"
    if stale:
        note += f"; {layer.snapshot} snapshot — owner, mailing and value not used"
    return {"facts": facts, "note": note, "no_building": no_building == len(rows)}


# The government sale page: its own figures. Known sale sites use their importer's parser; other pages
# a labelled-value read, only when the page shows this address and isn't shared with other properties.
_WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_NUMWORD = r"\d{1,2}(?:\.\d)?|" + "|".join(_WORDNUM)
_SALE_PATTERNS = {
    "beds": [r"\b(?:beds?|bedrooms?)\s*:\s*(\d{1,2})\b", rf"\b({_NUMWORD})[\s-]+bed(?:room)?s?\b"],
    "baths": [r"\b(?:baths?|bathrooms?)\s*:\s*(\d{1,2}(?:\.\d)?)\b", rf"\b({_NUMWORD})[\s-]+(?:full[\s-]+)?bath(?:room)?s?\b"],
    "living_sqft": [r"\b(?:living (?:space|area)|square feet|sq\.? ?ft\.?|heated area|building area)\s*:\s*([\d,]{3,7})\b",
                    r"\b(\d[\d,]{2,6})\s*(?:±\s*|\+/-\s*)?(?:sq\.?\s*ft\.?|square feet)"],
    "year_built": [r"\byear built\s*:?\s*(\d{4})\b", r"\bbuilt in (\d{4})\b"],
}


def sale_page_values(text: str) -> dict[str, tuple[float, str]]:
    """field → (value, quote) for labelled figures stated once on a property's own sale page (ambiguous → left out)."""
    out: dict[str, tuple[float, str]] = {}
    for fld, pats in _SALE_PATTERNS.items():
        found: dict[float, str] = {}
        for pat in pats:
            for m in re.finditer(pat, text, re.I):
                if fld == "living_sqft" and re.search(r"(lot|site|land|acre)[^.]{0,20}$", text[max(0, m.start() - 25):m.start()], re.I):
                    continue
                raw = m.group(1).lower().replace(",", "")
                n = float(_WORDNUM.get(raw, raw)) if raw in _WORDNUM or re.fullmatch(r"\d+(?:\.\d+)?", raw) else None
                if n:
                    found.setdefault(n, " ".join(text[max(0, m.start() - 40):m.end() + 40].split()))
        if fld == "baths" and re.search(r"\bhalf[\s-]+bath", text, re.I) and not re.search(_SALE_PATTERNS["baths"][0], text, re.I):
            continue  # "2 full baths and 1 half bath": the count isn't stated as one number
        if len(found) == 1:
            (n, quote), = found.items()
            out[fld] = (n, quote)
    return out


def sale_page_facts(t, notes: str, shared: set[str]) -> dict:
    from urllib.parse import urlsplit
    from app.services import gov_sources as g
    from app.services import property_scan as ps
    from app.services.scraping_agent import ScrapingError
    urls = list(dict.fromkeys(_SOURCE_URL_RE.findall(notes or "")))[:3]
    if not urls:
        return {"facts": [], "note": "No sale page linked in the property's notes.", "skipped": True}
    want, addr = ps._street_key(t.street), ps.address_patterns(t)
    facts: list[dict] = []
    notes_out: list[str] = []
    errors: list[str] = []

    def add(url: str, name: str, cat: str, fld: str, value: Any, quote: str, as_of: Any = None) -> None:
        facts.append(ps._fact(cat, fld, value, src="sale_page", source_name=name, source_url=url,
                              source_kind="government", as_of=as_of, quote=quote[:300], official=True, check="api"))

    read = 0
    with g.Fetcher() as f:
        for url in urls:
            host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
            path = urlsplit(url).path.lower()
            if host.endswith("tarrantcountytx.gov"):
                notes_out.append(f"{host}: multi-property sale list — its account number is used for the parcel lookup")
                continue
            if path.endswith(".pdf"):
                notes_out.append(f"{host}: PDF not read")
                continue
            try:
                page = f.get(url)
            except ScrapingError as exc:
                errors.append(f"{host}: {str(exc)[:160]}")
                continue
            read += 1
            text = g.html_text(page, join_inline=True)
            vals: dict[str, tuple[Any, str]] = {}
            name = {"treasury.gov": "U.S. Treasury auction page", "irsauctions.gov": "IRS auction notice",
                    "properties.sc.egov.usda.gov": "USDA property listing",
                    "fdicrealestatelistings.com": "FDIC real estate listing"}.get(host, f"Sale page ({host})")
            if host == "treasury.gov" and path.endswith("realprop.shtml"):
                rows = [r for r in g.parse_treasury_list(page, url) if ps._street_key(r.get("property_address") or "") == want]
                if len(rows) != 1:
                    notes_out.append(f"{host}: {'not on' if not rows else 'several entries on'} the auction list")
                    continue
                r = rows[0]
                q = (r.get("description") or "")[:200]
                vals = {k: (r[c], q) for k, c in (("beds", "beds"), ("baths", "baths"), ("living_sqft", "sqft"),
                                                 ("year_built", "year_built")) if r.get(c)}
            else:
                if addr and not ps._near(ps._norm(text), addr, len(addr)):
                    notes_out.append(f"{host}: page doesn't show this address")
                    continue
                if url in shared and host not in ("treasury.gov", "irsauctions.gov", "properties.sc.egov.usda.gov",
                                                  "fdicrealestatelistings.com"):
                    notes_out.append(f"{host}: page lists several properties — not attributed")
                    continue
                vals = dict(sale_page_values(text))
                if host == "treasury.gov":
                    d = g.parse_treasury_detail(page)
                    for k, c, label in (("living_sqft", "sqft", "Living Space"), ("year_built", "year_built", "Year Built")):
                        if d.get(c):
                            vals[k] = (d[c], f"{label}: {d[c]}")
                    if d.get("annual_taxes"):
                        add(url, name, "value_tax", "annual_tax", f"${d['annual_taxes']:,.2f}",
                            f"{d.get('annual_taxes_year', '')} County Taxes: ${d['annual_taxes']:,.2f}", d.get("annual_taxes_year"))
                    if d.get("parcel_number"):
                        add(url, name, "ownership", "parcel_id", d["parcel_number"], f"Parcel No: {d['parcel_number']}")
                elif host == "irsauctions.gov":
                    d = g.parse_irs_detail(page)
                    if d.get("owner_name"):
                        add(url, name, "ownership", "owner_name", d["owner_name"],
                            f"right, title, and interest of {d['owner_name']} in and to the property")
                elif host == "fdicrealestatelistings.com":
                    m = re.search(r"/auctions/(\d+)", url)
                    fl = g.parse_fdic_detail(page, m.group(1))["fields"] if m else {}
                    for k, names in (("beds", ("bed",)), ("baths", ("bath",)), ("living_sqft", ("square feet", "sq ft", "sqft")),
                                     ("year_built", ("year built",))):
                        v = g._fdic_field(fl, *names)
                        if v not in (None, "") and _pos(v):
                            vals[k] = (_pos(v), f"{names[0]}: {v}")
                elif host == "properties.sc.egov.usda.gov":
                    d = g.parse_usda_detail(page)
                    if d.get("annual_taxes"):
                        add(url, name, "value_tax", "annual_tax", f"${d['annual_taxes']:,.0f}", f"Real Estate Taxes: ${d['annual_taxes']:,.0f}")
            for k, (v, quote) in vals.items():
                add(url, name, "property", k, int(v) if k != "baths" else v, quote)
    note = "; ".join(notes_out + errors) or None
    if errors and not read:
        raise PropertyDataError(note)
    return {"facts": facts, "note": note, "skipped": not read}


def census_facts(t) -> dict:
    """ZIP / county / coordinates of the Census geocoder's match — only when it matched this house number + street (+ city)."""
    from app.services import property_scan as ps
    m = ps.census_match(t)
    if not m:
        return {"facts": [], "note": "The U.S. Census geocoder couldn't match this address.", "match": None}
    matched = m.get("matched_address") or ""
    town = ps._norm(t.city)
    same = ps._street_key(matched) == ps._street_key(t.street) and (
        not town or town.endswith(" county") or re.search(rf"\b{re.escape(town)}\b", ps._norm(matched)) is not None)
    if not same:
        return {"facts": [], "note": f"Census matched a different address ({matched}) — not used.", "match": None}
    url = census_url(t)
    facts = []
    if m.get("zip"):
        facts.append(ps._fact("property", "property_zip", m["zip"], src="census", source_name="U.S. Census geocoder",
                              source_url=url, source_kind="government", quote=f"matched address: {matched}", official=True))
    if m.get("county"):
        facts.append(ps._fact("property", "county", m["county"], src="census", source_name="U.S. Census geocoder",
                              source_url=url, source_kind="government", quote=f"county: {m['county']}", official=True))
    return {"facts": facts, "note": None, "match": m}


def census_zip_correction(lead: Lead, t, match: Optional[dict]) -> Optional[dict]:
    """Replace a source's wrong ZIP with the Census one — only when Census matched this exact street
    (house number, name, suffix and directional: "Peabody St NW" ≠ "Peabody St NE", "Janie Bird Dr" ≠ "… Ave").
    The old ZIP is kept in ``zip_corrected_from`` so imports that still carry it find this property."""
    from app.services.stacking import normalize_address
    zip_ = (match or {}).get("zip")
    old = (lead.property_zip or "")[:5]
    if not zip_ or not old or old == zip_:
        return None
    street = (match.get("matched_address") or "").split(",")[0]
    if normalize_address(street) != normalize_address(t.street):
        return None
    lead.zip_corrected_from = lead.zip_corrected_from or lead.property_zip
    lead.property_zip = zip_
    return {"field": "property_zip", "value": zip_, "source": "U.S. Census geocoder", "source_url": census_url(t),
            "corrected_from": old}


def census_url(t) -> str:
    """Link to the Census geocoder query for this address (the source of a Census fact)."""
    from app.services import property_scan as ps
    return ps.CENSUS_GEOCODER + "?" + "&".join(f"{k}={v}" for k, v in (
        ("address", re.sub(r"\s+", "+", t.address)), ("benchmark", "Public_AR_Current"), ("vintage", "Current_Current"),
        ("format", "json")))


def _timed(fn: Callable[[], dict]) -> tuple[Optional[dict], Optional[str], int]:
    from app.services.property_scan import ScanError
    t0 = time.monotonic()
    try:
        return fn(), None, int((time.monotonic() - t0) * 1000)
    except (PropertyDataError, ScanError) as exc:
        return None, str(exc), int((time.monotonic() - t0) * 1000)
    except Exception as exc:  # one broken source never sinks the others
        return None, f"{type(exc).__name__}: {exc}", int((time.monotonic() - t0) * 1000)


def _run(sid: str, name: str, got: Optional[dict], err: Optional[str], ms: int, **extra) -> dict:
    n = len((got or {}).get("facts") or [])
    run = {"id": sid, "name": name, "facts": n, "latency_ms": ms, "error": err, **extra,
           "status": "error" if err else ("ok" if n else ("skipped" if (got or {}).get("skipped") else "empty"))}
    note = (got or {}).get("note")
    if note and not err:
        run["note"] = note
    return run


def refresh(db: Session, lead: Lead, user_id: Optional[str] = None) -> dict:
    """Fill this property's EMPTY fields from RentCast (when active) and free public sources; record the
    sources as a PropertyScan row. Returns {filled, missing, sources, cost_usd, seconds, scan}.
    Raises PropertyDataError only when every source that ran failed."""
    from app.models import PropertyScan
    from app.services import property_scan as ps
    from sqlalchemy import func, select

    started = time.monotonic()
    t = ps._target(lead)
    notes = lead.notes or ""
    accounts = list(dict.fromkeys(_ACCOUNT_RE.findall(notes))) if "tarrantcountytx.gov" in notes else []
    urls = list(dict.fromkeys(_SOURCE_URL_RE.findall(notes)))[:3]
    shared = {u for u in urls if (db.execute(select(func.count(Lead.id)).where(Lead.notes.contains(u))).scalar() or 0) > 1}
    runs: list[dict] = []
    facts: list[dict] = []

    # 1. Census geocoder → county (picks the parcel layer), ZIP, coordinates.
    got, err, ms = _timed(lambda: census_facts(t))
    runs.append(_run("census", "U.S. Census geocoder", got, err, ms))
    match = (got or {}).get("match")
    facts += (got or {}).get("facts") or []
    rec = lead.property_record
    county = (match or {}).get("county") or (rec.county if rec else None)
    layer = parcel_layer(county, t.state) or (PARCEL_LAYERS[0] if accounts and t.state == "TX" else None)
    if match and match.get("longitude") is not None:
        point = (match["longitude"], match["latitude"])
    else:
        point = (rec.longitude, rec.latitude) if rec and rec.latitude is not None and rec.longitude is not None else None

    # 2 + 3. County parcel layer and the sale page, in parallel (network only — no DB in threads).
    jobs: dict[str, tuple[str, Callable[[], dict]]] = {
        "sale_page": ("Government sale page", lambda: sale_page_facts(t, notes, shared))}
    if layer:
        jobs["county_parcels"] = (layer.name, lambda: parcel_facts(layer, t, accounts, point))
    else:
        runs.append({"id": "county_parcels", "name": "County parcel layer", "status": "skipped", "facts": 0,
                     "note": f"No free parcel layer for {county or 'this county'}{', ' + t.state if t.state else ''} yet."})
    no_building = False
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futures = {sid: ex.submit(_timed, fn) for sid, (_, fn) in jobs.items()}
        for sid, fut in futures.items():
            got, err, ms = fut.result()
            runs.append(_run(sid, jobs[sid][0], got, err, ms))
            facts += (got or {}).get("facts") or []
            no_building = no_building or bool((got or {}).get("no_building"))

    # 4. Grok on the official record — only for fields the free sources didn't fill.
    planned = ps.plan_fill(lead, facts)
    need = [_FACT_OF[c] for c in missing_fields(lead) if _FACT_OF[c] not in planned and _FACT_OF[c] in ps._DATA_FIELDS]
    if no_building:
        need = [f for f in need if f not in _PHYSICAL]
    cost = 0.0
    grok = next(s for s in ps.source_states(db) if s["id"] == "grok")
    summary = None
    if not need:
        runs.append({"id": "grok", "name": "Grok research (official record)", "status": "skipped", "facts": 0,
                     "note": "Nothing left for web research." + (" No building on the parcel record." if no_building else "")})
    elif not grok["ready"]:
        runs.append({"id": "grok", "name": "Grok research (official record)", "status": "skipped", "facts": 0,
                     "note": "Grok is off or has no xAI key (Settings → Property data sources)."})
    else:
        key, model = ps._key(db, ps.CATALOG_BY_ID["grok"]), ps.scan_model(db)
        got, err, ms = _timed(lambda: ps.grok_research(key, model, t, passes={"data": ps.grok_data_focus(need)}))
        extra = {}
        if got:
            cost = got["cost_usd"]
            summary = got["summary"] or None
            extra = {k: got[k] for k in ("model", "cost_usd", "web_searches", "dropped", "matched_address")}
            extra["note"] = "Looked for: " + ", ".join(need)
            err = got["partial_error"]
        runs.append(_run("grok", "Grok research (official record)", got, err, ms, **extra))
        facts += (got or {}).get("facts") or []

    # 5. RentCast (writes its budget counter) — last, and not while it's known to be inactive.
    down = _rentcast_down.get("until", 0) > time.monotonic()
    if not rentcast_key(db):
        runs.append({"id": "rentcast", "name": "RentCast", "status": "skipped", "facts": 0, "note": "No RentCast API key."})
    elif down:
        runs.append({"id": "rentcast", "name": "RentCast", "status": "skipped", "facts": 0,
                     "note": f"Skipped: {_rentcast_down['error']}"})
    else:
        raw: dict = {}

        def rc() -> dict:
            try:
                enrich(db, lead, raw)
            except PropertyDataError as exc:
                if any(s in str(exc) for s in ("inactive", "API key", "budget")):
                    _rentcast_down.update(until=time.monotonic() + RENTCAST_RETRY_S, error=str(exc))
                if not raw:
                    raise
            return {"facts": ps.rentcast_facts(raw)}

        got, err, ms = _timed(rc)
        runs.append(_run("rentcast", "RentCast", got, err, ms))
        facts += (got or {}).get("facts") or []

    # Fill empty lead fields, then the property record where it fits.
    filled = ps.apply_to_lead(lead, facts)
    corrected = census_zip_correction(lead, t, match)
    if corrected:
        filled.append(corrected)
    rec = _record(db, lead)
    best = {fld: f for fld, (_, f) in ps.plan_fill(_Blank(), facts).items()}
    if match:
        rec.county = rec.county or match.get("county")
        rec.matched_address = rec.matched_address or match.get("matched_address")
        if rec.latitude is None and rec.longitude is None and match.get("latitude") is not None:
            rec.latitude, rec.longitude = match["latitude"], match["longitude"]
            rec.photo_available = None
    if "owner_name" in best and not rec.owner_names:
        rec.owner_names = json.dumps([best["owner_name"]["value"]])
    if "assessed_value" in best and not rec.tax_assessed_value:
        rec.tax_assessed_value = ps._num(best["assessed_value"]["value"])
        year = ps._year(best["assessed_value"].get("as_of"))
        rec.tax_year = rec.tax_year or (int(year) if year else None)
    annual = [f for f in facts if f["field"] == "annual_tax" and f["source"] == "sale_page"]
    if annual and not rec.tax_annual:
        rec.tax_annual = ps._num(annual[0]["value"])
        year = ps._year(annual[0].get("as_of"))
        rec.tax_year = rec.tax_year or (int(year) if year else None)
    ran = [r for r in runs if r["status"] != "skipped"]
    errors = [f"{r['name']}: {r['error']}" for r in ran if r.get("error")]
    ok = [r for r in ran if r["status"] in ("ok", "empty")]
    used_rentcast = any(r["id"] == "rentcast" and r["status"] == "ok" for r in runs)
    rec.source = ("rentcast + " if used_rentcast else "") + DATA_SOURCE_LABEL
    rec.fetched_at = utcnow()
    rec.error = "; ".join(errors)[:2000] or None

    seconds = round(time.monotonic() - started, 1)
    still = missing_fields(lead)
    scan = PropertyScan(lead_id=lead.id, status="failed" if not ok else ("partial" if errors else "done"),
                        address=full_address(lead), sources=json.dumps(runs), facts=json.dumps(facts),
                        applied=json.dumps(filled), summary=summary, cost_usd=round(cost, 4),
                        duration_ms=int(seconds * 1000), error="; ".join(errors)[:2000] or None, created_by=user_id)
    db.add(scan)
    line = (f"Data {utcnow():%Y-%m-%d}: " + (("filled " + "; ".join(f"{a['field']} {a['value']}" + (f" (was {a['corrected_from']})" if a.get("corrected_from") else "")
                                                   + f" ({a['source']})" for a in filled))
                                             if filled else "nothing new to fill")
            + (f". Still missing: {', '.join(still)}" if still else "") + (f". Cost ${cost:.2f}" if cost else ""))
    kept = [ln for ln in (lead.research_note or "").splitlines() if ln.strip() and not ln.startswith("Data ")]
    lead.research_note = "\n".join(kept + [line])[-4000:]
    db.flush()
    if not ok:
        raise PropertyDataError("; ".join(errors) or "No property data source could run.")
    return {"filled": filled, "missing": still, "sources": runs, "cost_usd": round(cost, 4), "seconds": seconds,
            "scan": ps.serialize(scan)}


class _Blank:
    """Stand-in lead with every field empty: which value would each fact field take (for the property record)."""
    def __getattr__(self, name: str) -> None:
        return None


# ── Photo (Street View, satellite fallback) ─────────────────────────────────

def maps_key(db: Session) -> str:
    from app.services.settings import get_setting
    return (get_setting(db, "google_maps_api_key") or "").strip()


def _location(lead: Lead) -> str:
    rec = lead.property_record
    if rec and rec.latitude and rec.longitude:
        return f"{rec.latitude},{rec.longitude}"
    return full_address(lead)


def photo(db: Session, lead: Lead) -> tuple[bytes, str]:
    """JPEG of the house: Street View if imagery exists, else a satellite view."""
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    cached = PHOTOS_DIR / f"{lead.id}.jpg"
    if cached.is_file():
        return cached.read_bytes(), "image/jpeg"
    key = maps_key(db)
    if not key:
        raise PropertyDataError("No Google Maps API key — add it in Settings → Property Data.")
    loc = _location(lead)
    try:
        meta = httpx.get(f"{MAPS_BASE}/streetview/metadata", timeout=_TIMEOUT,
                         params={"location": loc, "source": "outdoor", "key": key}).json()
        status = meta.get("status")
        if status in ("REQUEST_DENIED", "OVER_QUERY_LIMIT"):
            raise PropertyDataError(f"Google Maps: {status} — {meta.get('error_message', 'check the key')}")
        if status == "OK":
            resp = httpx.get(f"{MAPS_BASE}/streetview", timeout=_TIMEOUT, params={
                "location": loc, "size": "640x400", "fov": 80, "source": "outdoor", "key": key})
        else:
            resp = httpx.get(f"{MAPS_BASE}/staticmap", timeout=_TIMEOUT, params={
                "center": loc, "zoom": 19, "size": "640x400", "maptype": "satellite",
                "markers": f"color:red|{loc}", "key": key})
    except httpx.HTTPError as exc:
        raise PropertyDataError(f"Google Maps unreachable: {exc}") from exc
    if resp.status_code >= 400 or not resp.headers.get("content-type", "").startswith("image/"):
        raise PropertyDataError(f"Google Maps photo error {resp.status_code}.")
    rec = _record(db, lead)
    rec.photo_available = status == "OK"
    db.flush()
    cached.write_bytes(resp.content)
    return resp.content, resp.headers.get("content-type", "image/jpeg")


# ── Brief for the dialer / AI agent ─────────────────────────────────────────

def _names(rec: Optional[PropertyRecord]) -> list[str]:
    if rec is None or not rec.owner_names:
        return []
    try:
        return list(json.loads(rec.owner_names))
    except ValueError:
        return []


_NAME_NOISE = re.compile(r"\b(LLC|INC|CORP|CO|TRUST|TRUSTEE|ETAL|ET AL|JR|SR|II|III|THE|OF|AND|&)\b")


def name_tokens(name: str) -> set[str]:
    cleaned = _NAME_NOISE.sub(" ", re.sub(r"[^A-Za-z ]", " ", (name or "").upper()))
    return {t for t in cleaned.split() if len(t) > 1}


def owner_matches(lead_owner: str, record_names: list[str]) -> Optional[bool]:
    """True/False if comparable; None when either side is missing."""
    lead_tokens = name_tokens(lead_owner)
    if not lead_tokens or not record_names:
        return None
    return any(len(lead_tokens & name_tokens(n)) >= min(2, len(lead_tokens)) for n in record_names)


def seller_reported_mortgage(lead: Lead) -> Optional[dict]:
    """Latest answer to the call flow's mortgage question, if any."""
    from app.services.call_guide import classify
    for call in sorted(lead.calls, key=lambda c: c.created_at or datetime.min, reverse=True):
        if not call.answers or not call.call_flow:
            continue
        questions = json.loads(call.call_flow.questions or "[]")
        for idx, ans in json.loads(call.answers).items():
            try:
                if classify(questions[int(idx)]) == "mortgage" and ans.strip():
                    return {"answer": ans.strip(), "at": iso_utc(call.created_at)}
            except (ValueError, IndexError):
                continue
    return None


def brief(db: Session, lead: Lead) -> dict:
    """Everything a dialer or AI agent should know before calling this property."""
    from app.services.number_check import check_all_phones

    rec = lead.property_record
    lists = sorted({m.source_list.list_type for m in lead.list_memberships if m.source_list}, key=lambda x: x.value)
    names = _names(rec)
    val = lead.valuations[0] if lead.valuations else None
    est_value = rec.est_value if rec and rec.est_value else None
    est_mortgage = rec.est_mortgage_balance if rec else None
    taxes_owed = lead.taxes_owed or 0
    equity = round(est_value - (est_mortgage or 0) - taxes_owed, -2) if est_value else None

    debt_items = []
    if lead.taxes_owed:
        debt_items.append({"kind": "taxes_owed", "label": "Delinquent property taxes (county list)", "amount": lead.taxes_owed, "certain": True})
    if ListType.PRE_FORECLOSURE in lists:
        debt_items.append({"kind": "pre_foreclosure", "label": "Pre-foreclosure filing — mortgage in default", "amount": None, "certain": True})
    if ListType.TAX_DELINQUENT in lists and not lead.taxes_owed:
        debt_items.append({"kind": "tax_delinquent", "label": "On the tax-delinquent list", "amount": None, "certain": True})
    reported = seller_reported_mortgage(lead)
    if reported:
        debt_items.append({"kind": "seller_reported", "label": f"Seller said: \"{reported['answer']}\"", "amount": None, "certain": True, "at": reported["at"]})
    if est_mortgage is not None:
        debt_items.append({"kind": "est_mortgage", "label": rec.debt_basis, "amount": est_mortgage, "certain": False})

    listing = None
    if rec and rec.listing_status:
        listing = {
            "status": rec.listing_status, "price": rec.listing_price, "days_on_market": rec.listing_days_on_market,
            "listed": iso_utc(rec.listing_date), "mls": rec.listing_mls,
            "agent": json.loads(rec.listing_agent) if rec.listing_agent else None,
        }

    return {
        "lead_id": lead.id,
        "address": full_address(lead),
        "owner": lead.owner_name,
        "owner_is_llc": lead.owner_is_llc,
        "absentee": lead.absentee,
        "mailing_address": lead.mailing_address,
        "specs": {"beds": lead.beds, "baths": lead.baths, "sqft": lead.sqft, "year_built": lead.year_built,
                  "type": rec.property_type if rec else None},
        "lists": [lt.value for lt in lists],
        "stack_depth": lead.stack_depth,
        "status": lead.status.value,
        "data": {
            "source": rec.source if rec else None,
            "fetched_at": iso_utc(rec.fetched_at) if rec else None,
            "error": rec.error if rec else None,
            "matched_address": rec.matched_address if rec else None,
        },
        "owner_of_record": names,
        "owner_match": owner_matches(lead.owner_name, names),
        "owner_occupied": rec.owner_occupied if rec else None,
        "value": {
            "estimate": est_value, "low": rec.est_value_low if rec else None, "high": rec.est_value_high if rec else None,
            "assessed": (rec.tax_assessed_value if rec and rec.tax_assessed_value else lead.assessed_value),
            "arv": val.arv if val else None, "mao": val.mao if val else None,
        },
        "tax": {
            "annual": rec.tax_annual if rec else None, "year": rec.tax_year if rec else None,
            "owed": lead.taxes_owed,
            "history": json.loads(rec.tax_history) if rec and rec.tax_history else {},
        },
        "last_sale": {"price": rec.last_sale_price if rec else None, "date": iso_utc(rec.last_sale_date) if rec else None},
        "debt": {"items": debt_items, "est_mortgage": est_mortgage, "est_equity": equity},
        "listing": listing,
        "photo": {"url": f"/api/leads/{lead.id}/photo", "configured": bool(maps_key(db)), "street_view": rec.photo_available if rec else None},
        "phones": check_all_phones(db, lead),
        "ai_consent": {
            "at": iso_utc(lead.ai_call_consent_at), "source": lead.ai_call_consent_source,
        } if lead.ai_call_consent_at else None,
        "recent_calls": [
            {"at": iso_utc(c.created_at), "outcome": c.outcome.value, "score": c.lead_score}
            for c in sorted(lead.calls, key=lambda c: c.created_at or datetime.min, reverse=True)[:3]
        ],
    }


def agent_facts(b: dict) -> list[str]:
    """Plain-language facts for the AI agent's prompt — no dollar figures (it must not quote numbers)."""
    facts = []
    if b["owner_of_record"]:
        facts.append(f"Public records list the owner as {', '.join(b['owner_of_record'])}.")
    if b["owner_occupied"] is True:
        facts.append("Records say the owner lives there.")
    elif b["owner_occupied"] is False or b["absentee"]:
        facts.append("Records say the owner does not live there (absentee).")
    if b["listing"] and (b["listing"]["status"] or "").lower() == "active":
        dom = b["listing"]["days_on_market"]
        facts.append(f"It is currently listed for sale on the MLS{f' ({dom} days on market)' if dom is not None else ''} "
                     "— ask whether they're working with that agent; don't interfere with the listing.")
    elif b["listing"]:
        facts.append("It was listed on the MLS before but the listing is no longer active.")
    kinds = {d["kind"] for d in b["debt"]["items"]}
    if "taxes_owed" in kinds or "tax_delinquent" in kinds:
        facts.append("County records show delinquent property taxes — raise gently only if relevant.")
    if "pre_foreclosure" in kinds:
        facts.append("There is a pre-foreclosure filing — be empathetic; listen for an auction date.")
    if "est_mortgage" in kinds:
        facts.append("There is probably a mortgage from the last purchase.")
    return facts
