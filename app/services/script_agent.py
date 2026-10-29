"""Scripting agents: draft a new call flow, or improve an existing one.

A scripting agent (``Agent.kind == "scripting"``) writes a call flow in the
``CallFlow`` shape from a short brief.  When improving an existing flow it can
learn from the calls made on it — outcome counts, Hot/Warm/Cold distribution,
which questions most often go unanswered, and the provider's AI summaries.

Nothing is saved here: the caller shows the draft in the call-flow editor and
the user decides whether to create it.  Compliance rules (no price talk,
honest about being AI, honor stop requests, no legal/tax advice) are always
part of the brief and re-checked on the output.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Agent, AiCall, Call, CallFlow, ListType
from app.services.agents import AgentError, complete_json

# Placeholders call_guide.fill() fills from the lead and settings, plus
# [agreed time], which the caller fills on the call.
_KNOWN_PLACEHOLDERS = ("Lead Name", "Owner Name", "Company Name", "Company", "Property Address", "Address",
                       "City", "State", "Market", "agreed time")

MIN_QUESTIONS, MAX_QUESTIONS = 6, 12
MAX_RULES = 15
HISTORY_CALLS = 60        # most recent calls considered
HISTORY_SUMMARIES = 12    # AI summaries passed to the model
SUMMARY_CHARS = 300

LIST_TYPE_TONE: dict[str, str] = {
    ListType.PRE_FORECLOSURE.value: (
        "Pre-foreclosure: the owner may be facing an auction date. Be urgent but empathetic — acknowledge "
        "the stress, never shame, focus on timeline and options; ask about the sale date gently."),
    ListType.TAX_DELINQUENT.value: (
        "Tax delinquent: the owner may be embarrassed. Be matter-of-fact and non-judgmental; ask about back "
        "taxes only once motivation is clear."),
    ListType.PROBATE.value: (
        "Probate / inherited: the person likely lost a family member. Be gentle and patient, offer condolences "
        "once, never rush. The person may be an heir but not the executor/decision-maker — find out who can "
        "sign and whether probate is opened or complete."),
    ListType.DIVORCE.value: (
        "Divorce: emotionally sensitive and there may be two owners. Stay neutral, never take sides, confirm "
        "whether both parties must agree to a sale."),
    ListType.ABSENTEE_OWNER.value: (
        "Absentee owner: owner lives elsewhere. Be practical — occupancy, management burden, condition from a "
        "distance, and whether they'd consider selling as-is."),
    ListType.TIRED_LANDLORD.value: (
        "Tired landlord: bad tenants, repairs, vacancies. Be relatable and practical — lease status, tenant "
        "situation, deferred maintenance, how much longer they want to manage it."),
    ListType.VACANT_CODE_VIOLATION.value: (
        "Vacant / code violation: the property may be a liability (fines, vandalism). Be helpful and direct — "
        "condition, open violations or fines, utilities, and access for a walkthrough."),
    ListType.GOVERNMENT_SALE.value: (
        "Government sale / REO (HUD, Treasury, USDA, federal auctions): the seller is an agency, so calls usually go "
        "to the listing broker or the occupant, not the agency. Be businesslike — sale date, bidding rules, "
        "occupancy, condition, and whether the property is still available."),
}

LIST_TYPE_LABELS = {
    "pre_foreclosure": "Pre-foreclosure", "tax_delinquent": "Tax delinquent", "probate": "Probate",
    "divorce": "Divorce", "absentee_owner": "Absentee owner", "tired_landlord": "Tired landlord",
    "vacant_code_violation": "Vacant / code violation", "government_sale": "Government sale / REO",
}

COMPLIANCE_RULES = (
    "Never quote a price, estimate value, or discuss offers — defer numbers to the acquisitions manager.",
    "If asked whether you're an AI, say yes honestly.",
    "If the person asks to stop or be removed: confirm removal, end the call politely, mark Do Not Call.",
    "Never give legal, tax, or financial advice.",
)

# Each compliance rule must be recognisably present in the draft's rules; if the
# model dropped one we add it back (rules are the operator's guardrails).
_COMPLIANCE_CHECKS = (
    (re.compile(r"\b(price|value|offer|numbers?)\b", re.I), COMPLIANCE_RULES[0]),
    (re.compile(r"\b(AI|artificial|robot|automated)\b"), COMPLIANCE_RULES[1]),
    (re.compile(r"\b(remove|removed|removal|stop|do not call|DNC)\b", re.I), COMPLIANCE_RULES[2]),
    (re.compile(r"\b(legal|tax)\b.*\badvice\b|\badvice\b.*\b(legal|tax)\b", re.I), COMPLIANCE_RULES[3]),
)

SYSTEM = f"""You write phone call scripts ("call flows") for a real-estate investment company that buys houses \
directly from motivated sellers. A call flow is read/spoken by a caller (a human or an AI voice agent) whose \
only job is to qualify the seller and book a callback with the acquisitions manager.

