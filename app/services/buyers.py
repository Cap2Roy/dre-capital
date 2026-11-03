"""Buyer-network matching: find cash buyers whose buy-box fits a property.

Manual: "Send to your top five buyers first, matched by buy box (area, price
band, beds and baths, rehab level). Then the wider list, then a marketplace."

Every match carries a score and the reasons behind it ("buys in 76119", "owns 14 homes in
76119", "price band fits"). A buyer must cover the property's area to match: its ZIP, city,
county or metro — or, for a buyer that lists no cities / ZIPs, its state.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Buyer, Lead, Valuation
from app.timeutil import utcnow

_CONF_RANK = {None: 0, "low": 1, "medium": 2, "high": 3}
_HOUSE_TYPES = {"single_family", "multi_family", "condo", "townhouse"}
_TYPE_LABEL = {"single_family": "single-family homes", "multi_family": "multi-family", "condo": "condos",
               "townhouse": "townhouses", "mobile_home": "mobile homes", "land": "land / lots",
               "commercial": "commercial"}
# Regions buyers name instead of a city → the counties they cover.
_METROS = {
    "dfw": {"tarrant", "dallas", "collin", "denton", "johnson", "parker", "ellis", "kaufman", "rockwall", "wise"},
    "dallas-fort worth": {"tarrant", "dallas", "collin", "denton", "johnson", "parker", "ellis", "kaufman", "rockwall",
                          "wise"},
    "metroplex": {"tarrant", "dallas", "collin", "denton", "johnson", "parker", "ellis", "kaufman", "rockwall", "wise"},
    "north texas": {"tarrant", "dallas", "collin", "denton", "johnson", "parker", "ellis", "kaufman", "rockwall", "wise",
                    "grayson", "hood", "hunt"},
    "central florida": {"orange", "osceola", "seminole", "lake", "polk", "volusia", "marion"},
    "east tennessee": {"knox", "blount", "sevier", "anderson", "loudon", "jefferson", "hamblen"},
}
_HOLDING_RE = re.compile(r"^Owns (\d+) (homes|vacant lots) in (.+?) \(", re.M)
_RENT_RE = re.compile(r"rent", re.I)


@dataclass
class BuyerMatch:
    buyer: Buyer
    score: int
    reasons: list[str] = field(default_factory=list)


def lead_value(lead: Lead, valuation: Optional[Valuation] = None) -> tuple[Optional[float], str]:
    """(value to compare with buyers' price bands, what it is): MAO, else assessed value, else estimated
    value, else a sale price from the property's listings (rents ignored)."""
    if valuation is not None and valuation.mao and valuation.mao > 0:
        return float(valuation.mao), "MAO"
    if lead.assessed_value:
        return float(lead.assessed_value), "assessed value"
    rec = lead.property_record
    if rec is not None and rec.est_value:
        return float(rec.est_value), "estimated value"
    for x in lead.listings or []:
        if x.price and x.price > 1000 and not _RENT_RE.search(x.price_label or "") and x.status != "rental":
            return float(x.price), (x.price_label or "listing price").lower()
    return None, ""


def lead_property_type(lead: Lead) -> tuple[Optional[str], str]:
    """(single_family | multi_family | condo | mobile_home | land | commercial | None, how we know)."""
    rec = lead.property_record
    t = ((rec.property_type if rec is not None else None) or "").lower()
    for words, kind in ((("land", "lot", "vacant"), "land"), (("multi", "duplex", "triplex", "fourplex", "apartment"),
                        "multi_family"), (("condo",), "condo"), (("townhouse", "townhome"), "townhouse"),
                        (("manufactured", "mobile"), "mobile_home"), (("single",), "single_family"),
                        (("commercial", "industrial", "retail", "office"), "commercial")):
        if any(w in t for w in words):
            return kind, f"record: {rec.property_type}"
    notes = (lead.notes or "").lower()
    if re.search(r"\b(fourplex|triplex|duplex|multi-?family)\b", notes):
        return "multi_family", "sale notice"
    if re.search(r"\b(vacant (lot|land)|unimproved|raw land)\b", notes):
        return "land", "sale notice"
    if lead.sqft and lead.sqft > 10000:
        return "commercial", f"{lead.sqft:,} sq ft building"
    if lead.sqft or lead.beds or lead.year_built:
        return "single_family", "has a house (sq ft / beds / year built)"
    if lead.assessed_value and lead.assessed_value < 60000:
        return "land", "no building on record and low assessed value (inferred)"
    return None, ""


def _areas(b: Buyer) -> list[str]:
    return [x.strip().lower() for x in (b.target_cities or "").split(",") if x.strip()]


def _word_in(needle: str, hay: str) -> bool:
    return bool(needle) and re.search(rf"(?<![a-z]){re.escape(needle)}(?![a-z])", hay) is not None


def area_match(b: Buyer, state: str, city: str, zip5: Optional[str], county: Optional[str]) -> tuple[int, list[str]]:
    """(score, reasons) for the buyer covering this area; 0 when it doesn't."""
    state, city = (state or "").upper(), (city or "").strip().lower()
    county = re.sub(r"\s+county$", "", (county or "").strip().lower())
    states = {s.strip().upper() for s in (b.target_states or "").split(",") if s.strip()}
    if states and state and state not in states:
        return 0, []
    zips = {z.strip() for z in (b.target_zips or "").split(",") if z.strip()}
    areas = _areas(b)
    held = {(kind, area): n for n, kind, area in _HOLDING_RE.findall(b.evidence or "")}
    owned = lambda area: [f"{n} {kind}" for (kind, a), n in held.items() if a.lower() == area.lower()]  # noqa: E731
    if zip5 and zip5 in zips:
        own = owned(f"ZIP {zip5}")
        return 5, [f"owns {' + '.join(own)} in {zip5}" if own else f"buys in {zip5}"]
    if city and any(_word_in(city, a) for a in areas):
        own = [f"{n} {k} in ZIP {a[4:]}" for (k, a), n in held.items() if a.startswith("ZIP ")]
        if own:
            return 3, [f"owns {', '.join(own[:3])} in {city.title()}"]
        return 3, [f"buys in {city.title()}"]
    if county and any(_word_in(f"{county} county", a) for a in areas):
        return 3, [f"buys in {county.title()} County"]
    if county:
        for a in areas:
            metro = next((k for k in _METROS if _word_in(k, a)), None)
            if metro and county in _METROS[metro]:
                return 2, [f"buys across {a.title()}"]
    if states and state and not areas and not zips:
        return 1, [f"buys anywhere in {state}"]
    return 0, []


def _price_fit(b: Buyer, value: Optional[float], what: str) -> tuple[int, Optional[str]]:
    if not value or (b.min_price is None and b.max_price is None):
        return 0, None
    lo, hi = b.min_price or 0, b.max_price or float("inf")
    band = f"${lo / 1000:,.0f}k–" + (f"${hi / 1000:,.0f}k" if hi != float("inf") else "up")
    if lo <= value <= hi:
        return 2, f"price band fits ({what} ${value / 1000:,.0f}k in {band})"
    if lo * 0.75 <= value <= hi * 1.25:
        return 1, f"near price band ({what} ${value / 1000:,.0f}k vs {band})"
    return -2, f"outside price band ({what} ${value / 1000:,.0f}k vs {band})"


def _type_fit(b: Buyer, kind: Optional[str]) -> tuple[int, Optional[str]]:
    types = {t.strip() for t in (b.property_types or "").split(",") if t.strip()}
    if not kind or not types:
        return 0, None
    if kind in types:
        return 2, f"buys {_TYPE_LABEL.get(kind, kind)}"
    if kind in _HOUSE_TYPES and types & _HOUSE_TYPES:
        return 1, "buys houses"
    return -3, f"doesn't list {_TYPE_LABEL.get(kind, kind)}"


def _recent(b: Buyer) -> Optional[str]:
    try:
        d = datetime.fromisoformat(b.last_purchase) if b.last_purchase else None
    except ValueError:
        return None
    return f"bought as recently as {b.last_purchase}" if d and utcnow() - d < timedelta(days=365) else None


def rank_buyers_for_lead(db: Session, lead: Lead, valuation: Optional[Valuation] = None) -> list[BuyerMatch]:
    """Active buyers covering the property's area, scored on area, property type, price band, beds,
    rehab level, recent activity and a phone / email on file; best first (ties: confidence, then ranking)."""
    buyers = db.execute(select(Buyer).where(Buyer.active == True)).scalars().all()  # noqa: E712
    rec = lead.property_record
    county = rec.county if rec is not None else None
    if not county and (lead.property_city or "").lower().endswith(" county"):
        county = lead.property_city
    value, what = lead_value(lead, valuation)
    kind, _ = lead_property_type(lead)
    beds = lead.beds or 0
    rehab = (lead.rehab_level or "").lower()
    out: list[BuyerMatch] = []
    for b in buyers:
        score, reasons = area_match(b, lead.property_state or "", lead.property_city or "",
                                    (lead.property_zip or "")[:5] or None, county)
        if score <= 0:
            continue
        for pts, why in (_type_fit(b, kind), _price_fit(b, value, what)):
            score += pts
            if why:
                reasons.append(why)
        if b.min_beds is not None and b.max_beds is not None and beds and b.min_beds <= beds <= b.max_beds:
            score += 1
            reasons.append(f"{beds} beds in their range")
        if rehab and b.rehab_level:
            if rehab == b.rehab_level.lower():
                score += 2
                reasons.append(f"rehab level {b.rehab_level}")
            elif rehab in b.rehab_level.lower() or b.rehab_level.lower() in rehab:
                score += 1
                reasons.append(f"rehab level close ({b.rehab_level})")
        recent = _recent(b)
        if recent:
            score += 1
            reasons.append(recent)
        if b.phone or b.email:
            score += 1
            reasons.append("phone / email on file")
        out.append(BuyerMatch(b, score, reasons))
    out.sort(key=lambda m: (-m.score, -_CONF_RANK.get(m.buyer.confidence, 0), m.buyer.ranking))
    return out


def match_buyers_for_lead(db: Session, lead: Lead, valuation: Optional[Valuation] = None) -> list[Buyer]:
    """Buyers of ``rank_buyers_for_lead``, best first."""
    return [m.buyer for m in rank_buyers_for_lead(db, lead, valuation)]
