"""Phone finder: owner phone numbers (and sale contacts) for one property.

Sources (``PHONE_SOURCES``; enabled per source in Settings → Property data sources →
Phone finder sources, stored in the ``phone_finder_sources`` setting):

- ``listing`` — the property's own sale page (the ``(source: …)`` URL in its notes), read
  politely (robots.txt, our User-Agent, ≥1 s per host). Numbers printed for THIS sale — IRS
  sale contact, Treasury auction line, USDA servicing office, FDIC listing broker / closing
  agent, Tarrant constable office / tax-suit attorney — are stored as ``agency`` /
  ``listing_agent`` contacts (never ``owner``), confidence high. Agent phones of
  marketplace listings already found for the property are added the same way.
- ``grok`` — xAI Responses API + server-side ``web_search``: numbers publicly published for
  THIS owner (individual: directory / business pages tying the name to the property or
  mailing address; entity: state filings, company site, business listings) and contacts the
  sale notice publishes. Every number cites a page; we re-read it (sites whose terms forbid
  bots are not fetched). A page we can't read counts only if the search engine itself returned
  its URL (Grok can cite URLs it made up). Grades:
  high = number next to the owner's name + address on the page we read; medium = page not
  readable by us but the tool-read quote shows number + name; low = name next to number but
  no address on our read; numbers not on a readable cited page are dropped.
- ``batchdata`` / ``realestateapi`` — paid skip-trace APIs (BatchData Property Skip Trace v3,
  RealEstateAPI SkipTrace v2). They return owner phones with line type and a real DNC scrub,
  stored with ``dnc=`` and confidence high (persons whose name doesn't match the owner are skipped).

All numbers go through ``phones.upsert_phone``: nothing is removed or downgraded.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlsplit

import httpx
from sqlalchemy.orm import Session

from app.models import Lead, Phone
from app.services import property_scan as ps
from app.services.phones import normalize_us, upsert_phone
from app.services.property_data import full_address
from app.timeutil import iso_utc, utcnow

SETTING_KEY = "phone_finder_sources"
GROK_MAX_TOOL_CALLS = 15
READ_MAX_PAGES = 12
WINDOW = 300  # chars: name / address must appear this close to the number on the page


@dataclass(frozen=True)
class PhoneSource:
    id: str
    name: str
    key_setting: Optional[str]
    description: str
    docs_url: str
    pricing_url: str
    default_enabled: bool
    note: str = ""


PHONE_SOURCES: tuple[PhoneSource, ...] = (
    PhoneSource(
        "listing", "Sale / listing page contacts", None,
        "Reads the property's own sale page (linked in its notes) and stores the contact numbers printed for that "
        "sale — IRS sale contact, Treasury auction line, USDA servicing office, FDIC broker, constable / tax "
        "attorney — as agency or listing-agent contacts, never as the owner.",
        "", "", True, note="Free; no key. Respects robots.txt, one request per second per site.",
    ),
    PhoneSource(
        "grok", "Public web research (Grok)", "xai_api_key",
        "Grok searches the public web for numbers published for this owner: business filings, the company's site, "
        "business listings, directory pages that tie the name to this property or mailing address. Every number "
        "cites its page; we re-read the page and grade confidence.",
        "https://docs.x.ai/developers/tools/web-search", "https://docs.x.ai/developers/models", True,
        note="Uses the xAI key and the Grok model chosen above. Measured $0.03–0.09 and 15–40 s per property (grok-4.3).",
    ),
    PhoneSource(
        "batchdata", "BatchData skip trace", "batchdata_api_key",
        "Property Skip Trace (v3): up to 3 people per property with phones, line type, federal DNC and TCPA flags.",
        "https://developer.batchdata.com/docs/batchdata/batchdata-v3/operations/create-a-property-skip-trace",
        "https://batchdata.io/pricing", False,
        note="Uses the BatchData key above. Billed per matched property.",
    ),
    PhoneSource(
        "realestateapi", "RealEstateAPI SkipTrace", "realestateapi_api_key",
        "SkipTrace v2: people tied to the address with phones, phone type and FTC DNC flag.",
        "https://developer.realestateapi.com/reference/skiptrace-v2", "https://www.realestateapi.com/pricing/", False,
        note="Uses the RealEstateAPI key above. Billed per match.",
    ),
)
SOURCES_BY_ID = {s.id: s for s in PHONE_SOURCES}


def _settings():
    from app.services import settings as s
    return s


def _key(db: Session, src: PhoneSource) -> str:
    return (_settings().get_setting(db, src.key_setting) or "").strip() if src.key_setting else ""


def _toggles(db: Session) -> dict[str, bool]:
    raw = _settings().get_setting(db, SETTING_KEY) or ""
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return {k: bool(v) for k, v in data.items()} if isinstance(data, dict) else {}


def phone_source_states(db: Session) -> list[dict]:
    """Sources with enabled / key status for Settings → Phone finder sources (keys never included)."""
    toggles = _toggles(db)
    out = []
    for s in PHONE_SOURCES:
        enabled = toggles.get(s.id, s.default_enabled)
        key_set = bool(_key(db, s))
        out.append({"id": s.id, "name": s.name, "description": s.description, "note": s.note,
                    "docs_url": s.docs_url, "pricing_url": s.pricing_url, "enabled": enabled,
                    "needs_key": bool(s.key_setting), "key_setting": s.key_setting, "key_set": key_set,
                    "ready": enabled and (key_set or not s.key_setting)})
    return out


# ── Property snapshot (plain values — safe for worker threads) ───────────────

@dataclass
class Info:
    target: ps.Target
    owner: str
    entity: bool
    first: str
    last: str
    mailing: Optional[ps.Target]
    notes: str
    sale_urls: list[str]


_SOURCE_URL_RE = re.compile(r"\(source:\s*(https?://[^\s)]+)\)")
_NAME_SUFFIX = {"jr", "sr", "ii", "iii", "iv", "etal", "et", "al", "est", "estate", "trustee", "deceased"}
_ENTITY_NOISE = {"llc", "inc", "corp", "corporation", "co", "company", "lp", "llp", "ltd", "trust", "the", "and",
                 "of", "holdings", "partnership", "group", "properties", "investments", "l.l.c"}


def _person_name(owner: str) -> tuple[str, str]:
    """(first, last) of an individual owner: 'HERNANDEZ, MARTIN' and 'Karlson T. Bessman' both work."""
    s = owner.split("&")[0].split(" AND ")[0].strip()
    if "," in s:
        last, rest = s.split(",", 1)
        toks = [t for t in re.findall(r"[A-Za-z'\-]+", rest) if t.lower() not in _NAME_SUFFIX]
        return (toks[0] if toks else ""), last.strip()
    toks = [t for t in re.findall(r"[A-Za-z'\-]+", s) if t.lower() not in _NAME_SUFFIX]
    if len(toks) < 2:
        return "", (toks[0] if toks else "")
    return toks[0], toks[-1]


def _info(lead: Lead) -> Info:
    from app.services.scraping_agent import is_entity
    owner = (lead.owner_name or "").strip()
    entity = bool(owner) and (bool(lead.owner_is_llc) or is_entity(owner))
    first, last = ("", "") if entity or not owner else _person_name(owner)
    mailing = None
    if (lead.mailing_address or "").strip():
        mailing = ps.Target(address=", ".join(x for x in (lead.mailing_address, lead.mailing_city, lead.mailing_state,
                                                           lead.mailing_zip) if x),
                            street=lead.mailing_address.strip(), city=(lead.mailing_city or "").strip(),
                            state=(lead.mailing_state or "").strip(), zip=(lead.mailing_zip or "").strip(),
                            county="", owner=owner)
    notes = lead.notes or ""
    return Info(target=ps._target(lead), owner=owner, entity=entity, first=first, last=last, mailing=mailing,
                notes=notes, sale_urls=list(dict.fromkeys(_SOURCE_URL_RE.findall(notes)))[:3])


# ── Matching helpers (on text normalized like property_scan._norm) ─────────────

def _word(w: str) -> str:
    return rf"(?<![a-z0-9]){re.escape(ps._norm(w))}(?![a-z0-9])"


def number_pattern(e164: str) -> str:
    d = e164[-10:]
    return rf"(?<!\d)(?:1[\s.\-]?)?{d[:3]}[\s.\-/]{{0,3}}{d[3:6]}[\s.\-]{{0,2}}{d[6:]}(?!\d)"


def owner_patterns(info: Info) -> list[str]:
    if info.entity:
        toks = [t for t in re.findall(r"[a-z0-9]+", ps._norm(info.owner)) if len(t) >= 3 and t not in _ENTITY_NOISE]
        return [_word(t) for t in toks[:3]]
    return [_word(x) for x in (info.last, info.first) if x]


def contact_patterns(name: str) -> list[str]:
    toks = [t for t in re.findall(r"[a-z]+", ps._norm(name)) if len(t) >= 3 and t not in _ENTITY_NOISE]
    return [_word(t) for t in toks[:2]]


def _near_all(text: str, anchor: str, pats: list[str], window: int = WINDOW) -> bool:
    for m in re.finditer(anchor, text):
        chunk = text[max(0, m.start() - window):m.end() + window]
        if all(re.search(p, chunk) for p in pats):
            return True
    return False


def name_matches(name: str, info: Info) -> bool:
    """True when ``name`` (from a page or provider) is this owner."""
    pats = owner_patterns(info)
    n = ps._norm(name)
    return bool(pats) and all(re.search(p, n) for p in pats)


# ── Results ─────────────────────────────────────────────────────────────────

@dataclass
class Found:
    number: str
    role: str
    source: str
    source_url: Optional[str]
    confidence: str
    contact_name: Optional[str] = None
    line_type: Optional[str] = None
    note: Optional[str] = None
    dnc: Optional[bool] = None
    checked: Optional[bool] = None  # True = re-read on the page by us


@dataclass
class Result:
    id: str
    status: str = "ok"            # ok | empty | error | skipped
    detail: str = ""
    cost_usd: Optional[float] = None
    found: list[Found] = field(default_factory=list)


_LINE_TYPES = {"mobile": "mobile", "wireless": "mobile", "cell": "mobile", "cellular": "mobile",
               "landline": "landline", "land line": "landline", "residential": "landline", "voip": "voip"}


def _line_type(v: Any) -> Optional[str]:
    s = str(v or "").strip().lower()
    for k, out in _LINE_TYPES.items():
        if k in s:
            return out
    return "unknown" if s else None


# ── 1. Sale / listing page contacts (deterministic) ─────────────────────────

_PHONE_RE = re.compile(
    r"(?<![\d$])(?:\+?1[\s.\-]?)?\(?([2-9]\d{2})\)?[\s.\-]{0,2}([2-9]\d{2})[\s.\-]?(\d{4})(?!\d)"
    r"(?:\s*(?:x|ext\.?|extension)\s*(\d{1,6}))?(?:\s*(option\s*\d+))?", re.I)
_FAX_RE = re.compile(r"\bfax\W*$", re.I)
_SKIP_LABEL_RE = re.compile(r"telephone operator|\btty\b|\btdd\b", re.I)


@dataclass
class PhoneHit:
    number: str
    extra: str      # extension / option, e.g. "x 4500"
    label: str      # text right before the number on its line


def line_phones(lines: list[str], i: int) -> list[PhoneHit]:
    """Phone numbers on ``lines[i]`` minus fax / switchboard numbers."""
    line = lines[i]
    out = []
    for m in _PHONE_RE.finditer(line):
        before = line[:m.start()]
        if not before.strip() and i:  # number alone on its line → the label is the previous line
            before = lines[i - 1]
        if _FAX_RE.search(before[-25:]) or _SKIP_LABEL_RE.search(before[-60:]):
            continue
        e164 = normalize_us("".join(m.group(1, 2, 3)))
        if e164:
            extra = " ".join(x for x in (f"x {m.group(4)}" if m.group(4) else "", m.group(5) or "") if x)
            out.append(PhoneHit(e164, extra, before.strip()[-80:]))
    return out


def _text_lines(html_body: str, url: str) -> list[str]:
    from app.services.scraping_agent import page_to_text
    return page_to_text(html_body, url, "html").text.splitlines()


def _shows_address(lines: list[str], t: ps.Target) -> bool:
    pats = ps.address_patterns(t)
    return bool(pats) and any(all(re.search(p, ps._norm(ln)) for p in pats) for ln in lines)


def _sale_line(notes: str, url: str) -> str:
    return next((ln for ln in notes.splitlines() if url in ln), "")


def _contacts_tarrant(f, url: str, body: str, info: Info) -> tuple[list[Found], str]:
    """Constable sale page: the attorney section the property's row sits in + the constable's
    delinquent-tax-sales phone. A writ section has no attorney line, so only the constable."""
    lines = _text_lines(body, url)
    sale = _sale_line(info.notes, url)
    ids = [m.group(1) for m in re.finditer(r"\b(?:Case|Account)\s+([\w\-]+)", sale)]
    addr = ps.address_patterns(info.target)
    anchor = next((i for i, ln in enumerate(lines) if any(re.search(rf"(?<![\w-]){re.escape(x)}(?![\w-])", ln) for x in ids)),
                  None)
    if anchor is None and addr:
        anchor = next((i for i, ln in enumerate(lines) if all(re.search(p, ps._norm(ln)) for p in addr)), None)
    if anchor is None:
        return [], "property not on the sale page any more"
    found: list[Found] = []
    status = lines[anchor].split("|")[-1].strip() if "|" in lines[anchor] else ""
    row_note = f"Sale page row: {lines[anchor][:120]}" if status else ""
    for j in range(anchor - 1, -1, -1):
        ln = lines[j]
        hits = line_phones(lines, j)
        if hits:
            firm = lines[j - 1].strip() if j and not _PHONE_RE.search(lines[j - 1]) else ""
            for h in hits:
                found.append(Found(h.number, "agency", "listing", url, "high", contact_name=(firm or h.label)[:255],
                                   note="; ".join(x for x in ("Tax-suit attorney for this sale (listed above the property's "
                                                              "cause / account on the constable's sale page)", h.extra,
                                                              row_note) if x)))
            break
        if "|" not in ln and ln.isupper() and not re.search(r"\d", ln) and len(ln.split()) >= 2:
            break  # another section (e.g. WRITS OF EXECUTION) — no attorney line for this row
    m = re.search(r"/constables/constable-(\d+)", url)
    if m:
        office = urljoin(url, f"/en/constables/constable-{m.group(1)}.html")
        olines = _text_lines(f.get(office), office)
        # The office block: a "Delinquent Tax Sales Division" heading with its number a few lines below.
        for k, ln in enumerate(olines):
            if "delinquent tax sales division" not in ln.lower():
                continue
            hits = next((h for i in range(k + 1, min(k + 5, len(olines))) if (h := line_phones(olines, i))), [])
            for h in hits:
                found.append(Found(h.number, "agency", "listing", office, "high",
                                   contact_name=f"Tarrant County Constable Precinct {m.group(1)} — Delinquent Tax Sales Division",
                                   note="; ".join(x for x in ("Constable's office that conducts this sale", h.extra, row_note) if x)))
            if hits:
                break
    return found, (f"row status: {status}" if status else "")


def _contacts_irs(f, url: str, body: str, info: Info) -> tuple[list[Found], str]:
    lines = _text_lines(body, url)
    if not _shows_address(lines, info.target):
        return [], "page doesn't show this address"
    found = []
    k = next((i for i, ln in enumerate(lines) if ln.strip().lower() == "contact information"), None)
    who = " — ".join(x for x in lines[k + 1:k + 3]) if k is not None else ""
    for i, ln in enumerate(lines):
        if ln.strip().lower() == "contact phone number" and i + 1 < len(lines):
            for h in line_phones(lines, i + 1):
                found.append(Found(h.number, "agency", "listing", url, "high", contact_name=(who or "Internal Revenue Service")[:255],
                                   note="IRS contact listed on this property's auction notice" + (f"; {h.extra}" if h.extra else "")))
    return found, ""


def _contacts_treasury(f, url: str, body: str, info: Info) -> tuple[list[Found], str]:
    """Treasury property page (no phone) → its Terms of Sale page ('Public Auction Line')."""
    pages = [(url, body)]
    m = re.search(r"""href=["']([^"'#]+ts\.s?html?)["']""", body, re.I)
    if m:
        terms = urljoin(url, m.group(1))
        pages.append((terms, f.get(terms)))
    found = []
    for page_url, page in pages:
        lines = _text_lines(page, page_url)
        for i in range(len(lines)):
            for h in line_phones(lines, i):
                label = re.search(r"([A-Z][\w&]*(?:\s+[A-Z][\w&]*){0,4})\s*(?:at|:)?\s*$", h.label)
                found.append(Found(h.number, "agency", "listing", page_url, "high",
                                   contact_name=("U.S. Treasury real property auctions — " + (label.group(1) if label else "contact"))[:255],
                                   note="Number printed on this property's Treasury sale" + (" terms page" if page_url != url else " page")
                                        + (f"; {h.extra}" if h.extra else "")))
    return found, ""


def _contacts_usda(f, url: str, body: str, info: Info) -> tuple[list[Found], str]:
    """USDA property page → its 'Servicing Office Location' page (property-specific link)."""
    m = re.search(r"""href=["']([^"']*contactCSCFCL[^"']*)["']""", body, re.I)
    if not m:
        return [], "no servicing-office link on the property page"
    office = urljoin(url, m.group(1).replace("&amp;", "&"))
    lines = _text_lines(f.get(office), office)
    k = next((i for i, ln in enumerate(lines) if ln.lower().startswith("property id")), -1)
    found = []
    for i in range(k + 1, len(lines)):
        hits = line_phones(lines, i)
        if not hits:
            continue
        block = []
        for ln in lines[k + 1:i]:
            if re.match(r"\s*\d", ln):
                break
            block.append(ln.strip())
        contact = next((ln.split(":", 1)[1].strip() for ln in lines[i:i + 4] if ln.lower().startswith("contact:")), "")
        for h in hits:
            found.append(Found(h.number, "agency", "listing", office, "high",
                               contact_name=" — ".join(block[:3])[:255] or "USDA servicing office",
                               note="; ".join(x for x in ("USDA servicing office for this property", h.extra,
                                                          f"Contact: {contact}" if contact else "") if x)))
    return found, ""