Hard requirements:
- The whole call must fit in about 4 minutes: a short role, a one-breath opening that asks permission to continue, \
{MIN_QUESTIONS}–{MAX_QUESTIONS} short questions in the order they should be asked, and a one-sentence close that confirms the callback.
- Use these exact placeholders where the value belongs (they are filled automatically at call time): \
[Lead Name], [Company Name], [Property Address], and [agreed time] in the close. [City], [State], [Market] are also \
available. Do not invent other bracketed placeholders and never hard-code a seller name, company name or address.
- Role is written to the caller in the second person ("You are Sam, an assistant calling on behalf of \
[Company Name] ... Keep the call under 4 minutes."). The opening is what the caller says first: it must confirm \
who they're speaking with by name ("Hi, is this [Lead Name]? This is Sam with [Company Name] ...") and mention \
[Property Address]. The close must use [agreed time].
- Every item in questions is an actual question, in plain spoken words, one per item, no numbering, no line \
breaks. Put condolences/acknowledgements in the opening or the rules, not in questions. A short parenthetical \
hint for the caller is fine, e.g. "(ask gently, only if ...)".
- Always cover: ownership / who can sign, occupancy, condition, motivation (why sell), timeline, and the best \
callback time. Asking whether they have a price in mind is fine; the caller must never state one.
- Rules MUST include, in your own words: never quote/estimate a price or discuss offers (defer to the acquisitions \
manager); if asked whether you are an AI, say yes honestly; if they ask to stop or be removed, confirm removal, end \
politely and mark Do Not Call; never give legal, tax or financial advice. Add any flow-specific rules (tone, \
skipping, sensitive topics).
- Scoring: one-line Hot / Warm / Cold criteria using the answers the questions collect.

