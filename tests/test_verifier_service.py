"""Tests for verifier/service.py (CleaningService and VerificationService)."""

from __future__ import annotations

from datetime import datetime

import dns.exception
import dns.resolver
import pytest

from core.constants import EmailVerdict
from core.schemas import RawCompany
from database.repository import CompanyRepository
from verifier.mx import MXChecker, MXLookupUnavailable
from verifier.service import CleaningService, VerificationService


class StubMXChecker:
    """A drop-in for :class:`verifier.mx.MXChecker` with no DNS involved."""

    def __init__(self, mapping: dict[str, bool | str]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def has_mx(self, domain: str) -> bool:
        self.calls.append(domain)
        result = self.mapping.get(domain, False)
        if result == "unavailable":
            raise MXLookupUnavailable(domain)
        return bool(result)


# --------------------------------------------------------------- CleaningService


def test_clean_drops_unusable_names(tmp_config):
    service = CleaningService(tmp_config)
    assert service.clean(RawCompany(company_name="")) is None
    assert service.clean(RawCompany(company_name="   ")) is None
    assert service.clean(RawCompany(company_name="A")) is None
    assert service.clean(RawCompany(company_name="12345")) is None


def test_clean_discards_malformed_email_and_phone_but_keeps_name(tmp_config):
    service = CleaningService(tmp_config)
    record = RawCompany(company_name="Foo Co", email="a..b@example.com", phone="123")
    cleaned = service.clean(record)
    assert cleaned is not None
    assert cleaned.email is None
    assert cleaned.phone is None
    assert cleaned.email_verdict == EmailVerdict.EMPTY


def test_clean_keeps_tax_id_that_fails_its_checksum(tmp_config):
    service = CleaningService(tmp_config)
    record = RawCompany(company_name="Foo Co", tax_id="99999999")
    cleaned = service.clean(record)
    assert cleaned is not None
    assert cleaned.tax_id == "99999999"  # kept, just unverified


def test_clean_discards_website_that_fails_validation_after_normalizing(tmp_config):
    service = CleaningService(tmp_config)
    record = RawCompany(company_name="Foo Co", website="https://user:pass@example.com/page")
    cleaned = service.clean(record)
    assert cleaned is not None
    assert cleaned.website is None


def test_clean_sets_empty_verdict_for_missing_email(tmp_config):
    service = CleaningService(tmp_config)
    cleaned = service.clean(RawCompany(company_name="Foo Co"))
    assert cleaned.email_verdict == EmailVerdict.EMPTY
    assert cleaned.email_checked_at is None


def test_clean_sets_invalid_syntax_is_never_reached_for_clean_alone(tmp_config):
    # clean() nulls out a malformed email before verify_email ever sees it, so
    # the record's stored verdict is EMPTY, not INVALID_SYNTAX -- that verdict
    # can still be reached, but only via verify_email() on data that bypassed
    # clean() (see test_verify_email_* below, and VerificationService.run).
    service = CleaningService(tmp_config)
    cleaned = service.clean(RawCompany(company_name="Foo Co", email="not-an-email"))
    assert cleaned.email is None
    assert cleaned.email_verdict == EmailVerdict.EMPTY


def test_clean_sets_disposable_verdict(tmp_config):
    service = CleaningService(tmp_config)
    cleaned = service.clean(RawCompany(company_name="Foo Co", email="a@mailinator.com"))
    assert cleaned.email == "a@mailinator.com"
    assert cleaned.email_verdict == EmailVerdict.DISPOSABLE


def test_clean_many_counts_rejected_and_cleaned(tmp_config):
    service = CleaningService(tmp_config)
    records = [
        RawCompany(company_name="Good Co"),
        RawCompany(company_name=""),
        RawCompany(company_name="Another Good Co"),
        RawCompany(company_name="1"),
    ]
    cleaned, rejected = service.clean_many(records)
    assert len(cleaned) == 2
    assert rejected == 2


# ------------------------------------------------------------- verify_email


def test_verify_email_empty(tmp_config):
    service = CleaningService(tmp_config)
    verdict, checked_at = service.verify_email(None)
    assert verdict == EmailVerdict.EMPTY
    assert checked_at is None


def test_verify_email_invalid_syntax(tmp_config):
    service = CleaningService(tmp_config)
    verdict, checked_at = service.verify_email("not-an-email")
    assert verdict == EmailVerdict.INVALID_SYNTAX
    assert checked_at is not None


def test_verify_email_disposable(tmp_config):
    service = CleaningService(tmp_config)
    verdict, _ = service.verify_email("a@mailinator.com")
    assert verdict == EmailVerdict.DISPOSABLE


def test_verify_email_unknown_when_mx_disabled(tmp_config):
    assert tmp_config.verifier.check_mx is False
    service = CleaningService(tmp_config)
    verdict, checked_at = service.verify_email("a@example.com")
    assert verdict == EmailVerdict.UNKNOWN
    assert checked_at is None


def test_verify_email_valid_with_stub_mx(tmp_config):
    config = tmp_config.model_copy(
        update={"verifier": tmp_config.verifier.model_copy(update={"check_mx": True})}
    )
    stub = StubMXChecker({"example.com": True})
    service = CleaningService(config, mx_checker=stub)
    verdict, checked_at = service.verify_email("a@example.com")
    assert verdict == EmailVerdict.VALID
    assert checked_at is not None


def test_verify_email_no_mx_with_stub_mx(tmp_config):
    config = tmp_config.model_copy(
        update={"verifier": tmp_config.verifier.model_copy(update={"check_mx": True})}
    )
    stub = StubMXChecker({"example.com": False})
    service = CleaningService(config, mx_checker=stub)
    verdict, checked_at = service.verify_email("a@example.com")
    assert verdict == EmailVerdict.NO_MX
    assert checked_at is not None


def test_verify_email_mx_unavailable_leaves_verdict_unknown(tmp_config):
    config = tmp_config.model_copy(
        update={"verifier": tmp_config.verifier.model_copy(update={"check_mx": True})}
    )
    stub = StubMXChecker({"example.com": "unavailable"})
    service = CleaningService(config, mx_checker=stub)
    verdict, checked_at = service.verify_email("a@example.com")
    assert verdict == EmailVerdict.UNKNOWN
    assert checked_at is None


# ------------------------------------------------------------ VerificationService


def _mx_enabled_config(tmp_config):
    return tmp_config.model_copy(
        update={"verifier": tmp_config.verifier.model_copy(update={"check_mx": True})}
    )


def _make_service(db_session, config, mapping):
    service = VerificationService(db_session, config=config)
    stub = StubMXChecker(mapping)
    service.mx = stub
    service.cleaner.mx = stub
    return service, stub


def test_verification_service_run_transitions_verdicts(db_session, tmp_config):
    repo = CompanyRepository(db_session)
    good = repo.create(
        company_name="Good Co", name_key="good", dedupe_key="mail:a@good.tw", email="a@good.tw"
    )
    dead = repo.create(
        company_name="Dead Co", name_key="dead", dedupe_key="mail:a@dead.tw", email="a@dead.tw"
    )
    disposable = repo.create(
        company_name="Disposable Co",
        name_key="disposable",
        dedupe_key="mail:a@mailinator.com",
        email="a@mailinator.com",
    )
    invalid = repo.create(
        company_name="Invalid Co",
        name_key="invalid",
        dedupe_key="raw:invalid co",
        email="not-an-email",
    )
    empty = repo.create(company_name="Empty Co", name_key="empty", dedupe_key="n:empty", email=None)
    db_session.commit()

    config = _mx_enabled_config(tmp_config)
    service, stub = _make_service(
        db_session, config, {"good.tw": True, "dead.tw": False}
    )

    summary = service.run(renormalize=False)

    assert summary.checked == 5
    assert summary.valid == 1
    assert summary.no_mx == 1
    assert summary.disposable == 1
    assert summary.invalid_syntax == 1
    assert summary.empty == 1

    assert good.email_verdict == EmailVerdict.VALID.value
    assert dead.email_verdict == EmailVerdict.NO_MX.value
    assert disposable.email_verdict == EmailVerdict.DISPOSABLE.value
    assert invalid.email_verdict == EmailVerdict.INVALID_SYNTAX.value
    assert empty.email_verdict == EmailVerdict.EMPTY.value
    assert good.email_checked_at is not None


def test_verification_service_mx_unavailable_keeps_unknown_not_false_negative(
    db_session, tmp_config
):
    repo = CompanyRepository(db_session)
    company = repo.create(
        company_name="Flaky Co",
        name_key="flaky",
        dedupe_key="mail:a@flaky.tw",
        email="a@flaky.tw",
        email_verdict=EmailVerdict.UNKNOWN.value,
    )
    db_session.commit()

    config = _mx_enabled_config(tmp_config)
    service, stub = _make_service(db_session, config, {"flaky.tw": "unavailable"})

    summary = service.run(renormalize=False)

    assert summary.checked == 1
    assert summary.valid == 0
    assert summary.no_mx == 0
    assert company.email_verdict == EmailVerdict.UNKNOWN.value


def test_verification_service_renormalizes_messy_stored_fields(db_session, tmp_config):
    repo = CompanyRepository(db_session)
    company = repo.create(
        company_name="ＦＯＯ Ｃｏ",
        name_key="",
        dedupe_key="raw:messy",
        phone="+886-2-2723-1234",
        email="Mailto:A@B.COM",
    )
    db_session.commit()

    config = _mx_enabled_config(tmp_config)
    service, stub = _make_service(db_session, config, {"b.com": True})

    summary = service.run(renormalize=True)

    assert summary.updated == 1
    assert company.company_name == "FOO Co"
    assert company.phone == "02-27231234"
    assert company.email == "a@b.com"
    assert company.name_key != ""


def test_verification_service_run_on_explicit_subset(db_session, tmp_config):
    repo = CompanyRepository(db_session)
    a = repo.create(company_name="A Co", name_key="a", dedupe_key="n:a", email=None)
    repo.create(company_name="B Co", name_key="b", dedupe_key="n:b", email=None)
    db_session.commit()

    config = _mx_enabled_config(tmp_config)
    service, stub = _make_service(db_session, config, {})

    summary = service.run(companies=[a], renormalize=False)
    assert summary.checked == 1


def test_verification_service_run_calls_progress_on_the_last_item(db_session, tmp_config):
    repo = CompanyRepository(db_session)
    a = repo.create(company_name="A Co", name_key="a", dedupe_key="n:a", email=None)
    db_session.commit()

    config = _mx_enabled_config(tmp_config)
    service, stub = _make_service(db_session, config, {})

    seen = []
    service.run(companies=[a], renormalize=False, progress=lambda done, total: seen.append((done, total)))
    assert seen == [(1, 1)]


# --------------------------------------------------------------------- MXChecker
#
# These exercise verifier/mx.py's own resolution/caching logic. No real DNS is
# ever involved: dnspython's ``Resolver.resolve`` is replaced with a fake that
# returns scripted answers or raises scripted exceptions.


class FakeResolver:
    """Replaces ``dns.resolver.Resolver`` with scripted, offline answers."""

    def __init__(self, behavior: dict[tuple[str, str], object]) -> None:
        self.behavior = behavior
        self.calls: list[tuple[str, str]] = []

    def resolve(self, domain: str, rtype: str):
        self.calls.append((domain, rtype))
        result = self.behavior.get((domain, rtype), dns.resolver.NoAnswer())
        if isinstance(result, Exception):
            raise result
        return list(range(result))  # anything with a length works as a stand-in


def _mx_checker(tmp_config, behavior, session=None) -> MXChecker:
    checker = MXChecker(tmp_config, session=session)
    checker._resolver = FakeResolver(behavior)
    return checker


def test_mx_checker_has_mx_false_for_blank_domain(tmp_config):
    checker = _mx_checker(tmp_config, {})
    assert checker.has_mx(None) is False
    assert checker.has_mx("") is False
    assert checker.has_mx("   ") is False


def test_mx_checker_has_mx_true_when_mx_records_exist(tmp_config):
    checker = _mx_checker(tmp_config, {("example.com", "MX"): 1})
    assert checker.has_mx("EXAMPLE.com.") is True  # normalizes case and trailing dot


def test_mx_checker_has_mx_memoizes_within_the_instance(tmp_config):
    checker = _mx_checker(tmp_config, {("example.com", "MX"): 1})
    assert checker.has_mx("example.com") is True
    assert checker.has_mx("example.com") is True
    assert checker._resolver.calls.count(("example.com", "MX")) == 1


def test_mx_checker_falls_back_to_a_record_when_no_mx(tmp_config):
    checker = _mx_checker(
        tmp_config,
        {
            ("example.com", "MX"): dns.resolver.NoAnswer(),
            ("example.com", "A"): 1,
        },
    )
    assert checker.has_mx("example.com") is True


def test_mx_checker_false_when_neither_mx_nor_a_aaaa_exist(tmp_config):
    checker = _mx_checker(
        tmp_config,
        {
            ("example.com", "MX"): dns.resolver.NoAnswer(),
            ("example.com", "A"): dns.exception.DNSException("no A"),
            ("example.com", "AAAA"): dns.exception.DNSException("no AAAA"),
        },
    )
    assert checker.has_mx("example.com") is False


def test_mx_checker_nxdomain_is_false_without_falling_back(tmp_config):
    checker = _mx_checker(tmp_config, {("example.com", "MX"): dns.resolver.NXDOMAIN()})
    assert checker.has_mx("example.com") is False
    assert checker._resolver.calls == [("example.com", "MX")]  # no A/AAAA attempted


@pytest.mark.parametrize("exc", [dns.exception.Timeout(), dns.resolver.NoNameservers()])
def test_mx_checker_transient_dns_failure_raises_unavailable(tmp_config, exc):
    checker = _mx_checker(tmp_config, {("example.com", "MX"): exc})
    with pytest.raises(MXLookupUnavailable):
        checker.has_mx("example.com")


def test_mx_checker_without_a_resolver_returns_false(tmp_config):
    checker = MXChecker(tmp_config)
    checker._resolver = None
    assert checker.has_mx("example.com") is False


def test_mx_checker_close_clears_the_memo(tmp_config):
    checker = _mx_checker(tmp_config, {("example.com", "MX"): 1})
    checker.has_mx("example.com")
    checker.close()
    checker.has_mx("example.com")
    assert checker._resolver.calls.count(("example.com", "MX")) == 2


def test_mx_checker_uses_the_persistent_db_cache_across_instances(db_session, tmp_config):
    first = _mx_checker(tmp_config, {("example.com", "MX"): 1}, session=db_session)
    assert first.has_mx("example.com") is True

    # A second checker, sharing the session, must never touch the resolver --
    # the earlier answer should already be served from MXCacheRepository.
    second = MXChecker(tmp_config, session=db_session)

    class ExplodingResolver:
        def resolve(self, *_a, **_k):
            raise AssertionError("must not hit DNS when the db cache has an answer")

    second._resolver = ExplodingResolver()
    assert second.has_mx("example.com") is True
