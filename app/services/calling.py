"""Click-to-call via Quo (formerly OpenPhone) or Twilio.

Compliance note: this is a **manual-dial** system.  The operator clicks once
per call; there is no autodialer, no preview/power dial, and no prerecorded
messages.  The operator is on the line and talks live.  This satisfies the
manual's hard rule: "Manual-dial. No autodialer, no prerecorded messages."

Providers (see ``get_calling_provider``):
- ``quo``: the UI hands the number to the Quo app (``tel:`` link) and the
  operator presses Call there; results sync back via ``services.quo``.
- ``twilio``: Twilio rings the operator, then bridges to the seller.
- ``dry-run``: nothing configured — the call is logged so the pipeline still
  works end-to-end in dev.
"""
from __future__ import annotations

import json
from typing import Optional

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Call, CallDirection, CallOutcome, Lead, Phone
from app.services.skiptrace import is_callable


def initiate_call(
    db: Session,
    lead: Lead,
    phone: Phone,
    operator_number: Optional[str] = None,
    caller_id: Optional[str] = None,
) -> Call:
    """Start a compliant manual call through the active provider.

    Raises ValueError if the phone is DNC-flagged / opted out, or if Twilio is
    active and no operator number was given (Twilio must ring the operator first).
    """
    if not is_callable(phone):
        raise ValueError(
            f"Phone {phone.number} is DNC-flagged or opted out. Mail only."
        )

    from app.services.settings import get_calling_provider, get_twilio_config
    provider = get_calling_provider(db)
    call_sid: Optional[str] = None

    if provider == "twilio":
        if not operator_number:
            raise ValueError("Operator number is required to bridge a Twilio call.")
        settings = get_settings()
        twilio_cfg = get_twilio_config(db)
        twilio_sid = twilio_cfg["account_sid"] or settings.twilio_account_sid
        twilio_token = twilio_cfg["auth_token"] or settings.twilio_auth_token
        twilio_from = twilio_cfg["from_number"] or settings.twilio_from_number
        try:
            from twilio.rest import Client
            client = Client(twilio_sid, twilio_token)
            # Bridge: operator answers first, then Twilio dials the seller.
            twiml = (
                f"<Response>"
                f"<Dial>{phone.number}</Dial>"
                f"</Response>"
            )
            twilio_call = client.calls.create(
                to=operator_number,
                from_=twilio_from,
                twiml=twiml,
                status_callback=f"{settings.app_base_url}/api/calls/twilio-status",
            )
            call_sid = twilio_call.sid
        except Exception as exc:  # pragma: no cover — network/Twilio errors
            # Don't pretend the call connected: surface the failure so the
            # router returns an error and no "successful" call is committed.
            raise RuntimeError(f"Twilio call failed: {exc}") from exc
    # Quo: the operator dials in the Quo app; the Quo call ID arrives on sync.
    # Dry-run: no provider — the call is logged for the pipeline only.
    # Attempt count = prior calls on this lead + 1
    from sqlalchemy import select, func as sqlfunc
    prior = db.execute(
        select(sqlfunc.count(Call.id)).where(Call.lead_id == lead.id)
    ).scalar() or 0

    call = Call(
        lead_id=lead.id,
        phone_id=phone.id,
        caller_id=caller_id,
        direction=CallDirection.OUTBOUND_MANUAL,
        outcome=CallOutcome.NO_ANSWER,  # updated by operator after the call
        attempt_count=prior + 1,
        call_sid=call_sid,
        provider=provider,
    )
    db.add(call)
    db.flush()
    return call


def log_call_outcome(
    db: Session,
    call: Call,
    outcome: CallOutcome,
    notes: str | None = None,
    duration_seconds: int | None = None,
    call_flow_id: str | None = None,
    answers: dict[str, str] | None = None,
    lead_score: str | None = None,
) -> Call:
    """Update a call with its outcome and set follow-up per the manual.

    Most deals close after touch 5+, so every non-yes outcome schedules a
    follow-up.  DNC requests immediately opt out the phone.  ``answers`` are
    the call-guide responses keyed by question index; ``lead_score`` is the
    Hot/Warm/Cold rubric result.
    """
    from datetime import datetime, timedelta

    call.outcome = outcome
    call.notes = notes
    # A provider sync may already hold the real duration — only overwrite it
    # when the operator actually entered one.
    if duration_seconds is not None:
        call.duration_seconds = duration_seconds
    if call_flow_id is not None:
        call.call_flow_id = call_flow_id
    if answers is not None:
        cleaned = {k: v.strip() for k, v in answers.items() if v and v.strip()}
        call.answers = json.dumps(cleaned) if cleaned else None
    if lead_score is not None:
        call.lead_score = lead_score or None
    db.flush()

    # Schedule follow-up for non-final outcomes
    from app.models import FollowUp, LeadStatus
    lead = call.lead

    if outcome == CallOutcome.DNC_REQUEST:
        # Honor opt-out instantly.  Call has a phone_id FK but no relationship —
        # look the phone up directly.
        phone = db.get(Phone, call.phone_id) if call.phone_id else None
        if phone:
            from app.services.skiptrace import mark_opted_out
            mark_opted_out(db, phone)

    if outcome == CallOutcome.VERBAL_YES:
        lead.status = LeadStatus.WARM
    elif outcome == CallOutcome.WARM:
        lead.status = LeadStatus.WARM
    elif outcome == CallOutcome.OFFER_SENT:
        lead.status = LeadStatus.WORKING
    elif outcome == CallOutcome.NOT_INTERESTED:
        lead.status = LeadStatus.WORKING

    # Follow-up cadence: "Most deals close after touch 5."
    # Cadence is configurable in Settings → Call Follow-up tab.
    from app.services.settings import get_call_cadence
    cadence = get_call_cadence(db)
    _outcome_map = {
        CallOutcome.NO_ANSWER: cadence["no_answer"],
        CallOutcome.VOICEMAIL: cadence["voicemail"],
        CallOutcome.NOT_INTERESTED: cadence["not_interested"],
        CallOutcome.CALLBACK_REQUESTED: cadence["callback_requested"],
        CallOutcome.WARM: cadence["warm"],
        CallOutcome.OFFER_SENT: cadence["offer_sent"],
        CallOutcome.DNC_REQUEST: cadence["dnc_request"],
    }
    days = _outcome_map.get(outcome, cadence["default"])
    if days:
        followup = FollowUp(
            lead_id=lead.id,
            due_at=datetime.now() + timedelta(days=days),
            reason=f"Follow up after: {outcome.value}",
        )
        db.add(followup)
        lead.next_followup = followup.due_at
    db.flush()
    return call
