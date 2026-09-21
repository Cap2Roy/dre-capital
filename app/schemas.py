"""Pydantic schemas for the API layer."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class ORMBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ── Dashboard ──────────────────────────────────────────────────────────────

class DashboardStats(ORMBase):
    total_leads: int
    new_leads: int
    warm_leads: int
    under_contract: int
    calls_today: int
    followups_today: int
    stack_today: int
    stack_this_week: int
    stack_mail_only: int
    dnc_blocked: int


# ── Leads ──────────────────────────────────────────────────────────────────

class PhoneOut(ORMBase):
    id: str
    number: str
    quality: str
    dnc_flagged: bool
    opted_out: bool
    is_primary: bool

class CompOut(ORMBase):
    id: str
    address: str
    sold_price: float
    sqft: Optional[int] = None
    beds: Optional[int] = None
    baths: Optional[float] = None
    sold_date: Optional[datetime] = None
    distance_miles: Optional[float] = None
    price_per_sqft: Optional[float] = None
    is_renovated: bool

class ValuationOut(ORMBase):
    id: str
    arv: float
    repair_estimate: float
    fee: float
    mao: float
    comp_count: int
    notes: Optional[str] = None
    created_at: datetime

class CallOut(ORMBase):
    id: str
    outcome: str
    direction: str
    attempt_count: int
    duration_seconds: Optional[int] = None
    notes: Optional[str] = None
    created_at: datetime

class FollowUpOut(ORMBase):
    id: str
    due_at: datetime
    reason: Optional[str] = None
    done: bool

class ContractOut(ORMBase):
    id: str
    contract_price: float
    assignment_fee: float
    buyer_price: Optional[float] = None
    status: str
    signed_at: Optional[datetime] = None
    disclosure_sent: bool

class LeadOut(ORMBase):
    id: str
    property_address: str
    property_city: Optional[str] = None
    property_state: Optional[str] = None
    property_zip: Optional[str] = None
    owner_name: str
    owner_is_llc: bool
    mailing_address: Optional[str] = None
    absentee: bool
    beds: Optional[int] = None
    baths: Optional[float] = None
    sqft: Optional[int] = None
    year_built: Optional[int] = None
    assessed_value: Optional[float] = None
    taxes_owed: Optional[float] = None
    status: str
    stack_depth: int
    next_followup: Optional[datetime] = None
    notes: Optional[str] = None
    created_at: datetime
    phones: list[PhoneOut] = []
    comps: list[CompOut] = []
    valuations: list[ValuationOut] = []
    calls: list[CallOut] = []
    followups: list[FollowUpOut] = []
    contracts: list[ContractOut] = []


class LeadCreate(BaseModel):
    property_address: str
    property_city: Optional[str] = None
    property_state: Optional[str] = None
    property_zip: Optional[str] = None
    owner_name: str = ""
    owner_is_llc: bool = False
    mailing_address: Optional[str] = None
    mailing_city: Optional[str] = None
    mailing_state: Optional[str] = None
    mailing_zip: Optional[str] = None
    beds: Optional[int] = None
    baths: Optional[float] = None
    sqft: Optional[int] = None
    year_built: Optional[int] = None
    assessed_value: Optional[float] = None
    taxes_owed: Optional[float] = None


class LeadUpdate(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None
    next_followup: Optional[datetime] = None


class LeadAssign(BaseModel):
    list_type: str  # ListType value


# ── Import ──────────────────────────────────────────────────────────────────

class ImportRequest(BaseModel):
    list_name: str
    list_type: str
    county: Optional[str] = None
    state: Optional[str] = None


class ImportResult(ORMBase):
    source_list_id: str
    name: str
    list_type: str
    record_count: int


# ── Valuation ──────────────────────────────────────────────────────────────

class ValuationRequest(BaseModel):
    repair_level: str = "light"  # light | cosmetic_plus | gut
    fee: float = 15000
    roof: bool = False
    hvac: bool = False
    foundation: bool = False
    repipe: bool = False
    panel: bool = False
    notes: Optional[str] = None


# ── Calls ──────────────────────────────────────────────────────────────────

class CallInitiateRequest(BaseModel):
    phone_id: str
    operator_number: str  # the sales rep's phone


class CallLogRequest(BaseModel):
    outcome: str
    notes: Optional[str] = None
    duration_seconds: Optional[int] = None


# ── Buyers ──────────────────────────────────────────────────────────────────

class BuyerOut(ORMBase):
    id: str
    name: str
    company: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    target_states: Optional[str] = None
    target_cities: Optional[str] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    min_beds: Optional[int] = None
    max_beds: Optional[int] = None
    rehab_level: Optional[str] = None
    ranking: int
    active: bool

class BuyerCreate(BaseModel):
    name: str
    company: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    target_states: Optional[str] = None
    target_cities: Optional[str] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    min_beds: Optional[int] = None
    max_beds: Optional[int] = None
    rehab_level: Optional[str] = None
    ranking: int = 10


# ── Contracts ──────────────────────────────────────────────────────────────

class ContractCreate(BaseModel):
    buyer_id: Optional[str] = None
    contract_price: float
    assignment_fee: float = 15000
    buyer_price: Optional[float] = None
    disclosure_sent: bool = False
    notes: Optional[str] = None


# ── Follow-ups ─────────────────────────────────────────────────────────────

class FollowUpCreate(BaseModel):
    due_at: datetime
    reason: Optional[str] = None


# ── Source lists ───────────────────────────────────────────────────────────

class SourceListOut(ORMBase):
    id: str
    name: str
    list_type: str
    county: Optional[str] = None
    state: Optional[str] = None
    record_count: int
    imported_at: datetime
