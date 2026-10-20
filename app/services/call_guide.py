"""Property-aware call guide: turns a call flow into a conversation for one lead.

The operator follows the call flow's script, but every step is adapted to what
we already know about the property and the owner:

- placeholders (``[Lead Name]``, ``[Property Address]``, ``[Company Name]``, …)
  are filled from the lead and settings;
- each question is tagged with a topic and annotated with facts on record
  (county data, list memberships, valuation, prior call answers) plus a tip on
  how to ask it — confirm what we know instead of asking cold;
- answers captured on earlier calls pre-fill the matching question so the
  seller is never asked the same thing twice;
- :func:`score_answers` applies the flow's Hot/Warm/Cold rubric to the answers,
  using the valuation to judge whether an asking price is realistic.

Everything here is deterministic — no LLM — so the guidance is explainable.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Call, CallFlow, CallOutcome, Lead, ListType

# ── Topic classification ────────────────────────────────────────────────────

# Ordered: first match wins.  Patterns run against the lowercased question.
_TOPICS: list[tuple[str, str]] = [
    ("distress", r"behind on|payments or taxes|delinquen"),
    ("owner", r"owner on title|on title|relationship to the owner|\bowner\b"),
    ("occupancy", r"occupied|tenant|vacant"),
    ("condition", r"condition|repair|roof|foundation|hvac|plumbing"),
    ("motivation", r"why .*sell|reason .*sell"),
    ("timeline", r"how soon|close\b|timeline|timeframe"),
    ("price", r"price in mind|asking|how much .*want"),
    ("mortgage", r"mortgage|owed|loan"),
    ("listing", r"listed|agent|realtor|other investors"),
    ("callback", r"call you back|best time|best number|callback"),
]

_LIST_LABELS = {
    ListType.PRE_FORECLOSURE: "Pre-foreclosure",
    ListType.TAX_DELINQUENT: "Tax delinquent",
    ListType.PROBATE: "Probate",
    ListType.DIVORCE: "Divorce",
    ListType.ABSENTEE_OWNER: "Absentee owner",
    ListType.TIRED_LANDLORD: "Tired landlord",
    ListType.VACANT_CODE_VIOLATION: "Vacant / code violation",
}

_PLACEHOLDER = re.compile(r"\[([^\[\]]+)\]")


def classify(question: str) -> str:
    q = question.lower()
    for topic, pattern in _TOPICS:
        if re.search(pattern, q):
            return topic
    return "other"


def _money(v: Optional[float]) -> str:
    return f"${v:,.0f}" if v else "—"


# ── Lead facts ──────────────────────────────────────────────────────────────

def _list_types(lead: Lead) -> set[ListType]:
    return {m.source_list.list_type for m in lead.list_memberships if m.source_list}


def _latest_valuation(lead: Lead):
    return max(lead.valuations, key=lambda v: v.created_at or datetime.min) if lead.valuations else None


def _prior_calls(db: Session, lead: Lead) -> list[Call]:
    return list(db.execute(
        select(Call).where(Call.lead_id == lead.id).order_by(Call.created_at.desc())
    ).scalars())


def _placeholder_values(db: Session, lead: Lead) -> dict[str, str]:
    from app.services.settings import get_setting

    company = get_setting(db, "company_name") or "Direct Real Estate Capital LLC"
    city_state = ", ".join(x for x in (lead.property_city, lead.property_state) if x)
    address = lead.property_address + (f", {lead.property_city}" if lead.property_city else "")
    owner = (lead.owner_name or "").strip()
    if owner.isupper():
        owner = owner.title()
    values = {
        "company name": company,
        "company": company,
        "property address": address,
        "address": address,
        "city": lead.property_city or "",
        "state": lead.property_state or "",
        "market": city_state,
    }
    # An entity name isn't a person to greet — leave [Lead Name] open so the
    # operator asks who they're speaking with.
    if owner and not lead.owner_is_llc:
        values["lead name"] = owner
        values["owner name"] = owner
    return values


def fill(text: str, values: dict[str, str]) -> tuple[str, list[str]]:
    """Replace known ``[Placeholder]``s; return the text and the ones left open."""
    unfilled: list[str] = []

    def sub(m: re.Match) -> str:
        key = m.group(1).strip().lower()
        if values.get(key):
            return values[key]
        unfilled.append(m.group(1))
        return m.group(0)

    return _PLACEHOLDER.sub(sub, text or ""), unfilled


def _annotate(topic: str, lead: Lead, lists: set[ListType], valuation) -> tuple[list[str], Optional[str], str]:
    """Facts on record, an asking tip, and a mode (ask | confirm | sensitive | optional)."""
    known: list[str] = []
    tip: Optional[str] = None
    mode = "ask"

    if topic == "owner":
        if lead.owner_name:
            known.append(f"County records list the owner as {lead.owner_name}.")
            mode = "confirm"
            tip = "Confirm rather than ask cold: \"Records show the property under ___ — is that you?\""
        if lead.owner_is_llc:
            known.append("Owned by an LLC/entity.")
            tip = "Ask if they're a member/manager with authority to sell for the entity."
    elif topic == "occupancy":
        if lead.absentee and lead.mailing_address:
            known.append(f"Owner's mailing address is elsewhere ({lead.mailing_address}) — likely tenant-occupied or vacant.")
            mode = "confirm"
        if ListType.VACANT_CODE_VIOLATION in lists:
            known.append("On the vacant / code-violation list — likely vacant.")
            mode = "confirm"
        if ListType.TIRED_LANDLORD in lists:
            known.append("On the tired-landlord list — likely a tenant; ask about lease and rent status.")
            mode = "confirm"
    elif topic == "condition":
        specs = " / ".join(x for x in (
            f"{lead.beds} bd" if lead.beds else "",
            f"{lead.baths:g} ba" if lead.baths else "",
            f"{lead.sqft:,} sqft" if lead.sqft else "",
        ) if x)
        if specs:
            known.append(f"Records: {specs}.")
        if lead.year_built:
            age = datetime.now().year - lead.year_built
            known.append(f"Built {lead.year_built} (~{age} yrs).")
            if age >= 25:
                tip = "Older home — ask specifically about roof and HVAC age."
        if lead.rehab_level:
            known.append(f"Estimated rehab level: {lead.rehab_level}.")
        if ListType.VACANT_CODE_VIOLATION in lists:
            known.append("Open code violations on record — ask what was cited.")
        if valuation and valuation.repair_estimate:
            known.append(f"Internal repair estimate {_money(valuation.repair_estimate)} — do not share.")
    elif topic == "motivation":
        for lt in sorted(lists, key=lambda x: x.value):
            known.append(f"On the {_LIST_LABELS[lt]} list.")
        if ListType.PROBATE in lists:
            mode, tip = "sensitive", "Probate — likely inherited. Be gentle; offer condolences before asking."
        elif ListType.DIVORCE in lists:
            mode, tip = "sensitive", "Divorce filing — stay neutral; both parties may need to sign."
        elif ListType.PRE_FORECLOSURE in lists:
            tip = "Pre-foreclosure — their timeline may be urgent; listen for the auction date."
    elif topic == "timeline":
        if ListType.PRE_FORECLOSURE in lists:
            known.append("Pre-foreclosure — ask whether an auction/sale date is set.")
    elif topic == "price":
        if lead.assessed_value:
            known.append(f"Assessed value {_money(lead.assessed_value)}.")
        if valuation:
            known.append(f"Internal only: ARV {_money(valuation.arv)}, MAO {_money(valuation.mao)}.")
        tip = "Never quote a number — \"Eylon will cover numbers on the call with you.\""
    elif topic == "mortgage":
        if ListType.PRE_FORECLOSURE in lists:
            known.append("Pre-foreclosure filing — a mortgage is likely behind.")
    elif topic == "distress":
        distress = []
        if lead.taxes_owed:
            distress.append(f"{_money(lead.taxes_owed)} in property taxes owed on record.")
        if ListType.TAX_DELINQUENT in lists and not lead.taxes_owed:
            distress.append("On the tax-delinquent list.")
        if ListType.PRE_FORECLOSURE in lists:
            distress.append("Pre-foreclosure filing on record.")
        known.extend(distress)
        if distress:
            mode, tip = "sensitive", "Distress on record — ask gently, without judgment."
        else:
            mode, tip = "optional", "No distress signals on record — only ask if their motivation suggests it."
    elif topic == "callback":
        callable_numbers = [p.number for p in lead.phones if not p.dnc_flagged and not p.opted_out]
        if callable_numbers:
            known.append(f"Number on file: {callable_numbers[0]}.")
        tip = "Lock a specific callback window before hanging up."
    return known, tip, mode


def build_call_guide(db: Session, lead: Lead, flow: Optional[CallFlow] = None) -> dict:
    """Assemble the lead-specific script for ``flow`` (default flow if omitted)."""
    if flow is None:
        flow = db.execute(
            select(CallFlow).where(CallFlow.is_default == True, CallFlow.active == True).limit(1)  # noqa: E712
        ).scalars().first() or db.execute(
            select(CallFlow).where(CallFlow.active == True).order_by(CallFlow.created_at.desc()).limit(1)  # noqa: E712
        ).scalars().first()
    if flow is None:
        return {"flow": None}

    values = _placeholder_values(db, lead)
    lists = _list_types(lead)
    valuation = _latest_valuation(lead)
    calls = _prior_calls(db, lead)

    # Latest non-empty answer per question index, from earlier calls on this flow.
    prior_answers: dict[str, dict] = {}
    for c in calls:  # newest first
        if c.call_flow_id != flow.id or not c.answers:
            continue
        for idx, ans in json.loads(c.answers).items():
            prior_answers.setdefault(idx, {"answer": ans, "at": c.created_at.isoformat() if c.created_at else None})

    unfilled: set[str] = set()
    role, u = fill(flow.role, values); unfilled.update(u)
    opening, u = fill(flow.opening, values); unfilled.update(u)
    close, u = fill(flow.close, values); unfilled.update(u)

    questions = []
    for i, raw in enumerate(json.loads(flow.questions or "[]")):
        text, u = fill(raw, values); unfilled.update(u)
        topic = classify(raw)
        known, tip, mode = _annotate(topic, lead, lists, valuation)
        prior = prior_answers.get(str(i))
        if prior:
            mode = "confirm"
        questions.append({
            "index": i,
            "text": text,
            "topic": topic,
            "mode": mode,
            "known": known,
            "tip": tip,
            "prior_answer": prior,
        })

    rules = [fill(r, values)[0] for r in json.loads(flow.rules or "[]")]

    context: list[str] = []
    talked = [c for c in calls if c.outcome not in (CallOutcome.NO_ANSWER, CallOutcome.VOICEMAIL)]
    if talked:
        last = talked[0]
        when = last.created_at.strftime("%b %d") if last.created_at else "earlier"
        context.append(f"You spoke on {when} — last outcome: {last.outcome.value.replace('_', ' ')}. Reference that conversation in the opening.")
        if last.ai_summary:
            context.append("Last call summary: " + last.ai_summary.replace("\n", " ").strip())
    if lists:
        context.append("Lists: " + ", ".join(_LIST_LABELS[lt] for lt in sorted(lists, key=lambda x: x.value)) + f" (stack depth {lead.stack_depth}).")
    if lead.owner_is_llc:
        context.append("Owner is an LLC — confirm you're speaking with someone authorized to sell.")

    return {
        "flow": {"id": flow.id, "name": flow.name},
        "lead_id": lead.id,
        "role": role,
        "opening": opening,
        "questions": questions,
        "rules": rules,
        "close": close,
        "scoring": json.loads(flow.scoring or "{}"),
        "context": context,
        "unfilled_placeholders": sorted(unfilled),
    }


# ── Hot / Warm / Cold scoring ───────────────────────────────────────────────

_YES = re.compile(r"\b(yes|yeah|yep|yup|correct|i am|i'm the owner|i own|we own|we are|that's me|it's me|sure)\b")
_NO_START = re.compile(r"^\s*(no|nope|not)\b")
_FAST = re.compile(r"\b(asap|immediately|right away|this week|next week|two weeks|2 weeks|this month|30 days|a month|one month|1 month|soon)\b")
_MID = re.compile(r"\b(60|90|two months|three months|2 months|3 months|couple (of )?months|few months|60 to 90)\b")
_EXPLORING = re.compile(r"\b(just exploring|exploring|just looking|curious|no rush|not sure|someday|eventually|maybe next year|not in a hurry)\b")
_FLEXIBLE = re.compile(r"\b(flexible|negotiable|open|make (me )?an offer|whatever|no idea|not sure|fair)\b")
_MOTIVE = re.compile(r"\b(moving|relocat|job|divorc|inherit|passed|died|estate|repairs?|tired|tenant|landlord|financial|behind|foreclos|retir|health|sick|downsiz|bills|debt|vacant|can't afford|cannot afford)\w*")
_DISTRESS = re.compile(r"\b(behind|late|missed|foreclos|delinquen|owe|default|auction|can't pay|cannot pay)\w*")
_LISTED = re.compile(r"\b(listed|realtor|agent|on the market|mls|zillow)\b")
_AMOUNT = re.compile(r"\$?\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k|thousand|m|mil|million)?\b", re.I)


def _parse_amount(text: str) -> Optional[float]:
    best = None
    for num, unit in _AMOUNT.findall(text):
        value = float(num.replace(",", ""))
        unit = (unit or "").lower()
        if unit in ("k", "thousand"):
            value *= 1_000
        elif unit in ("m", "mil", "million"):
            value *= 1_000_000
        if value >= 10_000:  # ignore small numbers like "30 days"
            best = value
    return best


def _days(text: str) -> Optional[int]:
    m = re.search(r"(\d+)\s*(day|week|month)", text)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return n * {"day": 1, "week": 7, "month": 30}[unit]


def score_answers(db: Session, lead: Lead, flow: CallFlow, answers: dict[str, str]) -> dict:
    """Apply the Hot/Warm/Cold rubric to the answers captured so far.

    Returns ``{"score": "hot"|"warm"|"cold"|None, "reasons": [...]}``; ``None``
    means not enough has been answered to call it yet.
    """
    questions = json.loads(flow.questions or "[]")
    by_topic: dict[str, str] = {}
    for idx, ans in (answers or {}).items():
        try:
            topic = classify(questions[int(idx)])
        except (ValueError, IndexError):
            continue
        if ans and ans.strip():
            by_topic[topic] = ans.strip().lower()

    reasons: list[str] = []
    lists = _list_types(lead)

    owner_ans = by_topic.get("owner", "")
    is_owner = bool(owner_ans) and bool(_YES.search(owner_ans)) and not _NO_START.match(owner_ans)

    tl = by_topic.get("timeline", "")
    days = _days(tl)
    fast = bool(_FAST.search(tl)) or (days is not None and days <= 30)
    mid = bool(_MID.search(tl)) or (days is not None and 30 < days <= 120)
    exploring = bool(_EXPLORING.search(tl))

    listing = by_topic.get("listing", "")
    listed = bool(listing) and bool(_LISTED.search(listing)) and not _NO_START.match(listing)

    price_ans = by_topic.get("price", "")
    asking = _parse_amount(price_ans) if price_ans else None
    flexible = bool(price_ans) and bool(_FLEXIBLE.search(price_ans))
    has_price = asking is not None or (bool(price_ans) and bool(_YES.search(price_ans)))
    valuation = _latest_valuation(lead)
    ceiling = valuation.arv if valuation else (lead.assessed_value * 1.3 if lead.assessed_value else None)
    unrealistic = asking is not None and ceiling is not None and asking > ceiling

    motive = by_topic.get("motivation", "")
    motivated = bool(motive) and (bool(_MOTIVE.search(motive)) or len(motive.split()) >= 4)
    distress_text = " ".join(by_topic.get(t, "") for t in ("distress", "mortgage", "motivation"))
    distressed = (
        bool(_DISTRESS.search(distress_text)) and not _NO_START.match(by_topic.get("distress", ""))
    ) or bool(lists & {ListType.PRE_FORECLOSURE, ListType.TAX_DELINQUENT})

    # Cold disqualifiers win.
    if exploring:
        reasons.append("Just exploring — no real timeline.")
    if listed:
        reasons.append("Already listed with an agent / talking to others.")
    if unrealistic:
        reasons.append(f"Asking {_money(asking)} is above {'ARV' if valuation else 'assessed value +30%'} ({_money(ceiling)}).")
    if exploring or listed or unrealistic:
        return {"score": "cold", "reasons": reasons}

    if is_owner:
        reasons.append("Owner on title.")
    if motivated:
        reasons.append("Clear motivation to sell.")
    if fast:
        reasons.append("Wants to close within ~30 days.")
    elif mid:
        reasons.append("60–90 day timeline.")
    if flexible:
        reasons.append("Price is flexible.")
    elif has_price:
        reasons.append(f"Has a price in mind{f' ({_money(asking)})' if asking else ''}.")
    if distressed:
        reasons.append("Distress signals (payments/taxes/foreclosure).")

    if is_owner and fast and (motivated or distressed) and (flexible or distressed):
        return {"score": "hot", "reasons": reasons}
    if is_owner and (mid or fast or has_price):
        return {"score": "warm", "reasons": reasons}
    if not is_owner and owner_ans:
        reasons.insert(0, "Not confirmed as the owner on title.")
        return {"score": "cold", "reasons": reasons}
    return {"score": None, "reasons": reasons or ["Not enough answers yet to score."]}
