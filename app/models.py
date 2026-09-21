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
    absentee: Mapped[bool] = mapped_column(Boolean, default=False)
    # Property facts
    beds: Mapped[Optional[int]] = mapped_column(Integer)
    baths: Mapped[Optional[float]] = mapped_column(Float)
    sqft: Mapped[Optional[int]] = mapped_column(Integer)
    year_built: Mapped[Optional[int]] = mapped_column(Integer)
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
    lead: Mapped["Lead"] = relationship(back_populates="calls")
    caller: Mapped[Optional["User"]] = relationship(back_populates="calls")


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
    contracts: Mapped[list["Contract"]] = relationship(back_populates="buyer")


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