Return ONLY a JSON object with exactly these keys:
{{"name": str (short, ≤ 80 chars), "description": str (one sentence), "role": str, "opening": str, \
"questions": [str], "rules": [str], "close": str, "scoring": {{"hot": str, "warm": str, "cold": str}}, \
"notes": str (2–6 sentences for the user: what you wrote and why; when improving, what changed and what in the \
call history drove each change)}}"""


# ── Call history ────────────────────────────────────────────────────────────

def _loads(raw: Optional[str], default):
    if not raw:
        return default
    try:
        v = json.loads(raw)
    except (ValueError, TypeError):
        return default
    return v if isinstance(v, type(default)) else default


def _clip(text: Optional[str], n: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def call_history(db: Session, flow: CallFlow) -> Optional[dict]:
    """Compact, size-capped summary of recent calls on ``flow`` (None if there are none)."""
    calls = list(db.execute(
        select(Call).where(Call.call_flow_id == flow.id).order_by(Call.created_at.desc()).limit(HISTORY_CALLS)
    ).scalars())
    ai_calls = list(db.execute(
        select(AiCall).where(AiCall.call_flow_id == flow.id).order_by(AiCall.created_at.desc()).limit(HISTORY_CALLS)
    ).scalars())
    if not calls and not ai_calls:
        return None

    questions = _loads(flow.questions, [])
    outcomes: Counter = Counter()
    scores: Counter = Counter()
    asked = 0                       # calls where the guide captured at least one answer
    unanswered: Counter = Counter()
    summaries: list[str] = []

    for c in calls:
        outcome = c.outcome.value if hasattr(c.outcome, "value") else str(c.outcome or "")
        outcomes[outcome or "unknown"] += 1
        if c.lead_score:
            scores[c.lead_score.lower()] += 1
        answers = _loads(c.answers, {})
        if answers:
            asked += 1
            got = {str(k) for k, v in answers.items() if str(v or "").strip()}
            for i in range(len(questions)):
                if str(i) not in got:
                    unanswered[i] += 1
        if c.ai_summary and len(summaries) < HISTORY_SUMMARIES:
            summaries.append(f"[{outcome}{', ' + c.lead_score if c.lead_score else ''}] {_clip(c.ai_summary, SUMMARY_CHARS)}")
        elif c.notes and len(summaries) < HISTORY_SUMMARIES:
            summaries.append(f"[{outcome}, operator note] {_clip(c.notes, SUMMARY_CHARS)}")

    for a in ai_calls:
        outcomes[f"ai_call:{a.outcome or a.status}"] += 1
        if a.score:
            scores[a.score.lower()] += 1
        if a.summary and len(summaries) < HISTORY_SUMMARIES:
            summaries.append(f"[AI voice call, {a.outcome or a.status}{', ' + a.score if a.score else ''}] "
                             f"{_clip(a.summary, SUMMARY_CHARS)}")

    most_skipped = [
        {"index": i + 1, "question": _clip(questions[i], 160), "unanswered_in": n, "of_calls_with_answers": asked}
        for i, n in unanswered.most_common() if n and i < len(questions)
    ][:5]
    return {
        "calls_considered": len(calls) + len(ai_calls),
        "outcomes": dict(outcomes.most_common()),
        "lead_scores": dict(scores.most_common()),
        "calls_with_captured_answers": asked,
        "most_often_unanswered": most_skipped,
        "recent_summaries": summaries,
    }


# ── Draft ───────────────────────────────────────────────────────────────────

def _flow_dict(flow: CallFlow) -> dict:
    return {
        "name": flow.name, "description": flow.description or "", "role": flow.role or "",
        "opening": flow.opening or "", "questions": _loads(flow.questions, []), "rules": _loads(flow.rules, []),
        "close": flow.close or "", "scoring": _loads(flow.scoring, {}),
    }


def _text(v: Any, field: str, limit: int, *, required: bool = True) -> str:
    if v is None:
        v = ""
    if not isinstance(v, (str, int, float)):
        raise AgentError(f"The agent's draft has an invalid '{field}' (expected text).")
    s = re.sub(r"\s+", " ", str(v)).strip()
    if required and not s:
        raise AgentError(f"The agent's draft is missing '{field}'.")
    return s[:limit]


def _lines(v: Any, field: str) -> list[str]:
    if isinstance(v, str):
        v = [x for x in v.split("\n")]
    if not isinstance(v, list):
        raise AgentError(f"The agent's draft has an invalid '{field}' (expected a list).")
    out: list[str] = []
    for item in v:
        if isinstance(item, dict):  # {"question": "..."} / {"text": "..."}
            item = next((item[k] for k in ("question", "text", "rule") if isinstance(item.get(k), str)), "")
        if not isinstance(item, (str, int, float)):
            continue
        s = re.sub(r"\s+", " ", str(item)).strip()
        s = re.sub(r"^(\d+[.)]|[-•*])\s+", "", s)  # stray numbering / bullets
        if s and s not in out:
            out.append(s[:500])
    return out


def normalize(raw: Any) -> dict:
    """Validate the model's JSON into the CallFlow shape, or raise AgentError."""
    if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], dict):
        raw = raw[0]
    if not isinstance(raw, dict):
        raise AgentError("The agent didn't return a call flow (expected a JSON object).")
    if isinstance(raw.get("call_flow"), dict):
        raw = {**raw["call_flow"], "notes": raw.get("notes", raw["call_flow"].get("notes"))}

    questions = _lines(raw.get("questions"), "questions")
    if len(questions) < MIN_QUESTIONS:
        raise AgentError(f"The agent's draft has only {len(questions)} question(s); a flow needs "
                         f"{MIN_QUESTIONS}–{MAX_QUESTIONS}. Try again or add detail to the brief.")
    rules = _lines(raw.get("rules"), "rules")[:MAX_RULES]
    joined = " ".join(rules)
    missing = [rule for rx, rule in _COMPLIANCE_CHECKS if not rx.search(joined)]
    rules = missing + rules

    scoring_raw = raw.get("scoring")
    if not isinstance(scoring_raw, dict):
        raise AgentError("The agent's draft has no Hot/Warm/Cold scoring.")
    lower = {str(k).strip().lower(): v for k, v in scoring_raw.items()}
    scoring = {k: _text(lower.get(k), f"scoring.{k}", 500) for k in ("hot", "warm", "cold")}

    result = {
        "name": _text(raw.get("name"), "name", 160),
        "description": _text(raw.get("description"), "description", 512, required=False),
        "role": _text(raw.get("role"), "role", 2000),
        "opening": _text(raw.get("opening"), "opening", 1000),
        "questions": questions[:MAX_QUESTIONS],
        "rules": rules,
        "close": _text(raw.get("close"), "close", 1000),
        "scoring": scoring,
        "notes": _text(raw.get("notes"), "notes", 2000, required=False),
    }
    known = {p.lower() for p in _KNOWN_PLACEHOLDERS}
    text = " ".join([result["role"], result["opening"], result["close"], *result["questions"], *result["rules"]])
    unknown = sorted({m for m in re.findall(r"\[([^\[\]]{1,40})\]", text) if m.strip().lower() not in known})
    if missing:
        result["notes"] = (result["notes"] + " " if result["notes"] else "") + \
            f"Added {len(missing)} required compliance rule(s) the draft left out."
    if unknown:
        result["notes"] = (result["notes"] + " " if result["notes"] else "") + \
            "Check these bracketed placeholders — they aren't filled automatically: " + \
            ", ".join(f"[{u}]" for u in unknown) + "."
    absent = [ph for ph, where in (("[Lead Name]", result["opening"]), ("[agreed time]", result["close"]))
              if ph.lower() not in where.lower()]
    if absent:
        result["notes"] = (result["notes"] + " " if result["notes"] else "") + \
            "Heads-up: " + " and ".join(absent) + " missing from the " + \
            " / ".join("opening" if a == "[Lead Name]" else "close" for a in absent) + " — consider adding."
    return result


