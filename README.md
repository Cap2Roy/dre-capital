# DRE-Capital — Property Pipeline Platform

**Direct Real Estate Capital** — a wholesale real estate acquisitions pipeline
built for the *Acquisitions Operator Manual* workflow.

Find houses below market, confirm the numbers, get the owner on the phone, and
put the property under contract for assignment to a cash buyer.

## What It Does

The platform encodes the manual's daily loop end-to-end:

| Manual Task | Platform Feature |
|---|---|
| **Build the list** | Import county-record CSVs (appraisal, tax, probate, code violations). Flexible column mapping. **Daily auto-scrape** scheduler for county-tax / public-records sources (toggle + hour in Settings). |
| **Stack the lists** | Dedup by address, count list overlap per property → **stack depth**. 3+ = call today, 2 = this week, 1 = mail only. |
| **Value the house** | Comps engine (median $/sqft of renovated solds) + repair budget table + **MAO formula**: `(ARV × 0.70) − Repairs − Fee`. Pluggable comps provider seam (mock by default; real RentCast/MLS drops in). |
| **Get the phone** | Skip tracing (pluggable provider; mock by default). DNC registry scrub. Opt-out tracking. |
| **Make the call** | Compliant **click-to-call** via **Quo** (opens the Quo app; results sync back) or Twilio — manual-dial only, no autodialer. **Guided calls**: the call flow is adapted to each property — known facts per question, how to ask it, answers from earlier calls pre-filled, live **Hot/Warm/Cold** scoring, script quick-exits (not interested / Do Not Call). Auto-logs + schedules follow-ups. **No-answer quick-click** fires a follow-up SMS and returns the next lead. **Editable call flows** in Settings → Call Flows; seeds a default "DFW Qualification — Sam for Eylon" flow. |
| **Text the owner** | SMS through **Quo** (when Quo is your calling provider — same number you call from) or Twilio — template CRUD, manual send, inbound logging, **STOP-keyword auto opt-out** for DNC compliance (Quo via signed webhook, Twilio via `/api/messages/inbound`). |
| **Rehearse with AI** | **Talk to Sam live** (Google **Gemini Live on Vertex AI**): a real-time voice agent runs your call flow in the browser — you play the seller over your mic, interrupt freely, watch the live transcript, then get the recording, transcript, and an automatic Hot/Warm/Cold score. Uses the app's Google Cloud service account (no API key). Optional **phone test call** via Bland AI (needs a Bland key; lead numbers blocked — AI-voice calls to sellers need prior written consent). |
| **Lock & hand off** | Contract generation — **seller purchase agreement** + **buyer assignment agreement**, fill-from-lead, honest email send (raises if SMTP unconfigured). Buy-box buyer matching — top 5 first. |
| **Source buyers** | Buyer sourcing channels (`network | title_partner | meetup | marketplace | referral`) + **title-company partnership** tracking (referrals, coverage, fees) via TitleCompany partners. |
| **Schedule & remind** | Meeting scheduling + **reminder notifications** (SMS, fired by the scheduler before each meeting). **Area-focus** setting defaults the dial queue to a primary state (MI). |

## Tech Stack

- **Backend**: FastAPI (Python 3.13), SQLAlchemy 2.0 ORM
- **Database**: SQLite (dev) / Cloud SQL Postgres (prod) — same ORM layer
- **Frontend**: Server-rendered Jinja2 + vanilla JS, custom design system
- **Telephony**: Quo (formerly OpenPhone — click-to-call into the Quo app, then sync duration / AI summary / transcript via the Quo API) or Twilio (operator-first bridge). Manual-dial only — the operator talks live, never an autodialer.
- **Deploy**: Docker → Google Cloud Run (single instance, in-container SQLite) + Secret Manager

## Quick Start (Local)

```bash
cd dre-capital
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Seed demo data (Texas wholesale leads, buyers, contracts)
python -m app.seed

# Run
uvicorn app.main:app --reload --port 8000
# Open http://localhost:8000
```