def _contacts_fdic(f, url: str, body: str, info: Info) -> tuple[list[Found], str]:
    from app.services.gov_sources import apollo_state
    state = apollo_state(body)
    found = []
    for obj in state.values():
        re_info = obj.get("real_estate_info") if isinstance(obj, dict) else None
        if not isinstance(re_info, dict):
            continue
        # The page shows these as "Listing Agent Information" and "Asset Manager" (closing_agent_* fields).
        for prefix, role, what in (("listing_agent", "listing_agent", "FDIC listing agent"),
                                   ("closing_agent", "agency", "FDIC asset manager (closing agent)")):
            num = normalize_us(re_info.get(f"{prefix}_phone") or "")
            if not num:
                continue
            name = re_info.get(f"{prefix}_name") or ""
            firm = re_info.get(f"{prefix}_company") or re_info.get(f"{prefix}_firm") or ""
            found.append(Found(num, role, "listing", url, "high", contact_name=", ".join(x for x in (name, firm) if x)[:255] or what,
                               note=f"{what} for this property (FDIC listing page)"))
        break
    return found, ""


def _contacts_generic(f, url: str, body: str, info: Info) -> tuple[list[Found], str]:
    """Other sale pages: labelled numbers on a page that shows this address and few numbers."""
    lines = _text_lines(body, url)
    if not _shows_address(lines, info.target):
        return [], "page doesn't show this address"
    hits = [(i, h) for i in range(len(lines)) for h in line_phones(lines, i)]
    if len({h.number for _, h in hits}) > 4:
        return [], "page lists many numbers (multi-property page) — not attributed"
    found = []
    for i, h in hits:
        prev = lines[i - 1].strip() if i else ""
        found.append(Found(h.number, "agency", "listing", url, "medium", contact_name=(h.label or prev)[:255] or None,
                           note="Contact number printed on this property's sale page" + (f"; {h.extra}" if h.extra else "")))
    return found, ""