def draft(db: Session, agent: Agent, brief: str, base_flow: Optional[CallFlow] = None,
          list_type: Optional[str] = None, use_call_history: bool = False) -> dict:
    """Ask the scripting ``agent`` for a call flow; returns the normalized draft (not saved)."""
    if list_type and list_type not in LIST_TYPE_TONE:
        raise AgentError(f"Unknown list type '{list_type}'.")

    parts: list[str] = []
    if agent.instructions and agent.instructions.strip():
        parts.append("Standing instructions from the user for this agent (follow them unless they conflict with "
                     "the hard requirements):\n" + agent.instructions.strip())
    parts.append("Brief:\n" + brief.strip())
    if list_type:
        parts.append(f"Lead list: {LIST_TYPE_LABELS[list_type]}. Tone: {LIST_TYPE_TONE[list_type]}")

    history = None
    if base_flow is not None:
        parts.append("Improve this existing call flow. Keep what works, its persona and voice unless the brief "
                     "says otherwise; change what the brief or evidence calls for. Name the result after the "
                     "original.\nExisting flow:\n" + json.dumps(_flow_dict(base_flow), ensure_ascii=False, indent=1))
        if use_call_history:
            history = call_history(db, base_flow)
            if history:
                parts.append(
                    "Call history on this flow (most recent first; question indexes are 1-based into the existing "
                    "flow's questions). Use it as evidence: reorder, reword, merge or drop questions that are often "
                    "unanswered, and address recurring objections. In `notes`, cite the specific numbers that "
                    "drove each change.\n" + json.dumps(history, ensure_ascii=False, indent=1))
            else:
                parts.append("No calls have been logged on this flow yet; say so in `notes`.")
    else:
        parts.append("Write a new call flow from scratch.")

    raw = complete_json(db, agent, SYSTEM, "\n\n".join(parts), max_tokens=8192, timeout=120.0)
    result = normalize(raw)
    result["history_used"] = bool(history)
    return result
