"""Tests for verifier/normalize.py."""

from __future__ import annotations

import pytest

from verifier.normalize import (
    clean_text,
    company_name_key,
    extract_emails,
    normalize_address,
    normalize_company_name,
    normalize_email,
    normalize_industry,
    normalize_person_name,
    normalize_phone,
    normalize_tax_id,
    normalize_website,
    squeeze,
    to_halfwidth,
    website_host,
)


# --------------------------------------------------------------------- basics


def test_to_halfwidth_folds_fullwidth_forms():
    assert to_halfwidth("ＡＢＣ１２３ａｂｃ") == "ABC123abc"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  a   b\tc\n d ", "a b c d"),
        ("", ""),
        ("no-change", "no-change"),
    ],
)
def test_squeeze_collapses_whitespace(raw, expected):
    assert squeeze(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("   ", None),
        ("​hello﻿ world​", "hello world"),
        ("Ａｂｃ", "Abc"),
    ],
)
def test_clean_text(raw, expected):
    assert clean_text(raw) == expected


def test_normalize_company_name_strips_scrape_artefacts():
    assert normalize_company_name(" ·-–—|,、 台積電 ·-–—|,、 ") == "台積電"


def test_normalize_company_name_none_when_empty():
    assert normalize_company_name("   ") is None
    assert normalize_company_name(None) is None


# --------------------------------------------------------------- company key


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("台灣大電力研究試驗中心股份有限公司", "臺灣大電力研究試驗中心"),
        ("宏達精密機械股份有限公司", "宏達精密機械"),
        ("ABC Company Limited", "ABC Corp."),
        ("ABC Co., Ltd.", "ABC Inc"),
    ],
)
def test_company_name_key_collapses_legal_form_and_spelling_variants(a, b):
    assert company_name_key(a) == company_name_key(b)


def test_company_name_key_specific_pairs_match():
    assert company_name_key("台灣大電力研究試驗中心股份有限公司") == company_name_key(
        "臺灣大電力研究試驗中心"
    )
    assert company_name_key("台積電（股）公司") == "台積電"


def test_company_name_key_empty_for_nothing_comparable():
    assert company_name_key(None) == ""
    assert company_name_key("") == ""
    assert company_name_key("   ") == ""


def test_company_name_key_strips_legal_suffix_variants():
    assert company_name_key("全泰化工有限公司") == company_name_key("全泰化工")


# -------------------------------------------------------------------- email


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("mailto:Test@Example.com", "test@example.com"),
        ("mailto:Test@Example.com?subject=hi", "test@example.com"),
        ("Name <A@B.COM>", "a@b.com"),
        ("業務信箱 <sales@hongda-precision.com.tw>", "sales@hongda-precision.com.tw"),
        ("contact (at) example (dot) com", "contact@example.com"),
        ("contact[at]example[dot]com", "contact@example.com"),
        ("contact AT example DOT com", None),  # bare "AT"/"DOT" words are not rewritten
        ("HELLO@WORLD.COM", "hello@world.com"),
        (None, None),
        ("", None),
        ("not an email at all", None),
    ],
)
def test_normalize_email(raw, expected):
    assert normalize_email(raw) == expected


def test_extract_emails_finds_all_in_order_deduped():
    text = "contact a@b.com or b@c.com, also a@b.com again"
    assert extract_emails(text) == ["a@b.com", "b@c.com"]


def test_extract_emails_empty_for_blank_text():
    assert extract_emails("") == []
    assert extract_emails(None) == []


# -------------------------------------------------------------------- phone


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+886-2-2723-1234", "02-27231234"),
        ("(02)27231234", "02-27231234"),
        ("0912345678", "0912-345-678"),
        ("0912-345-678", "0912-345-678"),
        ("0800-091-091", "0800-091-091"),
        ("0800091091", "0800-091-091"),
        ("02-2723-8899#210", "02-27238899#210"),
        ("(02)2723-8899 分機 210", "02-27238899#210"),
        ("02-2723-8899 轉 210", "02-27238899#210"),
        ("02-2723-8899 ext. 210", "02-27238899#210"),
        ("+1-212-555-0100", "+12125550100"),
        ("886-3-5778899", "03-5778899"),
        ("03-4562211#18", "03-4562211#18"),
        (None, None),
        ("", None),
        ("no digits here", None),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


# ----------------------------------------------------------------- website


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("example.com", "https://example.com"),
        ("HTTPS://WWW.Example.COM/Path/", "https://www.example.com/Path"),
        ("https://example.com/?utm_source=abc&foo=bar", "https://example.com?foo=bar"),
        ("https://example.com/", "https://example.com"),
        ("mailto:a@b.com", None),
        ("tel:0227231234", None),
        ("javascript:void(0)", None),
        ("n/a", None),
        ("無", None),
        (None, None),
        ("", None),
    ],
)
def test_normalize_website(raw, expected):
    assert normalize_website(raw) == expected


def test_website_host_strips_www():
    assert website_host("https://www.example.com/x") == "example.com"
    assert website_host("https://example.com") == "example.com"
    assert website_host(None) is None
    assert website_host("not a url") is None


def test_normalize_website_rejects_non_http_scheme():
    assert normalize_website("ftp://example.com") is None


def test_normalize_website_rejects_unparsable_url():
    assert normalize_website("https://[::1") is None


# ----------------------------------------------------------------- address


def test_normalize_address_strips_leading_zip_when_followed_by_space():
    assert normalize_address("110 臺北市信義區松高路 11 號 8 樓") == "臺北市信義區松高路11號8樓"


def test_normalize_address_keeps_zip_glued_to_text():
    # No whitespace between the zip and the city name -> the leading-zip
    # pattern does not match, so the digits stay part of the address.
    assert normalize_address("407台中市西屯區工業區一路 25 號") == "407台中市西屯區工業區一路25號"


def test_normalize_address_strips_label_prefix():
    assert normalize_address("地址:台北市信義區") == "台北市信義區"
    assert normalize_address("Address: 123 Main St") == "123 Main St"


def test_normalize_address_none_for_empty():
    assert normalize_address(None) is None
    assert normalize_address("   ") is None


def test_is_mostly_cjk_false_for_empty_input():
    from verifier.normalize import _is_mostly_cjk

    assert _is_mostly_cjk("") is False


# ----------------------------------------------------------------- tax id


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("統一編號：69081935", "69081935"),
        ("統編 04595257", "04595257"),
        ("  12345678  ", "12345678"),
        (None, None),
        ("no digits", None),
    ],
)
def test_normalize_tax_id(raw, expected):
    assert normalize_tax_id(raw) == expected


# ----------------------------------------------------------------- person


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("聯絡人：陳建志", "陳建志"),
        ("聯絡窗口: 林小姐", "林小姐"),
        ("Contact: John Doe", "John Doe"),
        ("Contact Person: Jane", "Jane"),
        ("王大明", "王大明"),
        (None, None),
        ("   ", None),
    ],
)
def test_normalize_person_name(raw, expected):
    assert normalize_person_name(raw) == expected


# ---------------------------------------------------------------- industry


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("產業：金屬加工", "金屬加工"),
        ("行業: 電子零組件", "電子零組件"),
        ("類別：化學原料", "化學原料"),
        ("Industry: Manufacturing", "Manufacturing"),
        ("金屬加工／CNC", "金屬加工/CNC"),  # NFKC folds the full-width slash too
        (None, None),
    ],
)
def test_normalize_industry(raw, expected):
    assert normalize_industry(raw) == expected