_PROFILES: tuple[tuple[str, Callable], ...] = (
    ("tarrantcountytx.gov", _contacts_tarrant), ("tarrantcounty.com", _contacts_tarrant),
    ("irsauctions.gov", _contacts_irs), ("treasury.gov", _contacts_treasury),
    ("properties.sc.egov.usda.gov", _contacts_usda), ("fdicrealestatelistings.com", _contacts_fdic),
)


def sale_page_contacts(info: Info) -> Result:
    from app.services.gov_sources import Fetcher
    from app.services.scraping_agent import ScrapingError
    res = Result("listing")
    if not info.sale_urls:
        res.status, res.detail = "skipped", "no sale page linked in the property's notes"
        return res
    notes, errors = [], []
    with Fetcher() as f:
        for url in info.sale_urls:
            host = (urlsplit(url).hostname or "").lower()
            if urlsplit(url).path.lower().endswith(".pdf"):
                notes.append(f"{host}: PDF not read")
                continue
            fn = next((fn for h, fn in _PROFILES if host == h or host.endswith("." + h)), _contacts_generic)
            try:
                found, note = fn(f, url, f.get(url), info)
            except ScrapingError as exc:
                errors.append(str(exc)[:160])
                continue
            for x in found:
                x.checked = True
            res.found += found
            if note:
                notes.append(f"{host}: {note}")
    res.detail = "; ".join(notes + errors)
    res.status = "ok" if res.found else ("error" if errors else "empty")
    return res


