"""Duplicate detection.

Identity is expressed as a single ``dedupe_key`` string with a namespace
prefix, strongest signal first:

===============  ==========================================  ==================
key form         meaning                                     confidence
===============  ==========================================  ==================
``tax:12345675`` verified unified business number            exact
``mail:a@b.com`` published contact address                   exact
``np:key|0212``  normalized name + phone                     high
``nw:key|b.com`` normalized name + website host              high
``n:key``        normalized name alone                       moderate
``raw:<text>``   nothing normalizable; never merges          none
===============  ==========================================  ==================

The key is stored on the row and uniquely indexed, so
:meth:`~database.repository.CompanyRepository.upsert` gets duplicate detection
from the database rather than from an N^2 scan.
"""

from __future__ import annotations

from collections.abc import Iterable

from core.schemas import RawCompany
from verifier.normalize import (
    company_name_key,
    normalize_email,
    normalize_phone,
    normalize_tax_id,
    website_host,
)
from verifier.validators import is_valid_email, is_valid_tax_id


def build_dedupe_key(
    company_name: str | None,
    tax_id: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    website: str | None = None,
) -> str:
    """Strongest identity key derivable from the supplied fields.

    Inputs may be raw or normalized; they are normalized again here so the key
    never depends on the caller having done it first.
    """
    tax = normalize_tax_id(tax_id)
    if tax and is_valid_tax_id(tax):
        return f"tax:{tax}"

    address = normalize_email(email)
    if address and is_valid_email(address):
        return f"mail:{address}"

    name_key = company_name_key(company_name)
    if name_key:
        digits = normalize_phone(phone)
        if digits:
            return f"np:{name_key}|{digits}"
        host = website_host(website)
        if host:
            return f"nw:{name_key}|{host}"
        return f"n:{name_key}"

    # Nothing identifiable. Fall back to the raw text so the row is still
    # storable, and so two such rows only merge when byte-identical.
    fallback = (company_name or "").strip().lower()
    return f"raw:{fallback}" if fallback else ""


def key_confidence(dedupe_key: str) -> str:
    """Human-readable confidence of a key, for the duplicates UI."""
    prefix = dedupe_key.split(":", 1)[0]
    return {
        "tax": "exact",
        "mail": "exact",
        "np": "high",
        "nw": "high",
        "n": "moderate",
        "raw": "none",
    }.get(prefix, "none")


def deduplicate_batch(records: Iterable[RawCompany]) -> tuple[list[RawCompany], int]:
    """Collapse duplicates *within* one crawl batch before touching the DB.

    Later records fill gaps in the earlier one rather than replacing it, which
    matters when a directory lists the same company on several pages with
    different partial detail. Returns ``(unique_records, dropped_count)``.
    """
    merged: dict[str, RawCompany] = {}
    dropped = 0

    for record in records:
        key = build_dedupe_key(
            record.company_name,
            record.tax_id,
            record.email,
            record.phone,
            record.website,
        )
        if not key:
            dropped += 1
            continue

        existing = merged.get(key)
        if existing is None:
            merged[key] = record
            continue

        dropped += 1
        for field in (
            "tax_id", "email", "phone", "website",
            "address", "industry", "contact_person", "source_url",
        ):
            if not getattr(existing, field) and getattr(record, field):
                setattr(existing, field, getattr(record, field))

    return list(merged.values()), dropped
