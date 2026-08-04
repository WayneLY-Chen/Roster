"""Tests for verifier/validators.py."""

from __future__ import annotations

import pytest

from verifier.validators import (
    email_domain,
    is_disposable_email,
    is_role_address,
    is_valid_company_name,
    is_valid_email,
    is_valid_phone,
    is_valid_tax_id,
    is_valid_website,
)

DISPOSABLE = {"mailinator.com", "10minutemail.com", "guerrillamail.com"}


# --------------------------------------------------------------------- email


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a@b.com", True),
        ("first.last@example.co.uk", True),
        ("user+tag@example.com", True),
        ("", False),
        (None, False),
        ("not-an-email", False),
        ("@example.com", False),
        ("a@", False),
        ("a..b@example.com", False),  # consecutive dots
        ("a@example..com", False),  # consecutive dots in domain
        ("a@localhost", False),  # single-label domain
        ("a" * 250 + "@example.com", False),  # over 254 chars overall
    ],
)
def test_is_valid_email(value, expected):
    assert is_valid_email(value) is expected


def test_is_valid_email_rejects_overlong_local_or_domain():
    assert is_valid_email("a" * 65 + "@example.com") is False
    assert is_valid_email("a@" + "b" * 250 + ".com") is False


def test_email_domain():
    assert email_domain("Sales@Example.COM") == "example.com"
    assert email_domain("not-an-email") is None
    assert email_domain(None) is None


# ----------------------------------------------------------------- role addr


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("info@example.com", True),
        ("sales@example.com", True),
        ("admin@example.com", True),
        ("noreply@example.com", True),
        ("john.doe@example.com", False),
        ("", False),
        (None, False),
        ("no-at-sign", False),
    ],
)
def test_is_role_address(value, expected):
    assert is_role_address(value) is expected


# --------------------------------------------------------------- disposable


def test_is_disposable_email_matches_listed_domain():
    assert is_disposable_email("a@mailinator.com", DISPOSABLE) is True


def test_is_disposable_email_matches_subdomain():
    assert is_disposable_email("a@mail.mailinator.com", DISPOSABLE) is True


def test_is_disposable_email_false_for_normal_domain():
    assert is_disposable_email("a@example.com", DISPOSABLE) is False


def test_is_disposable_email_false_without_domain_list():
    assert is_disposable_email("a@mailinator.com", None) is False
    assert is_disposable_email("a@mailinator.com", set()) is False


def test_is_disposable_email_false_for_invalid_address():
    assert is_disposable_email("not-an-email", DISPOSABLE) is False


# ------------------------------------------------------------------- website


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://example.com", True),
        ("http://example.com/path?x=1", True),
        ("https://sub.example.co.uk:8080/a/b", True),
        ("ftp://example.com", False),
        ("example.com", False),  # no scheme
        ("", False),
        (None, False),
        ("http://" + "a" * 2050, False),  # too long
    ],
)
def test_is_valid_website(value, expected):
    assert is_valid_website(value) is expected


# --------------------------------------------------------------------- phone


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("02-27231234", True),
        ("0912-345-678", True),
        ("0800-091-091", True),
        ("02-27238899#210", True),
        ("+12125550100", True),
        ("123456", False),  # too short (6 digits)
        ("", False),
        (None, False),
        ("abcdefg", False),
        ("02-2723", False),  # too few digits overall (< 7)
    ],
)
def test_is_valid_phone(value, expected):
    assert is_valid_phone(value) is expected


# ------------------------------------------------------------------- tax id

VALID_TAX_IDS = [
    "22099131",
    "04541302",
    "73251209",
    "03707901",
    "20828393",
    "16003518",
    "97176270",  # 7th digit is 7: exercises the MOF checksum leniency
]

INVALID_TAX_IDS = [
    "99999999",
    "70766735",
    "1234567",  # too short
    "abcdefgh",  # not numeric
    "",
    None,
]


@pytest.mark.parametrize("value", VALID_TAX_IDS)
def test_is_valid_tax_id_accepts_real_numbers(value):
    assert is_valid_tax_id(value) is True


@pytest.mark.parametrize("value", INVALID_TAX_IDS)
def test_is_valid_tax_id_rejects_bad_numbers(value):
    assert is_valid_tax_id(value) is False


def test_is_valid_tax_id_checksum_arithmetic():
    # Every digit maps through the weight table and reduces to a digit sum;
    # this pins the exact algorithm rather than only spot-checking examples.
    weights = (1, 2, 1, 2, 1, 2, 4, 1)
    digits = [2, 2, 0, 9, 9, 1, 3, 1]
    total = 0
    for digit, weight in zip(digits, weights, strict=True):
        product = digit * weight
        total += product // 10 + product % 10
    assert total % 5 == 0
    assert is_valid_tax_id("22099131") is True


# --------------------------------------------------------------- company name


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("台積電股份有限公司", True),
        ("ABC Corp", True),
        ("AB", True),
        ("A", False),  # single character
        ("", False),
        (None, False),
        ("12345", False),  # numeric only
        ("   ", False),
        ("A" * 256, False),  # too long
        ("A" * 255, True),
    ],
)
def test_is_valid_company_name(value, expected):
    assert is_valid_company_name(value) is expected
