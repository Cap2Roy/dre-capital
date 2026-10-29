"""Pydantic schemas for the API layer."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, PlainSerializer, field_validator, model_validator

from app.timeutil import iso_utc, to_naive_utc

# Every API datetime: inputs normalized to naive UTC (storage convention),
# JSON output rendered as ISO-8601 with an explicit 'Z'.
UTCDateTime = Annotated[
    datetime,
    AfterValidator(to_naive_utc),
    PlainSerializer(iso_utc, return_type=str, when_used="json"),
]


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
    sold_date: Optional[UTCDateTime] = None
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
    created_at: UTCDateTime

class CallOut(ORMBase):
    id: str
    outcome: str
    direction: str
    provider: str = "dry-run"
    attempt_count: int
    duration_seconds: Optional[int] = None
    notes: Optional[str] = None
    call_flow_id: Optional[str] = None
    answers: dict[str, str] = {}
    lead_score: Optional[str] = None
    ai_summary: Optional[str] = None
    transcript: Optional[str] = None
    created_at: UTCDateTime

    @field_validator("answers", mode="before")
    @classmethod
    def _parse_answers(cls, v):
        if isinstance(v, str):
            try:
                import json
                return json.loads(v) or {}
            except (ValueError, TypeError):
                return {}
        return v or {}


class CallInitiateOut(CallOut):
    """Initiate response: ``dial_url`` is the tel: link for the Quo app (Quo only)."""
    dial_url: Optional[str] = None

class FollowUpOut(ORMBase):
    id: str
    due_at: UTCDateTime
    reason: Optional[str] = None
    done: bool

class ContractOut(ORMBase):
    id: str
    lead_id: str
    buyer_id: Optional[str] = None
    property_address: Optional[str] = None
    buyer_name: Optional[str] = None
    contract_price: float
    assignment_fee: float
    buyer_price: Optional[float] = None
    status: str
    signed_at: Optional[UTCDateTime] = None
    disclosure_sent: bool

    @model_validator(mode="wrap")
    @classmethod
    def _resolve_related(cls, data, handler):
        """Resolve address/buyer name from the ORM relationships."""
        out = handler(data)
        lead = getattr(data, "lead", None)
        buyer = getattr(data, "buyer", None)
        if out.property_address is None and lead is not None:
            out.property_address = lead.property_address
        if out.buyer_name is None and buyer is not None:
            out.buyer_name = buyer.name
        return out

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
    next_followup: Optional[UTCDateTime] = None
    notes: Optional[str] = None
    created_at: UTCDateTime
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
    next_followup: Optional[UTCDateTime] = None


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
    operator_number: Optional[str] = None  # required only for the Twilio bridge


class CallLogRequest(BaseModel):
    outcome: str
    notes: Optional[str] = None
    duration_seconds: Optional[int] = None
    call_flow_id: Optional[str] = None
    answers: Optional[dict[str, str]] = None  # call-guide answers keyed by question index
    lead_score: Optional[str] = None          # hot | warm | cold; blank = compute from answers


class CallScoreRequest(BaseModel):
    lead_id: str
    flow_id: Optional[str] = None
    answers: dict[str, str] = {}


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
    due_at: UTCDateTime
    reason: Optional[str] = None


# ── Source lists ───────────────────────────────────────────────────────────

class SourceListOut(ORMBase):
    id: str
    name: str
    list_type: str
    county: Optional[str] = None
    state: Optional[str] = None
    record_count: int
    imported_at: UTCDateTime

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


class ScrapeSourceUpdate(BaseModel):
    """Partial update: only fields present in the request body are applied."""
    name: Optional[str] = None
    url: Optional[str] = None
    source_type: Optional[str] = None
    state: Optional[str] = None
    county: Optional[str] = None
    search_params: Optional[str] = None
    api_key: Optional[str] = None
    auto_scrape: Optional[bool] = None


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
    created_at: UTCDateTime


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
    started_at: Optional[UTCDateTime] = None
    completed_at: Optional[UTCDateTime] = None
    created_at: UTCDateTime


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
    created_at: UTCDateTime


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


# ── Call flows (qualification scripts) ───────────────────────────────────────

class CallFlowOut(ORMBase):
    id: str
    name: str
    description: Optional[str] = None
    role: str
    opening: str
    questions: list[str] = []
    rules: list[str] = []
    close: str
    scoring: dict = {}
    is_default: bool
    active: bool
    created_at: UTCDateTime

    @field_validator("questions", "rules", mode="before")
    @classmethod
    def _parse_json_list(cls, v):
        if isinstance(v, str):
            try:
                import json
                return json.loads(v) or []
            except (ValueError, TypeError):
                return []
        return v or []

    @field_validator("scoring", mode="before")
    @classmethod
    def _parse_json_dict(cls, v):
        if isinstance(v, str):
            try:
                import json
                return json.loads(v) or {}
            except (ValueError, TypeError):
                return {}
        return v or {}


class CallFlowCreate(BaseModel):
    name: str
    description: Optional[str] = None
    role: str = ""
    opening: str = ""
    questions: list[str] = []
    rules: list[str] = []
    close: str = ""
    scoring: dict = {}
    is_default: bool = False
    active: bool = True


class CallFlowUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    role: Optional[str] = None
    opening: Optional[str] = None
    questions: Optional[list[str]] = None
    rules: Optional[list[str]] = None
    close: Optional[str] = None
    scoring: Optional[dict] = None
    is_default: Optional[bool] = None
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
    created_at: UTCDateTime


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
    scheduled_at: UTCDateTime
    location: Optional[str] = None
    notes: Optional[str] = None
    reminder_minutes: int
    reminder_sent: bool
    created_at: UTCDateTime


class MeetingCreate(BaseModel):
    lead_id: Optional[str] = None
    title: str
    scheduled_at: UTCDateTime
    location: Optional[str] = None
    notes: Optional[str] = None
    reminder_minutes: int = 60


# ── AI voice agent test calls ────────────────────────────────────────────────

class AiCallCreate(BaseModel):
    to_number: Optional[str] = None         # tester's own phone (rehearsal) …
    phone_id: Optional[str] = None          # … or a lead's verified number (real call to the owner)
    greet_name: Optional[str] = None        # who the agent asks for ("Hi, is this ___?")
    lead_id: Optional[str] = None           # rehearse this property's conversation
    call_flow_id: Optional[str] = None      # default flow if omitted
    voice: Optional[str] = None             # Bland voice; default from settings
    consent: bool = False                   # tester confirms they own / may receive AI calls on this number


class AiCallOut(ORMBase):
    id: str
    to_number: str
    greet_name: Optional[str] = None
    lead_id: Optional[str] = None
    call_flow_id: Optional[str] = None
    voice: Optional[str] = None
    provider: str
    provider_call_id: Optional[str] = None
    phone_id: Optional[str] = None
    outcome: Optional[str] = None
    status: str
    answered_by: Optional[str] = None
    duration_seconds: Optional[int] = None
    has_recording: bool = False
    transcript: Optional[str] = None
    summary: Optional[str] = None
    score: Optional[str] = None
    price: Optional[float] = None
    error: Optional[str] = None
    created_at: UTCDateTime

    @model_validator(mode="before")
    @classmethod
    def _recording_flag(cls, data):
        # Never expose the provider's recording URL; the UI streams via our proxy.
        if hasattr(data, "recording_url"):
            return {**{k: getattr(data, k) for k in cls.model_fields if hasattr(data, k)},
                    "has_recording": bool(data.recording_url)}
        return data
