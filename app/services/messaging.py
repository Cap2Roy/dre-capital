"""Outbound/inbound SMS (and email) via Twilio, with a pluggable mock/dry-run.

Compliance note: SMS is manual — the operator sends a text, or a template is
fired explicitly from the UI.  No automated text-blasting without a human click.
Inbound STOP/UNSUBSCRIBE keywords instantly opt the number out (DNC).
"""
from __future__ import annotations

from typing import Optional

from jinja2 import Template
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Lead,
    MessageChannel,
    MessageDirection,
    MessageLog,
    MessageTemplate,
    Phone,
)


# Keywords Twilio routes to the webhook when someone texts "stop" etc.
_STOP_KEYWORDS = {"stop", "stopall", "unsubscribe", "cancel", "quit", "end", "halt"}


def get_sms_client(db: Session):
    """Build a Twilio client from DB settings (env fallback), or None if unconfigured."""
    from app.services.settings import get_twilio_config
    from app.config import get_settings

    cfg = get_twilio_config(db)
    s = get_settings()
    sid = cfg["account_sid"] or s.twilio_account_sid
    token = cfg["auth_token"] or s.twilio_auth_token
    if sid and token:
        from twilio.rest import Client
        return Client(sid, token)
    return None


def sms_from_number(db: Session) -> str:
    """The number SMS is sent from.  Prefer a dedicated SMS number, else the voice line."""
    from app.services.settings import get_setting, get_twilio_config

    dedicated = get_setting(db, "twilio_sms_number")
    if dedicated:
        return dedicated
    return get_twilio_config(db)["from_number"]


def render_template(raw_body: str, lead: Lead) -> str:
    """Fill Jinja2 placeholders against a lead context (owner + property)."""
    ctx = {
        "lead": lead,
        "owner_name": lead.owner_name or "",
        "property_address": lead.property_address or "",
        "property_city": lead.property_city or "",
        "property_state": lead.property_state or "",
        "property_zip": lead.property_zip or "",
    }
    return Template(raw_body).render(**ctx)


def find_phone_by_number(db: Session, number: str) -> Optional[Phone]:
    """Resolve a normalized E.164 phone number to a Phone row (inbound SMS)."""
    cleaned = number.strip()
    return db.execute(
        select(Phone).where(Phone.number == cleaned)
    ).scalars().first()


def send_sms(
    db: Session,
    lead: Lead,
    phone: Phone,
    body: Optional[str] = None,
    template: Optional[MessageTemplate] = None,
    sent_by: Optional[str] = None,
) -> MessageLog:
    """Send a manual SMS to a lead's phone.  Honors opt-out; dry-runs without Twilio.

    Exactly one of ``body`` or ``template`` must be provided.  If the phone is
    opted out / DNC-flagged we refuse and log nothing.
    """
    from app.services.skiptrace import is_callable

    if not is_callable(phone):
        raise ValueError("Phone is DNC-flagged or opted out.")

    if template is not None:
        body = render_template(template.body, lead)
    if not body:
        raise ValueError("SMS body is required.")

    # Twilio enforces 1600-char SMS limit (concatenated).  Guard against longer.
    if len(body) > 1600:
        raise ValueError("SMS body exceeds 1600 characters.")

    entry = MessageLog(
        lead_id=lead.id,
        phone_id=phone.id,
        channel=MessageChannel.SMS,
        direction=MessageDirection.OUTBOUND,
        to_number=phone.number,
        from_number=sms_from_number(db),
        subject=None,
        body=body,
        template_id=template.id if template else None,
        sent_by=sent_by,
    )
    db.add(entry)
    db.flush()

    client = get_sms_client(db)
    from_number = sms_from_number(db)
    if client and from_number:
        try:
            msg = client.messages.create(
                to=phone.number,
                from_=from_number,
                body=body,
            )
            entry.twilio_sid = msg.sid
            entry.status = "sent"
        except Exception as exc:  # network / Twilio errors — surface loudly
            entry.status = "failed"
            db.flush()
            raise RuntimeError(f"Twilio SMS failed: {exc}") from exc
    else:
        # Dev dry-run: record the intent without a real send.
        entry.status = "dry-run"
    db.flush()
    return entry


def handle_inbound_sms(
    db: Session,
    from_number: str,
    body: str,
    to_number: str = "",
) -> dict:
    """Log an inbound SMS; honor STOP keywords by opting the number out.

    Returns a small JSON-friendly payload for the Twilio webhook.
    """
    normalized_body = (body or "").strip().lower()
    phone = find_phone_by_number(db, from_number)
    lead = phone.lead if phone else None

    entry = MessageLog(
        lead_id=lead.id if lead else None,
        phone_id=phone.id if phone else None,
        channel=MessageChannel.SMS,
        direction=MessageDirection.INBOUND,
        to_number=to_number,
        from_number=from_number,
        body=body or "",
        status="received",
    )
    db.add(entry)

    # Honor opt-out keywords (STOP, STOPALL, UNSUBSCRIBE, …).
    first_word = normalized_body.split()[0] if normalized_body else ""
    opted_out = False
    if first_word in _STOP_KEYWORDS:
        if phone:
            from app.services.skiptrace import mark_opted_out
            mark_opted_out(db, phone)
            opted_out = True
        entry.status = "opt-out received"
    db.flush()
    return {"received": True, "opted_out": opted_out, "lead_id": lead.id if lead else None}