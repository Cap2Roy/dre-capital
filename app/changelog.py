"""Structured changelog for the DRE-Capital CRM.

Append a new entry to :data:`VERSIONS` for every release.  Each entry is::

    {
        "version": "2.1.0",
        "date": "2026-10-07",
        "title": "Editable call flows",
        "sections": {
            "Added": ["..."],
            "Fixed": ["..."],
            "Changed": ["..."],
        },
    }

The ``/whats-new`` page renders this list newest-first, and the API reference
is generated live from the registered FastAPI routes (so it never drifts).
The current app version is :data:`CURRENT_VERSION`.
"""
from __future__ import annotations

CURRENT_VERSION = "2.1.0"

VERSIONS: list[dict] = [
    {
        "version": "2.1.0",
        "date": "2026-10-07",
        "title": "Editable call flows + What's New",
        "sections": {
            "Added": [
                "Call flows — reusable qualification scripts (role, opening, ordered questions, rules, close, Hot/Warm/Cold scoring) with full CRUD at /api/call-flows.",
                "Default 'DFW Qualification — Sam for Eylon' flow seeded; editable in Settings → Call Flows.",
                "Log-Call modal now shows the default call-flow script live so the operator can follow it.",
                "'What's New' page — version log + API reference (this page).",
            ],
            "Changed": [
                "Call model gains an optional call_flow_id FK so a logged call can record which flow was used.",
                "default_call_flow_id added to the Settings key registry.",
            ],
        },
    },
    {
        "version": "2.0.0",
        "date": "2026-10-06",
        "title": "Compliance, honesty, and seed-data overhaul",
        "sections": {
            "Fixed": [
                "Scheduler wrote scrape-failure errors to a non-existent attribute — now persisted as error_message.",
                "Meeting reminders had no past bound: past meetings with reminder_sent=False re-fired forever. Added a 6h late-cutoff.",
                "Contract email never sent (only 'queued') — wired real SMTP send; SMTP-unconfigured now returns an honest 502, no phantom 'queued'.",
                "Lead.owner_email added so seller-party contract emails have a recipient; MessageLog.to_address so email recipients don't trample the phone column.",
                "Buyer matching now scores rehab_level (was documented but missing).",
                "STOP opt-out matches standalone tokens anywhere ('please stop' now works); dropped ambiguous cancel/end/halt.",
                "Dial queue excludes terminal lead statuses (UNDER_CONTRACT / ASSIGNED / CLOSED).",
                "create_contract defaults to 'pending' (was hardcoded 'signed') and guards lead-state regression.",
                "Valuation response surfaces comps_provider + comps_is_mock so the mock fallback is visible.",
            ],
            "Added": [
                "Lead.rehab_level + MessageLog.to_address columns.",
                "Scheduler persists last_auto_scrape_date in Settings so restarts don't re-run the daily scrape.",
            ],
            "Changed": [
                "Seed is fully idempotent (safe to re-run across all tables).",
                "Seed data expanded: 65 leads (30 TX + 35 MI), 14 buyers across 5 sourcing channels, 6 title partners, 2 contracts, meetings, message logs, scrape jobs, settings.",
            ],
        },
    },
    {
        "version": "1.6.0",
        "date": "2026-10-05",
        "title": "Pluggable comps provider seam",
        "sections": {
            "Added": [
                "Pluggable comps provider interface — mock provider retained by default; real RentCast/MLS drops in without touching the valuation router.",
            ],
        },
    },
    {
        "version": "1.5.0",
        "date": "2026-10-04",
        "title": "Meeting notifications + area focus",
        "sections": {
            "Added": [
                "Meeting scheduling with SMS reminders fired by the scheduler before each meeting.",
                "Area-focus setting defaults the dial queue to a primary state (MI).",
            ],
        },
    },
    {
        "version": "1.4.0",
        "date": "2026-10-03",
        "title": "Buyer sourcing + title-company partnerships",
        "sections": {
            "Added": [
                "Buyer sourcing channels: network | title_partner | meetup | marketplace | referral.",
                "Title-company partnership tracking — referrals, coverage states, referral fees.",
                "Buy-box buyer matching (top 5) against lead valuation.",
            ],
        },
    },
    {
        "version": "1.3.0",
        "date": "2026-10-02",
        "title": "Contract generation",
        "sections": {
            "Added": [
                "Seller purchase agreement + buyer assignment agreement contract templates.",
                "Fill-from-lead contract generation with honest email send (raises if SMTP unconfigured).",
            ],
        },
    },
    {
        "version": "1.2.0",
        "date": "2026-10-01",
        "title": "Daily auto-scrape scheduler",
        "sections": {
            "Added": [
                "Daily auto-scrape scheduler for county-tax / public-records sources (toggle + hour in Settings).",
            ],
        },
    },
    {
        "version": "1.1.0",
        "date": "2026-09-30",
        "title": "SMS + message templates + dial-queue quick-click",
        "sections": {
            "Added": [
                "SMS via Twilio with template CRUD, manual send, inbound webhook logging.",
                "STOP-keyword auto opt-out for DNC compliance.",
                "Dial-queue no-answer quick-click fires a follow-up SMS and returns the next lead (keep-dialing).",
                "Michigan focus: seeded MI leads + counties.",
            ],
        },
    },
    {
        "version": "1.0.0",
        "date": "2026-09-28",
        "title": "Initial platform",
        "sections": {
            "Added": [
                "End-to-end wholesale acquisitions pipeline: list import, stacking, valuation (MAO), skip trace, click-to-call, contracts.",
                "Server-rendered Jinja2 UI with custom design system.",
                "Free public-domain ArcGIS parcel sources (Harris & Dallas TX, Coconino/Mohave/Maricopa AZ).",
            ],
        },
    },
]
