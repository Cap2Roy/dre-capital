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
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

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
        if "budget" in str(exc) or "API key" in str(exc):
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
