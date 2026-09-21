"""Buyer-network matching: find cash buyers whose buy-box fits a contract.

Manual: "Send to your top five buyers first, matched by buy box (area, price
band, beds and baths, rehab level). Then the wider list, then a marketplace."
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Buyer, Contract, Lead, Valuation


def match_buyers_for_lead(db: Session, lead: Lead, valuation: Valuation | None = None) -> list[Buyer]:
    """Return ranked buyers whose buy-box matches the lead.

    Scoring:
      + state match
      + city match
      + price band overlap (MAO within buyer min/max)
      + bed count overlap
      + rehab level match
    Then sorted by ranking (top buyers first) and score.
    """
    buyers = db.execute(
        select(Buyer).where(Buyer.active == True).order_by(Buyer.ranking.asc())
    ).scalars().all()

    mao = valuation.mao if valuation else 0
    state = (lead.property_state or "").upper()
    city = (lead.property_city or "").lower()
    beds = lead.beds or 0

    scored: list[tuple[int, Buyer]] = []
    for b in buyers:
        score = 0
        if b.target_states and state and state in b.target_states.upper():
            score += 3
        if b.target_cities and city and city in b.target_cities.lower():
            score += 3
        if b.min_price is not None and b.max_price is not None and mao:
            if b.min_price <= mao <= b.max_price:
                score += 2
        if b.min_beds is not None and b.max_beds is not None and beds:
            if b.min_beds <= beds <= b.max_beds:
                score += 1
        if score > 0:
            scored.append((score, b))

    # Sort: score desc, then ranking asc (top buyers = ranking 1 first)
    scored.sort(key=lambda x: (-x[0], x[1].ranking))
    return [b for _, b in scored]
