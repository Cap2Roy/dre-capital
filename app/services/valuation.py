"""Property valuation: ARV via comps, repair estimate, and the MAO formula.

Manual's formula:
    MAO = (ARV × 0.70) − Repairs − Fee

Comps rules (appraiser-style):
  * sold only (not active listings)
  * within 0.5–1 mile, same subdivision / school zone
  * sold in last 3–6 months
  * sqft within 20%
  * use median $/sqft of renovated comparables
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Comp, Lead, Valuation

ARV_FACTOR = 0.70  # manual: (ARV × 0.70) − repairs − fee


def median_price_per_sqft(comps: list[Comp]) -> Optional[float]:
    """Median $/sqft across renovated sold comps with sqft data."""
    ppsfs = [c.price_per_sqft for c in comps
             if c.price_per_sqft and c.price_per_sqft > 0 and c.sqft and c.sqft > 0]
    if not ppsfs:
        # derive from raw price/sqft if missing
        ppsfs = [c.sold_price / c.sqft for c in comps if c.sqft and c.sqft > 0]
    if not ppsfs:
        return None
    return statistics.median(ppsfs)


def compute_arv(lead: Lead, comps: list[Comp]) -> tuple[Optional[float], int]:
    """Return (arv, comp_count_used).  ARV = median $/sqft × lead.sqft.

    Falls back to median sold_price if sqft is unknown.
    """
    if not comps:
        return None, 0
    # Use renovated comps when available, else all
    renovated = [c for c in comps if c.is_renovated]
    use = renovated if len(renovated) >= 3 else comps

    if lead.sqft and lead.sqft > 0:
        ppsf = median_price_per_sqft(use)
        if ppsf:
            return round(ppsf * lead.sqft), len(use)
    # Fallback: median sold price
    prices = [c.sold_price for c in use if c.sold_price]
    if prices:
        return round(statistics.median(prices)), len(use)
    return None, 0


def repair_budget_per_sqft(level: str) -> float:
    """Manual's repair budget table."""
    table = {
        "light": 25,          # paint, flooring, fixtures — $20-30/sqft
        "cosmetic_plus": 45,  # cosmetic + kitchen & baths — $35-55/sqft
        "gut": 85,            # full gut or structural — $70-100+/sqft
    }
    return table.get(level, 25)


def big_five_costs(
    roof: bool = False, hvac: bool = False, foundation: bool = False,
    repipe: bool = False, panel: bool = False,
) -> float:
    """Manual's big-five line items (midpoint of each range)."""
    cost = 0.0
    if roof:
        cost += 11500       # $8-15k
    if hvac:
        cost += 9000        # $6-12k
    if foundation:
        cost += 20000       # $10-30k
    if repipe:
        cost += 11500       # $8-15k
    if panel:
        cost += 5500        # $3-8k
    return cost


def compute_mao(arv: float, repairs: float, fee: float) -> float:
    """MAO = (ARV × 0.70) − Repairs − Fee."""
    return round(arv * ARV_FACTOR - repairs - fee)


def run_valuation(
    db: Session,
    lead: Lead,
    *,
    repair_level: str = "light",
    fee: float = 15000,
    roof: bool = False, hvac: bool = False, foundation: bool = False,
    repipe: bool = False, panel: bool = False,
    notes: str | None = None,
) -> Valuation:
    """Compute and persist a valuation for a lead.

    Uses existing comps on the lead (added via comp import or mock provider).
    """
    comps = db.execute(select(Comp).where(Comp.lead_id == lead.id)).scalars().all()
    arv, comp_count = compute_arv(lead, comps)

    if arv is None:
        # No comps — estimate ARV from assessed value as a rough fallback.
        arv = lead.assessed_value or 0

    sqft = lead.sqft or 0
    repair_cost = repair_budget_per_sqft(repair_level) * sqft + big_five_costs(
        roof=roof, hvac=hvac, foundation=foundation, repipe=repipe, panel=panel
    )

    mao = compute_mao(arv, repair_cost, fee)

    valuation = Valuation(
        lead_id=lead.id,
        arv=arv,
        repair_estimate=round(repair_cost),
        fee=fee,
        mao=mao,
        comp_count=comp_count,
        notes=notes,
    )
    db.add(valuation)
    db.flush()
    return valuation


def mock_comps_for_lead(lead: Lead, count: int = 6) -> list[Comp]:
    """Generate plausible sold comps around a lead for the mock provider.

    In production this is replaced by a real MLS / PropStream API call.
    Here we synthesize comps within ±15% of assessed value so the valuation
    pipeline is exercised end-to-end without external keys.
    """
    import random
    base = lead.assessed_value or 250000
    comps: list[Comp] = []
    for i in range(count):
        sqft = lead.sqft or random.randint(1400, 2400)
        variance = random.uniform(0.85, 1.15)
        sold = round(base * variance / 1000) * 1000
        ppsf = round(sold / sqft, 2)
        days_ago = random.randint(5, 150)
        comps.append(Comp(
            lead_id=lead.id,
            address=f"{100 + i} Mock St, {lead.property_city or 'Houston'}, {lead.property_state or 'TX'}",
            sold_price=sold,
            sqft=sqft,
            beds=lead.beds or random.randint(3, 4),
            baths=lead.baths or float(random.randint(2, 3)),
            sold_date=datetime.now() - timedelta(days=days_ago),
            distance_miles=round(random.uniform(0.2, 0.9), 2),
            price_per_sqft=ppsf,
            is_renovated=random.random() > 0.4,
        ))
    return comps