No `.env` needed for local dev — SQLite + mock skip-trace + mock comps work
out of the box. Copy `.env.example` to `.env` to configure Twilio, a real
skip-trace provider, or Cloud SQL Postgres.

## Configuration

| Env Var | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./dre_capital.db` | SQLite (dev) or Cloud SQL Postgres (prod) |
| `SECRET_KEY` | dev default | Session signing — override in prod |
| `TWILIO_ACCOUNT_SID` | (empty) | Twilio click-to-call. Empty = dry-run. |
| `TWILIO_AUTH_TOKEN` | (empty) | Twilio auth |
| `TWILIO_FROM_NUMBER` | (empty) | Caller ID number |
| `APP_BASE_URL` | `http://localhost:8000` | Public URL for Twilio callbacks |
| `SKIPTRACE_PROVIDER` | `mock` | `mock` or real provider key |
| `COMPS_PROVIDER` | `mock` | `mock` or real MLS/PropStream key |

## Deploy to GCP

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
PROJECT_ID=YOUR_PROJECT_ID ./deploy/deploy.sh                       # code-only redeploy
QUO_API_KEY=... PROJECT_ID=YOUR_PROJECT_ID ./deploy/deploy.sh       # set/rotate the company Quo key
BLAND_API_KEY=... PROJECT_ID=YOUR_PROJECT_ID ./deploy/deploy.sh     # set the AI voice agent (Bland) key
```

The script:
1. Enables Cloud Run, Cloud Build, and Secret Manager APIs
2. Creates the session secret once (never rotated by redeploys), and stores the company
   Quo key in Secret Manager when `QUO_API_KEY` is exported
3. Builds the container via Cloud Build (`.gcloudignore` keeps local DBs/secrets out)
4. Deploys to Cloud Run pinned to **one instance**, **updating** env vars — only the
   variables you export change (e.g. `GOOGLE_CLIENT_ID`, `RENTCAST_API_KEY`,
   `TWILIO_*`, `CALLING_PROVIDER`); everything else keeps its current value

**Storage caveat:** the database is SQLite inside the container. It's recreated (with
demo data) on every deploy/restart, so leads, calls, and *personal* Quo keys saved in the
UI don't survive a redeploy. The session key and company Quo key do (Secret Manager).
Moving to Cloud SQL Postgres (`DATABASE_URL=postgresql+psycopg2://…`) makes everything
persistent; the code already supports it.

On start the app re-registers its Quo webhooks (`/api/calls/quo-webhook`, HMAC-verified)
so call results, AI summaries, transcripts, and STOP replies sync automatically.

## Compliance (Hard Rules from the Manual)

- **DNC scrub**: every list is scrubbed against the federal Do-Not-Call registry.
  DNC-flagged numbers are **mail only** — the call button is disabled and a red
  badge appears.
- **Manual-dial only**: Quo click-to-call opens the number in the Quo app and the operator
  presses Call; Twilio rings the operator first. No autodialer, no prerecorded messages.
- **Instant opt-out**: when a seller says stop, the phone is flagged `opted_out`
  immediately and logged. The call button is permanently disabled for that number.
- **State rules**: Texas-specific assignment disclosure is built into the
  contract workflow.

## API

All endpoints under `/api/`. Key routes:

