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
    comps_provider: Optional[str] = None
    comps_is_mock: bool = False
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
    owner_email: Optional[str] = None
    mailing_address: Optional[str] = None
    absentee: bool
    beds: Optional[int] = None
    baths: Optional[float] = None
    sqft: Optional[int] = None
    rehab_level: Optional[str] = None
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
    owner_email: Optional[str] = None
    owner_is_llc: bool = False
    mailing_address: Optional[str] = None
    mailing_city: Optional[str] = None
    mailing_state: Optional[str] = None
    mailing_zip: Optional[str] = None
    beds: Optional[int] = None
    baths: Optional[float] = None
    sqft: Optional[int] = None
    year_built: Optional[int] = None
    rehab_level: Optional[str] = None
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
    refresh_comps: bool = False  # force a fresh comps fetch instead of reusing stored ones


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
    source: Optional[str] = None
    title_company_id: Optional[str] = None

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
    source: Optional[str] = None
    title_company_id: Optional[str] = None


# ── Title companies (buyer-sourcing partners) ───────────────────────────────

class TitleCompanyOut(ORMBase):
    id: str
    name: str
    contact_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    coverage_states: Optional[str] = None
    referral_fee: Optional[float] = None
    notes: Optional[str] = None
    active: bool


class TitleCompanyCreate(BaseModel):
    name: str
    contact_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    coverage_states: Optional[str] = None
    referral_fee: Optional[float] = None
    notes: Optional[str] = None
    active: bool = True


# ── Contracts ──────────────────────────────────────────────────────────────

class ContractCreate(BaseModel):
    buyer_id: Optional[str] = None
    status: Optional[str] = None  # pending|signed|assigned|closed|fallen; defaults to pending
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

# ── Scraper ──────────────────────────────────────────────────────────────────

class ScrapeSourceCreate(BaseModel):
    name: str
    url: str
    source_type: str  # rentcast, arcgis, county_tax, public_api, html
    state: Optional[str] = None
    county: Optional[str] = None
    search_params: Optional[str] = None  # JSON string
    api_key: Optional[str] = None
    auto_scrape: bool = False


class ScrapeSourceOut(ORMBase):
    id: str
    name: str
    url: str
    source_type: str
    state: Optional[str] = None
    county: Optional[str] = None
    search_params: Optional[str] = None
    active: bool
    auto_scrape: bool = False
    created_at: datetime


class ScrapeJobCreate(BaseModel):
    source_id: str
    search_params: Optional[str] = None  # JSON overrides


class ScrapeResultOut(ORMBase):
    id: str
    job_id: str
    property_data: str  # JSON
    llm_analysis: Optional[str] = None
    imported: bool
    imported_lead_id: Optional[str] = None


class ScrapeJobOut(ORMBase):
    id: str
    source_id: str
    status: str
    search_params: Optional[str] = None
    total_found: int
    imported: int
    skipped: int
    error_message: Optional[str] = None
    llm_summary: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime


class ScrapeImportRequest(BaseModel):
    result_ids: list[str]
    list_type: str = "probate"
    list_name: Optional[str] = None


# ── Messaging (SMS / email) ─────────────────────────────────────────────────

class MessageTemplateOut(ORMBase):
    id: str
    name: str
    channel: str
    category: str
    subject: Optional[str] = None
    body: str
    active: bool
    created_at: datetime


class MessageTemplateCreate(BaseModel):
    name: str
    channel: str = "sms"  # sms | email
    category: str = "general"
    subject: Optional[str] = None
    body: str
    active: bool = True


class MessageTemplateUpdate(BaseModel):
    name: Optional[str] = None
    channel: Optional[str] = None
    category: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    active: Optional[bool] = None


class MessageLogOut(ORMBase):
    id: str
    lead_id: Optional[str] = None
    phone_id: Optional[str] = None
    channel: str
    direction: str
    to_number: Optional[str] = None
    from_number: Optional[str] = None
    to_address: Optional[str] = None
    subject: Optional[str] = None
    body: str
    status: Optional[str] = None
    template_id: Optional[str] = None
    sent_by: Optional[str] = None
    created_at: datetime


class SendSmsRequest(BaseModel):
    phone_id: str
    body: Optional[str] = None  # if omitted, template_id is used
    template_id: Optional[str] = None


class NoAnswerSmsRequest(BaseModel):
    """Quick-click: log NO_ANSWER, fire the no-answer template SMS, return next lead."""
    template_id: Optional[str] = None  # defaults to the active no_answer template
    notes: Optional[str] = None


# ── Meetings ───────────────────────────────────────────────────────────────

class MeetingOut(ORMBase):
    id: str
    lead_id: Optional[str] = None
    title: str
    scheduled_at: datetime
    location: Optional[str] = None
    notes: Optional[str] = None
    reminder_minutes: int
    reminder_sent: bool
    created_at: datetime


class MeetingCreate(BaseModel):
    lead_id: Optional[str] = None
    title: str
    scheduled_at: datetime
    location: Optional[str] = None
    notes: Optional[str] = None
    reminder_minutes: int = 60
