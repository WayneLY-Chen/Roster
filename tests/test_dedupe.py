"""Tests for verifier/dedupe.py."""

from __future__ import annotations

import pytest

from core.schemas import RawCompany
from verifier.dedupe import build_dedupe_key, deduplicate_batch, key_confidence


# ---------------------------------------------------------------- build key


def test_build_dedupe_key_prefers_tax_id():
    key = build_dedupe_key(
        "台積電",
        tax_id="22099131",
        email="a@b.com",
        phone="02-1234",
        website="tsmc.com",
    )
    assert key == "tax:22099131"


def test_build_dedupe_key_falls_back_to_email_when_tax_invalid():
    key = build_dedupe_key(
        "台積電",
        tax_id="99999999",  # fails the checksum
        email="a@b.com",
        phone="02-1234",
        website="tsmc.com",
    )
    assert key == "mail:a@b.com"


def test_build_dedupe_key_falls_back_to_name_and_phone():
    key = build_dedupe_key("台積電", phone="02-1234-5678")
    assert key.startswith("np:")
    assert "02-12345678" in key


def test_build_dedupe_key_falls_back_to_name_and_website_host():
    key = build_dedupe_key("台積電", website="https://www.tsmc.com")
    assert key.startswith("nw:")
    assert key.endswith("|tsmc.com")


def test_build_dedupe_key_falls_back_to_name_alone():
    key = build_dedupe_key("台積電")
    assert key.startswith("n:")


def test_build_dedupe_key_raw_fallback_when_nothing_identifiable():
    key = build_dedupe_key("###")
    assert key.startswith("raw:")


def test_build_dedupe_key_empty_when_nothing_at_all():
    assert build_dedupe_key(None) == ""
    assert build_dedupe_key("") == ""
    assert build_dedupe_key("   ") == ""


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("tax:12345675", "exact"),
        ("mail:a@b.com", "exact"),
        ("np:key|0212", "high"),
        ("nw:key|b.com", "high"),
        ("n:key", "moderate"),
        ("raw:some text", "none"),
        ("", "none"),
        ("unknown:x", "none"),
    ],
)
def test_key_confidence(prefix, expected):
    assert key_confidence(prefix) == expected


# --------------------------------------------------------------- dedupe batch


def test_deduplicate_batch_merges_partial_records_within_batch():
    # Both records resolve to the exact same dedupe key (a shared, valid tax
    # id), which is what makes them merge *within* one batch -- deduplicate_batch
    # only ever merges byte-identical keys, never a "same company, different
    # key strength" case (that is upsert/find_match's job at the DB layer).
    first = RawCompany(
        company_name="宏達精密機械股份有限公司",
        tax_id="69081935",
        phone="04-2359-1234",
        contact_person="陳建志",
        source="page1",
    )
    second = RawCompany(
        company_name="宏達精密機械",
        tax_id="69081935",
        address="台中市西屯區工業區一路25號",
        industry="金屬加工",
        email="sales@hongda-precision.com.tw",
        source="page2",
    )

    unique, dropped = deduplicate_batch([first, second])

    assert dropped == 1
    assert len(unique) == 1
    merged = unique[0]
    # The earlier record's already-known fields are preserved...
    assert merged.contact_person == "陳建志"
    assert merged.phone == "04-2359-1234"
    assert merged.source == "page1"
    # ...and the later record fills the gaps.
    assert merged.address == "台中市西屯區工業區一路25號"
    assert merged.industry == "金屬加工"
    assert merged.email == "sales@hongda-precision.com.tw"


def test_deduplicate_batch_keeps_distinct_records_separate():
    records = [
        RawCompany(company_name="Company A", email="a@example.com"),
        RawCompany(company_name="Company B", email="b@example.com"),
    ]
    unique, dropped = deduplicate_batch(records)
    assert dropped == 0
    assert len(unique) == 2


def test_deduplicate_batch_drops_records_with_no_identifiable_key():
    records = [RawCompany(company_name=""), RawCompany(company_name="   ")]
    unique, dropped = deduplicate_batch(records)
    assert unique == []
    assert dropped == 2


def test_deduplicate_batch_does_not_overwrite_existing_nonempty_fields():
    first = RawCompany(company_name="Foo Inc", email="first@example.com", phone="02-1111")
    second = RawCompany(company_name="Foo Inc", email="first@example.com", phone="02-2222")

    unique, dropped = deduplicate_batch([first, second])

    assert dropped == 1
    assert unique[0].phone == "02-1111"
