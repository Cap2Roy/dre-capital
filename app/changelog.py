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

CURRENT_VERSION = "2.5.0"

VERSIONS: list[dict] = [
    {
        "version": "2.5.0",
        "date": "2026-10-21",
        "title": "Talk to Sam live — Google Gemini Live voice agent",
        "sections": {
            "Added": [
                "AI Test Call → 'Talk to Sam live': a real-time AI voice agent (Gemini Live on Vertex AI) runs your call flow in the browser. You play the seller over your microphone; the agent answers in natural speech, follows the questions in order, and stops talking when you interrupt.",
                "Live transcript as you talk, mic level meter, mute, and end call; 30 voices to choose from; optional property to rehearse (uses that lead's facts on record, never our offer numbers).",
                "After the call: one recording with both voices (seekable), the full transcript, and an automatic Hot/Warm/Cold score with reasons, agreed callback time, and next steps based on the flow's rubric.",
                "Settings → Live AI Agent (Google Gemini Live · Vertex AI): model, default voice, region, and a readiness check. Runs on the app's Google Cloud service account — no API key needed.",
            ],
            "Changed": [
                "The Bland AI phone test call stays available below the live agent for when a Bland key is added.",
            ],
            "Fixed": [
                "Browsers kept old styles/scripts after an update — static files are now versioned per release.",
            ],
        },
    },
    {
        "version": "2.4.0",
        "date": "2026-10-21",
        "title": "AI Test Call — hear the agent speak your call flow",
        "sections": {
            "Added": [
                "AI Test Call page: an AI voice agent (Bland AI) calls a number you enter and speaks a call flow — e.g. Sam for Eylon — holding the conversation like the real thing.",
                "Listen live from the browser while the call happens, hang up from the CRM, then replay the recording with the transcript, summary, who answered, and cost.",
                "Optionally rehearse a specific property: the agent uses that lead's address and facts on record (never our ARV/MAO numbers) while still calling your number.",
                "Settings → AI Voice Agent: Bland API key (masked, or BLAND_API_KEY env/Secret Manager), default voice, Test Key with balance + voice list.",
                "Guardrails: AI test calls require confirming you own the number, numbers belonging to CRM leads are blocked (AI-voice calls to sellers need prior written consent), one active call at a time, 20 per day.",
            ],
            "Fixed": [
                "Non-admin users had no way to reach Settings → Profile (My Quo Line); the sidebar now shows 'My Settings' to everyone.",
            ],
        },
    },
    {
        "version": "2.3.0",
        "date": "2026-10-21",
        "title": "Personal Quo lines, Quo texting, auto-sync, correct times",
        "sections": {
            "Added": [
                "My Quo Line (Settings → Profile): every user can add their own Quo API key to call and text from their own Quo number. The key is verified with Quo before saving and never shown again; without one, the company line is used.",
                "Texts (manual, no-answer follow-ups, meeting reminders) go out through Quo when Quo is your calling provider, so sellers see the same number you call from.",
                "Quo auto-sync: signed Quo webhooks fill in call duration, AI summary, and transcript automatically after you hang up, and STOP replies to Quo texts opt the number out. Re-arms itself on every restart; 'Enable Auto-Sync' button in Settings.",
                "Add Lead form now captures owner email and rehab level.",
            ],
            "Fixed": [
                "Dates and times were off by several hours — every timestamp is now stored in UTC and shown in your local time.",
                "Production deploys no longer wipe environment settings or log everyone out; the company Quo key lives in Secret Manager; the service runs a single instance so every user sees the same data.",
                "The API's reported version no longer said 2.0.0.",
            ],
        },
    },
    {
        "version": "2.2.0",
        "date": "2026-10-21",
        "title": "Quo calling + property-aware guided calls",
        "sections": {
            "Added": [
                "Quo (formerly OpenPhone) as a calling provider: ☎ Call opens the number in the Quo app (tel: link) — still manual-dial — and 'Sync from Quo' pulls the call's duration, Quo AI summary + next steps, and transcript into the CRM.",
                "Settings → Calling & API Keys → Quo card: provider (Auto / Quo / Twilio), API key (masked, or QUO_API_KEY env), phone-number ID auto-detection, live 'Test Connection'.",
                "Guided Call: the call flow adapted to each property — placeholders filled from the lead, every question annotated with what's on record (county data, list memberships, taxes owed, valuation kept internal) plus how to ask it (Confirm / Ask gently / Optional).",
                "Answers from earlier calls pre-fill the matching questions so the seller is never asked twice; the close line picks up the agreed callback time.",
                "Live Hot/Warm/Cold scoring from the answers using the flow's rubric (asking price checked against ARV); operator can override. Score + answers are saved on the call.",
                "Script quick-exits: 'Never inquired / not interested', 'Wrong person', and 'Stop calling → Do Not Call' (opts the number out).",
                "Call history shows score, provider, and expandable answers / AI summary / transcript; per-call 'Sync' for Quo calls.",
                "API: GET /api/calls/provider, POST /api/calls/{id}/sync, GET /api/call-flows/guide/{lead_id}, POST /api/call-flows/score, POST /api/leads/{id}/skiptrace.",
            ],
            "Fixed": [
                "Add Lead always failed with a 500 (owner_email / rehab_level not accepted by the lead service).",
                "Lead detail and Comps showed the oldest valuation/contract after a re-value — now newest first.",
                "Contracts page couldn't link to the lead and always said 'Unassigned' — contracts now include lead, address, and buyer.",
                "Call-flow editor put every question on one line (literal \\n) and saved multi-line questions as one.",
                "Stored XSS: user, lead, buyer, and call-note text is now escaped everywhere it's rendered; user data no longer goes into inline onclick handlers.",
                "'Skip Trace Now' / 'Re-trace' did nothing — now run the skip trace.",
                "Editing a scrape source silently turned off its daily auto-scrape.",
                "'Mark Signed' never recorded the signed date.",
                "A blank contract price created a $0 contract.",
                "Logging a call erased the provider-synced duration when the duration field was left blank.",
                "Leads page: MI missing from the state filter, 'dead' missing from the status filter, and the table never loaded if the queue count failed.",
                "Call-queue badge showed '—' on most pages; Leads nav not highlighted on lead detail; avatar initials rendered as 'A A'; contract statuses pending/signed/fallen showed as raw text.",
            ],
        },
    },
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
