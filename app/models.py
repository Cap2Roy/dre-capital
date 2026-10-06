"""SQLAlchemy domain models for DRE-Capital.

Schema follows the Acquisitions Operator Manual workflow:

    SourceList ─┐
                ├─→ LeadListMembership ─→ Lead (property+owner)
    Lead ─┬─→ Phone (skip-traced, DNC-flagged)
          ├─→ Valuation (ARV, repairs, MAO)
          ├─→ Comp (sold comparables)
          ├─→ Call (manual-dial call log)
          ├─→ FollowUp (next-attempt date — "a lead with no next date is lost")
          ├─→ Contract (signed assignment contract)
          └─→ Buyer (cash-buyer network + buy-box matching)

Compliance is enforced at the model level:
  * Phone.dnc_flagged  — DNC-registry match → mail only, never call
  * Phone.opted_out    — seller said stop → instant honor + logged
  * Call.direction      — outbound manual-dial only; no autodialer field exists
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _uuid() -> str:
    return uuid.uuid4().hex


# ── Enums ──────────────────────────────────────────────────────────────────

class LeadStatus(str, enum.Enum):
    NEW = "new"
    WORKING = "working"
    WARM = "warm"
    UNDER_CONTRACT = "under_contract"
    ASSIGNED = "assigned"
    CLOSED = "closed"
    DEAD = "dead"


class ListType(str, enum.Enum):
    """The seven motivated-seller lists from the manual."""
    PRE_FORECLOSURE = "pre_foreclosure"
    TAX_DELINQUENT = "tax_delinquent"
    PROBATE = "probate"
    DIVORCE = "divorce"
    ABSENTEE_OWNER = "absentee_owner"
    TIRED_LANDLORD = "tired_landlord"
    VACANT_CODE_VIOLATION = "vacant_code_violation"


class PhoneQuality(str, enum.Enum):
    UNVERIFIED = "unverified"
    GOOD = "good"
    STALE = "stale"
    BAD = "bad"
    DNC = "dnc"           # Do-Not-Call registry match
    OPTED_OUT = "opted_out"  # Seller said stop


class CallOutcome(str, enum.Enum):
    NO_ANSWER = "no_answer"
    VOICEMAIL = "voicemail"
    WRONG_NUMBER = "wrong_number"
    NOT_INTERESTED = "not_interested"
    CALLBACK_REQUESTED = "callback_requested"
    WARM = "warm"
    OFFER_SENT = "offer_sent"
    VERBAL_YES = "verbal_yes"
    DNC_REQUEST = "dnc_request"


class CallDirection(str, enum.Enum):
    """Manual-dial only. Inbound exists for returned calls / callbacks."""
    OUTBOUND_MANUAL = "outbound_manual"
    INBOUND = "inbound"



class MessageChannel(str, enum.Enum):
    SMS = "sms"
    EMAIL = "email"


class MessageDirection(str, enum.Enum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"

# ── Mixin ──────────────────────────────────────────────────────────────────

class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


# ── Users / sales team ─────────────────────────────────────────────────────

class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(40), default="acquisitions")  # acquisitions | manager | admin
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Relations
    calls: Mapped[list["Call"]] = relationship(back_populates="caller")


# ── Source lists ───────────────────────────────────────────────────────────

class SourceList(TimestampMixin, Base):
    """A batch of records imported from one county source (e.g. delinquent-tax CSV)."""
    __tablename__ = "source_lists"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    list_type: Mapped[ListType] = mapped_column(Enum(ListType), index=True)
    county: Mapped[Optional[str]] = mapped_column(String(120), index=True)
    state: Mapped[Optional[str]] = mapped_column(String(2), index=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    memberships: Mapped[list["LeadListMembership"]] = relationship(back_populates="source_list")


# ── Lead (property + owner of record) ──────────────────────────────────────

class Lead(TimestampMixin, Base):
    """A property and its owner of record — the central entity.

    ``stack_depth`` is denormalized from LeadListMembership for fast call-order
    sorting.  Per the manual: 3+ lists → call today, 2 → this week, 1 → mail only.
    """
    __tablename__ = "leads"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # Property address
    property_address: Mapped[str] = mapped_column(String(255), index=True)
    property_city: Mapped[Optional[str]] = mapped_column(String(120))
    property_state: Mapped[Optional[str]] = mapped_column(String(2), index=True)
    property_zip: Mapped[Optional[str]] = mapped_column(String(12))
    # Owner of record
    owner_name: Mapped[str] = mapped_column(String(255), index=True)
    owner_is_llc: Mapped[bool] = mapped_column(Boolean, default=False)
    mailing_address: Mapped[Optional[str]] = mapped_column(String(255))  # != property → absentee
    mailing_city: Mapped[Optional[str]] = mapped_column(String(120))
    mailing_state: Mapped[Optional[str]] = mapped_column(String(2))
    mailing_zip: Mapped[Optional[str]] = mapped_column(String(12))
    owner_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    absentee: Mapped[bool] = mapped_column(Boolean, default=False)
    # Property facts
    beds: Mapped[Optional[int]] = mapped_column(Integer)
    baths: Mapped[Optional[float]] = mapped_column(Float)
    sqft: Mapped[Optional[int]] = mapped_column(Integer)
    year_built: Mapped[Optional[int]] = mapped_column(Integer)
    rehab_level: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)  # light | cosmetic+ | gut
    # Assessed / tax
    assessed_value: Mapped[Optional[float]] = mapped_column(Float)
    taxes_owed: Mapped[Optional[float]] = mapped_column(Float)
    # Workflow
    status: Mapped[LeadStatus] = mapped_column(Enum(LeadStatus), default=LeadStatus.NEW, index=True)
    stack_depth: Mapped[int] = mapped_column(Integer, default=0, index=True)
    assigned_to: Mapped[Optional[str]] = mapped_column(String(32), ForeignKey("users.id"), nullable=True)
    next_followup: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    # Relations
    list_memberships: Mapped[list["LeadListMembership"]] = relationship(back_populates="lead", cascade="all, delete-orphan")
    phones: Mapped[list["Phone"]] = relationship(back_populates="lead", cascade="all, delete-orphan")
    comps: Mapped[list["Comp"]] = relationship(back_populates="lead", cascade="all, delete-orphan")
    valuations: Mapped[list["Valuation"]] = relationship(back_populates="lead", cascade="all, delete-orphan")
    calls: Mapped[list["Call"]] = relationship(back_populates="lead")
    followups: Mapped[list["FollowUp"]] = relationship(back_populates="lead", cascade="all, delete-orphan")
    contracts: Mapped[list["Contract"]] = relationship(back_populates="lead")
    messages: Mapped[list["MessageLog"]] = relationship(back_populates="lead")
    meetings: Mapped[list["Meeting"]] = relationship(back_populates="lead")


class LeadListMembership(TimestampMixin, Base):
    """Many-to-many: which lists a lead appears on. Count = stack depth."""
    __tablename__ = "lead_list_memberships"
    __table_args__ = (UniqueConstraint("lead_id", "source_list_id", name="uq_lead_list"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(String(32), ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    source_list_id: Mapped[str] = mapped_column(String(32), ForeignKey("source_lists.id", ondelete="CASCADE"), index=True)
    lead: Mapped["Lead"] = relationship(back_populates="list_memberships")
    source_list: Mapped["SourceList"] = relationship(back_populates="memberships")


# ── Skip trace / phones ────────────────────────────────────────────────────

class Phone(TimestampMixin, Base):
    """A skip-traced phone number for a lead's owner.

    Compliance: ``dnc_flagged`` (federal DNC registry) or ``quality == DNC``
    blocks outbound calls entirely.  ``opted_out`` (seller said stop) also
    blocks.  The UI greys out the call button and shows "Mail only".
    """
    __tablename__ = "phones"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(String(32), ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    number: Mapped[str] = mapped_column(String(20), index=True)
    quality: Mapped[PhoneQuality] = mapped_column(Enum(PhoneQuality), default=PhoneQuality.UNVERIFIED, index=True)
    dnc_flagged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    opted_out_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    lead: Mapped["Lead"] = relationship(back_populates="phones")


# ── Comps & valuation ──────────────────────────────────────────────────────

class Comp(TimestampMixin, Base):
    """A sold comparable property used to derive ARV."""
    __tablename__ = "comps"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(String(32), ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    address: Mapped[str] = mapped_column(String(255))
    sold_price: Mapped[float] = mapped_column(Float)
    sqft: Mapped[Optional[int]] = mapped_column(Integer)
    beds: Mapped[Optional[int]] = mapped_column(Integer)
    baths: Mapped[Optional[float]] = mapped_column(Float)
    sold_date: Mapped[Optional[datetime]] = mapped_column(DateTime)
    distance_miles: Mapped[Optional[float]] = mapped_column(Float)
    price_per_sqft: Mapped[Optional[float]] = mapped_column(Float)
    is_renovated: Mapped[bool] = mapped_column(Boolean, default=False)
    lead: Mapped["Lead"] = relationship(back_populates="comps")


class Valuation(TimestampMixin, Base):
    """An ARV + repair estimate + MAO computation for a lead.

    MAO = (ARV × 0.70) − Repairs − Fee  (manual's formula).
    """
    __tablename__ = "valuations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(String(32), ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    arv: Mapped[float] = mapped_column(Float)                       # after-repair value
    repair_estimate: Mapped[float] = mapped_column(Float, default=0)
    fee: Mapped[float] = mapped_column(Float, default=15000)
    mao: Mapped[float] = mapped_column(Float)                       # max allowable offer
    comp_count: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    lead: Mapped["Lead"] = relationship(back_populates="valuations")


# ── Calls (manual-dial log) ────────────────────────────────────────────────

class Call(TimestampMixin, Base):
    """A manual-dial call.  No autodialer; no prerecorded messages.

    ``attempt_count`` is the running total for the lead; the manual says most
    deals close after touch 5+, so this drives persistence.
    """
    __tablename__ = "calls"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(String(32), ForeignKey("leads.id"), index=True)
    phone_id: Mapped[Optional[str]] = mapped_column(String(32), ForeignKey("phones.id"), nullable=True)
    caller_id: Mapped[Optional[str]] = mapped_column(String(32), ForeignKey("users.id"), nullable=True)
    direction: Mapped[CallDirection] = mapped_column(Enum(CallDirection), default=CallDirection.OUTBOUND_MANUAL)
    outcome: Mapped[CallOutcome] = mapped_column(Enum(CallOutcome), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer)
    call_sid: Mapped[Optional[str]] = mapped_column(String(64))  # Twilio call SID
    notes: Mapped[Optional[str]] = mapped_column(Text)
    call_flow_id: Mapped[Optional[str]] = mapped_column(String(32), ForeignKey("call_flows.id"), nullable=True)
    lead: Mapped["Lead"] = relationship(back_populates="calls")
    caller: Mapped[Optional["User"]] = relationship(back_populates="calls")
    call_flow: Mapped[Optional["CallFlow"]] = relationship(back_populates="calls")


class FollowUp(TimestampMixin, Base):
    """Next-attempt date for a lead.  "A lead with no next date is a lead you lose." """
    __tablename__ = "followups"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(String(32), ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    reason: Mapped[Optional[str]] = mapped_column(String(255))
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    lead: Mapped["Lead"] = relationship(back_populates="followups")


# ── Buyers network ─────────────────────────────────────────────────────────

class Buyer(TimestampMixin, Base):
    """A cash buyer in the network, with a buy-box for matching."""
    __tablename__ = "buyers"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120), index=True)
    company: Mapped[Optional[str]] = mapped_column(String(120))
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    email: Mapped[Optional[str]] = mapped_column(String(255))
    # Buy box
    target_states: Mapped[Optional[str]] = mapped_column(String(255))  # "TX,GA"
    target_cities: Mapped[Optional[str]] = mapped_column(String(255))
    min_price: Mapped[Optional[float]] = mapped_column(Float)
    max_price: Mapped[Optional[float]] = mapped_column(Float)
    min_beds: Mapped[Optional[int]] = mapped_column(Integer)
    max_beds: Mapped[Optional[int]] = mapped_column(Integer)
    rehab_level: Mapped[Optional[str]] = mapped_column(String(40))  # light | cosmetic+ | gut
    ranking: Mapped[int] = mapped_column(Integer, default=0)  # 1 = top buyer
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Sourcing: where this buyer came from (network | title_partner | meetup | marketplace | referral).
    source: Mapped[Optional[str]] = mapped_column(String(40), default="network")
    # Preferred / referring title company for this buyer's closings.
    title_company_id: Mapped[Optional[str]] = mapped_column(String(32), ForeignKey("title_companies.id"), nullable=True)
    contracts: Mapped[list["Contract"]] = relationship(back_populates="buyer")
    title_company: Mapped[Optional["TitleCompany"]] = relationship(back_populates="buyers")


class TitleCompany(TimestampMixin, Base):
    """A title-company partner — a primary source of cash-buyer referrals."""
    __tablename__ = "title_companies"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(180), index=True)
    contact_name: Mapped[Optional[str]] = mapped_column(String(120))
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    email: Mapped[Optional[str]] = mapped_column(String(255))
    address: Mapped[Optional[str]] = mapped_column(String(255))
    # States this partner covers (for closing / escrow).
    coverage_states: Mapped[Optional[str]] = mapped_column(String(255))  # "TX,MI"
    # Referral / marketing fee paid per closing (if any).
    referral_fee: Mapped[Optional[float]] = mapped_column(Float, default=0)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    buyers: Mapped[list["Buyer"]] = relationship(back_populates="title_company")


# ── Contracts / assignments ────────────────────────────────────────────────

class Contract(TimestampMixin, Base):
    """A signed assignment contract. Discloses that we sell the contract, not the house."""
    __tablename__ = "contracts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(String(32), ForeignKey("leads.id"), index=True)
    buyer_id: Mapped[Optional[str]] = mapped_column(String(32), ForeignKey("buyers.id"), nullable=True)
    contract_price: Mapped[float] = mapped_column(Float)             # our offer / assignment price
    assignment_fee: Mapped[float] = mapped_column(Float, default=15000)
    buyer_price: Mapped[Optional[float]] = mapped_column(Float)       # what the cash buyer pays
    status: Mapped[str] = mapped_column(String(40), default="pending")  # pending|signed|assigned|closed|fallen
    signed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    assigned_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    disclosure_sent: Mapped[bool] = mapped_column(Boolean, default=False)  # TX assignment disclosure
    notes: Mapped[Optional[str]] = mapped_column(Text)
    lead: Mapped["Lead"] = relationship(back_populates="contracts")
    buyer: Mapped[Optional["Buyer"]] = relationship(back_populates="contracts")

# ── Settings (key-value store for API keys, call cadence, etc.) ──────────────

class Setting(TimestampMixin, Base):
    """Application-level key-value settings (API keys, cadence overrides, etc.).

    Keys that end in '_key', '_token', '_sid', '_password' are treated as secrets
    — masked in API responses.
    """
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    category: Mapped[str] = mapped_column(String(64), default="general")
    label: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

# ── Property Scraper ────────────────────────────────────────────────────────

class ScrapeSource(TimestampMixin, Base):
    """A data source for property scraping — county tax site, public records API, etc."""
    __tablename__ = "scrape_sources"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(64))  # county_tax, public_api, html
    state: Mapped[Optional[str]] = mapped_column(String(2))
    county: Mapped[Optional[str]] = mapped_column(String(120))
    # Search parameters stored as JSON string
    search_params: Mapped[Optional[str]] = mapped_column(Text)  # JSON: {"zip":"75201","min_value":100000,...}
    # Auth/API key if needed
    api_key: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Whether this source should be scraped by the daily auto-scrape scheduler.
    auto_scrape: Mapped[bool] = mapped_column(Boolean, default=False)
    jobs: Mapped[list["ScrapeJob"]] = relationship(back_populates="source", cascade="all, delete-orphan")


class ScrapeJobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ScrapeJob(TimestampMixin, Base):
    """A single scrape run against a source — tracks progress and results."""
    __tablename__ = "scrape_jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    source_id: Mapped[str] = mapped_column(String(32), ForeignKey("scrape_sources.id"), index=True)
    status: Mapped[ScrapeJobStatus] = mapped_column(Enum(ScrapeJobStatus), default=ScrapeJobStatus.PENDING, index=True)
    # Search overrides for this specific job
    search_params: Mapped[Optional[str]] = mapped_column(Text)  # JSON
    # Progress
    total_found: Mapped[int] = mapped_column(Integer, default=0)
    imported: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    # Error info
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Raw + processed data (JSON string of results)
    raw_results: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # LLM analysis summary
    llm_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    source: Mapped["ScrapeSource"] = relationship(back_populates="jobs")
    results: Mapped[list["ScrapeResult"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class ScrapeResult(TimestampMixin, Base):
    """An individual property found by a scrape job, pending review/import."""
    __tablename__ = "scrape_results"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(String(32), ForeignKey("scrape_jobs.id"), index=True)
    # Extracted property data (JSON)
    property_data: Mapped[str] = mapped_column(Text)  # JSON: address, owner, beds, baths, sqft, etc.
    # LLM analysis of this property
    llm_analysis: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Import status
    imported: Mapped[bool] = mapped_column(Boolean, default=False)
    imported_lead_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    job: Mapped["ScrapeJob"] = relationship(back_populates="results")

# ── Call flows (qualification scripts) ────────────────────────────────────────

class CallFlow(TimestampMixin, Base):
    """A reusable call-qualification script the operator follows on a manual call.

    Stores the persona/role, opening line, ordered questions, do/don't rules,
    closing line, and a Hot/Warm/Cold scoring rubric.  ``questions``, ``rules``,
    and ``scoring`` are stored as JSON strings (SQLite has no native JSON type).
    Exactly one flow may be marked ``is_default``; the rest are user-created.
    """
    __tablename__ = "call_flows"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(160), index=True)
    description: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # Persona context: "You are Sam, an assistant calling on behalf of <Company>…"
    role: Mapped[str] = mapped_column(Text)
    opening: Mapped[str] = mapped_column(Text)
    # JSON array of ordered question strings
    questions: Mapped[str] = mapped_column(Text, default="[]")
    # JSON array of rule strings (do/don't)
    rules: Mapped[str] = mapped_column(Text, default="[]")
    close: Mapped[str] = mapped_column(Text)
    # JSON object: {"hot": "...", "warm": "...", "cold": "..."}
    scoring: Mapped[str] = mapped_column(Text, default="{}")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    calls: Mapped[list["Call"]] = relationship(back_populates="call_flow")



# ── Message templates & SMS/email history ────────────────────────────────────

class MessageTemplate(TimestampMixin, Base):
    """Reusable SMS/email body templates with Jinja2 placeholders.

    Placeholders are filled against a lead context, e.g.
    ``{{ lead.owner_name }}``, ``{{ lead.property_address }}``.
    """
    __tablename__ = "message_templates"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120), index=True)
    channel: Mapped[MessageChannel] = mapped_column(Enum(MessageChannel), default=MessageChannel.SMS, index=True)
    category: Mapped[str] = mapped_column(String(64), default="general")  # e.g. no_answer, follow_up, offer
    # Email only (NULL for SMS)
    subject: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    body: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class MessageLog(TimestampMixin, Base):
    """Outbound/inbound SMS (and email) tied to a lead + phone."""
    __tablename__ = "message_logs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    lead_id: Mapped[Optional[str]] = mapped_column(String(32), ForeignKey("leads.id"), index=True, nullable=True)
    phone_id: Mapped[Optional[str]] = mapped_column(String(32), ForeignKey("phones.id"), index=True, nullable=True)
    channel: Mapped[MessageChannel] = mapped_column(Enum(MessageChannel), default=MessageChannel.SMS)
    direction: Mapped[MessageDirection] = mapped_column(Enum(MessageDirection), default=MessageDirection.OUTBOUND)
    to_number: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    from_number: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    to_address: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # email recipient (SMS uses to_number)
    subject: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    body: Mapped[str] = mapped_column(Text)
    # Delivery: sent / delivered / failed / received
    status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    twilio_sid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    template_id: Mapped[Optional[str]] = mapped_column(String(32), ForeignKey("message_templates.id"), nullable=True)
    sent_by: Mapped[Optional[str]] = mapped_column(String(32), ForeignKey("users.id"), nullable=True)
    lead: Mapped[Optional["Lead"]] = relationship(back_populates="messages")


class Meeting(TimestampMixin, Base):
    """A scheduled meeting/appointment with a lead (for notifications)."""
    __tablename__ = "meetings"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    lead_id: Mapped[Optional[str]] = mapped_column(String(32), ForeignKey("leads.id"), index=True, nullable=True)
    title: Mapped[str] = mapped_column(String(256))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    location: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Reminder minutes before scheduled_at
    reminder_minutes: Mapped[int] = mapped_column(Integer, default=60)
    reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    lead: Mapped[Optional["Lead"]] = relationship(back_populates="meetings")
