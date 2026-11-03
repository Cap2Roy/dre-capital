"""Buyer finder: discover real cash buyers for a market and add them to the buyer network.

Two sources run in parallel for a market (city + state, optionally the ZIPs of our properties there):

- ``web_research`` — Grok (xAI Responses API + web search) looks for businesses that buy houses for cash
  there: "we buy houses" companies, flippers, landlord / rental investment companies, iBuyers, funds, local
  wholesale buyers. A buyer is kept only when its cited page is a URL the search engine returned or a page
  our own re-read shows the company name on; phone / email are kept only when the re-read page prints them
  (page not readable → kept, confidence at most medium). Buy-box values only as the company states them.
- ``county_parcels`` — where we have the county parcel layer (Tarrant; Dallas), entity owners (LLC, INC,
  LP, TRUST, HOLDINGS, PROPERTIES, HOMES …) holding ≥3 house-sized parcels in the property's ZIP (city or
  county when there is no ZIP), mailed somewhere other than the parcel: buyer_type portfolio_owner, the
  count, the value range of their holdings (an *inferred* price band, labelled so), the latest deed date
  and the mailing address of record. Source = a query URL on the layer listing the owner's parcels.

Buyers are deduped on normalized company name, phone, website and (parcel owners) mailing address.
Finder-made rows merge new evidence; user-entered buyers are never changed. Each market's last search is
kept in the ``buyer_markets_searched`` setting (JSON) so "find for all markets" skips fresh ones.
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Buyer, Lead, LeadStatus
from app.services import owner_phones as op
from app.services import property_scan as ps
from app.services.phones import normalize_us
from app.services.property_data import parcel_layer
from app.timeutil import utcnow

SETTING_KEY = "buyer_markets_searched"
RESEARCH_TTL = timedelta(days=30)      # a market's search is fresh this long
RETRY_AFTER = timedelta(hours=6)       # a failed / partial market isn't retried by find-all sooner
FINDER_SOURCES = ("web_research", "county_parcels")
GROK_MAX_TOOL_CALLS = 20
READ_MAX_PAGES = 30
ARCGIS_TIMEOUT = 60.0
TIGERWEB = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/tigerWMS_Current/MapServer"
ZCTA_LAYER, PLACES_LAYER, COUNTIES_LAYER = 2, 28, 82
_CONF_RANK = {"low": 1, "medium": 2, "high": 3}
_RANKING = {"high": 80, "medium": 90, "low": 99}     # discovered buyers sort after the hand-ranked network
ACTIVE_EXCLUDED = (LeadStatus.DEAD, LeadStatus.CLOSED)


class BuyerFinderError(RuntimeError):
    """A source failed in a way worth showing as-is."""


# ── Market ───────────────────────────────────────────────────────────────────

def market_key(city: str, state: str) -> str:
    return f"{' '.join((city or '').split()).lower()}|{(state or '').strip().upper()}"


@dataclass
class Market:
    city: str
    state: str
    zips: list[str] = field(default_factory=list)
    county: Optional[str] = None          # "Tarrant" (no " County")

    @property
    def key(self) -> str:
        return market_key(self.city, self.state)

    @property
    def label(self) -> str:
        return f"{self.city}, {self.state}"

    @property
    def county_wide(self) -> bool:      # leads whose city is "Tarrant County"
        return self.city.lower().endswith(" county")


def _county_name(name: Optional[str]) -> Optional[str]:
    s = re.sub(r"\s+(county|parish|borough)$", "", " ".join((name or "").split()), flags=re.I)
    return s.title() or None


def _clean_zips(zips) -> list[str]:
    return sorted({z[:5] for z in (str(x).strip() for x in zips or []) if re.fullmatch(r"\d{5}(-?\d{4})?", z)})


def active_leads(db: Session) -> list[Lead]:
    return db.execute(select(Lead).where(Lead.archived_at.is_(None), Lead.status.notin_(ACTIVE_EXCLUDED))).scalars().all()


def _market_county(db: Session, city: str, state: str) -> Optional[str]:
    """County from a property in this market whose record names it."""
    if city.lower().endswith(" county"):
        return _county_name(city)
    for lead in db.execute(select(Lead).where(Lead.property_state == state)).scalars():
        if market_key(lead.property_city or "", state) == market_key(city, state) and lead.property_record \
                and lead.property_record.county:
            return _county_name(lead.property_record.county)
    return None


def market_for_lead(lead: Lead) -> Market:
    rec = lead.property_record
    return Market(city=" ".join((lead.property_city or "").split()), state=(lead.property_state or "").strip().upper(),
                  zips=_clean_zips([lead.property_zip]), county=_county_name(rec.county) if rec and rec.county else None)


def _profile(db: Session, m: Market) -> dict:
    """What our properties in this market look like (types, values) — context for the web search."""
    from app.services.buyers import lead_property_type, lead_value
    types: dict[str, int] = {}
    values: list[float] = []
    for lead in active_leads(db):
        if market_key(lead.property_city or "", lead.property_state or "") != m.key:
            continue
        kind, _ = lead_property_type(lead)
        types[kind or "unknown"] = types.get(kind or "unknown", 0) + 1
        v, _ = lead_value(lead)
        if v:
            values.append(v)
    return {"types": types, "value_range": (min(values), max(values)) if values else None}


# ── Polite HTTP to ArcGIS hosts (≤ 1 request / second / host) ───────────────

_host_lock = threading.Lock()
_host_next: dict[str, float] = {}


def _polite(url: str) -> None:
    host = urlsplit(url).hostname or ""
    with _host_lock:
        now = time.monotonic()
        wait = max(0.0, _host_next.get(host, 0.0) - now)
        _host_next[host] = now + wait + 1.0
    if wait:
        time.sleep(wait)


def _arcgis(layer_url: str, data: dict) -> dict:
    _polite(layer_url)
    host = urlsplit(layer_url).hostname
    try:
        r = httpx.post(layer_url + "/query", data={"f": "json", **data}, timeout=ARCGIS_TIMEOUT,
                       headers={"User-Agent": ps.USER_AGENT})
        r.raise_for_status()
        out = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise BuyerFinderError(f"{host} unreachable: {type(exc).__name__}") from exc
    if out.get("error"):
        err = out["error"]
        raise BuyerFinderError(f"{host}: {err.get('message')} {' '.join(err.get('details') or [])}".strip())
    return out


_zcta_cache: dict[str, Optional[dict]] = {}


def _zcta(zip5: str) -> Optional[dict]:
    """Census ZCTA for a ZIP → {"geometry" (lon/lat polygon, ~50 m generalized), "lat", "lon"}."""
    if zip5 not in _zcta_cache:
        out = _arcgis(f"{TIGERWEB}/{ZCTA_LAYER}", {
            "where": f"ZCTA5='{zip5}'", "outFields": "ZCTA5,INTPTLAT,INTPTLON", "returnGeometry": "true",
            "outSR": "4326", "maxAllowableOffset": "0.0005", "geometryPrecision": "5"})
        feats = out.get("features") or []
        if feats and feats[0].get("geometry"):
            a = feats[0]["attributes"]
            geom = {**feats[0]["geometry"], "spatialReference": {"wkid": 4326}}
            _zcta_cache[zip5] = {"geometry": geom, "lat": float(a["INTPTLAT"]), "lon": float(a["INTPTLON"])}
        else:
            _zcta_cache[zip5] = None
    return _zcta_cache[zip5]


def _county_at(lon: float, lat: float) -> Optional[str]:
    out = _arcgis(f"{TIGERWEB}/{COUNTIES_LAYER}", {
        "geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects", "outFields": "NAME", "returnGeometry": "false"})
    feats = out.get("features") or []
    return _county_name(feats[0]["attributes"].get("NAME")) if feats else None


def _resolve_county(m: Market, state_fips: str) -> Optional[str]:
    """County of a market from Census TIGERweb: a ZIP's ZCTA point, else the incorporated place."""
    for z in m.zips:
        zc = _zcta(z)
        if zc:
            return _county_at(zc["lon"], zc["lat"])
    city = m.city.replace("'", "''")
    out = _arcgis(f"{TIGERWEB}/{PLACES_LAYER}", {"where": f"BASENAME='{city}' AND STATE='{state_fips}'",
                                                 "outFields": "INTPTLAT,INTPTLON", "returnGeometry": "false"})
    feats = out.get("features") or []
    if not feats:
        return None
    a = feats[0]["attributes"]
    return _county_at(float(a["INTPTLON"]), float(a["INTPTLAT"]))


