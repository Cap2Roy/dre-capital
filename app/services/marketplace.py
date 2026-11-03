"""Marketplace check: where is this property listed for sale?

``find_listings(db, lead)`` stores one ``MarketplaceListing`` per page (unique per lead + URL):

1. **Origin** — the government / sheriff sale page the lead was imported from (the
   "Scraped sale info: Program …; Minimum bid $…; (source: URL)" line in ``lead.notes``).
   Deterministic, no network; ``check=confirmed``, ``source=import``.
2. **RentCast MLS listing** already stored on ``lead.property_record`` (``source=rentcast``).
3. **Grok web search** (xAI Responses API + server-side ``web_search``): the exact address on
   portals (Zillow, Redfin, Realtor.com, …), auction sites, classifieds, government REO sites
   and broker sites. A listing is kept only when
     - its URL was returned by the search engine (a page Grok only "opened" or cited must also be
       re-read and confirmed by us — models open and cite made-up portal URLs), and
     - the property's house number + street (+ city or ZIP) is shown by the URL itself, by the
       page we re-read, or — for sites that block bots — by the search snippet Grok quotes.
   URLs that spell out a *different* address of the same city/ZIP are dropped as wrong-property
   matches. Pages are re-read only where robots.txt allows our bot (≤1 request/s per host);
   portals whose terms forbid bots (Zillow, Redfin, Realtor.com, Facebook, Craigslist, …) are
   never fetched and stay ``check=unchecked``.
4. Listing-agent phones → ``phones.upsert_phone(role="listing_agent", source="listing")``.

Model: grok-4.3. Measured Nov 2026 on six real government-sale properties (two runs each):
grok-4.3 ~$0.10 and ~20 s per property and the best recall (8 sites for the one MLS-listed property,
record pages on two more); grok-4.20-non-reasoning ~$0.13 and ~17 s, fewer finds, reported a city
search page as a Zillow listing and a made-up Realtor.com URL it had "opened".
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx
from sqlalchemy.orm import Session

from app.models import Lead, MarketplaceListing
from app.services.phones import upsert_phone
from app.services.property_data import full_address
from app.services.property_scan import (
    GROK_TIMEOUT, XAI_BASE, ScanError, Target, _message_text, _read_pages, _target, _touched_urls,
    _url_key, _was_touched, _xai_error, is_official, search_result_urls,
)
from app.timeutil import iso_utc, utcnow

MODEL = "grok-4.3"
MAX_TOOL_CALLS = 12
MAX_PAGE_CHECKS = 8
STATUSES = ("for_sale", "pending", "sold", "off_market", "auction", "rental", "unknown")
KINDS = ("marketplace", "auction", "government", "mls", "classified")

# host suffix → (site id, label, kind, robots/ToS lets our bot re-read the page)
SITES: dict[str, tuple[str, str, str, bool]] = {
    "zillow.com": ("zillow", "Zillow", "marketplace", False),
    "redfin.com": ("redfin", "Redfin", "marketplace", False),
    "realtor.com": ("realtor", "Realtor.com", "marketplace", False),
    "trulia.com": ("trulia", "Trulia", "marketplace", False),
    "homes.com": ("homes", "Homes.com", "marketplace", False),
    "movoto.com": ("movoto", "Movoto", "marketplace", False),
    "har.com": ("har", "HAR.com", "marketplace", False),
    "auction.com": ("auction_com", "Auction.com", "auction", False),
    "hubzu.com": ("hubzu", "Hubzu", "auction", False),
    "xome.com": ("xome", "Xome", "auction", False),
    "realtytrac.com": ("realtytrac", "RealtyTrac", "marketplace", False),
    "bid4assets.com": ("bid4assets", "Bid4Assets", "auction", False),
    "facebook.com": ("facebook", "Facebook Marketplace", "classified", False),
    "craigslist.org": ("craigslist", "Craigslist", "classified", False),
    "loopnet.com": ("loopnet", "LoopNet", "marketplace", False),
    "crexi.com": ("crexi", "Crexi", "marketplace", False),
    "land.com": ("land_com", "Land.com", "marketplace", False),
    "landwatch.com": ("landwatch", "LandWatch", "marketplace", False),
    "landsofamerica.com": ("landsofamerica", "Lands of America", "marketplace", False),
    "hudhomestore.gov": ("hud", "HUD Home Store", "government", True),
    "homepath.fanniemae.com": ("homepath", "Fannie Mae HomePath", "government", True),
    "homesteps.com": ("homesteps", "Freddie Mac HomeSteps", "government", True),
    "listings.vrmco.com": ("va", "VA REO (Vendor Resource Mgmt)", "government", True),
    "irsauctions.gov": ("irs", "IRS auction", "government", True),
    "treasury.gov": ("treasury", "U.S. Treasury auction", "government", True),
    "cwsmarketing.com": ("cws", "CWS Marketing (Treasury auctions)", "auction", True),
    "properties.sc.egov.usda.gov": ("usda", "USDA property sales", "government", True),
    "fdicrealestatelistings.com": ("fdic", "FDIC real estate listings", "government", True),
    "tarrantcountytx.gov": ("tarrant_constable", "Tarrant County Constable sale", "government", True),
}

# "Program …" text of an imported lead → (site id, label, kind, sale is an auction)
_PROGRAMS = (
    (r"tarrant county constable", "tarrant_constable", "Tarrant County Constable Pct 3", True),
    (r"treasury", "treasury", "U.S. Treasury auction", True),
    (r"\birs\b", "irs", "IRS auction", True),
    (r"usda", "usda", None, None),             # label = program text; foreclosure sale → auction, REO → for sale
    (r"fdic", "fdic", "FDIC failed-bank real estate", False),
)
_NOTE_RE = re.compile(r"Scraped sale info:\s*(?P<body>.*?)\s*\(source:\s*(?P<url>https?://[^\s)]+)\)")
_FIELD_RE = re.compile(r"(Program|Sale date|Case|Account|Status|Plaintiff/lender|Amount owed|Minimum bid|List price)\s+(.*)", re.S)
_DIRECTIONALS = {"n", "s", "e", "w", "ne", "nw", "se", "sw", "north", "south", "east", "west",
                 "northeast", "northwest", "southeast", "southwest"}
_SUFFIXES = {"st", "street", "ave", "av", "avenue", "rd", "road", "dr", "drive", "ct", "court", "ln", "lane", "blvd",
             "boulevard", "way", "pl", "place", "cir", "circle", "ter", "terrace", "pkwy", "parkway", "hwy", "highway",
             "trl", "trail", "loop", "sq", "square", "cv", "cove", "pt", "point", "xing", "crossing", "run", "pass",
             "path", "row", "aly", "alley"}
_UNIT_RE = re.compile(r"(\s#|\b(apt|unit|ste|suite|bldg)\b).*$", re.I)
# Portals whose property pages always spell the address in the URL; their other URLs are search / area pages.
ADDRESS_URL_SITES = {"zillow", "redfin", "realtor", "trulia", "homes", "movoto", "har"}


# ── Address matching ─────────────────────────────────────────────────────────

def _words(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split())


class AddressMatcher:
    """House number + street name (directionals / suffix optional) and, near it, the city or ZIP."""

    def __init__(self, t: Target) -> None:
        street = _words(_UNIT_RE.sub("", t.street))
        parts = street.split()
        self.number = parts[0] if parts and parts[0].isdigit() else ""
        core = [w for w in parts[1:] if w not in _DIRECTIONALS]
        if len(core) > 1 and core[-1] in _SUFFIXES:
            core = core[:-1]
        self.core = core
        dirs = "|".join(sorted(_DIRECTIONALS, key=len, reverse=True))
        self.street_re = re.compile(rf"(?<![0-9]){re.escape(self.number)} (?:(?:{dirs}) )?{' '.join(map(re.escape, core))}\b") \
            if self.number and core else None
        self.city = _words(t.city)
        self.zip = (t.zip or "")[:5]

    def _place_near(self, text: str, start: int, end: int, window: int) -> bool:
        chunk = text[max(0, start - window):end + window]
        return bool((self.city and re.search(rf"\b{re.escape(self.city)}\b", chunk))
                    or (self.zip and re.search(rf"\b{self.zip}\b", chunk)))

    def in_text(self, text: str, window: int = 300) -> bool:
        """Address (+ city or ZIP within ``window`` chars) appears in free text."""
        if not self.street_re:
            return False
        text = _words(text)
        return any(self._place_near(text, m.start(), m.end(), window) for m in self.street_re.finditer(text))

    def url_verdict(self, url: str) -> Optional[bool]:
        """True: the URL spells out this address. False: it spells out another address in the same
        city / ZIP (a neighbour's page). None: the URL doesn't name an address."""
        path = _words(urlsplit(url).path + " " + urlsplit(url).query)
        if self.in_text(path, window=80):
            return True
        has_place = (self.zip and re.search(rf"\b{self.zip}\b", path)) or (self.city and self.city in path)
        if has_place and re.search(r"(?<![0-9])\d{1,6} [a-z]", path):
            return False
        return None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _money(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    s = str(v).lower().replace(",", "")
    m = re.search(r"\$?\s*(\d+(?:\.\d+)?)\s*([km])?\b", s)
    if not m:
        return None
    n = float(m.group(1)) * {"k": 1e3, "m": 1e6}.get(m.group(2) or "", 1)
    return n if n > 0 else None


def _site_for(url: str) -> Optional[tuple[str, str, str, bool]]:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    for suffix, info in SITES.items():
        if host == suffix or host.endswith("." + suffix):
            return info
    return None


def _clean_url(url: str) -> str:
    """Drop the fragment and tracking parameters; keep everything that identifies the page."""
    p = urlsplit(url.strip())
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if not k.lower().startswith(("utm_", "fbclid", "gclid", "mc_"))]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), ""))


def _same_url(a: str, b: str) -> bool:
    return _url_key(a) == _url_key(b)


def _same_listing(a: str, b: str) -> bool:
    """Same page, or the same listing id with / without its slug ("/auctions/293" vs "/auctions/293-single-family")."""
    (ha, pa, qa), (hb, pb, qb) = _url_key(a), _url_key(b)
    if ha != hb or qa != qb:
        return False
    short, long_ = sorted((pa, pb), key=len)
    return long_ == short or long_.startswith(short + "-")


def _norm_text(s: Optional[str], n: int) -> Optional[str]:
    s = " ".join(str(s or "").split())
    return s[:n] or None


# ── 1. Origin: the sale page the lead was imported from ─────────────────────

def origin_listing(lead: Lead) -> Optional[dict]:
    """The government / sheriff sale row from the lead's import note, or None."""
    m = None
    for m in _NOTE_RE.finditer(lead.notes or ""):
        pass  # the latest import line wins
    if not m:
        return None
    body, url = m.group("body"), m.group("url").rstrip(".,;")
    fields = {}
    for bit in body.split("; "):
        fm = _FIELD_RE.match(bit)
        if fm:
            fields[fm.group(1).lower()] = fm.group(2).strip()
    program = fields.get("program", "")
    site, label, auction = None, None, None
    for pat, sid, lbl, auc in _PROGRAMS:
        if re.search(pat, program, re.I):
            site, label, auction = sid, lbl or program[:80], auc
            break
    known = _site_for(url)
    if site is None:
        site, label = (known[0], known[1]) if known else ((urlsplit(url).hostname or "sale").removeprefix("www.")[:40], program[:80] or "Sale list")
    if auction is None:   # USDA: foreclosure sale vs REO
        auction = bool(re.search(r"foreclosure", program + " " + fields.get("status", ""), re.I))
    sale_date = (fields.get("sale date") or "").split(" ")[0] or None
    if fields.get("minimum bid"):
        price, price_label = _money(fields["minimum bid"]), "Minimum bid"
    elif fields.get("list price"):
        price, price_label = _money(fields["list price"]), "List price"
    else:
        price, price_label = None, None
    status = "auction" if auction else "for_sale"
    notes = [program] if program else []
    if fields.get("status"):
        notes.append(f"Status on the list: {fields['status']}")
    if sale_date:
        notes.append(f"Sale date {fields['sale date']}")
        if sale_date < utcnow().date().isoformat():
            status = "unknown"
            notes.append("sale date has passed — outcome not known")
    if fields.get("case"):
        notes.append(f"Case {fields['case']}")
    if fields.get("plaintiff/lender"):
        notes.append(f"Plaintiff {fields['plaintiff/lender']}")
    return {
        "site": site, "site_label": label, "kind": "government" if is_official(url) or (known and known[2] == "government") else "auction",
        "url": url, "status": status, "price": price, "price_label": price_label, "listed_date": None,
        "days_on_market": None, "agent_name": None, "agent_phone": None, "agent_company": None,
        "check": "confirmed", "source": "import", "note": "; ".join(notes)[:500] or None,
    }


# ── 2. RentCast MLS listing on the property record ──────────────────────────

def rentcast_listing(lead: Lead) -> Optional[dict]:
    rec = lead.property_record
    if rec is None or not rec.listing_status:
        return None
    agent = {}
    if rec.listing_agent:
        try:
            agent = json.loads(rec.listing_agent) or {}
        except ValueError:
            agent = {}
    active = rec.listing_status.strip().lower() == "active"
    return {
        "site": "rentcast_mls", "site_label": f"MLS ({rec.listing_mls})" if rec.listing_mls else "MLS (via RentCast)",
        "kind": "mls",
        # The API request that returned this listing (RentCast gives no public listing page).
        "url": "https://api.rentcast.io/v1/listings/sale?" + urlencode({"address": full_address(lead)}, quote_via=quote),
        "status": "for_sale" if active else "off_market",
        "price": rec.listing_price, "price_label": "List price" if active else "Last list price",
        "listed_date": rec.listing_date.date().isoformat() if rec.listing_date else None,
        "days_on_market": rec.listing_days_on_market,
        "agent_name": _norm_text(agent.get("name"), 255), "agent_phone": _norm_text(agent.get("phone") or agent.get("office_phone"), 32),
        "agent_company": _norm_text(agent.get("office"), 255),
        "check": "confirmed", "source": "rentcast",
        "note": f"RentCast MLS listing status: {rec.listing_status}" + (f" ({rec.listing_mls})" if rec.listing_mls else ""),
    }


# ── 3. Grok web search ───────────────────────────────────────────────────────

INSTRUCTIONS = """You check where ONE U.S. property is listed or shown online, for a real-estate acquisitions team. Use web search and open pages.

Search the exact address (house number + street + city / ZIP) on:
- portals: Zillow, Redfin, Realtor.com, Trulia, Homes.com, Movoto (and HAR.com for Texas);
- auction / distressed: Auction.com, Hubzu, Xome, RealtyTrac, Bid4Assets, CWS Marketing, county or sheriff sale sites;
- government REO: HUD Home Store, Fannie Mae HomePath, VA (listings.vrmco.com), USDA, FDIC, IRS, Treasury;
- classifieds: Facebook Marketplace, Craigslist;
- commercial or land (only if the property is one): LoopNet, Crexi, Land.com / LandWatch;
- local broker / MLS IDX sites.
Search budget: spend it on the marketplaces — do not search for or report the government sale page we already have (given below). Run at least these searches: the full address in quotes; "<number> <street>" <city> on Zillow, on Redfin and on Realtor.com (site: queries); the address with "auction" OR "foreclosure"; then other sites where the property type makes sense.

Rules — wrong-property matches are worse than missing a listing:
- Report a page only if it is about THIS property: same house number, same street, same city or ZIP. Search results are full of neighbours (same street, other number) and the same number on other streets — never report those.
- url: the exact page you saw in the search results or opened — the property's own page (portal property pages put the address in the URL), not a search-results, city or street page.
- Property-record pages for this exact property on Zillow, Redfin, Realtor.com, Trulia, Homes.com and similar sites count even when it is not for sale: report them with status off_market or sold.
- address_text: the property address exactly as the page or search snippet shows it.
- quote: verbatim text from the page or snippet showing the status and/or price (max 200 characters).
- status: for_sale (active listing), pending (under contract / contingent), sold (sold record), off_market (a property-record page that is not for sale, e.g. Zillow "off market" or Redfin "not for sale"), auction (scheduled auction / sheriff or tax sale), rental (for rent), unknown.
- record_page: true for property-record pages (off market / sold / estimate-only) — they are not active listings.
- price and price_label exactly as shown: "List price", "Minimum bid", "Starting bid", "Last sold price", "Last list price", "Rent". For record pages give the last sale or last list price only if the page shows it; never an estimate (Zestimate / Redfin Estimate) as price. null when not shown.
- listed_date (YYYY-MM-DD when shown), days_on_market, agent_name, agent_phone, agent_company: only when the page shows them, else null. Never guess.
- In "checked", name each site you searched that had no page for this property."""

SCHEMA = {
    "type": "object",
    "properties": {
        "listings": {"type": "array", "items": {"type": "object", "properties": {
            "site_name": {"type": "string"},
            "url": {"type": "string"},
            "kind": {"type": "string", "enum": list(KINDS)},
            "status": {"type": "string", "enum": list(STATUSES)},
            "record_page": {"type": "boolean"},
            "address_text": {"type": "string"},
            "quote": {"type": "string"},
            "price": {"type": ["string", "null"]},
            "price_label": {"type": ["string", "null"]},
            "listed_date": {"type": ["string", "null"]},
            "days_on_market": {"type": ["integer", "null"]},
            "agent_name": {"type": ["string", "null"]},
            "agent_phone": {"type": ["string", "null"]},
            "agent_company": {"type": ["string", "null"]},
        }, "required": ["site_name", "url", "kind", "status", "record_page", "address_text", "quote", "price",
                        "price_label", "listed_date", "days_on_market", "agent_name", "agent_phone", "agent_company"],
            "additionalProperties": False}},
        "checked": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string", "description": "One or two sentences: where it is listed, or that no listing was found"},
    },
    "required": ["listings", "checked", "summary"],
    "additionalProperties": False,
}


def _prompt(t: Target, origin: Optional[dict]) -> str:
    lines = [f"Property: {t.address}"]
    if origin:
        lines.append(f"Already known (do not report): {origin['url']} — {origin['note'] or origin['site_label']}. "
                     "Look for the same property on other sites, including auction sites.")
    return "\n".join(lines)


def grok_listings(key: str, model: str, t: Target, origin: Optional[dict] = None) -> tuple[dict, dict]:
    """One Responses API call with web_search → (parsed output, raw response). Raises ScanError."""
    body = {
        "model": model,
        "instructions": INSTRUCTIONS,
        "input": [{"role": "user", "content": _prompt(t, origin)}],
        "tools": [{"type": "web_search"}],
        "max_tool_calls": MAX_TOOL_CALLS,
        "store": False,
        "text": {"format": {"type": "json_schema", "name": "listings", "schema": SCHEMA, "strict": True}},
    }
    try:
        r = httpx.post(f"{XAI_BASE}/responses", json=body, timeout=GROK_TIMEOUT,
                       headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    except httpx.TimeoutException as exc:
        raise ScanError(f"Grok listing search timed out after {GROK_TIMEOUT:.0f}s ({model}).") from exc
    except httpx.HTTPError as exc:
        raise ScanError(f"xAI unreachable: {type(exc).__name__}") from exc
    if r.status_code >= 400:
        raise ScanError(_xai_error(r))
    resp = r.json()
    if resp.get("status") != "completed":
        raise ScanError(f"Grok listing search did not complete ({resp.get('status')}: "
                        f"{resp.get('incomplete_details') or resp.get('error')})")
    try:
        return json.loads(_message_text(resp)), resp
    except ValueError as exc:
        raise ScanError("Grok listing search returned no structured result.") from exc


def _page_checks(urls: list[str]) -> dict[str, tuple[Optional[str], Optional[str]]]:
    """Re-read pages robots.txt allows (property_scan._read_pages: own UA, crawl delay, one host at a time)."""
    by_host: dict[str, list[str]] = {}
    for u in urls[:MAX_PAGE_CHECKS]:
        by_host.setdefault(urlsplit(u).hostname or "", []).append(u)
    out: dict[str, tuple[Optional[str], Optional[str]]] = {}
    for group in by_host.values():
        out.update(_read_pages(group))
    return out


_ESTIMATE_RE = re.compile(r"estimat|\best\b|zestimate|\bvalue\b|\bavm\b", re.I)


def vet_listings(out: dict, resp: dict, t: Target) -> tuple[list[dict], dict[str, int]]:
    """Grok's listings → rows we can stand behind, plus counts of what was dropped and why."""
    results, touched = search_result_urls(resp), _touched_urls(resp)
    matcher = AddressMatcher(t)
    dropped = {"unvisited_url": 0, "unverified_url": 0, "wrong_property": 0, "not_a_property_page": 0,
               "address_not_shown": 0, "bad_url": 0}
    cands: list[dict] = []
    for x in out.get("listings") or []:
        url = (x.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            dropped["bad_url"] += 1
            continue
        in_results = _was_touched(url, results)
        if not in_results and not _was_touched(url, touched):
            dropped["unvisited_url"] += 1
            continue
        verdict = matcher.url_verdict(url)
        if verdict is False:
            dropped["wrong_property"] += 1
            continue
        site = _site_for(url)
        if verdict is None and site and site[0] in ADDRESS_URL_SITES:
            dropped["not_a_property_page"] += 1   # e.g. zillow.com/lake-roesiger-snohomish-wa/ (a search page)
            continue
        if any(_same_url(url, c["url"]) for c in cands):
            continue
        x = dict(x, url=_clean_url(url), _url_match=verdict is True, _in_results=in_results,
                 _said_match=matcher.in_text(x.get("address_text") or "") or matcher.in_text(x.get("quote") or ""))
        cands.append(x)
    fetchable = [c["url"] for c in cands if (_site_for(c["url"]) or (None, None, None, True))[3]]
    pages = _page_checks(fetchable) if fetchable else {}
    rows: list[dict] = []
    for c in cands:
        url = c["url"]
        text, reason = pages.get(url, (None, None))
        price = _money(c.get("price"))
        notes: list[str] = []
        if not c["_in_results"] and not (text is not None and matcher.in_text(text)):
            # Only opened / cited by the model, never returned by the search engine, and we couldn't
            # confirm it ourselves: the URL may be made up.
            dropped["unverified_url"] += 1
            continue
        if price is not None and _ESTIMATE_RE.search(c.get("price_label") or ""):
            notes.append(f"{c.get('price_label')} {c.get('price')} is an estimate, not a price")
            price = None
        if text is not None and matcher.in_text(text):
            check = "confirmed"
            if price is not None and not re.search(rf"(?<![\d.]){int(price)}(?:\.\d+)?(?!\d)", text.replace(",", "")):
                notes.append(f"price {c.get('price')} reported by search not on the page — dropped")
                price = None
            phone_digits = re.sub(r"\D", "", c.get("agent_phone") or "")[-10:]
            if phone_digits and phone_digits not in re.sub(r"\D", "", text):
                notes.append("agent phone not on the page — dropped")
                c["agent_phone"] = None
        elif text is not None:
            # We read the page and the address isn't on it.
            if c["_url_match"]:
                check = "unchecked"
                notes.append("page text doesn't show the address (built by JavaScript?); matched by its URL")
            else:
                dropped["wrong_property"] += 1
                continue
        elif c["_url_match"] or c["_said_match"]:
            check = "unchecked"
            site = _site_for(url)
            why = reason or ("site blocks bots — not re-read" if site and not site[3] else "not re-read")
            how = "URL names this address" if c["_url_match"] else "address shown in the search snippet"
            notes.append(f"{how}; {why}")
        else:
            dropped["address_not_shown"] += 1
            continue
        status = c.get("status") if c.get("status") in STATUSES else "unknown"
        if c.get("record_page") and status in ("for_sale", "pending", "auction"):
            status = "off_market"   # a record page is never an active listing
        label = _norm_text(c.get("price_label"), 40) if price is not None else None
        if c.get("record_page"):
            notes.insert(0, "property-record page, not an active listing")
        if c.get("quote"):
            notes.append(f'"{_norm_text(c["quote"], 200)}"')
        site = _site_for(url)
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
        kind = site[2] if site else (c.get("kind") if c.get("kind") in KINDS else "marketplace")
        if not site and is_official(url):
            kind = "government"
        rows.append({
            "site": site[0] if site else host[:40], "site_label": (site[1] if site else host)[:80],
            "kind": kind, "url": url, "status": status, "price": price, "price_label": label,
            "listed_date": _norm_text(c.get("listed_date"), 32),
            "days_on_market": c.get("days_on_market") if isinstance(c.get("days_on_market"), int) and c["days_on_market"] >= 0 else None,
            "agent_name": _norm_text(c.get("agent_name"), 255), "agent_phone": _norm_text(c.get("agent_phone"), 32),
            "agent_company": _norm_text(c.get("agent_company"), 255),
            "check": check, "source": "web", "note": "; ".join(notes)[:500] or None,
        })
    return rows, {k: v for k, v in dropped.items() if v}


# ── Store ────────────────────────────────────────────────────────────────────

_UPDATABLE = ("site_label", "kind", "status", "price", "price_label", "listed_date", "days_on_market", "agent_name",
              "agent_phone", "agent_company", "check", "note")


def _store(db: Session, lead: Lead, row: dict) -> str:
    """Insert or refresh one listing → "added" | "updated" | "same". An imported (origin) row is never
    overwritten by a web finding for the same URL."""
    existing = next((x for x in lead.listings if x.url == row["url"] or _same_url(x.url, row["url"])), None)
    if existing is None:
        listing = MarketplaceListing(lead_id=lead.id, **{k: v for k, v in row.items() if not k.startswith("_")})
        db.add(listing)
        lead.listings.append(listing)
        return "added"
    if existing.source == "import" and row["source"] != "import":
        return "same"
    changed = False
    for attr in _UPDATABLE:
        val = row.get(attr)
        if attr in ("agent_name", "agent_phone", "agent_company", "listed_date", "days_on_market") and val is None:
            continue  # a later pass that didn't see the agent doesn't erase it
        if getattr(existing, attr) != val:
            setattr(existing, attr, val)
            changed = True
    existing.source = row["source"]
    return "updated" if changed else "same"


def serialize(x: MarketplaceListing) -> dict:
    return {
        "id": x.id, "site": x.site, "site_label": x.site_label, "kind": x.kind, "url": x.url, "status": x.status,
        "price": x.price, "price_label": x.price_label, "listed_date": x.listed_date, "days_on_market": x.days_on_market,
        "agent_name": x.agent_name, "agent_phone": x.agent_phone, "agent_company": x.agent_company,
        "check": x.check, "source": x.source, "note": x.note, "found_at": iso_utc(x.found_at) if x.found_at else None,
    }


def _note_line(rows: list[dict], sources: list[dict]) -> str:
    sites = []
    for r in rows:
        bit = f"{r['site_label']} ({r['status']}" + (f", {r['price_label'] or 'price'} ${r['price']:,.0f}" if r.get("price") else "") + ")"
        if bit not in sites:
            sites.append(bit)
    errs = [f"{s['id']}: {s['detail']}" for s in sources if s["status"] == "error"]
    line = f"Listings check {utcnow().date().isoformat()}: " + ("; ".join(sites) if sites else "no listing found")
    return line + (f". Errors — {'; '.join(errs)}" if errs else "")


def find_listings(db: Session, lead: Lead, model: Optional[str] = None) -> dict:
    """Find where ``lead``'s property is listed; store/refresh MarketplaceListing rows (flush, no commit).

    Returns {listings, added, updated, sources: [{id, status, detail, cost_usd}], cost_usd}.
    Source status: ok (found something) | none (searched, nothing) | skipped (not configured) | error.
    """
    from app.services.settings import get_setting

    t = _target(lead)
    rows: list[dict] = []
    sources: list[dict] = []

    origin = origin_listing(lead)
    sources.append({"id": "origin", "status": "ok" if origin else "none", "cost_usd": 0.0,
                    "detail": f"{origin['site_label']} sale page" if origin else "no sale-list source URL in the notes"})
    if origin:
        rows.append(origin)

    rc = rentcast_listing(lead)
    sources.append({"id": "rentcast", "status": "ok" if rc else ("none" if lead.property_record else "skipped"), "cost_usd": 0.0,
                    "detail": rc["note"] if rc else ("no MLS listing on the RentCast record" if lead.property_record
                                                     else "no RentCast record for this property")})
    if rc:
        rows.append(rc)

    key = (get_setting(db, "xai_api_key") or "").strip()
    cost = 0.0
    if not key:
        sources.append({"id": "grok", "status": "skipped", "detail": "no xAI API key", "cost_usd": 0.0})
    elif not t.street:
        sources.append({"id": "grok", "status": "skipped", "detail": "no street address", "cost_usd": 0.0})
    else:
        model = model or MODEL
        started = time.monotonic()
        try:
            out, resp = grok_listings(key, model, t, origin)
            usage = resp.get("usage") or {}
            cost = round((usage.get("cost_in_usd_ticks") or 0) / 1e10, 4)
            web_rows, dropped = vet_listings(out, resp, t)
            fresh = []
            for r in web_rows:
                dup = next((o for o in rows if _same_listing(r["url"], o["url"])), None)
                if dup is None:
                    fresh.append(r)
                elif dup["source"] == "import" and r.get("agent_phone") and not dup.get("agent_phone"):
                    # The sale page we imported from names a listing agent we didn't capture at import.
                    dup.update(agent_name=r["agent_name"], agent_phone=r["agent_phone"], agent_company=r["agent_company"])
                    dup["note"] = ((dup["note"] or "") + f"; agent per search of {r['url']} ({r['check']})")[:500]
                    dup["_agent_check"] = r["check"]
            web_rows = fresh
            rows += web_rows
            searches = (usage.get("server_side_tool_usage_details") or {}).get("web_search_calls") or 0
            detail = (f"{len(web_rows)} page(s) kept"
                      + (f", dropped {', '.join(f'{v} {k.replace('_', ' ')}' for k, v in dropped.items())}" if dropped else "")
                      + f"; {searches} searches, {time.monotonic() - started:.0f}s, {resp.get('model') or model}")
            if (out.get("summary") or "").strip():
                detail += f". {out['summary'].strip()}"
            sources.append({"id": "grok", "status": "ok" if web_rows else "none", "detail": detail[:1000], "cost_usd": cost,
                            "dropped": dropped, "checked": [str(c) for c in out.get("checked") or []][:30]})
        except ScanError as exc:
            sources.append({"id": "grok", "status": "error", "detail": str(exc)[:500], "cost_usd": 0.0})

    added = updated = 0
    for row in rows:
        res = _store(db, lead, row)
        added += res == "added"
        updated += res == "updated"
    # Agent phones, page-confirmed listings first so the phone keeps the best source URL.
    for row in sorted((r for r in rows if r.get("agent_phone")), key=lambda r: r.get("_agent_check", r["check"]) != "confirmed"):
        upsert_phone(db, lead, row["agent_phone"], role="listing_agent", source="listing", source_url=row["url"],
                     confidence="high" if row.get("_agent_check", row["check"]) == "confirmed" else "low",
                     contact_name=row.get("agent_name") or row.get("agent_company"),
                     note="Listing agent" + (f" ({row['agent_company']})" if row.get("agent_company") else ""))

    lead.listings_checked_at = utcnow()
    kept = [ln for ln in (lead.research_note or "").splitlines() if ln.strip() and not ln.startswith("Listings check ")]
    lead.research_note = "\n".join(kept + [_note_line(rows, sources)])
    db.flush()
    return {"listings": [serialize(x) for x in lead.listings], "added": added, "updated": updated,
            "sources": sources, "cost_usd": cost}
