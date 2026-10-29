"""Government property-sale sources — deterministic adapters (no LLM parsing).

Each adapter fetches a public government sale site politely (robots.txt checked on
every hop, ≥1 s between requests to a host, the site's Crawl-delay honoured up to
60 s) and returns a JSON array of records in the scraper schema plus sale fields:

    property_address, property_city, property_state, property_zip   (only when the source has them)
    owner_name, mailing_*                                            (when the source names the owner)
    sale_date (YYYY-MM-DD), sale_time, case_number, minimum_bid, list_price, amount_owed
    program, agency, source_url (the property's own page when it has one), list_type

Sources (see ``DIRECTORY``):
  gov_treasury  Treasury seized real-property auctions (treasury.gov/auctions/treasury/rp)
  gov_irs       IRS auctions — real estate only (irsauctions.gov)
  gov_usda      USDA Rural Development / FSA REO + foreclosure listings (properties.sc.egov.usda.gov)
  gov_fdic      FDIC failed-bank real estate (fdicrealestatelistings.com, linked from fdic.gov)
  gov_tarrant   Tarrant County TX Constable Pct 3 delinquent-tax sales + writs of execution,
                with owner/situs from Tarrant Appraisal District parcels (mapit.tarrantcounty.com)

``ensure_government_sources`` registers them as daily auto-scrape sources (idempotent);
``import_all_new`` imports every not-yet-imported result from each source's latest job.
"""
from __future__ import annotations

import html as _html
import json
import logging
import re
import time
from datetime import date, datetime
from typing import Any, Callable, Optional
from urllib.parse import urlencode, urljoin, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ListType, ScrapeJob, ScrapeJobStatus, ScrapeResult, ScrapeSource
from app.services.scraping_agent import USER_AGENT, RobotsCache, ScrapingError, is_entity, validate_url
from app.timeutil import utcnow

log = logging.getLogger("dre_capital.gov_sources")

MAX_CRAWL_DELAY = 60.0
FETCH_TIMEOUT = 45.0   # treasury.gov takes ~10 s just to answer a redirect
MAX_REDIRECTS = 5
RATE_LIMIT_WAIT = 30.0  # one polite retry after HTTP 429 (Retry-After honoured up to MAX_CRAWL_DELAY)


# ── Polite fetching ───────────────────────────────────────────────────────────

class Fetcher:
    """HTTP for one scrape run: robots.txt checked on every hop, per-host rate limit."""

    def __init__(self) -> None:
        self.robots = RobotsCache(timeout=FETCH_TIMEOUT)
        self._client = httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=False, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self._last: dict[str, float] = {}

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc: Any) -> None:
        self._client.close()

    def _wait(self, url: str) -> None:
        host = (urlsplit(url).hostname or "").lower()
        delay = self.robots.delay(url, cap=MAX_CRAWL_DELAY)
        last = self._last.get(host)
        if last is not None:
            remaining = last + delay - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
        self._last[host] = time.monotonic()

    def get(self, url: str, params: Optional[dict[str, Any]] = None) -> str:
        if params:
            url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
        return self._request("GET", url, None)

    def post(self, url: str, data: dict[str, Any]) -> str:
        return self._request("POST", url, data)

    def _request(self, method: str, url: str, data: Optional[dict[str, Any]]) -> str:
        current = validate_url(url)
        retried = False
        for _ in range(MAX_REDIRECTS + 2):
            self.robots.check(current)
            self._wait(current)
            try:
                resp = self._client.request(method, current, data=data)
            except httpx.TimeoutException as exc:
                raise ScrapingError(f"{current} timed out after {FETCH_TIMEOUT:.0f}s.") from exc
            except httpx.HTTPError as exc:
                raise ScrapingError(f"Couldn't reach {current}: {type(exc).__name__}.") from exc
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    raise ScrapingError(f"{current} redirected without a Location header.")
                current = validate_url(urljoin(current, location))
                method, data = "GET", None
                continue
            if resp.status_code == 429 and not retried:
                retried = True
                after = resp.headers.get("retry-after", "")
                time.sleep(min(float(after), MAX_CRAWL_DELAY) if after.isdigit() else RATE_LIMIT_WAIT)
                continue
            if resp.status_code >= 400:
                hint = {401: "it needs a login", 403: "the site blocks automated access", 404: "page not found",
                        429: "rate limited — try later"}.get(resp.status_code, "")
                raise ScrapingError(f"HTTP {resp.status_code} from {current}" + (f" ({hint})." if hint else "."))
            return resp.text
        raise ScrapingError(f"Too many redirects starting at {url}.")


# ── Parsing helpers ───────────────────────────────────────────────────────────

_SKIP_BLOCK_RE = re.compile(r"(?is)<(script|style|svg|noscript|template)\b.*?</\1\s*>")
_INLINE_TAG_RE = re.compile(r"(?i)</?(?:font|b|strong|span|i|em|u|small|big)\b[^>]*>")
_HREF_RE = re.compile(r"""(?is)<a\b[^>]*?\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))[^>]*>""")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def html_text(fragment: str, *, links: bool = False, join_inline: bool = False) -> str:
    """Visible text of an HTML fragment on one line.

    ``links`` keeps each anchor's href as `` [[href]] `` before its text; ``join_inline``
    removes inline formatting tags without a space (Treasury splits words across them).
    """
    s = _SKIP_BLOCK_RE.sub(" ", fragment)
    if links:
        s = _HREF_RE.sub(lambda m: f" [[{_html.unescape(m.group(1) or m.group(2) or m.group(3) or '')}]] ", s)
    if join_inline:
        s = _INLINE_TAG_RE.sub("", s)
    s = _TAG_RE.sub(" ", s)
    s = _html.unescape(s).replace("\xa0", " ")
    return _WS_RE.sub(" ", s).strip()


US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR", "guam": "GU",
    "virgin islands": "VI", "u.s. virgin islands": "VI", "american samoa": "AS",
    "northern mariana islands": "MP",
}
_STATE_CODES = set(US_STATES.values())


def state_code(name: Optional[str]) -> Optional[str]:
    """'Texas' / 'TX' / 'Tx.' → 'TX'; None when it isn't a US state or territory."""
    s = (name or "").strip().rstrip(".").strip()
    if s.upper() in _STATE_CODES:
        return s.upper()
    return US_STATES.get(s.lower())


_ADDR_TAIL_RE = re.compile(r"^(?P<rest>.+?),\s*(?P<state>[A-Za-z][A-Za-z .]*?)\.?,?\s+(?P<zip>\d{5})(?:-\d{4})?$")