```
GET  /api/health                  — health check
GET  /api/dashboard               — pipeline stats
GET  /api/dashboard/queue          — today's call queue (stack-sorted)
GET  /api/leads                   — list/filter/sort leads
POST /api/leads                   — create lead
GET  /api/leads/{id}              — full lead detail
PATCH /api/leads/{id}             — update status/notes
POST /api/leads/{id}/followups    — schedule follow-up
POST /api/leads/{id}/valuation    — compute ARV + repairs + MAO
POST /api/import/csv              — upload county CSV
GET  /api/import/lists            — list source lists
POST /api/calls/initiate          — click-to-call (compliant)
POST /api/calls/{id}/log          — log outcome + call-guide answers + Hot/Warm/Cold score + auto-followup
GET  /api/calls/provider          — active calling provider (?check=true tests Quo live)
POST /api/calls/{id}/sync         — pull duration, AI summary, transcript from Quo
POST /api/calls/quo-webhook       — Quo events (public, openphone-signature HMAC)
POST /api/calls/quo/webhooks      — enable Quo auto-sync for the company key (admin)
GET  /api/auth/me/quo             — my Quo line status (key never returned)
PUT  /api/auth/me/quo             — save / change / remove my personal Quo key
GET  /api/ai-calls/status         — AI agent configured? (balance + voices)
POST /api/ai-calls                — AI agent calls your number with a call flow (consent required)
GET  /api/ai-calls/{id}           — status, duration, transcript, summary (refreshed from Bland)
POST /api/ai-calls/{id}/listen    — live audio WebSocket URL (PCM16 LE, 16 kHz mono)
POST /api/ai-calls/{id}/stop      — hang up
GET  /api/ai-calls/{id}/recording — stream the recording
WS   /api/live-agent/ws           — live agent call: mic PCM16 16 kHz in, agent PCM16 24 kHz + JSON events out
GET  /api/live-agent/status       — Gemini Live readiness (project, model, voice, voices)
GET  /api/call-flows/guide/{lead_id} — call flow adapted to one property (facts on record, tips, prior answers)
POST /api/call-flows/score        — Hot/Warm/Cold from answers (rubric + valuation)
POST /api/leads/{id}/skiptrace    — run skip trace for a lead
GET  /api/call-flows             — list call flows (filter ?active=true)
GET  /api/call-flows/default     — the default (or most recent active) flow
GET  /api/call-flows/{id}        — one flow
POST /api/call-flows             — create flow (admin)
PATCH /api/call-flows/{id}       — update flow (admin; is_default demotes others)
DELETE /api/call-flows/{id}      — delete flow (admin)
GET  /api/buyers                  — buyer network
GET  /api/buyers/match/{lead_id}  — buy-box matched buyers
POST /api/contracts               — create assignment contract
GET  /api/contracts               — list contracts
```


The in-app **What's New** page (`/whats-new`, sidebar ★) renders the full version log (newest-first) plus a live API reference generated from the registered routes. The version log lives in `app/changelog.py` — append a new entry to `VERSIONS` for each release.

## Project Structure

```
dre-capital/
├── app/
│   ├── main.py              — FastAPI app + page routes
│   ├── config.py            — env-driven settings
│   ├── database.py          — SQLAlchemy engine + session
│   ├── changelog.py          — structured version log (What's New page source)
│   ├── schemas.py           — Pydantic API schemas
│   ├── seed.py              — dev data seeder (TX wholesale leads)
│   ├── routers/             — API endpoints
│   │   ├── dashboard.py     — stats + call queue
│   │   ├── leads.py          — lead CRUD + timeline
│   │   ├── importer.py       — CSV import + stack recompute
│   │   ├── valuation.py      — comps + MAO
│   │   ├── calls.py          — click-to-call + logging
│   │   ├── call_flows.py    — call flow CRUD (qualification scripts)
│   │   ├── buyers.py         — buyer network + matching
│   │   └── contracts.py      — assignment contracts
│   ├── services/             — business logic
│   │   ├── stacking.py       — list stacking engine
│   │   ├── valuation.py      — ARV/MAO computation
│   │   ├── skiptrace.py      — skip trace + DNC compliance
│   │   ├── calling.py        — provider-aware click-to-call (Quo / Twilio) + follow-ups
│   │   ├── quo.py            — Quo API: line detection, call lookup, AI summary, transcript
│   │   ├── call_guide.py     — property-aware call guide + Hot/Warm/Cold scoring
│   │   ├── buyers.py         — buy-box matching
│   │   └── importer.py       — CSV import + dedup
│   ├── templates/            — Jinja2 UI pages (incl. whats_new.html — version log + API reference)
│   └── static/               — CSS design system + JS
├── deploy/deploy.sh          — GCP Cloud Run deploy script
├── Dockerfile
├── requirements.txt
└── .env.example
```

## License

Proprietary — Direct Real Estate Capital (DRE-Capital).