# ── Source 1: county parcel layer → portfolio owners ────────────────────────

@dataclass(frozen=True)
class InvestorLayer:
    """How to read holdings off a county parcel layer (endpoint + name come from property_data)."""
    county: str
    state: str
    state_fips: str
    owner: str
    mail_street: str
    mail_city: str
    mail_state: Optional[str]          # None: the state is inside mail_city ("FORT WORTH, TX")
    mail_zip: str
    value: str
    value_label: str
    deed: Optional[str]
    deed_format: str                   # epoch_ms | yyyymmdd
    year: Optional[str]
    city: str                          # situs city field
    zip: Optional[str]                 # situs ZIP field; None → ZIP areas by the Census ZCTA polygon
    homes: str                         # where: house-sized residential parcels
    lots: str                          # where: vacant lots
    elsewhere: Optional[str]           # where: mailing address isn't the parcel
    homes_label: str


INVESTOR_LAYERS: tuple[InvestorLayer, ...] = (
    # TAD's layer leaves BEDROOMS / use codes empty: "house" = a lot / condo parcel with a 500–4,500 sq ft
    # building appraised under $1M; its situs ZIP is blank, so ZIP areas use the Census ZCTA polygon.
    InvestorLayer("tarrant", "TX", "48", "OWNER_NAME", "OWNER_ADDR", "OWNER_CITY", None, "OWNER_ZIP",
                  "APPRAISEDV", "appraised value", "DEED_DATE", "epoch_ms", "YEAR_BUILT", "CITY", None,
                  homes="PARCELTYPE IN (1,4) AND LIVING_ARE >= 500 AND LIVING_ARE <= 4500 AND APPRAISEDV > 0 "
                        "AND APPRAISEDV < 1000000",
                  lots="PARCELTYPE IN (1,2) AND IMPR_VALUE = 0 AND LIVING_ARE = 0 AND APPRAISEDV > 0 AND LAND_ACRES <= 5",
                  elsewhere="OWNER_ADDR <> SITUS_ADDR",
                  homes_label="house-sized parcels (500–4,500 sq ft building, under $1M)"),
    InvestorLayer("dallas", "TX", "48", "OWNER_NAME", "MAIL_LINE1", "MAIL_CITY", "MAIL_STAT", "MAIL_ZIP",
                  "MKT_VALUE", "market value", "DATE_ACQ", "yyyymmdd", None, "SITUS_CITY", "SITUS_ZIP",
                  homes="STAT_LAND_ LIKE 'A%' AND MKT_VALUE > 0", lots="STAT_LAND_ LIKE 'C%' AND MKT_VALUE > 0",
                  elsewhere=None, homes_label="state class A (single-family) parcels"),
)

# Entity owners (LIKE patterns on the owner field) …
_ENTITY_WORDS = ("LLC", "L L C", " INC", " LP", " LTD", "CORP", "TRUST", "HOLDING", "PROPERT", "HOMES", "INVEST",
                 "CAPITAL", "REALTY", "REAL ESTATE", "VENTURE", "EQUITY", "PARTNERS", "GROUP", "RENTAL", "ASSET",
                 "BORROWER", "FUND", "ENTERPRISE")
# … minus owners that aren't buyers of wholesale deals: government, utilities, churches, HOAs, banks /
# trustees (REO), estates, mobile-home parks and production home builders.
_NOT_BUYER = re.compile(
    r"\b(CITY OF|COUNTY|STATE OF|UNITED STATES|HOUSING (AUTH|FIN)|AUTHORITY|ISD|SCHOOL|COLLEGE|UNIVERSITY|CHURCH|"
    r"MINISTR|BAPTIST|METHODIST|CATHOLIC|DIOCESE|TEMPLE|MOSQUE|HABITAT FOR HUMANITY|LAND BANK|ASSOCIATION|ASSN|HOA|"
    r"COMMUNITY (IMPROVEMENT|DEVELOPMENT)|PUBLIC FACILITY|REDEVELOPMENT|ONCOR|ATMOS|E&P|ENERGY|PETROLEUM|PIPELINE|"
    r"MIDSTREAM|RAILROAD|RAILWAY|CEMETERY|BANK|MORTGAGE|SAVINGS|CREDIT UNION|"
    r"FEDERAL|SECRETARY|VETERANS|HUD|TRUSTEE|ESTATE OF|MHP|MHC|MOBILE HOME|MANUFACTURED|HOMEBUILD|BUILDERS?|LENNAR|"
    r"HORTON|PULTE|KB HOME|MERITAGE|HISTORYMAKER|BLOOMFIELD|IMPRESSION HOMES|FIRST TEXAS HOMES|WEEKLEY|"
    r"TAYLOR MORRISON|M/I HOMES|CENTEX|GEHAN|ASHTON WOODS|PERRY HOMES|HIGHLAND HOMES|ANTARES|CHESMAR|LGI HOMES|"
    r"CENTURY COMMUNITIES|TRINITY HABITAT)\b")
_NEW_BUILD_YEARS = 5      # holdings averaging this new = builder inventory / build-to-rent, not a deal buyer
_MIN_HOME_VALUE = 25000   # holdings averaging less = mobile-home lots


def investor_layer(county: Optional[str], state: str) -> Optional[InvestorLayer]:
    key = (county or "").lower()
    return next((x for x in INVESTOR_LAYERS if x.county == key and x.state == state), None)


def _layer_states() -> set[str]:
    return {x.state for x in INVESTOR_LAYERS}


@dataclass
class Area:
    label: str                     # "ZIP 76119" | "Fort Worth" | "Tarrant County"
    zip: Optional[str] = None
    where: Optional[str] = None
    geometry: Optional[dict] = None
    min_count: int = 3
    top_homes: int = 15
    top_lots: int = 5


def _areas(layer: InvestorLayer, m: Market) -> tuple[list[Area], list[str]]:
    """Query areas for a market (its ZIPs; else the city; else the county) + ZIPs with no Census polygon."""
    areas, skipped = [], []
    for z in m.zips:
        if layer.zip:
            areas.append(Area(f"ZIP {z}", zip=z, where=f"{layer.zip} = '{z}'"))
        elif (zc := _zcta(z)):
            areas.append(Area(f"ZIP {z}", zip=z, geometry=zc["geometry"]))
        else:
            skipped.append(f"ZIP {z}")
    if areas:
        return areas, skipped
    if m.county_wide:
        return [Area(f"{_county_name(m.city)} County", min_count=10, top_homes=25, top_lots=10)], skipped
    city = m.city.upper().replace("'", "''")
    return [Area(m.city, where=f"UPPER({layer.city}) LIKE '{city}%'", min_count=5, top_homes=25, top_lots=8)], skipped


