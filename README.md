# DRE-Capital — Property Pipeline Platform

**Direct Real Estate Capital** — a wholesale real estate acquisitions pipeline
built for the *Acquisitions Operator Manual* workflow.

Find houses below market, confirm the numbers, get the owner on the phone, and
put the property under contract for assignment to a cash buyer.

## What It Does

The platform encodes the manual's daily loop end-to-end:

| Manual Task | Platform Feature |
|---|---|
| **Build the list** | Import county-record CSVs (appraisal, tax, probate, code violations). Flexible column mapping. |
| **Stack the lists** | Dedup by address, count list overlap per property → **stack depth**. 3+ = call today, 2 = this week, 1 = mail only. |
| **Value the house** | Comps engine (median $/sqft of renovated solds) + repair budget table + **MAO formula**: `(ARV × 0.70) − Repairs − Fee`. |
| **Get the phone** | Skip tracing (pluggable provider; mock by default). DNC registry scrub. Opt-out tracking. |
| **Make the call** | Compliant **click-to-call** via Twilio (manual-dial only — no autodialer). Auto-logs + schedules follow-ups. |
| **Lock & hand off** | Contract creation with TX assignment disclosure. Buy-box buyer matching — top 5 first. |

## Tech Stack

- **Backend**: FastAPI (Python 3.13), SQLAlchemy 2.0 ORM
- **Database**: SQLite (dev) / Cloud SQL Postgres (prod) — same ORM layer
- **Frontend**: Server-rendered Jinja2 + vanilla JS, custom design system
- **Telephony**: Twilio (click-to-call bridge — operator talks live, not an autodialer)
- **Deploy**: Docker → Google Cloud Run + Cloud SQL Postgres + Secret Manager

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
./deploy/deploy.sh
```

The script:
1. Enables Cloud Run, SQL Admin, Secret Manager, Build APIs
2. Creates a Cloud SQL Postgres instance (`db-f1-micro`, Postgres 16)
3. Stores the DB URL + secret key in Secret Manager
4. Builds the container via Cloud Build
5. Deploys to Cloud Run with Cloud SQL connection + secrets

After deploy, add Twilio credentials for live calling:
```bash
gcloud run services update dre-capital --region us-central1 \
  --set-env-vars TWILIO_ACCOUNT_SID=AC...,TWILIO_AUTH_TOKEN=...,TWILIO_FROM_NUMBER=+1...
```

## Compliance (Hard Rules from the Manual)

- **DNC scrub**: every list is scrubbed against the federal Do-Not-Call registry.
  DNC-flagged numbers are **mail only** — the call button is disabled and a red
  badge appears.
- **Manual-dial only**: the system uses Twilio click-to-call (operator clicks once
  per call, talks live). No autodialer, no prerecorded messages, no power dialer.
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
POST /api/calls/{id}/log          — log call outcome + auto-followup
GET  /api/buyers                  — buyer network
GET  /api/buyers/match/{lead_id}  — buy-box matched buyers
POST /api/contracts               — create assignment contract
GET  /api/contracts               — list contracts
```

## Project Structure

```
dre-capital/
├── app/
│   ├── main.py              — FastAPI app + page routes
│   ├── config.py            — env-driven settings
│   ├── database.py          — SQLAlchemy engine + session
│   ├── models.py            — domain models (Lead, Phone, Comp, Call, …)
│   ├── schemas.py           — Pydantic API schemas
│   ├── seed.py              — dev data seeder (TX wholesale leads)
│   ├── routers/             — API endpoints
│   │   ├── dashboard.py     — stats + call queue
│   │   ├── leads.py          — lead CRUD + timeline
│   │   ├── importer.py       — CSV import + stack recompute
│   │   ├── valuation.py      — comps + MAO
│   │   ├── calls.py          — click-to-call + logging
│   │   ├── buyers.py         — buyer network + matching
│   │   └── contracts.py      — assignment contracts
│   ├── services/             — business logic
│   │   ├── stacking.py       — list stacking engine
│   │   ├── valuation.py      — ARV/MAO computation
│   │   ├── skiptrace.py      — skip trace + DNC compliance
│   │   ├── calling.py        — Twilio click-to-call + follow-ups
│   │   ├── buyers.py         — buy-box matching
│   │   └── importer.py       — CSV import + dedup
│   ├── templates/            — Jinja2 UI pages
│   └── static/               — CSS design system + JS
├── deploy/deploy.sh          — GCP Cloud Run deploy script
├── Dockerfile
├── requirements.txt
└── .env.example
```

## License

Proprietary — Direct Real Estate Capital (DRE-Capital).