def split_address(full: str) -> Optional[dict[str, Optional[str]]]:
    """'16131 Villa Fontana Way, Houston, Texas 77068' → street / city / state / zip (None if unparseable)."""
    m = _ADDR_TAIL_RE.match(_WS_RE.sub(" ", full or "").strip())
    if not m:
        return None
    st = state_code(m.group("state"))
    if not st or "," not in m.group("rest"):
        return None
    street, city = m.group("rest").rsplit(",", 1)
    street, city = street.strip(" ,"), city.strip(" ,")
    if not street or not city:
        return None
    return {"property_address": street, "property_city": city, "property_state": st, "property_zip": m.group("zip")}


_DATE_RE = re.compile(r"\b([A-Z][a-z]{2,8})\.?\s+(\d{1,2}),?\s+(\d{4})\b")


def parse_date(text: Optional[str]) -> Optional[str]:
    """First 'November 5, 2026' / 'Nov 05, 2026' in ``text`` → '2026-11-05'."""
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    for fmt in ("%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", fmt).date().isoformat()
        except ValueError:
            continue
    return None


def money(text: Optional[str]) -> Optional[float]:
    m = re.search(r"\d[\d,]*(?:\.\d+)?", (text or "").replace("$", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _int(text: Any) -> Optional[int]:
    v = money(str(text)) if text is not None else None
    return int(v) if v else None


def _baths(text: Optional[str]) -> Optional[float]:
    """Listing shorthand '3.1 baths' = 3 full + 1 half → 3.5; '2.5' stays 2.5."""
    v = money(text)
    if v is None:
        return None
    whole, frac = divmod(round(v * 10), 10)
    return whole + 0.5 if frac == 1 else v


def _clean(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = _WS_RE.sub(" ", str(v)).strip()
    return s or None


# ── 1. Treasury seized real property (CWS Asset Management & Sales) ──────────

TREASURY_URL = "https://www.treasury.gov/auctions/treasury/rp/realprop.shtml"
TREASURY_PROGRAM = "U.S. Treasury seized real property auction"

_TREAS_ENTRY_RE = re.compile(
    r"(?P<ptype>[A-Z][A-Z&/\- ]{2,60}?)\s*:\s*(?P<addr>[^:\[\]]{8,200}?)\s+"
    r"(?P<kind>ONLINE AUCTION DATE|LIVE AUCTION DATE|AUCTION DATE|SEALED BID[A-Z ]*?DATE|BROKER SALE)\s*:\s*"
    r"(?P<when>Coming Soon\.*|[A-Z][a-z]+,\s+[A-Z][a-z]+\.?\s+\d{1,2},\s+\d{4})\s*"
    r"(?P<desc>.*?)\bSale\s*#\s*(?P<sale>\d[\d\- &]*\d)\.?"
)
_TREAS_LINK_RE = re.compile(r"^\s*\[\[([^\]]+?\.s?html?)\]\]")


def parse_treasury_list(page: str, base_url: str = TREASURY_URL) -> list[dict[str, Any]]:
    """Every property block on the Treasury 'Upcoming Auctions' page."""
    text = html_text(page, links=True, join_inline=True)
    records: list[dict[str, Any]] = []
    for m in _TREAS_ENTRY_RE.finditer(text):
        addr = split_address(m.group("addr"))
        if not addr:
            continue
        desc = m.group("desc").strip()
        link = _TREAS_LINK_RE.match(text[m.end():])
        kind = m.group("kind")
        beds = re.search(r"(\d+)\s*bed(?:room)?s?\b", desc, re.I)
        baths = re.search(r"(\d+(?:\.\d)?)\s*baths?\b", desc, re.I)
        sqft = re.search(r"(\d[\d,]*)\s*±?\s*sq\.?\s*ft", desc, re.I)
        built = re.search(r"\bbuilt in (\d{4})", desc, re.I)
        records.append({
            **addr,
            "property_type": m.group("ptype").strip().title(),
            "sale_type": "Broker sale" if kind == "BROKER SALE" else kind.replace(" DATE", "").capitalize(),
            "sale_date": parse_date(m.group("when")),
            "case_number": m.group("sale").strip(),
            "beds": int(beds.group(1)) if beds else None,
            "baths": _baths(baths.group(1)) if baths else None,
            "sqft": _int(sqft.group(1)) if sqft else None,
            "year_built": int(built.group(1)) if built else None,
            "description": desc[:400],
            "vacant": bool(re.search(r"\bvacant\b", desc, re.I)),
            "program": TREASURY_PROGRAM,
            "agency": "U.S. Department of the Treasury (CWS Asset Management & Sales)",
            "source_url": urljoin(base_url, link.group(1)) if link else base_url,
            "list_type": ListType.GOVERNMENT_SALE.value,
        })
    return records


def parse_treasury_detail(page: str) -> dict[str, Any]:
    """Starting bid, deposit, auction time, size, year built, county, parcel, taxes from a property page."""
    t = html_text(page, join_inline=True)
    out: dict[str, Any] = {}

    def grab(pattern: str) -> Optional[str]:
        m = re.search(pattern, t, re.I)
        return m.group(1).strip() if m else None

    if (v := grab(r"Starting Bid:\s*\$\s*([\d,]+(?:\.\d+)?)")):
        out["minimum_bid"] = money(v)
    if (v := grab(r"Deposit:\s*\$\s*([\d,]+(?:\.\d+)?)")):
        out["deposit"] = money(v)
    if (v := grab(r"Auction Date and Time:\s*(.+?)\s+(?:Inspections?|Deposit|Starting Bid|Property)\b")):
        out["sale_date"] = parse_date(v) or None
        tm = re.search(r"\d{4}\s+(\d{1,2}(?::\d{2})?\s*[ap]m.*)$", v, re.I)
        if tm:
            out["sale_time"] = tm.group(1).strip()
    if (v := grab(r"Living Space:\s*([\d,]+)")):
        out["sqft"] = _int(v)
    if (v := grab(r"Year Built:\s*(\d{4})")):
        out["year_built"] = int(v)
    if (v := grab(r"\bCounty:\s*([A-Z][A-Za-z .'\-]*?)\s+(?=\d{4}\s|Parcel|Utilities|Zoning|HOA|Sale|Tax)")):
        out["county"] = v
    m = re.search(r"(\d{4})\s+County Taxes:\s*\$\s*([\d,]+(?:\.\d+)?)", t, re.I)
    if m:
        out["annual_taxes"] = money(m.group(2))
        out["annual_taxes_year"] = int(m.group(1))
    if (v := grab(r"Parcel No\.?:\s*([\w\-.]+)")):
        out["parcel_number"] = v
    return {k: v for k, v in out.items() if v not in (None, "")}


def scrape_treasury(url: str, search_params: dict[str, Any], api_key: Optional[str] = None) -> str:
    with Fetcher() as f:
        page_url = url or TREASURY_URL
        records = parse_treasury_list(f.get(page_url), page_url)
        if search_params.get("details", True):
            for rec in records:
                if rec["source_url"] != page_url:
                    rec.update(parse_treasury_detail(f.get(rec["source_url"])))
    return json.dumps(records)


# ── 2. IRS auctions (real estate only) ────────────────────────────────────────

IRS_URL = "https://www.irsauctions.gov/auction/items/"
IRS_PROGRAM = "IRS auction"

_IRS_CARD_RE = re.compile(r"(?is)<li\b(?P<attrs>[^>]*\bauction-item\b[^>]*)>(?P<body>.*?)</article>")


def _attr(attrs: str, name: str) -> Optional[str]:
    m = re.search(rf"\b{name}(?:=(?:\"([^\"]*)\"|'([^']*)'|([^\s>]*)))?", attrs)
    if not m:
        return None
    return (m.group(1) or m.group(2) or m.group(3) or "").strip()


def _select_options(page: str, name_part: str) -> dict[str, str]:
    m = re.search(rf"(?is)<select\b[^>]*name=[\"']?[^\"'\s>]*{name_part}[^>]*>(.*?)</select>", page)
    if not m:
        return {}
    return {v: html_text(label) for v, label in re.findall(r"(?is)<option\b[^>]*value=[\"']?([^\"'\s>]+)[^>]*>(.*?)</option>", m.group(1))}


def parse_irs_list(page: str, base_url: str = IRS_URL) -> list[dict[str, Any]]:
    """Real-estate cards (asset type 'Real-Estate' or 'Seeking Guaranteed Bids') from the upcoming-auctions page."""
    asset_types = _select_options(page, "asset_type")
    sale_types = _select_options(page, "sale_type")
    wanted = {k for k, v in asset_types.items() if re.search(r"real.?estate|guaranteed", v, re.I)}
    cards: list[dict[str, Any]] = []
    for m in _IRS_CARD_RE.finditer(page):
        attrs, body = m.group("attrs"), m.group("body")
        if _attr(attrs, "data-asset-type") not in wanted:
            continue
        href = re.search(r"""href=["']?(/ad/[^"'\s>]+)""", body)
        if not href:
            continue  # cards linking off-site (e.g. GSA vehicle sales) are not IRS real estate
        title = re.search(r"(?is)class=[\"']?treas-page-title[^>]*>(.*?)</span>", body)
        when_utc = _attr(attrs, "data-auction-datetime") or ""
        when_text = None
        if when_utc:
            tm = re.search(rf"(?is)<time\s+datetime=[\"']?{re.escape(when_utc)}[\"']?[^>]*>(.*?)</time>", body)
            when_text = html_text(tm.group(1)) if tm else None
        bid = re.search(r"""field--name-field-minimum-bid.*?content=["']?([\d.]+)""", body, re.S)
        place = re.search(r"(?is)<address[^>]*>(.*?)</address>", body)
        cards.append({
            "title": html_text(title.group(1)) if title else None,
            "source_url": urljoin(base_url, href.group(1)),
            "asset_type": asset_types.get(_attr(attrs, "data-asset-type") or ""),
            "sale_type": sale_types.get(_attr(attrs, "data-sale-type") or ""),
            "sale_date": parse_date(when_text) if when_text else (when_utc[:10] or None),
            "sale_time": (re.search(r"\d{1,2}:\d{2}\s*[AP]M", when_text or "") or [None])[0],
            "minimum_bid": float(bid.group(1)) if bid else None,
            "card_location": html_text(place.group(1)) if place else None,
        })
    return cards


def parse_irs_detail(page: str) -> dict[str, Any]:
    """Street address, taxpayer (owner), minimum bid, auction date, sale location from a notice-of-sale page."""
    out: dict[str, Any] = {}
    m = re.search(r"(?is)Asset Address</div>.*?<address[^>]*>(.*?)</address>", page)
    if m:
        lines = [html_text(x) for x in re.split(r"(?i)<br\s*/?>", m.group(1))]
        lines = [x for x in lines if x and x.lower() not in ("united states", "usa")]
        if len(lines) >= 2:
            loc = lines[-1]
            a = (re.match(r"^(?P<city>.+?),\s*(?P<zip>\d{5})(?:-\d{4})?\s+(?P<state>[A-Za-z]{2})$", loc)
                 or re.match(r"^(?P<city>.+?),\s*(?P<state>[A-Za-z]{2})\s+(?P<zip>\d{5})(?:-\d{4})?$", loc))
            if a and state_code(a.group("state")):
                out.update({
                    "property_address": ", ".join(lines[:-1]).rstrip(" ,"),
                    "property_city": a.group("city").strip(),
                    "property_state": state_code(a.group("state")),
                    "property_zip": a.group("zip"),
                })
    t = html_text(page)
    # Owner = the title holder: "Only the right, title, and interest of NAME in and to the property"
    # (boilerplate variants say "of the taxpayer" or leave it blank). Fallback: the taxpayer the
    # property was seized from ("taxes due from NAME. The property …"), which can be the owner's business.
    taxpayer = re.search(r"taxes due from (.{2,120}?)\.\s+The property", t, re.I)
    if taxpayer:
        out["taxpayer"] = taxpayer.group(1).strip(" ,.")
    for m in re.finditer(r"right, title,? and interest of ((?:(?!\bin and to\b).){2,120}?),? in and to the property", t, re.I):
        name = m.group(1).strip(" ,.")
        if name and not name.lower().startswith("the taxpayer"):
            out["owner_name"] = name
            break
    if not out.get("owner_name") and out.get("taxpayer"):
        out["owner_name"] = out["taxpayer"]
    out["has_legal_description"] = "Legal Description" in t
    bid = re.search(r"Bidding Details.*?Minimum Bid\s*\$?\s*([\d,]+\.\d{2})", t)
    if bid:
        out["minimum_bid"] = money(bid.group(1))
    when = re.search(r"Date of Auction\s+([A-Z][a-z]{2} \d{1,2}, \d{4})(?:\s+(\d{1,2}:\d{2}\s*[AP]M))?", t)
    if when:
        out["sale_date"] = parse_date(when.group(1))
        if when.group(2):
            out["sale_time"] = when.group(2)
    loc = re.search(r"Sale Location\s+(.+?)\s+Driving Directions", t)
    if loc:
        out["sale_location"] = loc.group(1).replace(" United States", "").strip()
    sqft = re.search(r"(\d[\d,]{2,})\s*(?:\+/-\s*)?(?:square feet|sq\.?\s*ft)", t, re.I)
    if sqft:
        out["sqft"] = _int(sqft.group(1))
    beds = re.search(r"\b(\d{1,2})\s*bed(?:room)?s?\b", t, re.I)
    if beds:
        out["beds"] = int(beds.group(1))
    baths = re.search(r"\b(\d{1,2}(?:\.\d)?)\s*bath(?:room)?s?\b", t, re.I)
    if baths:
        out["baths"] = float(baths.group(1))
    return out


def scrape_irs(url: str, search_params: dict[str, Any], api_key: Optional[str] = None) -> str:
    records: list[dict[str, Any]] = []
    with Fetcher() as f:
        list_url = url or IRS_URL
        for card in parse_irs_list(f.get(list_url), list_url):
            detail = parse_irs_detail(f.get(card["source_url"]))
            if not detail.get("property_address") or not (
                    detail.get("has_legal_description") or re.match(r"^\d", detail["property_address"])):
                continue  # not real property
            # Seized for the owner's unpaid federal taxes (incl. guaranteed-bid pre-sales of seized homes):
            # the named taxpayer still owns it → tax-delinquent lead. Otherwise the government is the seller.
            seized = (card.get("sale_type") or "").lower() == "seized" or bool(detail.get("owner_name"))
            rec = {
                "property_address": detail["property_address"],
                "property_city": detail.get("property_city"),
                "property_state": detail.get("property_state"),
                "property_zip": detail.get("property_zip"),
                "owner_name": detail.get("owner_name"),
                "owner_is_llc": is_entity(detail.get("owner_name")),
                "beds": detail.get("beds"),
                "baths": detail.get("baths"),
                "sqft": detail.get("sqft"),
                "sale_date": detail.get("sale_date") or card.get("sale_date"),
                "sale_time": detail.get("sale_time") or card.get("sale_time"),
                "sale_location": detail.get("sale_location"),
                "minimum_bid": detail.get("minimum_bid") or card.get("minimum_bid"),
                "sale_type": card.get("sale_type"),
                "description": card.get("title"),
                "partial_interest": bool(re.search(r"\b(half|partial|undivided)\b.*\binterest\b", card.get("title") or "", re.I)),
                "program": f"{IRS_PROGRAM} ({card.get('sale_type') or 'sale'})",
                "agency": "Internal Revenue Service",
                "source_url": card["source_url"],
                "list_type": (ListType.TAX_DELINQUENT if seized else ListType.GOVERNMENT_SALE).value,
            }
            records.append(rec)
    return json.dumps(records)


# ── 3. USDA Rural Development / FSA REO + foreclosure listings ────────────────

USDA_URL = "https://properties.sc.egov.usda.gov/resales/public/home"
USDA_PROGRAMS = {"SFH": "Single Family", "MFH": "Multi-Family", "FSA": "Farm & Ranch"}
# Field labels on USDA property-detail pages (values may be blank, so parse up to the next label).
_USDA_LABELS = ("(?:House Style|Beds|Baths|Rooms|Living Area|Basement|Garage|Foundation|Age|Heating|Cooling|"
                "Water|Sewage|Lot Size|Real Estate Taxes|Comments|Foreclosure Sale Information|Parcels|"
                "Mineral Rights|Easements|Total Acres)")
_USDA_HEADERS = {
    "listing type": "listing_type", "street address": "street", "address": "street", "city": "city",
    "state": "state", "county": "county", "zip": "zip", "price/bid": "price", "price": "price",
    "beds": "beds", "baths": "baths", "sq. ft.": "sqft", "acres": "acres", "units": "units",
}


def parse_usda_states(page: str) -> list[str]:
    """State codes the search form offers (it lists only states with active properties)."""
    m = re.search(r'(?is)<select\b[^>]*id="stateCode"[^>]*>(.*?)</select>', page)
    return [v for v in re.findall(r'value="(\d+)"', m.group(1))] if m else []


def parse_usda_results(page: str, base_url: str) -> list[dict[str, Any]]:
    m = re.search(r'(?is)<table\b[^>]*id="propertySummariesTable"[^>]*>(.*?)</table>', page)
    if not m:
        return []
    table = m.group(1)
    head = [html_text(h).lower() for h in re.findall(r"(?is)<th\b[^>]*>(.*?)</th>", table)]
    cols = [_USDA_HEADERS.get(h) for h in head]
    body = re.search(r"(?is)<tbody\b[^>]*>(.*?)</tbody>", table)
    rows: list[dict[str, Any]] = []
    for row in re.findall(r"(?is)<tr\b[^>]*>(.*?)</tr>", body.group(1) if body else ""):
        cells = re.findall(r"(?is)<td\b[^>]*>(.*?)</td>", row)
        if len(cells) != len(cols):
            continue
        rec: dict[str, Any] = {}
        for key, cell in zip(cols, cells):
            if key == "street":
                rec[key] = html_text(re.split(r"(?i)<br\s*/?>", cell)[0])
            elif key:
                rec[key] = html_text(cell)
        link = re.search(r"""href=["']([^"']*PropertyDetail[^"']*)""", row)
        rec["detail_url"] = urljoin(base_url, _html.unescape(link.group(1))) if link else None
        rows.append(rec)
    return rows


def parse_usda_detail(page: str) -> dict[str, Any]:
    t = html_text(page)
    out: dict[str, Any] = {}
    m = re.search(r"Sale Information\s+Location:\s*(.*?)\s*Sale Date:\s*(.*?)\s*Sale Time:\s*(.*?)\s*(?:Govt Bid:|Click here|For program)", t)
    if m:
        if m.group(1):
            out["sale_location"] = m.group(1)
        d = parse_date(m.group(2))
        if not d and re.match(r"\d{1,2}/\d{1,2}/\d{4}", m.group(2)):
            d = datetime.strptime(re.match(r"\d{1,2}/\d{1,2}/\d{4}", m.group(2)).group(0), "%m/%d/%Y").date().isoformat()
        if d:
            out["sale_date"] = d
        if m.group(3):
            out["sale_time"] = m.group(3)
    # Foreclosure listings show the government's opening bid; FSA farm sales a sealed-bid minimum + deadline.
    bid = (re.search(r"Govt Bid:\s*\$\s*([\d,]+(?:\.\d+)?)", t)
           or re.search(r"Minimum acceptable bid is \$\s*([\d,]+(?:\.\d+)?)", t, re.I))
    if bid:
        out["minimum_bid"] = money(bid.group(1))
    deadline = re.search(r"Bids Must Be Submitted By:\s*([A-Za-z]+,?\s+[A-Za-z]+\.?\s+\d{1,2},?\s+\d{4})", t, re.I)
    if deadline and not out.get("sale_date"):
        out["sale_date"] = parse_date(deadline.group(1))
        out["sale_type"] = "Sealed bid deadline"
    taxes = re.search(r"Real Estate Taxes:\s*\$\s*([\d,]+(?:\.\d+)?)", t)
    if taxes:
        out["annual_taxes"] = money(taxes.group(1))
    for label, key in (("House Style", "property_type"), ("Lot Size", "lot_size")):
        v = re.search(rf"{label}:\s*(.*?)\s*(?={_USDA_LABELS}:|$)", t)
        if v and v.group(1):
            out[key] = v.group(1)
    return out


def _usda_record(row: dict[str, Any], program: str, page_url: str) -> Optional[dict[str, Any]]:
    street = _clean(row.get("street"))
    if not street:
        return None
    listing = _clean(row.get("listing_type")) or ""
    foreclosure = listing.lower().startswith("foreclosure")
    zip5 = re.match(r"\d{5}", row.get("zip") or "")
    id_m = re.search(r"[?&]id=(\d+)", row.get("detail_url") or "")
    return {
        "property_address": street,
        "property_city": (_clean(row.get("city")) or "").rstrip(",").strip() or None,
        "property_state": state_code(row.get("state")),
        "property_zip": zip5.group(0) if zip5 else None,
        "county": _clean(row.get("county")),
        "property_type": USDA_PROGRAMS[program],
        "beds": _int(row.get("beds")),
        "baths": money(row.get("baths")),
        "sqft": _int(row.get("sqft")),
        # Foreclosure rows show the government's opening bid in "Price/Bid"; REO rows the list price.
        ("minimum_bid" if foreclosure else "list_price"): money(row.get("price")),
        "listing_status": listing or None,
        "listing_id": id_m.group(1) if id_m else None,
        "program": f"USDA {'Farm Service Agency' if program == 'FSA' else 'Rural Development'} "
                   f"{USDA_PROGRAMS[program].lower()} {'foreclosure sale' if foreclosure else 'REO'}",
        "agency": "U.S. Department of Agriculture",
        "source_url": row.get("detail_url") or page_url,
        "list_type": (ListType.PRE_FORECLOSURE if foreclosure else ListType.GOVERNMENT_SALE).value,
    }


def scrape_usda(url: str, search_params: dict[str, Any], api_key: Optional[str] = None) -> str:
    base = "https://properties.sc.egov.usda.gov/resales/public/"
    programs = [p for p in (search_params.get("programs") or list(USDA_PROGRAMS)) if p in USDA_PROGRAMS]
    records: list[dict[str, Any]] = []
    with Fetcher() as f:
        for prog in programs:
            search_url = urljoin(base, f"search{prog}")
            for st in parse_usda_states(f.get(search_url)):
                form = {"stateCode": st, "countyCode": "", "city": "", "zipCode": "",
                        "propertyType": USDA_PROGRAMS[prog], "listingType": "All Types", "minPrice": "",
                        "maxPrice": "", "bedrooms": "", "bathrooms": "", "squareFootage": "", "searchFormName": prog}
                for row in parse_usda_results(f.post(search_url, form), search_url):
                    rec = _usda_record(row, prog, search_url)
                    if not rec:
                        continue
                    if search_params.get("details", True) and row.get("detail_url"):
                        detail = parse_usda_detail(f.get(row["detail_url"]))
                        rec.update({k: v for k, v in detail.items() if v and not rec.get(k)})
                    records.append(rec)
    return json.dumps(records)


# ── 4. FDIC failed-bank real estate (Property Listing Site) ──────────────────

FDIC_URL = "https://www.fdicrealestatelistings.com/auctions"
_APOLLO_RE = re.compile(r"window\.__APOLLO_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S)


def apollo_state(page: str) -> dict[str, Any]:
    m = _APOLLO_RE.search(page)
    if not m:
        raise ScrapingError("FDIC listing page has no embedded listing data (__APOLLO_STATE__) — the site layout changed.")
    return json.loads(m.group(1))


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")


def parse_fdic_list(page: str) -> tuple[list[dict[str, Any]], int]:
    """(listings on this page, total listings the site reports)."""
    state = apollo_state(page)
    root = state.get("ROOT_QUERY") or {}
    key = next((k for k in root if k.startswith("auctions(")), None)
    if key is None:
        return [], 0
    result = root[key] or {}
    listings = [state[r["__ref"]] for r in result.get("auctions") or [] if r.get("__ref") in state]
    return listings, int(result.get("total") or len(listings))


def parse_fdic_detail(page: str, auction_id: str) -> dict[str, Any]:
    """Labelled property fields (square feet, parcel, occupancy, status, …) and asking price."""
    a = apollo_state(page).get(f"Auction.{auction_id}") or {}
    fields = {(f.get("label") or "").strip(): ((f.get("data") or {}).get("value"))
              for f in a.get("real_estate_dynamic_fields") or []}
    info = a.get("real_estate_info") or {}
    loc = a.get("auction_location") or {}
    return {
        "fields": {k: v for k, v in fields.items() if k and v not in (None, "")},
        "asking": info.get("asking"),
        "listing_agent": " / ".join(x for x in (info.get("listing_agent_name"), info.get("listing_agent_company"),
                                                 info.get("listing_agent_phone")) if x) or None,
        "county": ((loc.get("county") or {}).get("county_name")),
        "state": ((loc.get("state") or {}).get("abbreviation")),
    }


def _fdic_field(fields: dict[str, Any], *names: str) -> Any:
    """Value of the first label containing one of ``names`` (checked in priority order)."""
    for n in names:
        for label, v in fields.items():
            if n in label.lower():
                return v
    return None


def scrape_fdic(url: str, search_params: dict[str, Any], api_key: Optional[str] = None) -> str:
    list_url = url or FDIC_URL
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(list_url))
    records: list[dict[str, Any]] = []
    with Fetcher() as f:
        listings, total = parse_fdic_list(f.get(list_url))
        seen = {x.get("auction_id") for x in listings}
        page_no = 2
        while len(seen) < total and page_no <= 20:
            more, _ = parse_fdic_list(f.get(list_url, {"page": page_no}))
            new = [x for x in more if x.get("auction_id") not in seen]
            if not new:
                break
            listings += new
            seen |= {x.get("auction_id") for x in new}
            page_no += 1
        for a in listings:
            loc = a.get("auction_location") or {}
            if not loc.get("line_1"):
                continue
            aid = str(a.get("auction_id"))
            page_url = f"{origin}/auctions/{aid}-{_slug(a.get('title') or '')}"
            d = parse_fdic_detail(f.get(page_url), aid)
            fl = d["fields"]
            occupancy = _fdic_field(fl, "occupan")
            records.append({
                "property_address": ", ".join(x for x in (loc.get("line_1"), loc.get("line_2")) if x),
                "property_city": loc.get("city"),
                "property_state": d.get("state") or state_code(loc.get("state_name")),
                "property_zip": (loc.get("zip_code") or "")[:5] or None,
                "county": d.get("county"),
                "property_type": ((a.get("auction_category") or {}).get("name") or a.get("title") or "").strip() or None,
                "beds": _int(_fdic_field(fl, "bed")),
                "baths": money(_fdic_field(fl, "bath")),
                "sqft": _int(_fdic_field(fl, "square feet", "sq ft", "sqft")),
                "year_built": _int(_fdic_field(fl, "year built")),
                "land_acres": money(_fdic_field(fl, "total land area")) if "acre" in str(_fdic_field(fl, "land area units") or "").lower() else None,
                "parcel_number": _fdic_field(fl, "tax identification", "parcel", "apn"),
                "occupancy": occupancy,
                "vacant": bool(occupancy and "vacant" in str(occupancy).lower()),
                "listing_status": _fdic_field(fl, "listing status"),
                "list_price": money(str(d["asking"])) if d.get("asking") else None,
                "listing_agent": d.get("listing_agent"),
                "listing_id": aid,
                "description": (a.get("description") or "")[:400] or None,
                "program": "FDIC failed-bank real estate",
                "agency": "Federal Deposit Insurance Corporation",
                "source_url": page_url,
                "list_type": ListType.GOVERNMENT_SALE.value,
            })
    return json.dumps(records)


# ── 5. Tarrant County TX — Constable Pct 3 delinquent-tax sales ──────────────

TARRANT_URL = "https://www.tarrantcountytx.gov/en/constables/constable-3/delinquent-tax-sales/monthly-tax-sales-listings.html"
TAD_PARCELS_URL = "https://mapit.tarrantcounty.com/arcgis/rest/services/Dynamic/TADParcels/FeatureServer/0/query"
_CAUSE_RE = re.compile(r"^[A-Z]{0,3}\d{3,}-\d{2}$")


def parse_tarrant_index(page: str, base_url: str = TARRANT_URL) -> list[tuple[str, str]]:
    """(sale date ISO, page URL) for every monthly sale listed."""
    out: list[tuple[str, str]] = []
    for href, label in re.findall(r"""(?is)<a\b[^>]*href=["']([^"']*monthly-tax-sales-listings/[^"']+\.html)["'][^>]*>(.*?)</a>""", page):
        d = parse_date(html_text(label).title())
        if d:
            out.append((d, urljoin(base_url, href)))
    return out


def parse_tarrant_sale(page: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(tax-sale table rows, writs of execution) from one monthly sale page."""
    start = page.find('id="wpsm-mainContent"')
    main = page[page.find(">", start) + 1:] if start >= 0 else page
    tax_rows: list[dict[str, Any]] = []
    prev_end = 0
    for tm in re.finditer(r"(?is)<table\b.*?</table>", main):
        before = html_text(main[prev_end:tm.start()])
        prev_end = tm.end()
        firm = re.findall(r"([A-Z][A-Za-z&,.' ]{3,120}?)\s+-\s+Attorneys", before)
        for row in re.findall(r"(?is)<tr\b[^>]*>(.*?)</tr>", tm.group(0)):
            cells = [html_text(c) for c in re.findall(r"(?is)<t[dh]\b[^>]*>(.*?)</t[dh]>", row)]
            if len(cells) < 3 or not _CAUSE_RE.match(cells[0]):
                continue
            acct = re.match(r"\d{5,8}", cells[1])
            if not acct:
                continue
            tax_rows.append({
                "case_number": cells[0],
                "account_number": acct.group(0).zfill(8),
                "status": " ".join(c for c in cells[2:] if c).strip(),
                "attorney": firm[-1].strip() if firm else None,
            })
    writs: list[dict[str, Any]] = []
    for block in re.split(r'(?i)<div class="text section">', main):
        if "PROPERTY ADDRESS" not in block.upper():
            continue
        t = html_text(block)
        m = re.match(r"(?is)\s*(?P<cause>\S+)\s+(?P<plaintiff>.+?)\s+vs\.?\s+(?P<defendant>.+?)\s+PROPERTY ADDRESS:\s*"
                     r"(?P<addr>.+?\b\d{5}(?:-\d{4})?)\b\s*(?P<legal>.*)$", t)
        if not m:
            continue
        addr = split_address(m.group("addr"))
        if not addr:
            continue
        writs.append({**addr, "case_number": m.group("cause"), "lender": m.group("plaintiff").strip(),
                      "owner_name": m.group("defendant").strip(), "legal_description": m.group("legal").strip()[:400]})
    return tax_rows, writs


def parse_tad_owner_city(owner_city: str) -> tuple[Optional[str], Optional[str]]:
    """'FORT WORTH, TX' → ('FORT WORTH', 'TX')."""
    s = _clean(owner_city) or ""
    if "," in s:
        city, st = s.rsplit(",", 1)
        return city.strip() or None, (st.strip()[:2] or None)
    return s or None, None


def tad_record(attrs: dict[str, Any]) -> dict[str, Any]:
    """Tarrant Appraisal District parcel attributes → owner + property fields."""
    situs = _clean(attrs.get("SITUS_ADDR"))
    city = _clean(attrs.get("CITY"))
    owner_addr = _clean(attrs.get("OWNER_ADDR"))
    m_city, m_state = parse_tad_owner_city(attrs.get("OWNER_CITY") or "")
    m_zip = _clean(attrs.get("OWNER_ZIP"))
    situs_zip = (_clean(attrs.get("ZIPCODE")) or "")[:5] or None
    # TAD leaves the situs ZIP blank; when the owner's mailing address *is* the parcel, its ZIP is the parcel's.
    if not situs_zip and situs and owner_addr == situs and m_city and city and m_city.upper() == city.upper():
        situs_zip = m_zip
    owner = _clean(attrs.get("OWNER_NAME"))
    year = attrs.get("YEAR_BUILT")
    return {
        "property_address": situs,
        "property_city": city.title() if city else None,
        "property_state": "TX",
        "property_zip": situs_zip,
        "owner_name": owner,
        "owner_is_llc": is_entity(owner),
        "mailing_address": owner_addr,
        "mailing_city": m_city,
        "mailing_state": m_state,
        "mailing_zip": m_zip,
        "year_built": int(year) if year and year > 1700 else None,
        "sqft": int(attrs["LIVING_ARE"]) if attrs.get("LIVING_ARE") else None,
        "beds": int(attrs["BEDROOMS"]) if attrs.get("BEDROOMS") else None,
        "baths": float(attrs["BATHROOMS"]) if attrs.get("BATHROOMS") else None,
        "assessed_value": attrs.get("APPRAISEDV") or None,
        "land_acres": attrs.get("LAND_ACRES") or None,
        "legal_description": _clean(attrs.get("LEGAL_1")),
        "parcel_number": _clean(attrs.get("TAXPIN")),
    }


def tad_lookup(f: Fetcher, accounts: list[str]) -> dict[str, dict[str, Any]]:
    """Account number → TAD parcel attributes (one ArcGIS query per 100 accounts)."""
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(accounts), 100):
        batch = [a for a in accounts[i:i + 100] if a.isdigit()]
        if not batch:
            continue
        data = json.loads(f.get(TAD_PARCELS_URL, {
            "where": "ACCOUNT IN (" + ",".join(f"'{a}'" for a in batch) + ")",
            "outFields": "*", "returnGeometry": "false", "f": "json",
        }))
        if data.get("error"):
            raise ScrapingError(f"TAD parcel lookup failed: {data['error'].get('message')}")
        for feat in data.get("features") or []:
            attrs = feat.get("attributes") or {}
            if attrs.get("ACCOUNT"):
                out[str(attrs["ACCOUNT"]).strip()] = attrs
    return out


def scrape_tarrant(url: str, search_params: dict[str, Any], api_key: Optional[str] = None) -> str:
    index_url = url or TARRANT_URL
    include_withdrawn = bool(search_params.get("include_withdrawn"))
    today = date.today().isoformat()
    records: list[dict[str, Any]] = []
    with Fetcher() as f:
        sales = [(d, u) for d, u in parse_tarrant_index(f.get(index_url), index_url) if d >= today]
        tax_rows: list[dict[str, Any]] = []
        for sale_date, page_url in sorted(sales):
            rows, writs = parse_tarrant_sale(f.get(page_url))
            for r in rows:
                status = r["status"].lower()
                if "royalty" in status or ("for sale" not in status and not include_withdrawn):
                    continue
                tax_rows.append({**r, "sale_date": sale_date, "source_url": page_url})
            for w in writs:
                records.append({
                    **w,
                    "owner_is_llc": is_entity(w.get("owner_name")),
                    "sale_date": sale_date,
                    "sale_type": "Writ of execution (constable sale)",
                    "program": "Tarrant County Constable Pct 3 — writ of execution sale",
                    "agency": "Tarrant County Constable Precinct 3",
                    "county": "Tarrant",
                    "source_url": page_url,
                    "list_type": ListType.PRE_FORECLOSURE.value,
                })
        parcels = tad_lookup(f, sorted({r["account_number"] for r in tax_rows}))
        for r in tax_rows:
            attrs = parcels.get(r["account_number"])
            if not attrs or not _clean(attrs.get("SITUS_ADDR")):
                # no situs address on record → nothing to call or mail about
                log.info("Tarrant tax sale %s: TAD account %s has no parcel/situs address — skipped",
                         r["case_number"], r["account_number"])
                continue
            records.append({
                **tad_record(attrs),
                "case_number": r["case_number"],
                "account_number": r["account_number"],
                "listing_status": r["status"],
                "attorney": r["attorney"],
                "sale_date": r["sale_date"],
                "sale_type": "Delinquent-tax sale (constable)",
                "program": "Tarrant County Constable Pct 3 — delinquent-tax sale",
                "agency": "Tarrant County Constable Precinct 3",
                "county": "Tarrant",
                "source_url": r["source_url"],
                "parcel_source": "Tarrant Appraisal District (mapit.tarrantcounty.com TADParcels)",
                "list_type": ListType.TAX_DELINQUENT.value,
            })
    return json.dumps(records)


# ── Registry ──────────────────────────────────────────────────────────────────

ADAPTERS: dict[str, Callable[[str, dict[str, Any], Optional[str]], str]] = {
    "gov_treasury": scrape_treasury,
    "gov_irs": scrape_irs,
    "gov_usda": scrape_usda,
    "gov_fdic": scrape_fdic,
    "gov_tarrant": scrape_tarrant,
}

DIRECTORY: list[dict[str, str]] = [
    {
        "name": "U.S. Treasury Seized Real Property Auctions",
        "source_type": "gov_treasury",
        "url": TREASURY_URL,
        "description": "Homes, land and commercial property seized/forfeited under Treasury law (IRS-CI, HSI, Secret Service), sold by CWS. Address, auction date, sale #, starting bid, deposit, size, year built, county taxes, parcel. Government-owned — bid, no owner to call.",
        "search_hint": '{"details": true, "list_type": "government_sale"}',
        "list_type": ListType.GOVERNMENT_SALE.value,
        "state": "",
        "county": "",
    },
    {
        "name": "IRS Auctions — Seized Real Estate",
        "source_type": "gov_irs",
        "url": IRS_URL,
        "description": "Real estate the IRS seized for unpaid federal taxes (plus guaranteed-bid homes). Street address, taxpayer/owner of record, minimum bid, auction date, sale location. Seized sales import as tax-delinquent leads.",
        "search_hint": '{"list_type": "tax_delinquent"}',
        "list_type": ListType.TAX_DELINQUENT.value,
        "state": "",
        "county": "",
    },
    {
        "name": "USDA Rural Development / FSA Properties for Sale",
        "source_type": "gov_usda",
        "url": USDA_URL,
        "description": "USDA single-family, multi-family and farm/ranch REO and foreclosure listings nationwide. Address, county, price/bid, beds/baths/sqft, foreclosure sale date/location. Foreclosures import as pre-foreclosure, REO as government sale.",
        "search_hint": '{"programs": ["SFH", "MFH", "FSA"], "details": true, "list_type": "government_sale"}',
        "list_type": ListType.GOVERNMENT_SALE.value,
        "state": "",
        "county": "",
    },
    {
        "name": "FDIC Failed-Bank Real Estate Listings",
        "source_type": "gov_fdic",
        "url": FDIC_URL,
        "description": "Real estate the FDIC holds from failed banks (fdicrealestatelistings.com, linked from fdic.gov). Address, county, property type, size, parcel, occupancy, status, asking price when published.",
        "search_hint": '{"list_type": "government_sale"}',
        "list_type": ListType.GOVERNMENT_SALE.value,
        "state": "",
        "county": "",
    },
    {
        "name": "Tarrant County TX — Constable Pct 3 Delinquent Tax Sales",
        "source_type": "gov_tarrant",
        "url": TARRANT_URL,
        "description": "Upcoming monthly constable sales: delinquent-tax foreclosures (cause # + TAD account, enriched with owner, mailing address, situs, year built, appraised value from Tarrant Appraisal District) and writs of execution (plaintiff vs. owner, address). Site asks for a 30 s crawl delay, so a run takes ~1–2 min.",
        "search_hint": '{"include_withdrawn": false, "list_type": "tax_delinquent"}',
        "list_type": ListType.TAX_DELINQUENT.value,
        "state": "TX",
        "county": "Tarrant",
    },
]


def default_list_type(source: ScrapeSource) -> str:
    """The list type a bulk import uses for a source: search_params.list_type, else its directory entry's."""
    try:
        lt = (json.loads(source.search_params or "{}") or {}).get("list_type")
    except (json.JSONDecodeError, AttributeError):
        lt = None
    if lt in {e.value for e in ListType}:
        return lt
    entry = next((d for d in DIRECTORY if d["source_type"] == source.source_type), None)
    return entry["list_type"] if entry else ListType.GOVERNMENT_SALE.value


# ── Scoring + summary ─────────────────────────────────────────────────────────

def _days_until(iso: Optional[str]) -> Optional[int]:
    try:
        return (date.fromisoformat(str(iso)[:10]) - date.today()).days
    except (TypeError, ValueError):
        return None


def analyze_record(rec: dict[str, Any]) -> str:
    """Deterministic motivated-seller read: 'Signals: …. Lead score: N/100. Recommend: ….'"""
    signals: list[str] = []
    score = 30
    lt = rec.get("list_type")
    owner = (rec.get("owner_name") or "").strip()
    gov_owned = lt == ListType.GOVERNMENT_SALE.value
    if lt == ListType.TAX_DELINQUENT.value:
        if "Internal Revenue" in (rec.get("agency") or ""):
            signals.append("IRS seizure for unpaid federal taxes")
        else:
            signals.append("delinquent-tax foreclosure sale")
        score += 35
    elif lt == ListType.PRE_FORECLOSURE.value:
        signals.append("judgment writ of execution — forced sale" if rec.get("lender") else "foreclosure sale scheduled")
        score += 30
    elif gov_owned:
        signals.append(f"government-owned sale ({rec.get('agency') or rec.get('program') or 'federal'})")
        score += 15
    if owner and not gov_owned:
        signals.append("owner of record named")
        score += 5
    mailing = (rec.get("mailing_address") or "").strip().lower()
    if mailing and owner and mailing != (rec.get("property_address") or "").strip().lower():
        signals.append("absentee owner")
        score += 15
    if owner and is_entity(owner):
        signals.append("corporate/LLC ownership")
        score += 5
    if rec.get("vacant"):
        signals.append("vacant")
        score += 10
    year = rec.get("year_built")
    if isinstance(year, int) and 1700 < year < 1980:
        signals.append("older property (likely needs repairs)")
        score += 5
    if owner and re.search(r"\bEST\.?$|\bESTATE OF\b|\bDECEASED\b|\bDEC'?D\b|\bHEIRS?\b", owner, re.I):
        signals.append("owner of record is an estate (probate)")
        score += 10
    if rec.get("partial_interest"):
        signals.append("partial interest only")
        score -= 10
    days = _days_until(rec.get("sale_date"))
    if days is not None:
        if days < 0:
            signals.append(f"sale date {rec['sale_date']} has passed")
            score -= 25
        elif days <= 45:
            signals.append(f"sale in {days} days ({rec['sale_date']})")
            score += 10
    score = max(0, min(100, score))
    if gov_owned:
        action = "run comps and bid" + (f" before the {rec['sale_date']} sale" if days is not None and days >= 0 else "")
    elif score >= 60:
        action = "call the owner before the sale" if days is not None and days >= 0 else "call to qualify"
    else:
        action = "research further"
    return f"Signals: {', '.join(signals) or 'none noted'}. Lead score: {score}/100. Recommend: {action}."


def summarize(records: list[dict[str, Any]], source_name: str) -> str:
    if not records:
        return f"{source_name}: no properties listed right now."
    upcoming = sum(1 for r in records if (d := _days_until(r.get("sale_date"))) is not None and 0 <= d <= 30)
    owners = sum(1 for r in records if r.get("owner_name"))
    states = sorted({r["property_state"] for r in records if r.get("property_state")})
    parts = [f"{source_name}: {len(records)} properties"]
    if states:
        parts.append(f"in {', '.join(states)}")
    text = " ".join(parts) + "."
    if upcoming:
        text += f" {upcoming} sell within 30 days."
    if owners:
        text += f" {owners} name the owner of record."
    return text + " Review and import."


# ── Registration + bulk import ────────────────────────────────────────────────

def ensure_government_sources(db: Session) -> list[str]:
    """Create the government sale sources that don't exist yet (matched by URL or name).

    New rows are active + daily auto-scrape. Existing rows are left as the operator set them.
    Returns the names of the sources created.
    """
    existing = db.execute(select(ScrapeSource.name, ScrapeSource.url)).all()
    names = {n for n, _ in existing}
    urls = {u for _, u in existing}
    created: list[str] = []
    for entry in DIRECTORY:
        if entry["url"] in urls or entry["name"] in names:
            continue
        db.add(ScrapeSource(
            name=entry["name"],
            url=entry["url"],
            source_type=entry["source_type"],
            state=entry["state"] or None,
            county=entry["county"] or None,
            search_params=entry["search_hint"],
            active=True,
            auto_scrape=True,
        ))
        created.append(entry["name"])
    if created:
        db.commit()
    return created


def import_all_new(db: Session, source_ids: Optional[list[str]] = None) -> dict[str, Any]:
    """Import every not-yet-imported result of each government source's latest completed job.

    Results are grouped by their own ``list_type`` (e.g. IRS seized → tax_delinquent, USDA
    foreclosure → pre_foreclosure), falling back to the source's list type; one source list is
    created per (source, list type). Lead dedupe is handled by ``import_scrape_results``.
    """
    from app.services.scraper import import_scrape_results

    q = select(ScrapeSource).where(ScrapeSource.source_type.in_(list(ADAPTERS)))
    if source_ids:
        q = q.where(ScrapeSource.id.in_(source_ids))
    report: list[dict[str, Any]] = []
    total_imported = total_skipped = 0
    stamp = utcnow().strftime("%Y-%m-%d")
    for source in db.execute(q.order_by(ScrapeSource.name)).scalars().all():
        job = db.execute(
            select(ScrapeJob).where(ScrapeJob.source_id == source.id, ScrapeJob.status == ScrapeJobStatus.COMPLETED)
            .order_by(ScrapeJob.completed_at.desc(), ScrapeJob.created_at.desc()).limit(1)
        ).scalars().first()
        entry: dict[str, Any] = {"source_id": source.id, "source": source.name, "job_id": job.id if job else None,
                                 "imported": 0, "skipped": 0, "lists": []}
        if job is None:
            report.append(entry)
            continue
        pending = db.execute(
            select(ScrapeResult).where(ScrapeResult.job_id == job.id, ScrapeResult.imported == False)  # noqa: E712
        ).scalars().all()
        fallback = default_list_type(source)
        groups: dict[str, list[str]] = {}
        for r in pending:
            try:
                lt = (json.loads(r.property_data) or {}).get("list_type")
            except (json.JSONDecodeError, TypeError, AttributeError):
                lt = None
            groups.setdefault(lt if lt in {e.value for e in ListType} else fallback, []).append(r.id)
        for lt, ids in sorted(groups.items()):
            res = import_scrape_results(db, ids, lt, list_name=f"{source.name} — {lt.replace('_', ' ')} {stamp}")
            entry["imported"] += res["imported"]
            entry["skipped"] += res["skipped"]
            entry["lists"].append({"list_type": lt, "source_list_id": res["source_list_id"],
                                   "imported": res["imported"], "skipped": res["skipped"]})
        total_imported += entry["imported"]
        total_skipped += entry["skipped"]
        report.append(entry)
    return {"imported": total_imported, "skipped": total_skipped, "sources": report}