def _deed_date(layer: InvestorLayer, v: Any) -> Optional[str]:
    try:
        if v in (None, "", 0):
            return None
        if layer.deed_format == "epoch_ms":
            d = datetime.fromtimestamp(float(v) / 1000, timezone.utc).date()
        else:
            s = str(int(v))
            d = datetime(int(s[:4]), int(s[4:6]), int(s[6:8])).date()
    except (ValueError, OverflowError, OSError):
        return None
    return d.isoformat() if d.year >= 1950 else None


def _k(v: Optional[float]) -> str:
    return "—" if v is None else f"${v / 1000:,.0f}k" if v >= 1000 else f"${v:,.0f}"


def _sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _parcels_link(url: str, layer: InvestorLayer, owners: list[str]) -> str:
    """A query URL on the layer that lists every parcel these owners hold (spot-check the evidence)."""
    return url + "/query?" + urlencode({
        "where": f"{layer.owner} IN ({','.join(_sql_str(o) for o in owners)})", "outFields": "*",
        "returnGeometry": "false", "f": "html"})


def _mailing(layer: InvestorLayer, a: dict) -> tuple[str, str]:
    """(one-line mailing address as the record prints it, dedupe key) from a stats row."""
    street = " ".join(str(a.get(layer.mail_street) or "").split())
    city = " ".join(str(a.get(layer.mail_city) or "").split())
    if layer.mail_state:
        from app.services.gov_sources import state_code
        st = state_code(" ".join(str(a.get(layer.mail_state) or "").split())) or ""
        city = f"{city}, {st}".strip(", ")
    zip5 = str(a.get(layer.mail_zip) or "").strip()[:5]
    line = f"{', '.join(x for x in (street, city) if x)} {zip5}".strip()
    return line, _mailing_key(line)


def _holdings(layer: InvestorLayer, url: str, area: Area, kind: str) -> list[dict]:
    """Entity owners with ≥ area.min_count parcels of ``kind`` (homes | lots) in the area — one stats query."""
    where = " AND ".join(x for x in (
        layer.homes if kind == "homes" else layer.lots,
        "(" + " OR ".join(f"{layer.owner} LIKE '%{w}%'" for w in _ENTITY_WORDS) + ")",
        layer.elsewhere, area.where) if x)
    group = [layer.owner, layer.mail_street, layer.mail_city] + ([layer.mail_state] if layer.mail_state else []) \
        + [layer.mail_zip]
    stats = [{"statisticType": "count", "onStatisticField": "OBJECTID", "outStatisticFieldName": "n"},
             {"statisticType": "min", "onStatisticField": layer.value, "outStatisticFieldName": "vmin"},
             {"statisticType": "max", "onStatisticField": layer.value, "outStatisticFieldName": "vmax"},
             {"statisticType": "avg", "onStatisticField": layer.value, "outStatisticFieldName": "vavg"}]
    if layer.deed:
        stats.append({"statisticType": "max", "onStatisticField": layer.deed, "outStatisticFieldName": "last"})
    if layer.year:
        stats.append({"statisticType": "avg", "onStatisticField": layer.year, "outStatisticFieldName": "yavg"})
    data = {"where": where, "outStatistics": json.dumps(stats), "groupByFieldsForStatistics": ",".join(group),
            "having": f"COUNT(OBJECTID) >= {area.min_count}"}
    if area.geometry:
        data.update({"geometry": json.dumps(area.geometry), "geometryType": "esriGeometryPolygon", "inSR": "4326",
                     "spatialRel": "esriSpatialRelIntersects"})
    rows = [f.get("attributes") or {} for f in _arcgis(url, data).get("features") or []]
    year_now = utcnow().year
    out = []
    for a in rows:
        name = " ".join(str(a.get(layer.owner) or "").split())
        if not name or _NOT_BUYER.search(name.upper()):
            continue
        if kind == "homes" and ((a.get("yavg") or 0) >= year_now - _NEW_BUILD_YEARS
                                or (a.get("vavg") or 0) < _MIN_HOME_VALUE):
            continue
        mailing, mkey = _mailing(layer, a)
        out.append({"name": name, "mailing": mailing, "mkey": mkey, "n": int(a.get("n") or 0),
                    "vmin": a.get("vmin"), "vmax": a.get("vmax"), "vavg": a.get("vavg"),
                    "last": _deed_date(layer, a.get("last"))})
    out.sort(key=lambda r: (-r["n"], r["name"]))    # servers don't all honour orderByFieldsForStatistics
    return out


@dataclass
class Candidate:
    """A buyer found by one source, before dedupe against the network."""
    source: str
    name: str
    buyer_type: str
    confidence: str
    source_url: str
    evidence: list[str] = field(default_factory=list)
    phone: Optional[str] = None
    email: Optional[str] = None
    website: Optional[str] = None
    mailing_address: Optional[str] = None
    mailing_key: Optional[str] = None
    states: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    zips: list[str] = field(default_factory=list)
    property_types: list[str] = field(default_factory=list)
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    properties_owned: Optional[int] = None
    last_purchase: Optional[str] = None
    band_label: Optional[str] = None   # min/max_price inferred from holdings' values ("appraised value")


_HOLDING_RE = re.compile(r"^Owns (\d+) (homes|vacant lots) in (.+?) \(", re.M)
PRICE_BAND_PREFIX = "Price band"


def holdings_from_evidence(evidence: Optional[str]) -> dict[tuple[str, str], int]:
    """{(kind, area label): count} from a buyer's evidence lines ("Owns 14 homes in ZIP 76119 (…")."""
    return {(m.group(2), m.group(3)): int(m.group(1)) for m in _HOLDING_RE.finditer(evidence or "")}


def _confidence_from_deed(last: Optional[str]) -> str:
    if not last:
        return "low"
    age = (utcnow().date() - datetime.fromisoformat(last).date()).days
    return "high" if age <= 730 else "medium" if age <= 1826 else "low"