def listing_agent_phones(lead: Lead) -> list[Found]:
    """Agent phones on marketplace listings already found for this property (main thread)."""
    out = []
    for lst in getattr(lead, "listings", None) or []:
        num = normalize_us(lst.agent_phone or "")
        if num:
            out.append(Found(num, "listing_agent", "listing", lst.url, "high" if lst.check == "confirmed" else "medium",
                             contact_name=", ".join(x for x in (lst.agent_name, lst.agent_company) if x)[:255] or None,
                             note=f"Agent on the {lst.site_label} listing ({lst.status})", checked=lst.check == "confirmed"))
    return out


# ── 2. Public web research (Grok) ───────────────────────────────────────────

GROK_PHONE_INSTRUCTIONS = """You find PUBLISHED phone numbers for the owner of ONE U.S. property, for a real-estate acquisitions team, using web search and by opening pages.

Where to look:
- Individual owner: public pages that tie this person's name to THIS property address or to the owner's mailing address — people / phone directory pages, the owner's own business listing or website at that address, public notices naming them with a number.
- Company / LLC / trust owner: the state business-entity filing (registered agent / officers and any published phone), the company's own website, business listings (Google Business-style profiles, BBB, Yelp, chamber directories) for that company.
- Also report numbers the sale notice or listing for THIS property publishes for questions about the sale (agency office, revenue officer, auction company, listing broker, constable / sheriff, tax attorney) with role agency or listing_agent.
- Private owner and no mailing address given: first look up the owner's mailing address on the county appraisal district / assessor record of THIS property and report it in owner_mailing (address, the record's URL, and a verbatim quote showing the owner's name and that address); owners often live elsewhere, and a directory page with the owner's name at that mailing address counts as address_match mailing.

Rules — data validity matters more than coverage:
- Report only numbers printed on a page you found. Never guess, complete, or reformat partial / masked numbers.
- source_url: the exact page that shows the number (not a search page). quote: verbatim text from that page (max 300 characters) containing the number and the person or company name.
- role owner only when the page ties the number to the owner's name AND to this property's address, the owner's mailing address, or (for a company) its city. People with the same name elsewhere, relatives, neighbours, previous owners, the county, courts and auction companies are NOT the owner: list their numbers in rejected with the reason.
- address_match: what the page shows next to the name: property | mailing | city | none.
- line_type only when the page states it (wireless / landline / VoIP); otherwise unknown.
- Never put absence in phones. In checked, list what you searched for."""

