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

from app.database import SessionLocal, init_db
from app.models import (
    Buyer, Call, CallDirection, CallOutcome, Comp, Contract, FollowUp,
    Lead, LeadStatus, ListType, Phone, PhoneQuality, SourceList, User,
)
from app.services.stacking import add_lead_to_list, recompute_stack_depths
from app.services.skiptrace import skip_trace_lead
from app.services.valuation import mock_comps_for_lead, run_valuation
from app.services.buyers import match_buyers_for_lead


STREETS = ["Maple", "Oak", "Cedar", "Pine", "Elm", "Birch", "Walnut", "Magnolia", "Pecan", "Willow",
           "Sunset", "Lakeview", "Highland", "Riverside", "Park", "Hillcrest", "Meadow", "Ash", "Cherry", "Sycamore"]
CITIES = [("Houston", "TX"), ("Dallas", "TX"), ("San Antonio", "TX"), ("Austin", "TX"), ("Fort Worth", "TX")]


def _addr(i: int, city: str) -> tuple[str, str, str, str]:
    street = STREETS[i % len(STREETS)]
    num = 100 + i * 7
    zipc = f"7700{i % 9:02d}"
    return f"{num} {street} St", city, "TX", zipc


def run() -> None:
    init_db()
    db = SessionLocal()
    try:
        # User
        user = db.query(User).filter_by(email="ops@dre-capital.com").first()
        if not user:
            user = User(email="ops@dre-capital.com", name="Acquisitions Operator", role="acquisitions")
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
        ]
        source_lists = []
        for name, ltype, county, state in lists_spec:
            sl = SourceList(name=name, list_type=ltype, county=county, state=state)
            db.add(sl)
            source_lists.append(sl)
        db.flush()

        # Leads — assign each to 1-4 lists to produce stack depth variety
        random.seed(42)
        leads = []
        for i in range(30):
            city, state = CITIES[i % len(CITIES)]
            addr, city, state, zipc = _addr(i, city)
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

        # Buyers with buy-boxes
        buyers_spec = [
            ("Marcus Chen", "Cherokee Holdings", "+17135550001", "marcus@cherokee.com",
             "TX", "Houston,Dallas", 100000, 200000, 3, 4, "gut", 1),
            ("Sarah Patel", "Patel Property Group", "+17135550002", "sarah@patelpg.com",
             "TX", "Houston", 120000, 250000, 3, 4, "cosmetic_plus", 2),
            ("James Wright", "Wright Capital", "+17135550003", "james@wrightcap.com",
             "TX", "Houston,San Antonio", 80000, 180000, 3, 5, "light", 3),
            ("Linda Nguyen", "Nguyen Investments", "+17135550004", "linda@nguyeninv.com",
             "TX", "Dallas,Austin", 150000, 300000, 3, 4, "cosmetic_plus", 4),
            ("Robert Davis", "Davis Realty Partners", "+17135550005", "rob@davisrp.com",
             "TX", "Houston", 100000, 220000, 3, 5, "gut", 5),
        ]
        buyers = []
        for name, company, phone, email, states, cities_str, lo, hi, mn_b, mx_b, rehab, rank in buyers_spec:
            b = Buyer(name=name, company=company, phone=phone, email=email,
                      target_states=states, target_cities=cities_str,
                      min_price=lo, max_price=hi, min_beds=mn_b, max_beds=mx_b,
                      rehab_level=rehab, ranking=rank)
            db.add(b)
            buyers.append(b)
        db.flush()

        # One contract to show the full pipeline
        if leads and leads[0].valuations:
            val = leads[0].valuations[0]
            matched = match_buyers_for_lead(db, leads[0], val)
            top_buyer = matched[0] if matched else buyers[0]
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

        db.commit()
        print(f"Seeded: {len(leads)} leads, {len(source_lists)} lists, {len(buyers_spec)} buyers, 1 contract.")
    finally:
        db.close()


if __name__ == "__main__":
    run()
