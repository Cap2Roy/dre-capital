"""CSV list import.

Imports county-record CSV exports into source lists + leads, then recomputes
stack depths.  Accepts flexible column mapping via a header alias map so the
operator can drop in a county appraisal-district or tax-delinquent CSV without
renaming columns.
"""
from __future__ import annotations

import csv
import io
from typing import Iterable

from sqlalchemy.orm import Session

from app.models import Lead, ListType, SourceList
from app.services.stacking import (
    add_lead_to_list, get_or_create_lead, recompute_stack_depths,
)

# Flexible header aliases → canonical field
COLUMN_ALIASES = {
    "property_address": ["property_address", "address", "prop_address", "site_address", "property"],
    "property_city": ["property_city", "city", "prop_city"],
    "property_state": ["property_state", "state", "st", "prop_state"],
    "property_zip": ["property_zip", "zip", "zip_code", "zipcode", "prop_zip"],
    "owner_name": ["owner_name", "owner", "owner_of_record", "name", "grantee"],
    "mailing_address": ["mailing_address", "mail_address", "owner_mailing_address"],
    "mailing_city": ["mailing_city", "mail_city"],
    "mailing_state": ["mailing_state", "mail_state"],
    "mailing_zip": ["mailing_zip", "mail_zip"],
    "beds": ["beds", "bedrooms", "bed", "br"],
    "baths": ["baths", "bathrooms", "bath", "ba"],
    "sqft": ["sqft", "square_feet", "living_sqft", "gla", "living_area"],
    "year_built": ["year_built", "yr_built", "year", "yearblt"],
    "assessed_value": ["assessed_value", "appraised_value", "value", "assessed"],
    "taxes_owed": ["taxes_owed", "delinquent_tax", "tax_owed", "taxes_delinquent", "balance"],
    "owner_is_llc": ["owner_is_llc", "is_llc", "llc"],
}


def _build_header_map(headers: list[str]) -> dict[str, str]:
    """Map canonical field → actual CSV header name."""
    norm_headers = {h.lower().strip(): h for h in headers}
    mapping = {}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in norm_headers:
                mapping[field] = norm_headers[alias]
                break
    return mapping


def parse_bool(val: str) -> bool:
    return str(val).strip().lower() in ("yes", "true", "1", "y", "llc")


def parse_int(val: str) -> int | None:
    try:
        return int(float(str(val).replace(",", "").strip())) if str(val).strip() else None
    except (ValueError, TypeError):
        return None


def parse_float(val: str) -> float | None:
    try:
        return float(str(val).replace(",", "").replace("$", "").strip()) if str(val).strip() else None
    except (ValueError, TypeError):
        return None


def import_csv(
    db: Session,
    *,
    csv_text: str,
    list_name: str,
    list_type: ListType,
    county: str | None = None,
    state: str | None = None,
) -> SourceList:
    """Import a CSV of county records into a new source list + leads.

    Returns the SourceList.  Idempotent on address: re-importing the same
    address adds it to the new list without duplicating the lead.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    headers = reader.fieldnames or []
    hmap = _build_header_map(headers)

    if "property_address" not in hmap:
        raise ValueError("CSV must contain a property address column (address / property_address / site_address).")

    source_list = SourceList(
        name=list_name,
        list_type=list_type,
        county=county,
        state=state,
    )
    db.add(source_list)
    db.flush()

    added = 0
    affected_lead_ids: set[str] = set()
    for row in reader:
        addr = row.get(hmap["property_address"], "").strip()
        if not addr:
            continue

        lead, created = get_or_create_lead(
            db,
            property_address=addr,
            property_zip=(row.get(hmap.get("property_zip", ""), "") or None),
            property_city=(row.get(hmap.get("property_city", ""), "") or None),
            property_state=(row.get(hmap.get("property_state", ""), "") or state),
            owner_name=(row.get(hmap.get("owner_name", ""), "") or None),
            mailing_address=(row.get(hmap.get("mailing_address", ""), "") or None),
            mailing_city=(row.get(hmap.get("mailing_city", ""), "") or None),
            mailing_state=(row.get(hmap.get("mailing_state", ""), "") or None),
            mailing_zip=(row.get(hmap.get("mailing_zip", ""), "") or None),
            beds=parse_int(row.get(hmap.get("beds", ""), "")) if "beds" in hmap else None,
            baths=parse_float(row.get(hmap.get("baths", ""), "")) if "baths" in hmap else None,
            sqft=parse_int(row.get(hmap.get("sqft", ""), "")) if "sqft" in hmap else None,
            year_built=parse_int(row.get(hmap.get("year_built", ""), "")) if "year_built" in hmap else None,
            assessed_value=parse_float(row.get(hmap.get("assessed_value", ""), "")) if "assessed_value" in hmap else None,
            taxes_owed=parse_float(row.get(hmap.get("taxes_owed", ""), "")) if "taxes_owed" in hmap else None,
            owner_is_llc=parse_bool(row.get(hmap.get("owner_is_llc", ""), "")) if "owner_is_llc" in hmap else False,
        )
        if add_lead_to_list(db, lead, source_list):
            added += 1
        affected_lead_ids.add(lead.id)

    source_list.record_count = added
    recompute_stack_depths(db, affected_lead_ids)
    db.commit()
    return source_list