GROK_PHONE_SCHEMA = {
    "type": "object",
    "properties": {
        "phones": {"type": "array", "items": {"type": "object", "properties": {
            "number": {"type": "string"},
            "contact_name": {"type": "string", "description": "Name shown with the number on the page"},
            "role": {"type": "string", "enum": ["owner", "agency", "listing_agent"]},
            "relation": {"type": "string", "description": "Who this is, e.g. 'owner (directory listing at property)'"},
            "source_url": {"type": "string"},
            "source_name": {"type": "string"},
            "quote": {"type": "string"},
            "address_match": {"type": "string", "enum": ["property", "mailing", "city", "none"]},
            "line_type": {"type": "string", "enum": ["mobile", "landline", "voip", "unknown"]},
        }, "required": ["number", "contact_name", "role", "relation", "source_url", "source_name", "quote",
                        "address_match", "line_type"], "additionalProperties": False}},
        "rejected": {"type": "array", "items": {"type": "object", "properties": {
            "number": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["number", "reason"], "additionalProperties": False}},
        "owner_mailing": {"type": "object", "description": "Owner's mailing address from the official appraisal / "
                          "assessor record of this property; empty strings when not looked up or not found",
                          "properties": {"address": {"type": "string"}, "source_url": {"type": "string"},
                                         "quote": {"type": "string"}},
                          "required": ["address", "source_url", "quote"], "additionalProperties": False},
        "checked": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["phones", "rejected", "owner_mailing", "checked", "summary"],
    "additionalProperties": False,
}

# People-search / social sites whose terms forbid automated access: cited via Grok's search
# results only, never fetched by us (→ at most medium confidence).
NO_FETCH_HOSTS = (
    "whitepages.com", "spokeo.com", "truepeoplesearch.com", "fastpeoplesearch.com", "beenverified.com",
    "intelius.com", "radaris.com", "peoplefinders.com", "mylife.com", "thatsthem.com", "searchpeoplefree.com",
    "cyberbackgroundchecks.com", "clustrmaps.com", "fastbackgroundcheck.com", "usphonebook.com",
    "peoplesearchnow.com", "zabasearch.com", "411.com", "truthfinder.com", "instantcheckmate.com", "nuwber.com",
    "smartbackgroundchecks.com", "idcrawl.com", "linkedin.com", "facebook.com", "yelp.com", "peoplelooker.com",
    "advancedbackgroundchecks.com", "publicrecordsnow.com", "addresses.com", "anywho.com",
)


def _no_fetch(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in NO_FETCH_HOSTS)


def _grok_input(info: Info) -> str:
    t = info.target
    lines = [f"Property: {t.address}" + (f" ({t.county} County)" if t.county else "")]
    if info.owner:
        lines.append(f"Owner of record: {info.owner} ({'company / entity' if info.entity else 'individual'})")
    else:
        lines.append("Owner: a government agency selling a seized / foreclosed property — do not search for a private "
                     "owner; report only the contacts published for this sale.")
    if info.mailing:
        lines.append(f"Owner mailing address: {info.mailing.address}")
    sale = " ".join(ln.strip() for ln in info.notes.splitlines() if "source:" in ln)[:600]
    if sale:
        lines.append(f"Sale notice on our list: {sale}")
    return "\n".join(lines)


def _grok_call(key: str, model: str, info: Info) -> tuple[dict, dict]:
    body = {
        "model": model,
        "instructions": GROK_PHONE_INSTRUCTIONS,
        "input": [{"role": "user", "content": _grok_input(info)}],
        "tools": [{"type": "web_search"}],
        "max_tool_calls": GROK_MAX_TOOL_CALLS,
        "store": False,
        "text": {"format": {"type": "json_schema", "name": "owner_phones", "schema": GROK_PHONE_SCHEMA, "strict": True}},
    }
    try:
        r = httpx.post(f"{ps.XAI_BASE}/responses", json=body, timeout=ps.GROK_TIMEOUT,
                       headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    except httpx.TimeoutException as exc:
        raise ps.ScanError(f"Grok phone research timed out after {ps.GROK_TIMEOUT:.0f}s ({model}).") from exc
    except httpx.HTTPError as exc:
        raise ps.ScanError(f"xAI unreachable: {type(exc).__name__}") from exc
    if r.status_code >= 400:
        raise ps.ScanError(ps._xai_error(r))
    resp = r.json()
    if resp.get("status") != "completed":
        raise ps.ScanError(f"Grok phone research did not complete ({resp.get('status')}: "
                           f"{resp.get('incomplete_details') or resp.get('error')})")
    try:
        return json.loads(ps._message_text(resp)), resp
    except ValueError as exc:
        raise ps.ScanError("Grok phone research returned no structured result.") from exc


def _read_cited(urls: list[str]) -> dict[str, tuple[Optional[str], Optional[str]]]:
    """url → (normalized page text, None) | (None, reason); same host sequential, hosts in parallel."""
    by_host: dict[str, list[str]] = {}
    out: dict[str, tuple[Optional[str], Optional[str]]] = {}
    for u in urls:
        if _no_fetch(u):
            out[u] = (None, "site's terms forbid automated access — not re-read by us")
        elif sum(len(v) for v in by_host.values()) < READ_MAX_PAGES:
            by_host.setdefault(urlsplit(u).hostname or "", []).append(u)
        else:
            out[u] = (None, "not re-read (page limit)")
    if by_host:
        with ThreadPoolExecutor(max_workers=min(6, len(by_host))) as ex:
            for res in ex.map(ps._read_pages, by_host.values()):
                out.update(res)
    return out


_RANK = {"low": 1, "medium": 2, "high": 3}
# Any phone number in normalized page text (counting distinct numbers spots multi-record pages).
_PAGE_PHONE_RE = r"(?<!\d)[2-9]\d{2}[\s.\-/]{0,3}[2-9]\d{2}[\s.\-]{0,2}\d{4}(?!\d)"


def _mailing_target(addr: str, owner: str) -> Optional[ps.Target]:
    """Target for a street mailing address ('123 Main St, Mansfield, TX 76063'); None for PO boxes etc."""
    parts = [x.strip() for x in addr.split(",")]
    if not re.match(r"\d+\s+\S+", parts[0]):
        return None
    return ps.Target(address=addr, street=parts[0], city=parts[1] if len(parts) > 1 else "", state="", zip="",
                     county="", owner=owner)


def _addr_pats(*targets: Optional[ps.Target]) -> list[list[str]]:
    return [p for p in (ps.address_patterns(t) for t in targets if t) if p]


def grade_web_phone(p: dict, e164: str, info: Info, page: Optional[str], mailing: Optional[ps.Target] = None,
                    mailing_cap: Optional[str] = None) -> tuple[Optional[str], str]:
    """(confidence or None to drop, check note) for one Grok-cited number. ``mailing``: the owner's mailing
    address (from the lead, or confirmed by this run); ``mailing_cap``: max confidence it supports."""
    role = p["role"]
    num = number_pattern(e164)
    names = owner_patterns(info) if role == "owner" else contact_patterns(p.get("contact_name") or "")
    if page is None:
        quote = ps._norm(p.get("quote") or "")
        if not (re.search(num, quote) and names and all(re.search(n, quote) for n in names)):
            return None, "page not readable and the quote doesn't tie the number to the name"
        if role == "owner" and not any(all(re.search(a, quote) for a in pats)
                                       for pats in _addr_pats(info.target, mailing)):
            return None, "page not readable and the quote doesn't show the property / mailing address"
        return "medium", "tool-read quote shows the number with the name" + (" and address" if role == "owner" else "") + \
            "; page not re-read by us"
    if not re.search(num, page):
        return None, "number not found on the cited page"
    name_ok = bool(names) and _near_all(page, num, names)
    if role != "owner":
        if not name_ok:
            return None, "contact name not next to the number on the page"
        addr = ps.address_patterns(info.target)
        about = bool(addr) and all(re.search(a, page) for a in addr)
        if about and len(set(re.findall(_PAGE_PHONE_RE, page))) > 3:
            return None, "page lists several properties / contacts — number not attributable to this sale"
        if about and _near_all(page, num, addr, window=1500):
            return "high", "re-read: number next to the contact name, near this property's address"
        if about:
            return "medium", "re-read: number next to the contact name on a page about this property"
        return "low", "re-read: number next to the contact name; the page doesn't mention this property"
    if not name_ok:
        return None, "owner's name not next to the number on the page"
    for t, label, cap in ((info.target, "property address", None), (mailing, "mailing address", mailing_cap)):
        pats = ps.address_patterns(t) if t else []
        if pats and _near_all(page, num, pats + names):
            return cap or "high", f"re-read: number next to the owner's name and {label}"
    if info.entity and info.target.city and _near_all(page, num, names + [_word(info.target.city)]):
        return "medium", "re-read: number next to the company name and city"
    return None, "owner's name is next to the number but neither the property nor the mailing address is"


def _confirm_mailing(out: dict, info: Info, pages: dict, searched: set) -> tuple[Optional[ps.Target], Optional[str], str]:
    """Owner mailing address Grok reported from the assessor record → (target, cap, note) once confirmed."""
    om = out.get("owner_mailing") or {}
    url = (om.get("source_url") or "").strip()
    t = _mailing_target((om.get("address") or "").strip(), info.owner) if url.startswith("http") else None
    if t is None:
        return None, None, ""
    pats, names = ps.address_patterns(t), owner_patterns(info)
    text, _why = pages.get(url, (None, None))
    if text is not None and names and _near_all(text, names[0], names[1:] + pats, window=600):
        return t, None, f"owner mailing address {t.address} confirmed on {url}"
    quote = ps._norm(om.get("quote") or "")
    if text is None and ps._was_touched(url, searched) and all(re.search(x, quote) for x in names + pats):
        return t, "medium", f"owner mailing address {t.address} per {url} (not re-read)"
    return None, None, f"owner mailing address {t.address} not confirmed on its cited page"


def _parse_number(raw: str) -> tuple[Optional[str], str]:
    """(E.164, extension / option text) from a number as printed, e.g. '(800) 349-5097 x 4500'."""
    m = _PHONE_RE.search(raw or "")
    if not m:
        return None, ""
    extra = " ".join(x for x in (f"x {m.group(4)}" if m.group(4) else "", m.group(5) or "") if x)
    return normalize_us("".join(m.group(1, 2, 3))), extra


def grok_phones(key: str, model: str, info: Info) -> Result:
    res = Result("grok")
    t0 = time.monotonic()
    try:
        out, resp = _grok_call(key, model, info)
    except ps.ScanError as exc:
        res.status, res.detail = "error", str(exc)
        return res
    usage = resp.get("usage") or {}
    res.cost_usd = round((usage.get("cost_in_usd_ticks") or 0) / 1e10, 4)
    # Only URLs the search engine itself returned count as proof — Grok can "open" and cite URLs it
    # made up. Any other cited URL must be confirmed by our own re-read of the page.
    searched = ps.search_result_urls(resp)
    dropped: dict[str, int] = {}
    cands: list[tuple[dict, str, str]] = []
    sale_pages = {ps._url_key(u) for u in info.sale_urls}
    for p in out.get("phones") or []:
        e164, extra = _parse_number(p.get("number") or "")
        url = (p.get("source_url") or "").strip()
        reason = None
        if not e164:
            reason = "invalid number"
        elif not url.lower().startswith(("http://", "https://")):
            reason = "no source URL"
        elif p.get("role") not in ("owner", "agency", "listing_agent"):
            reason = "bad role"
        elif p["role"] == "owner" and not info.owner:
            reason = "no private owner"
        elif p["role"] == "owner" and not name_matches(p.get("contact_name") or "", info):
            reason = "name isn't the owner"
        elif p["role"] == "owner" and p.get("address_match") not in (
                ("property", "mailing", "city") if info.entity else ("property", "mailing")):
            reason = "not tied to the property / mailing address"
        elif p["role"] != "owner" and ps._was_touched(url, sale_pages):
            reason = "sale page — read by the sale-page reader, which attributes each section"
        if reason:
            dropped[reason] = dropped.get(reason, 0) + 1
        else:
            cands.append((p, e164, extra))
    mail_url = ((out.get("owner_mailing") or {}).get("source_url") or "").strip()
    want_mail = not info.mailing and info.owner and mail_url.startswith("http")
    pages = _read_cited(list(dict.fromkeys([p["source_url"].strip() for p, _, _ in cands] + ([mail_url] if want_mail else []))))
    mailing, mail_cap, mail_note = (info.mailing, None, "") if info.mailing else _confirm_mailing(out, info, pages, searched)
    best: dict[str, Found] = {}
    for p, e164, extra in cands:
        url = p["source_url"].strip()
        text, why = pages.get(url, (None, "not re-read"))
        if text is None and not ps._was_touched(url, searched):
            dropped["source not in search results and not re-readable"] = dropped.get(
                "source not in search results and not re-readable", 0) + 1
            continue
        conf, check = grade_web_phone(p, e164, info, text, mailing, mail_cap)
        if conf is None:
            dropped[check] = dropped.get(check, 0) + 1
            continue
        lt = p.get("line_type")
        f = Found(e164, p["role"], "web", url, conf, contact_name=(p.get("contact_name") or "")[:255] or None,
                  line_type=lt if lt in ("mobile", "landline", "voip") else None,
                  note="; ".join(x for x in ((p.get("relation") or "").strip()[:160], extra, check + (
                      f" ({why})" if text is None and why else "")) if x),
                  checked=text is not None)
        if e164 not in best or _RANK[conf] > _RANK[best[e164].confidence]:
            best[e164] = f
    res.found = list(best.values())
    rejected = [r for r in out.get("rejected") or [] if isinstance(r, dict)]
    res.detail = "; ".join(x for x in (
        f"{len(res.found)} kept", f"dropped {sum(dropped.values())} ({', '.join(f'{k}: {v}' for k, v in dropped.items())})" if dropped else "",
        mail_note,
        (f"{len(rejected)} not the owner per Grok (" + " | ".join(f"{r.get('number')}: {(r.get('reason') or '')[:120]}"
                                                                  for r in rejected[:3]) + ")") if rejected else "",
        f"{(usage.get('server_side_tool_usage_details') or {}).get('web_search_calls', 0)} searches, "
        f"{time.monotonic() - t0:.0f}s, {resp.get('model') or model}") if x)
    res.status = "ok" if res.found else "empty"
    return res


# ── 3. Skip-trace APIs ──────────────────────────────────────────────────────

BATCHDATA_URL = "https://api.batchdata.com/api/v3/property/skip-trace"
REAPI_URL = "https://api.realestateapi.com/v2/SkipTrace"


def _flag(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    s = str(v or "").strip().lower()
    return True if s in ("true", "y", "yes", "1") else False if s in ("false", "n", "no", "0") else None


def _person_found(source: str, url: str, info: Info, name: str, phones: list[dict], get: Callable[[dict], dict],
                  provider: str) -> tuple[list[Found], bool]:
    """Owner-matched person's phones → Found; (found, matched). Entity owners: persons the provider ties
    to the property are the company's people → medium."""
    matched = name_matches(name, info) if not info.entity else True
    if not matched:
        return [], False
    out = []
    for ph in phones:
        d = get(ph)
        num = normalize_us(d["number"] or "")
        if not num:
            continue
        note = "; ".join(x for x in (f"{provider} skip trace", d.get("extra") or "",
                                     f"person tied to {info.owner}" if info.entity else "") if x)
        out.append(Found(num, "owner", source, url, "medium" if info.entity else "high", contact_name=name[:255] or None,
                         line_type=_line_type(d.get("type")), note=note[:500], dnc=d.get("dnc")))
    return out, True


def batchdata_phones(key: str, info: Info) -> Result:
    res = Result("batchdata")
    t = info.target
    body = {"requests": [{"propertyAddress": {"street": t.street, "city": t.city, "state": t.state, "zip": t.zip}}],
            "options": {"includeTCPABlacklistedPhones": True}}
    try:
        data = ps._request(ps.CATALOG_BY_ID["batchdata"], "POST", BATCHDATA_URL, json=body,
                           headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    except ps.ScanError as exc:
        res.status, res.detail = "error", str(exc)
        return res
    persons = ps._pick(data, "results.persons") or []
    skipped = 0
    for p in persons if isinstance(persons, list) else []:
        nm = p.get("name") or {}
        name = nm.get("full") or " ".join(x for x in (nm.get("first"), nm.get("middle"), nm.get("last")) if x)
        litigator = bool(p.get("litigator")) or bool(ps._pick(p, "dnc.tcpa"))

        def get(ph: dict) -> dict:
            dnc = ph.get("dnc")
            flagged = None if dnc is None else bool(dnc) or bool(ph.get("tcpa")) or litigator
            extra = ", ".join(x for x in (ph.get("carrier"), f"score {ph['score']}" if ph.get("score") is not None else "",
                                          f"last seen {ph['lastReportedDate']}" if ph.get("lastReportedDate") else "",
                                          "TCPA-restricted" if ph.get("tcpa") else "", "TCPA litigator" if litigator else "") if x)
            return {"number": ph.get("number"), "type": ph.get("type"), "dnc": flagged, "extra": extra}

        found, ok = _person_found("skiptrace:batchdata", BATCHDATA_URL, info, name, p.get("phoneNumbers") or [], get, "BatchData")
        res.found += found
        skipped += not ok
    res.detail = f"{len(persons)} people returned" + (f", {skipped} not the owner (skipped)" if skipped else "")
    res.status = "ok" if res.found else "empty"
    return res


def realestateapi_phones(key: str, info: Info) -> Result:
    res = Result("realestateapi")
    t = info.target
    body = {"address": t.street, "city": t.city, "state": t.state, "zip": t.zip}
    if not info.entity and info.first and info.last:
        body.update(first_name=info.first, last_name=info.last)
    if info.mailing:
        body.update(mail_address=info.mailing.street, mail_city=info.mailing.city, mail_state=info.mailing.state,
                    mail_zip=info.mailing.zip)
    try:
        data = ps._request(ps.CATALOG_BY_ID["realestateapi"], "POST", REAPI_URL, json=body,
                           headers={"x-api-key": key, "Content-Type": "application/json"})
    except ps.ScanError as exc:
        res.status, res.detail = "error", str(exc)
        return res
    persons = (data or {}).get("persons") or []
    skipped = 0
    for p in persons if isinstance(persons, list) else []:
        name = p.get("fullName") or " ".join(x for x in (p.get("firstName"), p.get("middleName"), p.get("lastName")) if x)

        def get(ph: dict) -> dict:
            extra = ", ".join(x for x in (f"last seen {ph['phoneLastSeen']}" if ph.get("phoneLastSeen") else "",
                                          f"12-mo usage {ph['phoneUsage12Month']}" if ph.get("phoneUsage12Month") else "") if x)
            return {"number": ph.get("phone"), "type": ph.get("phoneType"), "dnc": _flag(ph.get("phoneFtcDnc")), "extra": extra}

        found, ok = _person_found("skiptrace:realestateapi", REAPI_URL, info, name, p.get("phones") or [], get, "RealEstateAPI")
        res.found += found
        skipped += not ok
    res.detail = f"{len(persons)} people returned" + (f", {skipped} not the owner (skipped)" if skipped else "")
    res.status = "ok" if res.found else "empty"
    return res


# ── Orchestration ───────────────────────────────────────────────────────────

def _phone_dict(p: Phone) -> dict:
    return {"id": p.id, "number": p.number, "role": p.role, "source": p.source, "source_url": p.source_url,
            "confidence": p.confidence, "line_type": p.line_type, "contact_name": p.contact_name, "note": p.note,
            "dnc_flagged": bool(p.dnc_flagged), "dnc_checked_at": iso_utc(p.dnc_checked_at) if p.dnc_checked_at else None}


def _snap(p: Phone) -> tuple:
    return (p.role, p.source, p.source_url, p.confidence, p.line_type, p.contact_name, p.note, p.dnc_flagged, p.dnc_checked_at)


def find_owner_phones(db: Session, lead: Lead) -> dict:
    """Run every enabled phone source (in parallel), store numbers via upsert_phone, stamp the lead.

    Returns {added: [phone dicts], updated: int, sources: [{id, status, detail, cost_usd}], cost_usd}.
    """
    info = _info(lead)
    states = {s["id"]: s for s in phone_source_states(db)}
    jobs: dict[str, Callable[[], Result]] = {}
    skipped: list[Result] = []
    for src in PHONE_SOURCES:
        st = states[src.id]
        if not st["enabled"]:
            skipped.append(Result(src.id, "skipped", "turned off in Settings"))
        elif src.key_setting and not st["key_set"]:
            skipped.append(Result(src.id, "skipped", "no API key"))
        elif src.id in ("batchdata", "realestateapi") and not info.owner:
            skipped.append(Result(src.id, "skipped", "no private owner of record (government sale) — nothing to trace"))
        else:
            key = _key(db, src)
            if src.id == "listing":
                jobs[src.id] = lambda: sale_page_contacts(info)
            elif src.id == "grok":
                jobs[src.id] = lambda key=key, model=ps.scan_model(db): grok_phones(key, model, info)
            elif src.id == "batchdata":
                jobs[src.id] = lambda key=key: batchdata_phones(key, info)
            else:
                jobs[src.id] = lambda key=key: realestateapi_phones(key, info)
    results: dict[str, Result] = {}
    if jobs:
        def run(item: tuple[str, Callable[[], Result]]) -> Result:
            sid, fn = item
            try:
                return fn()
            except Exception as exc:  # one source never fails the others
                return Result(sid, "error", f"{type(exc).__name__}: {str(exc)[:200]}")
        with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            for r in ex.map(run, jobs.items()):
                results[r.id] = r
    if "listing" in results:
        extra = listing_agent_phones(lead)
        results["listing"].found += extra
        if extra and results["listing"].status != "ok":
            results["listing"].status = "ok"
    for r in skipped:
        results[r.id] = r

    before = {p.id: _snap(p) for p in lead.phones}
    added: list[Phone] = []
    for sid in ("batchdata", "realestateapi", "listing", "grok"):  # strongest evidence first
        for f in (results.get(sid) or Result(sid)).found:
            phone, created = upsert_phone(db, lead, f.number, role=f.role, source=f.source, source_url=f.source_url,
                                          confidence=f.confidence, line_type=f.line_type, contact_name=f.contact_name,
                                          note=f.note, dnc=f.dnc)
            if phone is not None and created:
                added.append(phone)
    db.flush()
    updated = sum(1 for p in lead.phones if p.id in before and before[p.id] != _snap(p))
    cost = round(sum(r.cost_usd or 0 for r in results.values()), 4)
    sources = [{"id": s.id, "status": results[s.id].status, "detail": results[s.id].detail,
                "cost_usd": results[s.id].cost_usd} for s in PHONE_SOURCES]

    lead.contact_checked_at = utcnow()
    roles = [p.role for p in added]
    summary = (f"Phones {lead.contact_checked_at:%Y-%m-%d}: " +
               (", ".join(f"+{roles.count(r)} {r.replace('_', ' ')}" for r in ("owner", "listing_agent", "agency") if r in roles)
                or "no new numbers") + (f", {updated} updated" if updated else "") + ". " +
               "; ".join(f"{SOURCES_BY_ID[s['id']].name}: {s['status']}" + (f" ({s['detail'][:200]})" if s["detail"] and (s["status"] in ("error", "skipped") or s["id"] in ("listing", "grok")) else "")
                         for s in sources) + (f". Cost ${cost:.2f}" if cost else ""))
    lead.research_note = ((lead.research_note or "") + "\n" + summary).strip()[-2000:]
    db.flush()
    return {"added": [_phone_dict(p) for p in added], "updated": updated, "sources": sources, "cost_usd": cost}


# ── Settings → Test ─────────────────────────────────────────────────────────

_PROBE = Info(target=ps._PROBE, owner="Test Probe", entity=False, first="Test", last="Probe", mailing=None,
              notes="", sale_urls=[])


def test_phone_source(db: Session, source_id: str) -> dict:
    """Prove a source works with its cheapest request. Returns {ok, message}."""
    src = SOURCES_BY_ID.get(source_id)
    if src is None:
        return {"ok": False, "message": f"Unknown phone source: {source_id!r}"}
    if src.id == "listing":
        return {"ok": True, "message": "No key needed — reads the sale page linked in each property's notes."}
    if src.id == "grok":
        return ps.test_source(db, "grok")
    key = _key(db, src)
    if not key:
        return {"ok": False, "message": "No API key set."}
    # A non-existent address: proves the key reaches the skip-trace endpoint without a billable match.
    r = (batchdata_phones if src.id == "batchdata" else realestateapi_phones)(key, _PROBE)
    if r.status == "error":
        return {"ok": False, "message": r.detail}
    return {"ok": True, "message": f"{src.name} accepted the key (test trace of a non-existent address: {r.detail})."}