def parcel_investors(m: Market) -> dict:
    """Portfolio owners in the market from the county parcel layer → {status, detail, candidates, areas}."""
    t0 = time.monotonic()
    if m.state not in _layer_states():
        return {"status": "skipped", "detail": f"No county parcel layer for {m.state} (have: Tarrant, Dallas TX).",
                "candidates": [], "areas": [], "ms": 0}
    try:
        county = m.county or _resolve_county(m, next(x.state_fips for x in INVESTOR_LAYERS if x.state == m.state))
        m.county = county
        layer_def = investor_layer(county, m.state)
        if not layer_def:
            return {"status": "skipped", "detail": f"No county parcel layer for {county or 'this'} County, {m.state}.",
                    "candidates": [], "areas": [], "county": county, "ms": int((time.monotonic() - t0) * 1000)}
        pl = parcel_layer(layer_def.county, layer_def.state)
        areas, skipped = _areas(layer_def, m)
        by_mail: dict[str, Candidate] = {}
        owners_of: dict[str, list[str]] = {}
        band: dict[str, list[float]] = {}
        counts: list[str] = []
        searched: list[str] = list(skipped)
        for area in areas:
            for kind, top in (("homes", area.top_homes), ("lots", area.top_lots)):
                rows = _holdings(layer_def, pl.url, area, kind)[:top]
                counts.append(f"{area.label}: {len(rows)} {kind} owners")
                # Entities at one mailing address = one buyer to contact (e.g. a fund's series LLCs).
                merged: dict[str, list[dict]] = {}
                for r in rows:
                    merged.setdefault(r["mkey"], []).append(r)
                for mkey, group in merged.items():
                    group.sort(key=lambda r: -r["n"])
                    n = sum(r["n"] for r in group)
                    lo = min((r["vmin"] for r in group if r["vmin"]), default=None)
                    hi = max((r["vmax"] for r in group if r["vmax"]), default=None)
                    avg = sum((r["vavg"] or 0) * r["n"] for r in group) / n if n else None
                    last = max((r["last"] for r in group if r["last"]), default=None)
                    what = "homes" if kind == "homes" else "vacant lots"
                    names = [r["name"] for r in group]
                    line = (f"Owns {n} {what} in {area.label} ({pl.name}: {layer_def.value_label} {_k(lo)}–{_k(hi)}, "
                            f"avg {_k(avg)}" + (f"; latest deed {last}" if last else "") + ")"
                            + (f"; entities at this mailing address: {', '.join(names)}" if len(names) > 1 else ""))
                    c = by_mail.get(mkey)
                    if c is None:
                        c = by_mail[mkey] = Candidate(
                            source="county_parcels", name=group[0]["name"], buyer_type="portfolio_owner",
                            confidence="low", source_url="", mailing_address=group[0]["mailing"] or None,
                            mailing_key=mkey, states=[m.state],
                            cities=[f"{county} County"] if m.county_wide else [m.city])
                    c.evidence.append(line)
                    owners_of.setdefault(mkey, [])
                    owners_of[mkey] += [x for x in names if x not in owners_of[mkey]]
                    if area.zip and area.zip not in c.zips:
                        c.zips.append(area.zip)
                    ptype = "single_family" if kind == "homes" else "land"
                    if ptype not in c.property_types:
                        c.property_types.append(ptype)
                    c.properties_owned = (c.properties_owned or 0) + n
                    c.last_purchase = max(filter(None, (c.last_purchase, last)), default=None)
                    if kind == "homes":
                        band.setdefault(mkey, []).extend(x for x in (lo, hi) if x)
            searched.append(area.label)
        for mkey, c in by_mail.items():
            c.source_url = _parcels_link(pl.url, layer_def, owners_of[mkey])
            c.confidence = _confidence_from_deed(c.last_purchase)
            if band.get(mkey):
                c.min_price, c.max_price = min(band[mkey]), max(band[mkey])
                c.band_label = layer_def.value_label
            c.evidence.append(f"Residential = {layer_def.homes_label}"
                              + ("; parcels mailed elsewhere (not owner-occupied)" if layer_def.elsewhere else "")
                              + ". Contact: mailing address of record (no phone looked up).")
        detail = "; ".join(x for x in (", ".join(counts), *(f"{z}: no Census ZCTA polygon" for z in skipped),
                                       f"{len(by_mail)} buyers after merging entities at the same mailing address")
                           if x)
        return {"status": "ok" if by_mail else "empty", "detail": f"{pl.name}: {detail}",
                "candidates": list(by_mail.values()), "areas": searched, "county": county,
                "ms": int((time.monotonic() - t0) * 1000)}
    except BuyerFinderError as exc:
        return {"status": "error", "detail": str(exc)[:400], "candidates": [], "areas": [], "county": m.county,
                "ms": int((time.monotonic() - t0) * 1000)}


def price_band_line(lo: float, hi: float, value_label: str) -> str:
    return (f"{PRICE_BAND_PREFIX} {_k(lo)}–{_k(hi)} inferred from the {value_label}s of the homes they own "
            "(not stated by the buyer).")


# ── Source 2: Grok web research ──────────────────────────────────────────────

BUYER_TYPES = ("investor_company", "flipper", "landlord", "ibuyer", "wholesaler", "fund")
PROPERTY_TYPES = ("single_family", "multi_family", "condo", "townhouse", "mobile_home", "land", "commercial")

GROK_BUYER_INSTRUCTIONS = """You find REAL cash buyers of residential real estate in ONE U.S. market, for a wholesaling team that puts houses and lots under contract and assigns them to cash buyers. Use web search and open pages.

Who counts — businesses only: local "we buy houses" / cash home-buying companies, house flippers and rehabbers operating as a company, buy-and-hold landlord / rental investment companies, iBuyers that buy in this market, investment funds buying existing homes, and local wholesalers / investor groups that BUY deals (not only sell them).
Evidence that they buy here: their own website saying they buy in this city / county / ZIP, recent purchase or deal posts ("just bought", "recently purchased", flip case studies), a BBB or business profile in this market, a state business filing, news about their purchases here.

Rules — validity matters more than coverage:
- Only businesses with public business contact info (company phone, company email or website). Never report personal phone numbers of private individuals and never use people-search sites.
- Report only what a page you found states. Never infer, estimate or fill from memory; never construct URLs.
- source_url: the exact page showing they buy in this market (not a search page). quote: verbatim text from that page (max 300 characters) that names the company and says where / what it buys.
- phone and email: exactly as printed on a page; phone_source_url: the page that prints them (often the same page). null when none is printed.
- areas: only the cities / counties / regions the company itself names as where it buys (e.g. "Fort Worth", "Tarrant County", "Dallas-Fort Worth"); zips: only ZIP codes it names.
- property_types, price_min / price_max, condition: only what the company states (null / empty otherwise); price_quote: the verbatim words stating the price range.
- other_sources: up to 3 further pages corroborating the company (BBB, state filing, deal posts, news), each with what it shows.
- Skip lead-generation directories, agents / brokers who only list homes, lenders, title companies, and anyone with no sign of buying in this market.
- Up to 15 buyers, most local and most active first. In checked, list what you searched."""

