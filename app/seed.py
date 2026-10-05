"""Seed script: populate dev DB with realistic Texas wholesale data.

Run:  python -m app.seed

Creates:
  * 1 acquisitions operator user
  * 6 source lists across the seven list types (some overlap → stack depth)
  * ~30 leads in Houston/Dallas with overlapping list memberships
  * Skip-traced phones (some DNC-flagged to demo compliance)
  * Comps + valuations for a few warm leads
  * 5 cash buyers with buy-boxes
  * A couple of contracts to show the full pipeline
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from sqlalchemy import select
from app.database import SessionLocal, init_db

from app.models import (
    Buyer, Call, CallDirection, CallOutcome, Comp, Contract, FollowUp,
    Lead, LeadStatus, ListType, Meeting, MessageChannel, MessageDirection,
    MessageLog, MessageTemplate, Phone, PhoneQuality, ScrapeJob, ScrapeJobStatus,
    ScrapeSource, Setting, SourceList, TitleCompany, User,
)
from app.services.stacking import add_lead_to_list, recompute_stack_depths
from app.services.skiptrace import skip_trace_lead
from app.services.valuation import mock_comps_for_lead, run_valuation
from app.services.buyers import match_buyers_for_lead


STREETS = ["Maple", "Oak", "Cedar", "Pine", "Elm", "Birch", "Walnut", "Magnolia", "Pecan", "Willow",
           "Sunset", "Lakeview", "Highland", "Riverside", "Park", "Hillcrest", "Meadow", "Ash", "Cherry", "Sycamore"]
CITIES = [("Houston", "TX"), ("Dallas", "TX"), ("San Antonio", "TX"), ("Austin", "TX"), ("Fort Worth", "TX")]
MI_CITIES = [("Detroit", "MI"), ("Grand Rapids", "MI"), ("Warren", "MI"), ("Lansing", "MI"), ("Ann Arbor", "MI"),
             ("Sterling Heights", "MI"), ("Flint", "MI"), ("Livonia", "MI"), ("Troy", "MI"), ("Kalamazoo", "MI")]


def _addr(i: int, city: str, state: str = "TX") -> tuple[str, str, str, str]:
    street = STREETS[i % len(STREETS)]
    num = 100 + i * 7
    # 5-digit zip prefix by state (TX ~77001-79999, MI ~48001-49999).
    prefix = "48" if state == "MI" else "77"
    zipc = f"{prefix}{i % 100:03d}"
    return f"{num} {street} St", city, state, zipc


def run() -> None:
    init_db()
    db = SessionLocal()
    try:
        # Users — admin + acquisitions operator with hashed passwords
        from app.services.auth import hash_password
        admin = db.query(User).filter_by(email="admin@dre-capital.com").first()
        if not admin:
            admin = User(email="admin@dre-capital.com", name="Acquisitions Admin", role="admin",
                         password_hash=hash_password("capital2026"))
            db.add(admin)
        user = db.query(User).filter_by(email="ops@dre-capital.com").first()
        if not user:
            user = User(email="ops@dre-capital.com", name="Acquisitions Operator", role="acquisitions",
                         password_hash=hash_password("capital2026"))
            db.add(user)
        db.flush()

        # Source lists — overlapping across list types to create stack depth
        lists_spec = [
            ("Harris Co Pre-Foreclosure Sep 2026", ListType.PRE_FORECLOSURE, "Harris", "TX"),
            ("Harris Co Delinquent Tax Roll", ListType.TAX_DELINQUENT, "Harris", "TX"),
            ("Harris Co Probate Filings", ListType.PROBATE, "Harris", "TX"),
            ("Dallas Co Divorce Filings", ListType.DIVORCE, "Dallas", "TX"),
            ("Harris Co Absentee Owners", ListType.ABSENTEE_OWNER, "Harris", "TX"),
            ("Houston Code Violations", ListType.VACANT_CODE_VIOLATION, "Harris", "TX"),
            ("Wayne Co Pre-Foreclosure", ListType.PRE_FORECLOSURE, "Wayne", "MI"),
            ("Wayne Co Delinquent Tax Roll", ListType.TAX_DELINQUENT, "Wayne", "MI"),
            ("Oakland Co Probate Filings", ListType.PROBATE, "Oakland", "MI"),
            ("Macomb Co Delinquent Tax Roll", ListType.TAX_DELINQUENT, "Macomb", "MI"),
            ("Genesee Co Pre-Foreclosure", ListType.PRE_FORECLOSURE, "Genesee", "MI"),
            ("Kent Co Absentee Owners", ListType.ABSENTEE_OWNER, "Kent", "MI"),
            ("Washtenaw Co Probate Filings", ListType.PROBATE, "Washtenaw", "MI"),
            ("Travis Co Divorce Filings", ListType.DIVORCE, "Travis", "TX"),
            ("Bexar Co Code Violations", ListType.VACANT_CODE_VIOLATION, "Bexar", "TX"),
        ]
        source_lists = []
        for name, ltype, county, state in lists_spec:
            existing = db.execute(select(SourceList).where(SourceList.name == name)).scalars().first()
            sl = existing or SourceList(name=name, list_type=ltype, county=county, state=state)
            if existing is None:
                db.add(sl)
            source_lists.append(sl)
        db.flush()

        # Scraper sources (county tax / public-records feeds the daily auto-scrape runs).
        scrape_sources_spec = [
            ("Harris County Tax Assessor", "https://hcad.org/records-search", "county_tax", "TX", "Harris",
             '{"zip":"77001","min_value":80000,"status":"delinquent"}'),
            ("Detroit Wayne County Treasurer", "https://waynecountytreasurer.com/foreclosure", "county_tax", "MI", "Wayne",
             '{"zip":"48201","min_value":40000,"status":"foreclosure"}'),
            ("Oakland County Register of Deeds", "https://www.oakgov.com/government/register-deeds", "public_api", "MI", "Oakland",
             '{"zip":"48301","min_value":90000,"status":"probate"}'),
        ]
        for name, url, stype, state, county, params in scrape_sources_spec:
            existing = db.execute(select(ScrapeSource).where(ScrapeSource.name == name)).scalars().first()
            if existing is None:
                db.add(ScrapeSource(name=name, url=url, source_type=stype, state=state,
                                    county=county, search_params=params, active=True,
                                    auto_scrape=True))

        # Leads — assign each to 1-4 lists to produce stack depth variety
        random.seed(42)
        leads = []
        for i in range(30):
            city, state = CITIES[i % len(CITIES)]
            addr, city, state, zipc = _addr(i, city)
            existing = db.execute(select(Lead).where(Lead.property_address == addr)).scalars().first()
            if existing:
                leads.append(existing)
                continue
            absentee = i % 3 == 0
            llc = i % 5 == 0
            lead = Lead(
                property_address=addr,
                property_city=city,
                property_state=state,
                property_zip=zipc,
                owner_name=f"Owner {i+1}" + (" LLC" if llc else ""),
                owner_is_llc=llc,
                mailing_address=(f"{200+i} Elsewhere Ln, {city}, {state}" if absentee else addr),
                mailing_city=city if absentee else None,
                mailing_state=state if absentee else None,
                mailing_zip=zipc if absentee else None,
                absentee=absentee,
                beds=random.choice([3, 3, 4, 4, 5]),
                baths=random.choice([2.0, 2.0, 2.5, 3.0]),
                sqft=random.randint(1300, 2800),
                year_built=random.randint(1960, 2005),
                assessed_value=random.randint(140000, 380000),
                taxes_owed=random.choice([0, 0, 0, 2400, 5800, 12000]),
                owner_email=f"owner{i+1}@example.com" if i % 2 == 0 else None,
                rehab_level=random.choice(["light", "cosmetic_plus", "gut"]),
                status=LeadStatus.NEW,
                assigned_to=user.id,
            )
            db.add(lead)
            leads.append(lead)
        db.flush()

        # Michigan leads — focus market (strong buyer network there).
        for i in range(35):
            city, state = MI_CITIES[i % len(MI_CITIES)]
            addr, city, state, zipc = _addr(i + 100, city, state)
            existing = db.execute(select(Lead).where(Lead.property_address == addr)).scalars().first()
            if existing:
                leads.append(existing)
                continue
            absentee = i % 3 == 0
            llc = i % 4 == 0
            lead = Lead(
                property_address=addr,
                property_city=city,
                property_state=state,
                property_zip=zipc,
                owner_name=f"MI Owner {i+1}" + (" LLC" if llc else ""),
                owner_is_llc=llc,
                mailing_address=(f"{300+i} Outstate Rd, {city}, {state}" if absentee else addr),
                mailing_city=city if absentee else None,
                mailing_state=state if absentee else None,
                mailing_zip=zipc if absentee else None,
                absentee=absentee,
                beds=random.choice([3, 3, 4, 4, 5]),
                baths=random.choice([1.5, 2.0, 2.0, 2.5]),
                sqft=random.randint(1100, 2600),
                year_built=random.randint(1940, 2000),
                assessed_value=random.randint(60000, 220000),
                taxes_owed=random.choice([0, 0, 0, 1800, 4200, 9000]),
                owner_email=f"miowner{i+1}@example.com" if i % 3 == 0 else None,
                rehab_level=random.choice(["light", "cosmetic_plus", "gut"]),
                status=LeadStatus.NEW,
                assigned_to=user.id,
            )
            db.add(lead)
            leads.append(lead)
        db.flush()

        # Assign list memberships to create stack depth
        # Houston/Harris leads get 1-4 of the Harris lists; Dallas leads get the divorce list
        for i, lead in enumerate(leads):
            if lead.property_city == "Houston":
                # Pick 1-4 Harris lists for this lead
                harris_lists = [sl for sl in source_lists if sl.county == "Harris"]
                n = random.choice([1, 2, 2, 3, 3, 3, 4])
                chosen = random.sample(harris_lists, min(n, len(harris_lists)))
                for sl in chosen:
                    add_lead_to_list(db, lead, sl)
            elif lead.property_city == "Dallas":
                sl = next(s for s in source_lists if s.list_type == ListType.DIVORCE)
                add_lead_to_list(db, lead, sl)
                # Some Dallas leads also absentee
                if i % 2 == 0:
                    sl2 = next(s for s in source_lists if s.list_type == ListType.ABSENTEE_OWNER)
                    add_lead_to_list(db, lead, sl2)
            elif lead.property_state == "MI":
                # Michigan leads: Wayne or Oakland lists (Detroit/Grand Rapids/Ann Arbor).
                mi_lists = [sl for sl in source_lists if sl.county in ("Wayne", "Oakland")]
                n = random.choice([1, 2, 2, 3])
                for sl in random.sample(mi_lists, min(n, len(mi_lists))):
                    add_lead_to_list(db, lead, sl)
        recompute_stack_depths(db)
        db.flush()

        # Skip trace all leads (mock provider)
        for lead in leads:
            skip_trace_lead(db, lead)
        db.flush()

        # Mark a couple phones as opted-out to demo compliance
        opted = 0
        for lead in leads:
            for phone in lead.phones:
                if phone.dnc_flagged:
                    continue
                if opted < 2 and lead.id == leads[5].id:
                    phone.opted_out = True
                    phone.opted_out_at = datetime.now()
                    phone.quality = PhoneQuality.OPTED_OUT
                    opted += 1
        db.flush()

        # Comps + valuation for the first 8 leads (simulate "warm lead → comp same day")
        for lead in leads[:8]:
            comps = mock_comps_for_lead(lead, count=6)
            for c in comps:
                db.add(c)
            db.flush()
            run_valuation(db, lead, repair_level=random.choice(["light", "cosmetic_plus", "gut"]),
                          fee=15000)
        db.flush()

        # Mark a few leads warm + schedule follow-ups
        for lead in leads[:5]:
            lead.status = LeadStatus.WARM
            lead.next_followup = datetime.now() + timedelta(days=1)
            db.add(FollowUp(lead_id=lead.id, due_at=datetime.now() + timedelta(days=1), reason="Send offer"))
        db.flush()

        # Log a few calls
        for i, lead in enumerate(leads[:6]):
            callable_phones = [p for p in lead.phones if not p.dnc_flagged and not p.opted_out]
            if not callable_phones:
                continue
            phone = callable_phones[0]
            outcome = random.choice(list(CallOutcome)[:6])
            call = Call(
                lead_id=lead.id, phone_id=phone.id, caller_id=user.id,
                direction=CallDirection.OUTBOUND_MANUAL, outcome=outcome,
                attempt_count=1, duration_seconds=random.randint(0, 180),
            )
            db.add(call)
        db.flush()
        # Buyers with buy-boxes. Tuple: (name, company, phone, email, states, cities,
        # min_price, max_price, min_beds, max_beds, rehab, ranking, source)
        buyers_spec = [
            ("Marcus Chen", "Cherokee Holdings", "+17135550001", "marcus@cherokee.com",
             "TX", "Houston,Dallas", 100000, 200000, 3, 4, "gut", 1, "network"),
            ("Sarah Patel", "Patel Property Group", "+17135550002", "sarah@patelpg.com",
             "TX", "Houston", 120000, 250000, 3, 4, "cosmetic_plus", 2, "network"),
            ("James Wright", "Wright Capital", "+17135550003", "james@wrightcap.com",
             "TX", "Houston,San Antonio", 80000, 180000, 3, 5, "light", 3, "network"),
            ("Linda Nguyen", "Nguyen Investments", "+17135550004", "linda@nguyeninv.com",
             "TX", "Dallas,Austin", 150000, 300000, 3, 4, "cosmetic_plus", 4, "network"),
            ("Robert Davis", "Davis Realty Partners", "+17135550005", "rob@davisrp.com",
             "TX", "Houston", 100000, 220000, 3, 5, "gut", 5, "network"),
            ("Janet Kowalski", "Detroit Cash Group", "+13135550001", "janet@detroitcash.com",
             "MI", "Detroit,Warren", 40000, 120000, 3, 5, "gut", 1, "title_partner"),
            ("Michael Novak", "Novak Holdings MI", "+13135550002", "mike@novakmi.com",
             "MI", "Grand Rapids,Lansing", 50000, 150000, 3, 4, "cosmetic_plus", 2, "title_partner"),
            ("Aisha Rahman", "Ann Arbor Investors", "+13135550003", "aisha@aainvest.com",
             "MI", "Ann Arbor", 90000, 220000, 3, 4, "light", 3, "title_partner"),
            # New buyers across more sourcing channels.
            ("David Okafor", "Okafor REI Meetup", "+17135550006", "david@okaforrei.com",
             "TX", "Houston,Austin", 90000, 200000, 3, 4, "cosmetic_plus", 6, "meetup"),
            ("Priya Shah", "Shah Marketplace Buyers", "+17135550007", "priya@shahmb.com",
             "TX", "Fort Worth,Dallas", 110000, 260000, 3, 5, "light", 7, "marketplace"),
            ("Tom Brennan", "Brennan Referral Group", "+13135550004", "tom@brennanref.com",
             "MI", "Detroit,Livonia", 45000, 130000, 3, 4, "gut", 4, "referral"),
            ("Lisa Cho", "Cho Investments Meetup", "+13135550005", "lisa@choinv.com",
             "MI", "Grand Rapids,Kalamazoo", 60000, 170000, 3, 4, "cosmetic_plus", 5, "meetup"),
            ("Hassan Ali", "Ali Cash Buyers Marketplace", "+17135550008", "hassan@alicash.com",
             "TX", "San Antonio,Houston", 70000, 190000, 3, 5, "gut", 8, "marketplace"),
            ("Grace Müller", "Müller Referral Network", "+13135550006", "grace@mueller.net",
             "MI", "Ann Arbor,Troy", 95000, 240000, 3, 4, "light", 6, "referral"),
        ]
        # Title-company partners (a primary buyer-sourcing channel).
        title_partners_spec = [
            ("First American Title - Houston", "Dana Ruiz", "+17135551100", "dana@fat-houston.com", "TX", 500),
            ("Michigan Title Group", "Carl Brooks", "+13135551100", "carl@mititlegroup.com", "MI", 750),
            ("Stewart Title - Wayne County", "Lena Park", "+13135551101", "lena@stewartwayne.com", "MI", 500),
            ("Chicago Title - Dallas", "Marcus Lee", "+17135551102", "marcus@ctdallas.com", "TX", 600),
            ("Oakland County Title Co", "Rita Singh", "+13135551103", "rita@oaklandtitle.com", "MI", 650),
            ("Old Republic - Austin", "Victor Diaz", "+17135551104", "victor@oraustin.com", "TX", 550),
        ]
        title_partners = []
        for name, contact, phone, email, cov, fee in title_partners_spec:
            existing = db.execute(select(TitleCompany).where(TitleCompany.name == name)).scalars().first()
            tc = existing or TitleCompany(name=name, contact_name=contact, phone=phone,
                                           email=email, coverage_states=cov, referral_fee=fee)
            if existing is None:
                db.add(tc)
            title_partners.append(tc)
        db.flush()
        mi_partner = next((t.id for t in title_partners if "Michigan Title" in t.name), title_partners[0].id)
        tx_partner = next((t.id for t in title_partners if "Dallas" in t.name), title_partners[0].id)

        buyers = []
        for name, company, phone, email, states, cities_str, lo, hi, mn_b, mx_b, rehab, rank, source in buyers_spec:
            existing = db.execute(select(Buyer).where(Buyer.email == email)).scalars().first()
            if existing:
                buyers.append(existing)
                continue
            b = Buyer(name=name, company=company, phone=phone, email=email,
                      target_states=states, target_cities=cities_str,
                      min_price=lo, max_price=hi, min_beds=mn_b, max_beds=mx_b,
                      rehab_level=rehab, ranking=rank, source=source)
            # Title-partner buyers link to a title company in their state.
            if source == "title_partner":
                b.title_company_id = mi_partner if states == "MI" else tx_partner
            db.add(b)
            buyers.append(b)
        db.flush()

        # One contract to show the full pipeline (idempotent per lead)
        if leads and leads[0].valuations and not leads[0].contracts:
            val = leads[0].valuations[0]
            matched = match_buyers_for_lead(db, leads[0], val)
            top_buyer = matched[0] if matched else (buyers[0] if buyers else None)
            if top_buyer is not None:
                contract = Contract(
                    lead_id=leads[0].id,
                    buyer_id=top_buyer.id,
                    contract_price=val.mao,
                    assignment_fee=15000,
                    buyer_price=val.mao + 15000,
                    status="signed",
                    signed_at=datetime.now() - timedelta(days=2),
                    disclosure_sent=True,
                    notes="Assignment contract. TX disclosure sent.",
                )
                db.add(contract)
                leads[0].status = LeadStatus.UNDER_CONTRACT

        # A second contract in a different status to show pipeline variety.
        if len(leads) > 4 and leads[4].valuations and buyers and not leads[4].contracts:
            val2 = leads[4].valuations[0] if leads[4].valuations else None
            matched2 = match_buyers_for_lead(db, leads[4], val2) if val2 else []
            buyer2 = matched2[0] if matched2 else buyers[1] if len(buyers) > 1 else buyers[0]
            db.add(Contract(
                lead_id=leads[4].id,
                buyer_id=buyer2.id,
                contract_price=val2.mao if val2 else 120000,
                assignment_fee=12000,
                buyer_price=(val2.mao if val2 else 120000) + 12000,
                status="pending",
                disclosure_sent=False,
                notes="Pending assignment — awaiting seller signature.",
            ))

        # ── Meetings (schedule a few across leads + statuses) ───────────────
        existing_meetings = db.execute(select(Meeting)).scalars().all()
        if not existing_meetings:
            meeting_specs = [
                (leads[1] if len(leads) > 1 else None, "Property walkthrough", timedelta(hours=2), "On site at the property"),
                (leads[2] if len(leads) > 2 else None, "Offer meeting", timedelta(days=1, hours=3), "Coffee shop, Houston"),
                (leads[3] if len(leads) > 3 else None, "Signing appointment", timedelta(days=2), "Title company office"),
            ]
            for lead, title, delta, loc in meeting_specs:
                if lead is None:
                    continue
                db.add(Meeting(
                    lead_id=lead.id,
                    title=title,
                    scheduled_at=datetime.now() + delta,
                    location=loc,
                    notes=f"Auto-seeded meeting for {lead.owner_name}",
                    reminder_minutes=120,
                    reminder_sent=False,
                ))

        # ── Message logs (a few outbound SMS + one inbound STOP) ────────────
        existing_msgs = db.execute(select(MessageLog)).scalars().all()
        if not existing_msgs:
            for lead in leads[:4]:
                callable_phones = [p for p in lead.phones if not p.dnc_flagged and not p.opted_out]
                if not callable_phones:
                    continue
                ph = callable_phones[0]
                db.add(MessageLog(
                    lead_id=lead.id, phone_id=ph.id,
                    channel=MessageChannel.SMS, direction=MessageDirection.OUTBOUND,
                    to_number=ph.number, body="Hi {{ lead.owner_name }}, following up on your property. — DRE Capital",
                    status="sent", sent_by=user.id,
                ))
            # One inbound opt-out reply on a different lead to demo compliance.
            if len(leads) > 6:
                ph_in = next((p for p in leads[6].phones if not p.opted_out), None)
                if ph_in:
                    db.add(MessageLog(
                        lead_id=leads[6].id, phone_id=ph_in.id,
                        channel=MessageChannel.SMS, direction=MessageDirection.INBOUND,
                        from_number=ph_in.number, body="please stop",
                        status="opt-out received",
                    ))

        # ── Scrape jobs (history for the seeded sources) ────────────────────
        sources = db.execute(select(ScrapeSource)).scalars().all()
        existing_jobs = db.execute(select(ScrapeJob)).scalars().all()
        if sources and not existing_jobs:
            for src in sources[:3]:
                db.add(ScrapeJob(
                    source_id=src.id,
                    status=ScrapeJobStatus.COMPLETED,
                    search_params=src.search_params,
                    total_found=random.randint(20, 180),
                    imported=random.randint(5, 40),
                    skipped=random.randint(0, 20),
                    started_at=datetime.now() - timedelta(days=1, hours=random.randint(0, 6)),
                    completed_at=datetime.now() - timedelta(days=1),
                ))
            # One failed job to demo error handling.
            if len(sources) > 1:
                db.add(ScrapeJob(
                    source_id=sources[1].id,
                    status=ScrapeJobStatus.FAILED,
                    search_params=sources[1].search_params,
                    total_found=0, imported=0, skipped=0,
                    error_message="Connection timed out reaching county server (mock seed).",
                    started_at=datetime.now() - timedelta(days=2),
                    completed_at=datetime.now() - timedelta(days=2),
                ))

        # ── Settings rows (sensible defaults, idempotent) ────────────────────
        settings_defaults = [
            ("company_name", "Direct Real Estate Capital LLC", "general", "Company name"),
            ("auto_scrape_enabled", "0", "scraper", "Daily auto-scrape on/off"),
            ("auto_scrape_hour", "6", "scraper", "Hour (0-23) to run daily scrape"),
            ("comps_provider", "mock", "comps", "Active comps provider"),
            ("meeting_reminders_enabled", "1", "meetings", "Send meeting SMS reminders"),
        ]
        for key, value, category, label in settings_defaults:
            if db.get(Setting, key) is None:
                db.add(Setting(key=key, value=value, category=category, label=label))

        db.commit()
        print(f"Seeded: {len(leads)} leads, {len(source_lists)} lists, {len(buyers_spec)} buyers, 2 contracts, meetings, message logs, scrape jobs, settings.")
    finally:
        db.close()


def seed_default_templates(db) -> None:
    """Idempotently seed the out-of-the-box SMS/email message templates."""
    existing = {t.name for t in db.query(MessageTemplate).all()}
    defaults = [
        (MessageChannel.SMS, "no_answer", "No Answer - Follow-up Text",
         None,
         "Hi {{owner_name}}, this is [your name] with DRE-Capital about your property at {{property_address}} in {{property_city}}, {{property_state}}. "
         "Sorry I missed you — call or text me back anytime. Thanks!"),
        (MessageChannel.SMS, "voicemail", "Voicemail Follow-up Text",
         None,
         "Hi {{owner_name}}, I just left a voicemail about {{property_address}}. "
         "If you'd consider a cash offer, reply here or call me back. No pressure."),
        (MessageChannel.SMS, "offer", "Offer Text",
         None,
         "Hi {{owner_name}} — we'd like to make a cash offer on {{property_address}}, {{property_city}}, {{property_state}}. "
         "Can I send the offer over today?"),
        (MessageChannel.EMAIL, "offer", "Offer Email",
         "Cash offer for {{property_address}}",
         "Hi {{owner_name}},\n\nWe'd like to make a cash offer on your property at {{property_address}}, {{property_city}}, {{property_state}} {{property_zip}}. "
         "Please reply with a good time to send the offer over.\n\nThanks,\nDRE-Capital"),
        (MessageChannel.EMAIL, "contract", "Contract Email",
         "Your contract for {{property_address}}",
         "Hi {{owner_name}},\n\nAttached is the contract for {{property_address}}. Please review and reach out with any questions.\n\nThanks,\nDRE-Capital"),
        (MessageChannel.EMAIL, "contract_seller", "Seller Purchase Agreement",
         "Purchase Agreement — {{property_address}}",
         "PURCHASE AND SALE AGREEMENT\n"
         "{{today}}\n\n"
         "SELLER: {{owner_name}}\n"
         "BUYER: {{company_name}}\n\n"
         "PROPERTY: {{property_address}}, {{property_city}}, {{property_state}} {{property_zip}}\n"
         "{{beds}} bed / {{baths}} bath / {{sqft}} sq ft (approx.)\n\n"
         "PURCHASE PRICE: ${{ '{:,.0f}'.format(contract_price) }}\n"
         "EARNEST MONEY: To be deposited with the title company within 3 business days.\n\n"
         "CLOSING: Within 30 days, or as mutually agreed.  This is an assignment — "
         "{{company_name}} may assign its interest in this contract.  Closing subject to clear title.\n\n"
         "SELLER DISCLOSURE: {{company_name}} is an investor purchasing for resale/assignment and "
         "is not representing itself as the end owner of the property.\n\n"
         "Signature (Seller): __________________________\n"
         "Signature (Buyer):  __________________________",
         ),
        (MessageChannel.EMAIL, "contract_buyer", "Buyer Assignment Agreement",
         "Assignment Agreement — {{property_address}}",
         "ASSIGNMENT OF PURCHASE AND SALE AGREEMENT\n"
         "{{today}}\n\n"
         "ASSIGNOR (Seller side): {{company_name}}\n"
         "ASSIGNEE (Cash Buyer): {{buyer_name}}{{ ' (' + buyer_company + ')' if buyer_company else '' }}\n\n"
         "PROPERTY: {{property_address}}, {{property_city}}, {{property_state}} {{property_zip}}\n\n"
         "ASSIGNMENT FEE: ${{ '{:,.0f}'.format(assignment_fee) }}\n"
         "BUYER PURCHASE PRICE: ${{ '{:,.0f}'.format(buyer_price) if buyer_price else contract_price + assignment_fee }}\n\n"
         "The Assignor assigns to the Assignee all right, title and interest in the Purchase and "
         "Sale Agreement for the above property, in consideration of the assignment fee.  "
         "Assignee agrees to close on or before the contract closing date.\n\n"
         "Signature (Assignor):  __________________________\n"
         "Signature (Assignee):   __________________________",
         ),
        (MessageChannel.SMS, "meeting_reminder", "Meeting Reminder Text",
         None,
         "Hi {{owner_name}}, this is a reminder of our meeting tomorrow re: {{property_address}}. "
         "Reply to confirm or reschedule. Thanks — DRE-Capital"),
    ]
    added = 0
    for channel, category, name, subject, body in defaults:
        if name in existing:
            continue
        db.add(MessageTemplate(name=name, channel=channel, category=category,
                                subject=subject, body=body, active=True))
        added += 1
    if added:
        db.commit()
        print(f"Seeded {added} default message templates.")

def ensure_admin() -> None:
    """Production helper: create default users + demo data if DB is empty.

    Creates admin + acquisitions operator users (if not present), then seeds
    demo leads/lists/buyers/contracts if the DB has no leads at all.
    Called by Dockerfile on container startup.
    """
    import os
    from app.services.auth import hash_password

    db = SessionLocal()
    try:
        # Create admin if missing
        admin = db.query(User).filter(User.role == "admin").first()
        if not admin:
            email = os.getenv("ADMIN_EMAIL", "admin@dre-capital.com")
            password = os.getenv("ADMIN_PASSWORD", "capital2026")
            admin = User(
                email=email,
                name="Acquisitions Admin",
                role="admin",
                password_hash=hash_password(password),
            )
            db.add(admin)
            db.commit()
        # Create acquisitions operator if missing
        ops = db.query(User).filter(User.role == "acquisitions").first()
        if not ops:
            ops = User(
                email="ops@dre-capital.com",
                name="Field Operator",
                role="acquisitions",
                password_hash=hash_password("capital2026"),
            )
            db.add(ops)
            db.commit()
            print("Created default acquisitions operator: ops@dre-capital.com")

        # Create the Roy admin account if missing
        roy = db.query(User).filter(User.email == "roy@betterai360.com").first()
        if not roy:
            roy = User(
                email="roy@betterai360.com",
                name="Roy",
                role="admin",
                password_hash=hash_password("capital2026"),
            )
            db.add(roy)
            db.commit()
        # Tester service account (for automated/QA logins)
        tester = db.query(User).filter(User.email == "tester@dre-capital.com").first()
        if not tester:
            tester = User(
                email="tester@dre-capital.com",
                name="Service Tester",
                role="admin",
                password_hash=hash_password(os.getenv("TESTER_PASSWORD", "capital2026")),
            )
            db.add(tester)
            db.commit()
            print("Created service account: tester@dre-capital.com")
            print("Created admin: roy@betterai360.com")

        # Out-of-the-box message templates (independent of lead count).
        seed_default_templates(db)

        # Seed demo data if DB is empty
        lead_count = db.query(Lead).count()
        if lead_count == 0:
            print("DB has no leads — seeding demo data...")
            db.close()
            run()
            seed_default_templates(SessionLocal())
            return
    finally:
        db.close()


if __name__ == "__main__":
    run()
