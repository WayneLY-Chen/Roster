"""Tests for the automatic discovery engine (``crawler.discover``).

Uses :func:`discover_from_html` throughout so nothing here touches the
network -- the offline directory fixture in ``templates/`` stands in for a
real page.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.config import SourceConfig
from crawler.discover import discover_from_html

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "templates" / "sample_directory_page1.html"
FIXTURE_URL = "https://example.test/directory/page1.html"


@pytest.fixture
def fixture_html() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


def test_discover_finds_the_repeated_company_card(fixture_html):
    result = discover_from_html(fixture_html, FIXTURE_URL)
    assert result.list_selector == "div.company-card"
    assert result.item_count == 5


def test_discover_detects_core_contact_fields(fixture_html):
    result = discover_from_html(fixture_html, FIXTURE_URL)
    assert "company_name" in result.fields
    assert "email" in result.fields
    assert "phone" in result.fields

    assert result.fields["company_name"].hit_rate == pytest.approx(1.0)
    # Two of the five records are missing an email or omit a phone value in a
    # format the guesser cannot parse, so these are found often but not always.
    assert result.fields["email"].hit_rate >= 0.35
    assert result.fields["phone"].hit_rate >= 0.35


def test_discover_preview_has_correct_company_names(fixture_html):
    result = discover_from_html(fixture_html, FIXTURE_URL)
    names = [record.company_name for record in result.preview]
    assert names == [
        "宏達精密機械股份有限公司",
        "日新電子（股）公司",
        "全泰化工有限公司",
        "綠光紡織企業社",
        "海通物流股份有限公司",
    ]


def test_discover_finds_the_next_page_link(fixture_html):
    result = discover_from_html(fixture_html, FIXTURE_URL)
    assert result.next_selector is not None
    assert result.ok is True


def test_discover_reports_failure_when_nothing_repeats():
    html = "<html><body><p>Just one paragraph, nothing repeated here.</p></body></html>"
    result = discover_from_html(html, "https://example.test/none")

    assert result.ok is False
    assert result.list_selector == ""
    assert result.notes  # explains why, in Traditional Chinese, for the GUI


def test_discover_to_source_config_produces_a_valid_source(fixture_html):
    result = discover_from_html(fixture_html, FIXTURE_URL)
    source = result.to_source_config("my_directory")

    assert isinstance(source, SourceConfig)
    assert source.name == "my_directory"
    assert source.type == "generic_html"
    assert source.start_url == FIXTURE_URL
    assert source.list_selector == "div.company-card"
    assert "company_name" in source.fields
    assert source.pagination.type == "next_link"
    assert source.pagination.next_selector == result.next_selector