GROK_BUYER_SCHEMA = {
    "type": "object",
    "properties": {
        "buyers": {"type": "array", "items": {"type": "object", "properties": {
            "company": {"type": "string"},
            "buyer_type": {"type": "string", "enum": list(BUYER_TYPES)},
            "website": {"type": ["string", "null"]},
            "phone": {"type": ["string", "null"]},
            "email": {"type": ["string", "null"]},
            "phone_source_url": {"type": ["string", "null"]},
            "areas": {"type": "array", "items": {"type": "string"}},
            "zips": {"type": "array", "items": {"type": "string"}},
            "property_types": {"type": "array", "items": {"type": "string", "enum": list(PROPERTY_TYPES)}},
            "price_min": {"type": ["number", "null"]},
            "price_max": {"type": ["number", "null"]},
            "price_quote": {"type": ["string", "null"]},
            "condition": {"type": ["string", "null"], "description": "Condition they buy, as stated (e.g. 'any condition')"},
            "evidence": {"type": "string", "description": "One sentence: why this company is an active buyer here"},
            "source_url": {"type": "string"},
            "quote": {"type": "string"},
            "other_sources": {"type": "array", "items": {"type": "object", "properties": {
                "url": {"type": "string"}, "what": {"type": "string"}},
                "required": ["url", "what"], "additionalProperties": False}},
        }, "required": ["company", "buyer_type", "website", "phone", "email", "phone_source_url", "areas", "zips",
                        "property_types", "price_min", "price_max", "price_quote", "condition", "evidence",
                        "source_url", "quote", "other_sources"], "additionalProperties": False}},
        "checked": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["buyers", "checked", "summary"],
    "additionalProperties": False,
}

# Hosts shared by many companies: a page there identifies a company by its path, not its host.
_SHARED_HOSTS = ("facebook.com", "linkedin.com", "instagram.com", "yelp.com", "bbb.org", "google.com", "x.com",
                 "twitter.com", "youtube.com", "biggerpockets.com", "zillow.com", "nextdoor.com", "tiktok.com",
                 "opencorporates.com", "bizapedia.com", "sites.google.com", "wixsite.com", "linktr.ee")
_NAME_NOISE = op._ENTITY_NOISE | {"we", "buy", "buys", "houses", "homes", "house", "cash", "for", "real", "estate",
                                  "llc", "inc", "texas", "tx"}


def _grok_input(m: Market, profile: dict) -> str:
    lines = [f"Market: {m.label}" + (f" ({m.county} County)" if m.county and not m.county_wide else "")]
    if m.zips:
        lines.append(f"ZIP codes of our properties here: {', '.join(m.zips)}")
    types = {k: v for k, v in (profile.get("types") or {}).items() if k != "unknown"}
    if types:
        lines.append("Our properties here: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in types.items()))
    if profile.get("value_range"):
        lo, hi = profile["value_range"]
        lines.append(f"Their values: {_k(lo)}–{_k(hi)}")
    lines.append("Find active cash buyers for these properties.")
    return "\n".join(lines)


def _grok_call(key: str, model: str, m: Market, profile: dict) -> tuple[dict, dict]:
    body = {
        "model": model,
        "instructions": GROK_BUYER_INSTRUCTIONS,
        "input": [{"role": "user", "content": _grok_input(m, profile)}],
        "tools": [{"type": "web_search"}],
        "max_tool_calls": GROK_MAX_TOOL_CALLS,
        "store": False,
        "text": {"format": {"type": "json_schema", "name": "cash_buyers", "schema": GROK_BUYER_SCHEMA, "strict": True}},
    }
    # xAI answers 429 / 5xx when the model is at capacity: two retries, 20 s then 40 s later.
    for attempt in range(3):
        try:
            r = httpx.post(f"{ps.XAI_BASE}/responses", json=body, timeout=ps.GROK_TIMEOUT,
                           headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        except httpx.TimeoutException as exc:
            raise BuyerFinderError(f"Grok buyer research timed out after {ps.GROK_TIMEOUT:.0f}s ({model}).") from exc
        except httpx.HTTPError as exc:
            raise BuyerFinderError(f"xAI unreachable: {type(exc).__name__}") from exc
        if (r.status_code == 429 or r.status_code >= 500) and attempt < 2:
            time.sleep(20 * (attempt + 1))
            continue
        break
    if r.status_code >= 400:
        raise BuyerFinderError(ps._xai_error(r))
    resp = r.json()
    if resp.get("status") != "completed":
        raise BuyerFinderError(f"Grok buyer research did not complete ({resp.get('status')}: "
                               f"{resp.get('incomplete_details') or resp.get('error')})")
    try:
        return json.loads(ps._message_text(resp)), resp
    except ValueError as exc:
        raise BuyerFinderError("Grok buyer research returned no structured result.") from exc


def _http(url: Optional[str]) -> Optional[str]:
    u = (url or "").strip()
    return u if u.lower().startswith(("http://", "https://")) and urlsplit(u).hostname else None


def _read(urls: list[str]) -> dict[str, tuple[Optional[str], Optional[str]]]:
    """url → (normalized page text, None) | (None, reason). Robots + crawl delay per host (property_scan's
    reader), hosts in parallel; people-search / social hosts are never fetched."""
    by_host: dict[str, list[str]] = {}
    out: dict[str, tuple[Optional[str], Optional[str]]] = {}
    n = 0
    for u in dict.fromkeys(urls):
        if op._no_fetch(u):
            out[u] = (None, "site's terms forbid automated access — not re-read")
        elif n < READ_MAX_PAGES:
            by_host.setdefault(urlsplit(u).hostname or "", []).append(u)
            n += 1
        else:
            out[u] = (None, "not re-read (page limit)")
    if by_host:
        with ThreadPoolExecutor(max_workers=min(6, len(by_host))) as ex:
            for res in ex.map(ps._read_pages, by_host.values()):
                out.update(res)
    return out


def _name_pats(company: str) -> list[str]:
    toks = [t for t in re.findall(r"[a-z0-9]+", ps._norm(company)) if len(t) >= 3 and t not in _NAME_NOISE]
    if not toks:   # e.g. "We Buy Houses Texas": all generic words — need the whole phrase
        phrase = ps._norm(company)
        return [rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])"] if phrase else []
    return [op._word(t) for t in toks[:3]]


def _email_pat(email: str) -> str:
    return re.escape(ps._norm(email.replace("@", " ")))


def _numbers_on_page(quote: str, page: str) -> bool:
    nums = re.findall(r"\d[\d,]*(?:\.\d+)?", quote or "")
    flat = page.replace(",", "")
    return bool(nums) and all(n.replace(",", "") in flat for n in nums)


def _area_clean(a: str) -> Optional[str]:
    """'Fort Worth, TX' → 'Fort Worth' (stored comma-separated, so no commas inside an area); a bare state
    name → None (the market's state is stored separately)."""
    from app.services.gov_sources import state_code
    parts = [x.strip() for x in (a or "").split(",") if x.strip()]
    if len(parts) > 1 and state_code(parts[-1]):
        parts = parts[:-1]
    s = " ".join(" ".join(parts).split()).strip(" .")[:60]
    return None if not s or state_code(s) else s


def _contact_state(pat: str, urls: list[str], pages: dict, searched: set) -> str:
    """'verified' (printed on a page we re-read) | 'unchecked' (a search-result page we couldn't read) | 'absent'."""
    readable = [u for u in urls if pages.get(u, (None,))[0] is not None]
    if any(re.search(pat, pages[u][0]) for u in readable):
        return "verified"
    if any(u not in readable and ps._was_touched(u, searched) for u in urls):
        return "unchecked"
    return "absent"


def web_buyers(key: str, model: str, m: Market, profile: dict) -> dict:
    """Grok research → verified candidates. {status, detail, candidates, cost_usd, ms}."""
    t0 = time.monotonic()
    try:
        out, resp = _grok_call(key, model, m, profile)
    except BuyerFinderError as exc:
        return {"status": "error", "detail": str(exc)[:400], "candidates": [], "cost_usd": 0.0,
                "ms": int((time.monotonic() - t0) * 1000)}
    usage = resp.get("usage") or {}
    cost = round((usage.get("cost_in_usd_ticks") or 0) / 1e10, 4)
    searched = ps.search_result_urls(resp)
    dropped: dict[str, int] = {}

    def drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    raw = [b for b in out.get("buyers") or [] if isinstance(b, dict)]
    pending = []
    for b in raw:
        company = " ".join((b.get("company") or "").split())[:120]
        url = _http(b.get("source_url"))
        areas = [x for x in (_area_clean(a) for a in b.get("areas") or [] if isinstance(a, str)) if x]
        if not company:
            drop("no company name")
        elif not url:
            drop("no source URL")
        elif not areas and not b.get("zips"):
            drop("no buying area stated")
        else:
            pending.append((b, company, url, areas))
    pages = _read([u for b, _, url, _ in pending for u in (url, _http(b.get("phone_source_url"))) if u])
    cands: list[Candidate] = []
    city_pats = [op._word(x) for x in {m.city, *(m.county and [f"{m.county} county"] or [])} if x]
    for b, company, url, areas in pending:
        text, why = pages.get(url, (None, "not re-read"))
        names = _name_pats(company)
        in_search = ps._was_touched(url, searched)
        quote = ps._norm(b.get("quote") or "")
        area_pats = city_pats + [op._word(a) for a in areas]
        checks: list[str] = []
        if text is not None:
            if not names or not all(re.search(p, text) for p in names):
                drop("company name not on the cited page")
                continue
            here = any(re.search(p, text) for p in area_pats)
            conf = "high" if here else "medium"
            checks.append("page re-read: company name" + (" and buying area on the page" if here else " on the page"))
        else:
            if not in_search:
                drop("source not in search results and not re-readable")
                continue
            named = bool(names) and all(re.search(p, quote) for p in names)
            conf = "medium" if named and any(re.search(p, quote) for p in area_pats) else "low"
            checks.append(f"page not re-read ({why}); search result"
                          + ("; quote names the company and area" if conf == "medium" else ""))
        # Contact: kept when printed on a page we re-read; from a search-result page we couldn't read it is
        # kept unchecked (confidence capped at medium); printed nowhere we can see → dropped.
        urls = list(dict.fromkeys(x for x in (url, _http(b.get("phone_source_url"))) if x))
        phone = email = None
        contact_ok = False
        e164 = normalize_us(b.get("phone") or "")
        em = (b.get("email") or "").strip().lower()
        em = em if re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", em) else ""
        for label, value, pat in (("phone", e164, e164 and op.number_pattern(e164)), ("email", em, em and _email_pat(em))):
            if not value:
                continue
            state = _contact_state(pat, urls, pages, searched)
            if state == "absent":
                drop(f"{label} not on the cited page ({label} dropped)")
                continue
            if label == "phone":
                phone = value
            else:
                email = value
            contact_ok = contact_ok or state == "verified"
            checks.append(f"{label} printed on the page" if state == "verified" else f"{label} not re-checked (page unreadable)")
        # Website: only one the search results, the cited page or its URL show (Grok can make up domains).
        website = _http(b.get("website"))
        host = site_key(website) if website else None
        if website and not (any(h == host or f"{h}{p}" == host for h, p, _ in searched)
                            or (text is not None and host.split("/")[0] in text) or host == site_key(url)):
            drop("website not seen in search results or on the page (website dropped)")
            website = None
        if not (phone or email or website):
            drop("no business contact (phone, email or website)")
            continue
        if conf == "high" and not contact_ok:
            conf = "medium"
        zips = _clean_zips(b.get("zips"))
        ev = [f"{(b.get('evidence') or '').strip()[:300]} — “{(b.get('quote') or '').strip()[:300]}” ({url})",
              f"Checks: {'; '.join(checks)}."]
        cond = (b.get("condition") or "").strip()
        if cond:
            ev.append(f"Condition they buy (stated): {cond[:120]}")
        lo, hi = b.get("price_min"), b.get("price_max")
        pq = (b.get("price_quote") or "").strip()
        price_ok = bool(pq) and (lo or hi) and (text is None or _numbers_on_page(pq, text))
        if price_ok:
            ev.append(f"Price range stated: “{pq[:160]}”" + ("" if text is not None else " (per search result)"))
        elif lo or hi:
            drop("stated price range not confirmed (price dropped)")
        for o in (b.get("other_sources") or [])[:3]:
            ou = _http(o.get("url") if isinstance(o, dict) else None)
            if ou and ps._url_key(ou) != ps._url_key(url) and ps._was_touched(ou, searched):
                ev.append(f"Also: {(o.get('what') or '').strip()[:160]} ({ou})")
        ptypes = [p for p in b.get("property_types") or [] if p in PROPERTY_TYPES]
        cands.append(Candidate(
            source="web_research", name=company, buyer_type=b.get("buyer_type") if b.get("buyer_type") in BUYER_TYPES
            else "investor_company", confidence=conf, source_url=url, evidence=ev, phone=phone, email=email,
            website=website, states=[m.state], cities=areas, zips=zips, property_types=ptypes,
            min_price=float(lo) if price_ok and lo else None, max_price=float(hi) if price_ok and hi else None))
    detail = "; ".join(x for x in (
        f"{len(cands)} of {len(raw)} kept",
        f"dropped {sum(dropped.values())} ({', '.join(f'{k}: {v}' for k, v in dropped.items())})" if dropped else "",
        f"{(usage.get('server_side_tool_usage_details') or {}).get('web_search_calls', 0)} searches, "
        f"{time.monotonic() - t0:.0f}s, {resp.get('model') or model}") if x)
    return {"status": "ok" if cands else "empty", "detail": detail, "candidates": cands, "cost_usd": cost,
            "summary": (out.get("summary") or "").strip()[:600], "ms": int((time.monotonic() - t0) * 1000)}


# ── Dedupe + upsert ──────────────────────────────────────────────────────────

_LEGAL = {"llc", "l", "c", "ll", "inc", "incorporated", "lp", "llp", "ltd", "co", "corp", "corporation", "company",
          "the", "pllc", "lc", "and", "dba"}


def company_key(name: Optional[str]) -> str:
    return " ".join(t for t in re.findall(r"[a-z0-9]+", (name or "").lower()) if t not in _LEGAL)


def site_key(url: Optional[str]) -> Optional[str]:
    p = urlsplit((url or "").strip())
    host = (p.hostname or "").lower().removeprefix("www.")
    if not host:
        return None
    if any(host == h or host.endswith("." + h) for h in _SHARED_HOSTS):
        return host + (p.path or "/").rstrip("/").lower()
    return host


def _mailing_key(addr: Optional[str]) -> Optional[str]:
    if not addr:
        return None
    zip5 = (re.findall(r"\b(\d{5})(?:-\d{4})?\s*$", addr) or [""])[0]
    return f"{ps._norm(addr.split(',')[0])}|{zip5}"


def _keys(name, phone, website, mailing) -> list[str]:
    out = [f"n:{company_key(name)}"] if company_key(name) else []
    if phone:
        out.append(f"p:{phone}")
    if site_key(website):
        out.append(f"w:{site_key(website)}")
    if mailing:
        out.append(f"m:{mailing}")
    return out


def _split(s: Optional[str], sep: str = ",") -> list[str]:
    return [x.strip() for x in (s or "").split(sep) if x.strip()]


def _union(old: Optional[str], new: list[str], sep: str, limit: int) -> Optional[str]:
    items = _split(old)
    low = {x.lower() for x in items}
    for x in new:
        if x and x.lower() not in low:
            items.append(x)
            low.add(x.lower())
    joined = sep.join(items)
    while len(joined) > limit and items:
        items.pop()
        joined = sep.join(items)
    return joined or None


def _merge_evidence(old: Optional[str], new: list[str]) -> str:
    """Holding lines replace the same (kind, area); other lines are appended once."""
    lines = [x for x in (old or "").splitlines() if x.strip()]
    for ln in new:
        m = _HOLDING_RE.match(ln)
        if m:
            lines = [x for x in lines if not ((h := _HOLDING_RE.match(x)) and h.group(2, 3) == m.group(2, 3))]
            lines.append(ln)
        elif ln not in lines:
            lines.append(ln)
    return "\n".join(lines)


_BAND_RE = re.compile(rf"^{PRICE_BAND_PREFIX} .*? inferred from the (.+?)s of the homes they own", re.M)


def _with_band(evidence: str, lo: Optional[float], hi: Optional[float], label: Optional[str]) -> str:
    """Evidence with its inferred price-band line rewritten (or removed when ``label`` is None)."""
    lines = [x for x in evidence.splitlines() if not x.startswith(PRICE_BAND_PREFIX)]
    if label and lo is not None and hi is not None:
        lines.append(price_band_line(lo, hi, label))
    return "\n".join(lines)[-8000:]


def buyer_dict(b: Buyer) -> dict:
    from app.schemas import BuyerOut
    return BuyerOut.model_validate(b).model_dump(mode="json")


def _create(c: Candidate, now: datetime) -> Buyer:
    return Buyer(
        name=c.name[:120], company=c.name[:120], phone=c.phone, email=(c.email or None) and c.email[:255],
        target_states=",".join(c.states) or None, target_cities=_union(None, c.cities, ", ", 255),
        target_zips=_union(None, c.zips, ",", 500), property_types=_union(None, c.property_types, ",", 255),
        min_price=c.min_price, max_price=c.max_price, ranking=_RANKING[c.confidence], active=True, source=c.source,
        buyer_type=c.buyer_type, website=(c.website or None) and c.website[:500],
        mailing_address=(c.mailing_address or None) and c.mailing_address[:255],
        evidence=_with_band(_merge_evidence(None, c.evidence), c.min_price, c.max_price, c.band_label),
        properties_owned=c.properties_owned, last_purchase=c.last_purchase, source_url=c.source_url,
        confidence=c.confidence, found_at=now)


def _merge(b: Buyer, c: Candidate) -> bool:
    """Fold a candidate into a finder-made buyer; True if anything changed. A price range the buyer
    states beats one inferred from holdings; inferred bands from several areas are unioned."""
    before = {k: getattr(b, k) for k in ("phone", "email", "website", "mailing_address", "target_states",
                                         "target_cities", "target_zips", "property_types", "min_price", "max_price",
                                         "evidence", "properties_owned", "last_purchase", "confidence", "ranking")}
    b.phone = b.phone or c.phone
    b.email = b.email or ((c.email or None) and c.email[:255])
    b.website = b.website or ((c.website or None) and c.website[:500])
    b.mailing_address = b.mailing_address or ((c.mailing_address or None) and c.mailing_address[:255])
    b.target_states = _union(b.target_states, c.states, ",", 255)
    b.target_cities = _union(b.target_cities, c.cities, ", ", 255)
    b.target_zips = _union(b.target_zips, c.zips, ",", 500)
    b.property_types = _union(b.property_types, c.property_types, ",", 255)
    band = _BAND_RE.search(b.evidence or "")
    label = band.group(1) if band else None
    stated = (b.min_price is not None or b.max_price is not None) and not label
    if not stated and c.min_price is not None and c.max_price is not None:
        if c.band_label is None:                       # the candidate states its range
            b.min_price, b.max_price, label = c.min_price, c.max_price, None
        else:
            b.min_price = min(x for x in (b.min_price, c.min_price) if x is not None)
            b.max_price = max(x for x in (b.max_price, c.max_price) if x is not None)
            label = c.band_label
    new_ev = list(c.evidence)
    if c.source != b.source:
        new_ev.append(f"Also found via {c.source.replace('_', ' ')}: {c.source_url}")
    b.evidence = _with_band(_merge_evidence(b.evidence, new_ev), b.min_price, b.max_price, label)
    held = holdings_from_evidence(b.evidence)
    if held:
        b.properties_owned = sum(held.values())
    b.last_purchase = max(filter(None, (b.last_purchase, c.last_purchase)), default=None)
    if _CONF_RANK.get(c.confidence, 0) > _CONF_RANK.get(b.confidence, 0):
        b.confidence = c.confidence
        b.ranking = min(b.ranking or 99, _RANKING[c.confidence])
    return any(getattr(b, k) != v for k, v in before.items())


def upsert(db: Session, cands: list[Candidate]) -> list[tuple[Buyer, str]]:
    """Dedupe candidates against the network and each other → [(buyer, added | updated | existing)].
    Buyers someone entered by hand (source not a finder source) are matched but never changed."""
    now = utcnow()
    index: dict[str, Buyer] = {}
    for b in db.execute(select(Buyer)).scalars():
        for k in _keys(b.company or b.name, b.phone, b.website, _mailing_key(b.mailing_address)):
            index.setdefault(k, b)
        if b.company and b.name and company_key(b.name) != company_key(b.company):
            index.setdefault(f"n:{company_key(b.name)}", b)
    status: dict[int, tuple[Buyer, str]] = {}
    for c in cands:
        keys = _keys(c.name, c.phone, c.website, c.mailing_key or _mailing_key(c.mailing_address))
        b = next((index[k] for k in keys if k in index), None)
        if b is None:
            b = _create(c, now)
            db.add(b)
            status[id(b)] = (b, "added")
        elif b.source in FINDER_SOURCES and _merge(b, c) and status.get(id(b), (b, ""))[1] != "added":
            status[id(b)] = (b, "updated")
        else:
            status.setdefault(id(b), (b, "existing"))
        for k in keys:
            index.setdefault(k, b)
    db.flush()
    return list(status.values())


# ── Market search bookkeeping (setting ``buyer_markets_searched``) ──────────

def searched_markets(db: Session) -> dict[str, dict]:
    from app.services.settings import get_setting
    try:
        data = json.loads(get_setting(db, SETTING_KEY) or "{}")
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _fresh(iso: Optional[str], ttl: timedelta = RESEARCH_TTL) -> bool:
    if not iso:
        return False
    try:
        return utcnow() - datetime.fromisoformat(iso.rstrip("Z")) < ttl
    except ValueError:
        return False


def _record(db: Session, m: Market, web: dict, parcels: dict, added: int, updated: int, cost: float) -> None:
    from app.services.settings import set_setting
    data = searched_markets(db)
    now = utcnow().isoformat() + "Z"
    rec = data.get(m.key) or {}
    rec.update({"city": m.city, "state": m.state, "last_at": now,
                "added": int(rec.get("added") or 0) + added, "updated": int(rec.get("updated") or 0) + updated,
                "cost_usd": round(float(rec.get("cost_usd") or 0) + cost, 4)})
    if m.county:
        rec["county"] = m.county
    errors = {}
    if web["status"] in ("ok", "empty"):
        rec["web_at"] = now
    elif web["status"] == "error":
        errors["web_research"] = web["detail"][:300]
    if parcels["status"] in ("ok", "empty"):
        areas = rec.get("areas") or {}
        for a in parcels.get("areas") or []:
            areas[a] = now
        rec["areas"] = areas
        rec["parcel_layer"] = True
    elif parcels["status"] == "skipped" and "No county parcel layer" in parcels["detail"]:
        rec["parcel_layer"] = False
    elif parcels["status"] == "error":
        errors["county_parcels"] = parcels["detail"][:300]
    rec["errors"] = errors
    data[m.key] = rec
    set_setting(db, SETTING_KEY, json.dumps(data, sort_keys=True))


def _grok_key(db: Session) -> str:
    return ps._key(db, ps.CATALOG_BY_ID["grok"])


def _needs(rec: dict, m: Market, has_key: bool) -> tuple[bool, list[str]]:
    """(web research due, area labels whose parcel search is due) for a market's record."""
    web_due = has_key and not _fresh(rec.get("web_at"))
    if rec.get("parcel_layer") is False or (m.state not in _layer_states()):
        return web_due, []
    areas = rec.get("areas") or {}
    want = [f"ZIP {z}" for z in m.zips] or [m.city if not m.county_wide else f"{_county_name(m.city)} County"]
    if rec.get("parcel_layer") is None:
        return web_due, want
    return web_due, [a for a in want if not _fresh(areas.get(a))]


# ── Entry points ─────────────────────────────────────────────────────────────

def _run(db: Session, m: Market, *, web: bool = True, parcels: bool = True) -> dict:
    t0 = time.monotonic()
    key = _grok_key(db)
    model = ps.scan_model(db)
    profile = _profile(db, m)
    with ThreadPoolExecutor(max_workers=2) as ex:
        fw = ex.submit(web_buyers, key, model, m, profile) if web and key else None
        fp = ex.submit(parcel_investors, m) if parcels else None
        w = fw.result() if fw else {"status": "skipped", "candidates": [], "cost_usd": 0.0, "ms": 0,
                                    "detail": "No xAI API key in Settings." if web else "Searched in the last 30 days."}
        p = fp.result() if fp else {"status": "skipped", "candidates": [], "ms": 0, "areas": [],
                                    "detail": "Searched in the last 30 days."}
    res = upsert(db, w["candidates"] + p["candidates"])
    n = {s: sum(1 for _, x in res if x == s) for s in ("added", "updated", "existing")}
    cost = float(w.get("cost_usd") or 0)
    _record(db, m, w, p, n["added"], n["updated"], cost)
    db.flush()
    return {
        "market": {"city": m.city, "state": m.state, "zips": m.zips, "county": m.county, "key": m.key},
        **n,
        "buyers": [{**buyer_dict(b), "status": s} for b, s in res],
        "sources": [
            {"id": "web_research", "name": "Grok web research", "status": w["status"], "detail": w["detail"],
             "found": len(w["candidates"]), "cost_usd": cost, "ms": w["ms"]},
            {"id": "county_parcels", "name": "County parcel investors", "status": p["status"], "detail": p["detail"],
             "found": len(p["candidates"]), "cost_usd": 0.0, "ms": p["ms"]},
        ],
        "summary": w.get("summary") or "",
        "cost_usd": round(cost, 4), "duration_ms": int((time.monotonic() - t0) * 1000),
    }


def find_buyers_for_market(db: Session, city: str, state: str, zips: Optional[list[str]] = None) -> dict:
    """Run both sources for a market (city + state; ``zips`` narrows the parcel search to those ZIPs)."""
    city, state = " ".join((city or "").split()), (state or "").strip().upper()
    if not city or len(state) != 2:
        raise BuyerFinderError("City and 2-letter state are required.")
    m = Market(city=city, state=state, zips=_clean_zips(zips), county=_market_county(db, city, state))
    return _run(db, m)


def find_buyers_for_lead(db: Session, lead: Lead) -> dict:
    """Run both sources for the lead's market: its city / state, its ZIP for the parcel search."""
    m = market_for_lead(lead)
    if not m.city or len(m.state) != 2:
        raise BuyerFinderError("This property has no city / state to search buyers in.")
    m.county = m.county or _market_county(db, m.city, m.state)
    return _run(db, m)


def markets(db: Session) -> list[Market]:
    """Distinct markets (city + state) of active properties, with their ZIPs."""
    out: dict[str, Market] = {}
    counties: dict[str, str] = {}
    for lead in active_leads(db):
        city, state = " ".join((lead.property_city or "").split()), (lead.property_state or "").strip().upper()
        if not city or len(state) != 2:
            continue
        m = out.setdefault(market_key(city, state), Market(city=city, state=state))
        for z in _clean_zips([lead.property_zip]):
            if z not in m.zips:
                m.zips.append(z)
        rec = lead.property_record
        if rec and rec.county:
            counties.setdefault(m.key, _county_name(rec.county))
    for k, m in out.items():
        m.zips.sort()
        if m.county_wide:
            m.county = _county_name(m.city)
        else:
            m.county = counties.get(k)
    return list(out.values())


def market_coverage(db: Session) -> list[dict]:
    """Per market: properties, buyers covering it, last search, what's due."""
    from app.services.buyers import area_match
    recs = searched_markets(db)
    has_key = bool(_grok_key(db))
    buyers = db.execute(select(Buyer).where(Buyer.active == True)).scalars().all()  # noqa: E712
    counts: dict[str, int] = {}
    for lead in active_leads(db):
        k = market_key(lead.property_city or "", lead.property_state or "")
        counts[k] = counts.get(k, 0) + 1
    rows = []
    for m in markets(db):
        rec = recs.get(m.key) or {}
        county = m.county or rec.get("county")
        covering = [b for b in buyers if any(area_match(b, m.state, m.city, z, county)[0] > 0
                                             for z in (m.zips or [None]))]
        web_due, areas_due = _needs(rec, m, has_key)
        rows.append({
            "key": m.key, "city": m.city, "state": m.state, "county": county, "zips": m.zips,
            "properties": counts.get(m.key, 0), "buyers": len(covering),
            "buyers_by_source": {s: sum(1 for b in covering if (b.source or "network") == s)
                                 for s in sorted({b.source or "network" for b in covering})},
            "last_searched": rec.get("last_at"), "web_searched": rec.get("web_at"),
            "parcel_layer": rec.get("parcel_layer"), "areas_searched": rec.get("areas") or {},
            "errors": rec.get("errors") or {}, "due": bool(web_due or areas_due),
            "cost_usd": rec.get("cost_usd") or 0.0,
        })
    rows.sort(key=lambda r: (-r["properties"], r["state"], r["city"]))
    return rows


def find_all(db: Session, limit: int = 1) -> dict:
    """Search up to ``limit`` markets that are due (never searched, or searched over 30 days ago; a market
    whose last attempt had an error waits 6 hours). Call repeatedly until ``remaining`` is 0."""
    recs = searched_markets(db)
    has_key = bool(_grok_key(db))
    all_markets = markets(db)
    due = []
    for m in all_markets:
        rec = recs.get(m.key) or {}
        web_due, areas_due = _needs(rec, m, has_key)
        if (web_due or areas_due) and not (rec.get("errors") and _fresh(rec.get("last_at"), RETRY_AFTER)):
            due.append((m, web_due, bool(areas_due)))
    due.sort(key=lambda x: (-len(x[0].zips), x[0].state, x[0].city))
    processed, cost = [], 0.0
    for m, web_due, parcels_due in due[:max(1, limit)]:
        try:
            r = _run(db, m, web=web_due, parcels=parcels_due)
        except Exception as exc:  # one market never breaks the batch; record the attempt so the loop moves on
            db.rollback()
            _record(db, m, {"status": "error", "detail": f"{type(exc).__name__}: {exc}"},
                    {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}, 0, 0, 0.0)
            r = {"market": {"city": m.city, "state": m.state, "zips": m.zips, "key": m.key}, "added": 0,
                 "updated": 0, "existing": 0, "cost_usd": 0.0, "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                 "sources": []}
        db.commit()
        cost += r.get("cost_usd") or 0.0
        processed.append({k: r.get(k) for k in ("market", "added", "updated", "existing", "sources", "cost_usd",
                                                 "duration_ms", "error")})
    return {"processed": processed, "remaining": max(0, len(due) - len(processed)), "total_markets": len(all_markets),
            "cost_usd": round(cost, 4)}
