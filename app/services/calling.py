"""Click-to-call via Twilio.

Compliance note: this is a **manual-dial** system.  The operator clicks once
per call; there is no autodialer, no preview/power dial, and no prerecorded
messages.  Twilio connects the operator's phone to the seller's number — the
operator is on the line and talks live.  This satisfies the manual's hard
rule: "Manual-dial. No autodialer, no prerecorded messages."

If Twilio credentials are absent, ``initiate_call`` returns a dry-run payload
so the UI and call-logging pipeline still work end-to-end in dev.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Call, CallDirection, CallOutcome, Lead, Phone
from app.services.skiptrace import is_callable


def initiate_call(
    db: Session,
    lead: Lead,
    phone: Phone,
    operator_number: str,
    caller_id: Optional[str] = None,
) -> Call:
    """Initiate a compliant manual click-to-call.

    Uses Twilio's "connect operator to seller" pattern: Twilio first rings
    the operator; when they answer, it bridges to the seller.  The operator
    is a live participant — not an autodialer.

    Raises ValueError if the phone is DNC-flagged or opted out.
    """
    if not is_callable(phone):
        raise ValueError(
            f"Phone {phone.number} is DNC-flagged or opted out. Mail only."
        )

    settings = get_settings()
    call_sid: Optional[str] = None

    # Read Twilio config from DB settings, fall back to env vars
    from app.services.settings import get_twilio_config
    twilio_cfg = get_twilio_config(db)
    twilio_sid = twilio_cfg["account_sid"] or settings.twilio_account_sid
    twilio_token = twilio_cfg["auth_token"] or settings.twilio_auth_token
    twilio_from = twilio_cfg["from_number"] or settings.twilio_from_number

    if twilio_sid and twilio_token and twilio_from:
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
            call_sid = f"err:{exc!s}"[:64]
    # Dev dry-run: no call_sid, call logged for the pipeline.

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
) -> Call:
    """Update a call with its outcome and set follow-up per the manual.

    Most deals close after touch 5+, so every non-yes outcome schedules a
    follow-up.  DNC requests immediately opt out the phone.
    """
    from datetime import datetime, timedelta

    call.outcome = outcome
    call.notes = notes
    call.duration_seconds = duration_seconds
    db.flush()

    # Schedule follow-up for non-final outcomes
    from app.models import FollowUp, LeadStatus
    lead = call.lead

    if outcome == CallOutcome.DNC_REQUEST:
        # Honor opt-out instantly
        if call.phone:
            from app.services.skiptrace import mark_opted_out
            mark_opted_out(db, call.phone)

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
