"""Contract document generation + delivery.

Renders seller (purchase) and buyer (assignment) contract documents from
MessageTemplate rows whose ``category`` is ``contract_seller`` or
``contract_buyer``, filled from the lead + contract + buyer + operator.  The
rendered HTML is the contract; delivery is by email (recorded in MessageLog).

Email is honest: if no SMTP settings are configured, ``send_contract_email``
raises ``RuntimeError`` rather than faking a send — the same posture as the
SMS service's loud-failure choice.  No SMTP provider is configured by default.
"""
from __future__ import annotations

from typing import Optional

from jinja2 import Template
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    Buyer,
    Contract,
    Lead,
    MessageChannel,
    MessageDirection,
    MessageLog,
    MessageTemplate,
    User,
)
from app.services.messaging import render_template
from app.services.settings import get_setting

# Template categories that carry contract document bodies.
_CONTRACT_CATEGORIES = {"seller": "contract_seller", "buyer": "contract_buyer"}


def _operator_context(db: Session, lead: Lead) -> dict:
    """The acquiring operator / company signing the contract."""
    s = get_settings()
    name = "DRE-Capital"
    if lead.assigned_to:
        op = db.get(User, lead.assigned_to)
        if op:
            name = op.name or name
    company = get_setting(db, "company_name") or "Direct Real Estate Capital LLC"
    return {"operator_name": name, "company_name": company}


def contract_context(db: Session, contract: Contract, party: str) -> dict:
    """Build the Jinja2 context for rendering a contract document.

    ``party`` is ``"seller"`` or ``"buyer"``.  The buyer side also gets the
    matched Buyer record; both sides get the lead, contract figures, and
    operator/company.
    """
    lead = contract.lead
    ctx = {
        "lead": lead,
        "owner_name": lead.owner_name or "",
        "property_address": lead.property_address or "",
        "property_city": lead.property_city or "",
        "property_state": lead.property_state or "",
        "property_zip": lead.property_zip or "",
        "mailing_address": lead.mailing_address or lead.property_address or "",
        "beds": lead.beds if lead.beds is not None else "N/A",
        "baths": lead.baths if lead.baths is not None else "N/A",
        "sqft": lead.sqft if lead.sqft is not None else "N/A",
        "year_built": lead.year_built if lead.year_built is not None else "N/A",
        "contract_price": contract.contract_price,
        "assignment_fee": contract.assignment_fee,
        "buyer_price": contract.buyer_price,
        "contract": contract,
        "party": party,
        "today": _today(),
    }
    ctx.update(_operator_context(db, lead))
    buyer = contract.buyer
    # Safe defaults so the buyer block renders cleanly when no buyer is matched.
    ctx.update({
        "buyer_name": "",
        "buyer_company": "",
        "buyer_phone": "",
        "buyer_email": "",
    })
    if buyer is not None:
        ctx.update({
            "buyer_name": buyer.name or "",
            "buyer_company": buyer.company or "",
            "buyer_phone": buyer.phone or "",
            "buyer_email": buyer.email or "",
        })
    return ctx


def _today() -> str:
    from datetime import date
    return date.today().strftime("%B %d, %Y")


def find_contract_template(db: Session, party: str) -> Optional[MessageTemplate]:
    """Return the active contract document template for a party, or None."""
    category = _CONTRACT_CATEGORIES.get(party)
    if category is None:
        return None
    return db.execute(
        select(MessageTemplate).where(
            MessageTemplate.category == category,
            MessageTemplate.active == True,
        ).order_by(MessageTemplate.created_at.desc()).limit(1)
    ).scalars().first()


def render_contract_document(db: Session, contract: Contract, party: str) -> str:
    """Render the contract document body (HTML/text) for the given party.

    Raises ``ValueError`` if no template is configured for the party.
    """
    tpl = find_contract_template(db, party)
    if tpl is None:
        raise ValueError(f"No active {party} contract template configured")
    ctx = contract_context(db, contract, party)
    return Template(tpl.body).render(**ctx)


def _smtp_configured(db: Session) -> bool:
    """True if SMTP host + from-address are both set."""
    host = (get_setting(db, "smtp_host") or "").strip()
    frm = (get_setting(db, "smtp_from") or "").strip()
    return bool(host and frm)


def send_contract_email(
    db: Session,
    contract: Contract,
    party: str,
    to_address: Optional[str] = None,
    sent_by: Optional[str] = None,
) -> MessageLog:
    """Email the rendered contract document to the seller or buyer.

    Honesty contract: raises ``RuntimeError`` if SMTP is not configured — we
    never record a phantom "sent".  When SMTP is configured, sends via
    ``smtplib`` and records a MessageLog (channel=EMAIL) with status
    ``sent`` (or ``failed`` if the SMTP relay rejects it, in which case the
    row is still persisted for audit but a RuntimeError is raised so the
    operator sees the failure).
    """
    host = (get_setting(db, "smtp_host") or "").strip()
    frm = (get_setting(db, "smtp_from") or "").strip()
    if not (host and frm):
        raise RuntimeError(
            "SMTP is not configured (set smtp_host + smtp_from). "
            "Email delivery is unavailable until an SMTP provider is wired in Settings."
        )
    lead = contract.lead
    tpl = find_contract_template(db, party)
    # Resolve recipient: explicit override, else buyer email (buyer party),
    # else the lead's owner_email (seller party).
    if to_address is None and party == "buyer" and contract.buyer:
        to_address = contract.buyer.email
    if to_address is None and party == "seller":
        to_address = lead.owner_email
    if not to_address:
        raise ValueError("No recipient email address available for this contract party")

    body = render_contract_document(db, contract, party)
    subject = (tpl.subject if tpl and tpl.subject else f"Contract for {lead.property_address}")
    entry = MessageLog(
        lead_id=lead.id,
        channel=MessageChannel.EMAIL,
        direction=MessageDirection.OUTBOUND,
        to_address=to_address,
        from_number=frm,
        subject=subject[:256],
        body=body,
        status="queued",
        template_id=tpl.id if tpl else None,
        sent_by=sent_by,
    )
    db.add(entry)
    db.flush()

    # ── Actual SMTP delivery ───────────────────────────────────────
    port = int(get_setting(db, "smtp_port") or 587)
    user = (get_setting(db, "smtp_user") or "").strip() or None
    password = (get_setting(db, "smtp_password") or "").strip() or None
    use_tls = port in (465, 587)
    err = None
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(body)
        msg["Subject"] = subject[:256]
        msg["From"] = frm
        msg["To"] = to_address
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as srv:
                if user: srv.login(user, password or "")
                srv.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as srv:
                srv.ehlo()
                if use_tls: srv.starttls(); srv.ehlo()
                if user: srv.login(user, password or "")
                srv.send_message(msg)
        entry.status = "sent"
    except Exception as exc:  # noqa: BLE001
        err = str(exc)[:500]
        entry.status = "failed"
    db.flush()
    if err:
        raise RuntimeError(f"SMTP send failed: {err}")
    return entry
