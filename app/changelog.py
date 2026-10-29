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

CURRENT_VERSION = "3.0.0"

VERSIONS: list[dict] = [
    {
        "version": "3.0.0",
        "date": "2026-10-29",
        "title": "Real data: Properties, government sale scans, Scan property, a permanent database",
        "sections": {
            "Added": [
                "Permanent database: production now runs on Cloud SQL PostgreSQL (private network, daily backups) — properties, calls, and settings survive deploys. Demo data is gone; production holds only real properties.",
                "Government sale sources on the daily scan: U.S. Treasury seized real property, IRS seized real estate, USDA Rural Development / FSA, FDIC failed-bank listings (found via USA.gov's real-estate sales directory), and Tarrant County TX Constable Pct 3 delinquent tax sales. Each property carries its sale date, sale/case/account number, minimum bid or list price, and a link to its listing. New results are added to Properties automatically after each daily scan (archived ones stay archived). HUD Home Store and GSA real-estate sales can't be scanned — their sites forbid automated access.",
                "Scan property: a '🔎 Scan property' button on every property researches it from the county assessor and tax records, sales history, sheriff/foreclosure sales (official county lists), and listing sites — every fact with its source link and a check that the cited page really says it. Uses Grok web search, RentCast, and any data provider you enable.",
                "Settings → Property data sources: turn research sources on or off and add keys for ATTOM, Regrid, BatchData, PropertyRadar, RealEstateAPI, and Realie; each has a Test button. Choose the Grok model used for research.",
                "Edit, archive, and delete properties: edit any property and owner detail; archive a property (with a reason) to hide it from Properties, the call queue, and the dashboard; restore it or delete it permanently from 🗄 Archived. Properties with contracts can only be archived.",
                "New list type: Government sale / REO.",
            ],
            "Changed": [
                "Leads are now called Properties (menu, pages, and links — old /leads links still work).",
                "The daily scan runs on a schedule managed by Google Cloud (06:00 Central) so it no longer depends on the app being busy, and a long scan can't slow the app down.",
            ],
            "Fixed": [
                "Signing in works on any HTTPS deployment regardless of database type.",
                "Importing a list that has the same property twice (e.g. on two sale dates) no longer fails partway; the repeat is counted as skipped.",
            ],
        },
    },
    {
        "version": "2.9.0",
        "date": "2026-10-29",
        "title": "Twilio numbers for agents, Quo Sona calls, Twilio by default",
        "sections": {
            "Added": [
                "Twilio API keys: AI calling agents use a dedicated Twilio API key (SK…) — the account's main key never goes into the app. Agents can also have their own Twilio account + API key.",
                "Phone numbers (Agents page, admins): see the account's numbers and which agent uses each, search by area code and buy a number for an agent (about $1.15/month), or verify a number you already own — like your Quo line — as the caller ID. Sellers who call an agent's number back are connected to your Quo line.",
                "Quo calling option: a 'Quo Sona' calling agent has Quo's AI agent place the call from your Quo number. Connect once with 'Connect Quo' (sign in to Quo and approve); the call flow is condensed into Sona's task and Sona's summary is logged on the lead like any AI call (wrong numbers marked bad, stop requests honored). The connection survives deploys.",
                "Phone calls default to Twilio + Grok Voice: with Twilio and xAI keys configured, 'Sam (Grok Voice)' is created as the default calling agent. Quo Sona, Gemini Live and Bland are opt-in per agent.",
            ],
            "Fixed": [
                "Error messages across the app show the reason instead of raw JSON ({\"detail\": …}).",
                "Default keys shows a Twilio key that's set but still needs a caller ID number, instead of 'Not set'.",
            ],
        },
    },
    {
        "version": "2.8.0",
        "date": "2026-10-29",
        "title": "Grok (xAI) across agents and calls; complete, verified scraping",
        "sections": {
            "Added": [
                "xAI Grok as an agent provider, with a default xAI key (Agents → Default keys, or XAI_API_KEY at deploy). Models were picked by benchmarking real data: Grok 4.20 (non-reasoning) for scraping — it read all 170 of 170 properties on a live sheriff-sale list with nothing invented, for about 9¢ — and Grok 4.7 for writing call scripts (best-quality drafts in about 30 s).",
                "Grok agents are set up automatically: when an xAI key is configured and no scraping or script-writing agent exists yet, 'Grok Scout' and 'Grok Script Writer' are created as the defaults (tagged default key).",
                "Calling engine: Twilio + Grok Voice. Phone audio goes straight to Grok (G.711 μ-law, no conversion). The caller can interrupt, the agent hangs up by itself on a wrong number, a stop request, voicemail, or when done, and you get the recording, transcript, score, and live listen. Choose it per calling agent, alongside Gemini Live and Bland.",
                "Scraped foreclosure, tax, and sheriff-sale lists now capture the sale date, case/sheriff number, plaintiff (lender), and amount owed, and keep them on the lead when imported.",
            ],
            "Fixed": [
                "Scraping agents read whole pages: long lists are read in sections in parallel instead of stopping at 40 properties (a 170-property list returned 40 before; now 170).",
                "A scrape job's 'Imported' and 'Skipped' counts now update when you import its results.",
                "Scraping a page with amounts like '$6,812.44' no longer fails with 'could not convert string to float'.",
                "Sources without an agent use Grok for parsing and scoring when the xAI key is set and no other LLM is configured.",
            ],
        },
    },
    {
        "version": "2.7.0",
        "date": "2026-10-29",
        "title": "Agents: AI that calls from your script, scrapes lists, and writes scripts",
        "sections": {
            "Added": [
                "Agents page: add calling, scraping, and script-writing agents. Give each one its own API key, or tag it 'default key' to use the shared key for its provider. Pick a model and add your own instructions. Each kind has a default agent, and a Test button checks the key.",
                "Default keys (admin): one shared key per provider — Twilio, Bland AI, OpenAI, Anthropic Claude, OpenAI-compatible (Groq, Together, Ollama…). Gemini runs on the app's Google Cloud account and needs no key. Keys are never shown again after saving.",
                "Calling module: a calling agent places a real phone call through Twilio and speaks the call flow with a Gemini Live voice. The caller can interrupt naturally. The agent hangs up by itself on a wrong number, a stop request, voicemail, or when it's done (it never leaves messages). Answering machines are detected and hung up on. You get the recording, transcript, Hot/Warm/Cold score, and 'Listen live' with both voices. Bland AI remains available as a provider, using the agent's own key or the default.",
                "Choose the calling agent on AI Test Call and in the lead's '🤖 AI call' window. Calls to owners still require every number check (owner match, consent, 8 am–9 pm, not DNC) and are logged on the lead afterward.",
                "Scraping agents: point one at a web page (county tax-sale list, sheriff sale, listings), describe what you're after, and it reads the page and follows the next pages (up to its page limit). It pulls each property with its owner, amounts owed, and motivated-seller signals, scores it 0–100, and shows the results in the Scraper for import. It respects robots.txt and crawl delays. Saved sources can use an agent too ('AI agent' source type, or an agent to read any HTML source).",
                "Script-writing agents: '✨ Write with agent' drafts a new call flow from a short brief and list type. '✨ Improve' rewrites an existing flow using its call history (questions sellers often leave unanswered, outcome and score mix). Drafts open in the editor and keep the compliance rules — nothing is saved until you click Create.",
            ],
        },
    },
    {
        "version": "2.6.0",
        "date": "2026-10-29",
        "title": "Property data on every lead, MLS listings, call brief, verified AI calls",
        "sections": {
            "Added": [
                "Property card on every lead: estimated value with range, assessed value, annual property tax (with history), taxes owed, estimated equity, MLS listing status (price, days on market, MLS number, listing agent), owner of public record, and a photo of the house (Google Street View, satellite when there's none). 'Refresh property data' pulls it from RentCast (3 calls).",
                "Debt on the house: delinquent taxes and pre-foreclosure filings from county lists, what the seller said about their mortgage on a call, and an estimated mortgage balance from the last sale (clearly labeled as an estimate, not a recorded lien).",
                "MLS listings in the scraper: 'MLS Active Listings' and 'MLS Stale Listings (90+ days)' sources via RentCast's MLS-fed listings; imported leads keep their MLS number, price, days on market, and agent. (mls.com has no data of its own — it links out to agent sites and a paid foreclosure site.)",
                "Call brief: the guided-call screen and AI Test Call show the property (photo, value, tax, debt, MLS status) and the number check before you dial; the AI agent's instructions now include the property's facts (never dollar figures).",
                "Number verification for every phone: belongs to this lead, not DNC / opted out, never reported as a wrong number, owner confirmed on a call, owner matches public records (catches numbers of previous owners), and 8 am–9 pm at the property.",
                "AI calls to the owner: '🤖 AI call' appears only on numbers that pass every check — including written consent to AI calls recorded on the lead (TCPA). The agent first confirms it reached the right person about the right property, and ends the call on a wrong number or stop request. Afterward the call is logged on the lead and the number is marked confirmed, wrong (bad), or opted out.",
                "Human calls teach the system too: answering 'yes' to the owner question confirms the number; a wrong-number outcome marks it bad.",
                "Settings → Property Data: RentCast key, monthly call budget with usage, Google Maps key, and a test.",
            ],
        },
    },
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
