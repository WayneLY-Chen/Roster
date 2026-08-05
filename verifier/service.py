"""Cleaning and verification services.

:class:`CleaningService` turns a scraped :class:`~core.schemas.RawCompany` into
a storable :class:`~core.schemas.CleanCompany`. :class:`VerificationService`
re-runs contact verification over rows already in the database.

Both take an optional :class:`~verifier.mx.MXChecker`; without one, syntax
checks still run and the verdict simply stops short of ``VALID``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy.orm import Session

from core.config import AppConfig, get_config
from core.constants import EmailVerdict, LogCategory
from core.logging_setup import get_logger
from core.schemas import CleanCompany, RawCompany, VerifySummary
from database.models import Company, now
from database.repository import CompanyRepository
from verifier.classify import classify
from verifier.dedupe import build_dedupe_key
from verifier.mx import MXChecker, MXLookupUnavailable
from verifier.normalize import (
    company_name_key,
    normalize_address,
    normalize_company_name,
    normalize_email,
    normalize_industry,
    normalize_person_name,
    normalize_phone,
    normalize_tax_id,
    normalize_website,
)
from verifier.validators import (
    is_disposable_email,
    is_valid_company_name,
    is_valid_email,
    is_valid_phone,
    is_valid_tax_id,
    is_valid_website,
)

log = get_logger(LogCategory.CRAWL)


#: 自由欄位的上限。名錄頁面偶爾會把整段公告文字寫成「標題：內文」，解析出來
#: 就是一個超長的欄位；截斷比讓詳細資料視窗塞不下好。
_MAX_EXTRA_FIELDS = 30
_MAX_EXTRA_VALUE_CHARS = 500


def _clean_extra_fields(values: dict[str, str]) -> dict[str, str]:
    """自由欄位的最低限度整理：去空白、丟空值、限制長度與筆數。

    刻意不做正規化——這些欄位的意義只有那個名錄知道，程式沒有立場改寫它們。
    """
    cleaned: dict[str, str] = {}
    for key, value in values.items():
        label = str(key).strip()
        text = str(value).strip()
        if not label or not text:
            continue
        cleaned[label[:60]] = text[:_MAX_EXTRA_VALUE_CHARS]
        if len(cleaned) >= _MAX_EXTRA_FIELDS:
            break
    return cleaned


class CleaningService:
    """Normalize, validate and key a scraped record."""

    def __init__(
        self,
        config: AppConfig | None = None,
        mx_checker: MXChecker | None = None,
    ) -> None:
        self.config = config or get_config()
        self.mx = mx_checker
        self._disposable = set(self.config.verifier.disposable_domains)

    def clean(self, raw: RawCompany) -> CleanCompany | None:
        """Return a storable record, or ``None`` when it is unusable.

        A record is unusable only when its company name is missing or
        nonsensical -- every other field is optional in a public directory.
        """
        name = normalize_company_name(raw.company_name)
        if not is_valid_company_name(name):
            log.debug("dropping record with unusable name: {!r}", raw.company_name)
            return None
        assert name is not None

        # Directory listings mix promotional articles in with company cards
        # using identical markup, so this is the only place that can tell them
        # apart -- and every path into the database (crawl, import, gmail)
        # goes through here.
        if self.config.verifier.filter_advertisements:
            verdict = classify(name, raw.website)
            if not verdict.is_company:
                log.info("略過疑似廣告：{!r}（{}）", name[:40], verdict.reason)
                return None

        email = normalize_email(raw.email)
        if email and not is_valid_email(email):
            log.debug("discarding malformed email {!r} for {}", raw.email, name)
            email = None

        phone = normalize_phone(raw.phone)
        if phone and not is_valid_phone(phone):
            phone = None

        website = normalize_website(raw.website)
        if website and not is_valid_website(website):
            website = None

        tax_id = normalize_tax_id(raw.tax_id)
        if tax_id and not is_valid_tax_id(tax_id):
            log.debug("tax id {} fails its checksum; keeping it unverified", tax_id)

        verdict, checked_at = self.verify_email(email)

        return CleanCompany(
            company_name=name,
            name_key=company_name_key(name),
            dedupe_key=build_dedupe_key(name, tax_id, email, phone, website),
            tax_id=tax_id,
            email=email,
            phone=phone,
            website=website,
            address=normalize_address(raw.address),
            industry=normalize_industry(raw.industry),
            english_name=(raw.english_name or "").strip() or None,
            fax=normalize_phone(raw.fax) or None,
            products=(raw.products or "").strip() or None,
            contact_person=normalize_person_name(raw.contact_person),
            extra_fields=_clean_extra_fields(raw.extra_fields),
            source=raw.source,
            source_url=raw.source_url,
            email_verdict=verdict,
            email_checked_at=checked_at,
        )

    def clean_many(self, records: Iterable[RawCompany]) -> tuple[list[CleanCompany], int]:
        """Clean a batch. Returns ``(clean_records, rejected_count)``."""
        cleaned: list[CleanCompany] = []
        rejected = 0
        for record in records:
            result = self.clean(record)
            if result is None:
                rejected += 1
            else:
                cleaned.append(result)
        return cleaned, rejected

    def verify_email(self, email: str | None) -> tuple[EmailVerdict, datetime | None]:
        """Grade an address from cheapest check to most expensive."""
        if not email:
            return EmailVerdict.EMPTY, None
        if not is_valid_email(email):
            return EmailVerdict.INVALID_SYNTAX, now()
        if self.config.verifier.reject_disposable and is_disposable_email(
            email, self._disposable
        ):
            return EmailVerdict.DISPOSABLE, now()
        if not (self.config.verifier.check_mx and self.mx is not None):
            return EmailVerdict.UNKNOWN, None

        domain = email.rpartition("@")[2]
        try:
            has_mx = self.mx.has_mx(domain)
        except MXLookupUnavailable:
            # DNS is down, not the domain. Leave the verdict open so the next
            # run re-checks instead of recording a false negative.
            return EmailVerdict.UNKNOWN, None
        return (EmailVerdict.VALID if has_mx else EmailVerdict.NO_MX), now()


class VerificationService:
    """Re-verify contact data for rows already stored."""

    def __init__(self, session: Session, config: AppConfig | None = None) -> None:
        self.session = session
        self.config = config or get_config()
        self.repo = CompanyRepository(session)
        self.mx = MXChecker(self.config, session) if self.config.verifier.check_mx else None
        self.cleaner = CleaningService(self.config, self.mx)

    def run(
        self,
        companies: Iterable[Company] | None = None,
        renormalize: bool = True,
        progress=None,
    ) -> VerifySummary:
        """Verify every company (or a given subset).

        ``renormalize=True`` also re-applies normalization, which is what you
        want after changing normalization rules or importing external data.
        ``progress`` is an optional ``callable(done, total)`` for the GUI.
        """
        targets = list(companies) if companies is not None else self.repo.all()
        summary = VerifySummary()
        total = len(targets)

        for index, company in enumerate(targets, start=1):
            summary.checked += 1
            changed = False

            if renormalize:
                changed |= self._renormalize(company)

            verdict, checked_at = self.cleaner.verify_email(company.email)
            if company.email_verdict != verdict.value:
                company.email_verdict = verdict.value
                changed = True
            if checked_at is not None:
                company.email_checked_at = checked_at

            match verdict:
                case EmailVerdict.VALID:
                    summary.valid += 1
                case EmailVerdict.INVALID_SYNTAX:
                    summary.invalid_syntax += 1
                case EmailVerdict.NO_MX:
                    summary.no_mx += 1
                case EmailVerdict.DISPOSABLE:
                    summary.disposable += 1
                case EmailVerdict.EMPTY:
                    summary.empty += 1
                case _:
                    pass

            if changed:
                company.updated_at = now()
                summary.updated += 1

            if progress is not None and (index % 25 == 0 or index == total):
                progress(index, total)

        self.session.flush()
        log.info(
            "verified {} companies: {} valid, {} no-mx, {} bad syntax, {} empty",
            summary.checked,
            summary.valid,
            summary.no_mx,
            summary.invalid_syntax,
            summary.empty,
        )
        return summary

    def _renormalize(self, company: Company) -> bool:
        """Re-apply normalization in place. True when anything changed."""
        updates = {
            "company_name": normalize_company_name(company.company_name)
            or company.company_name,
            "email": normalize_email(company.email),
            "phone": normalize_phone(company.phone),
            "website": normalize_website(company.website),
            "address": normalize_address(company.address),
            "industry": normalize_industry(company.industry),
            "contact_person": normalize_person_name(company.contact_person),
            "tax_id": normalize_tax_id(company.tax_id),
        }
        changed = False
        for field, value in updates.items():
            if getattr(company, field) != value:
                setattr(company, field, value)
                changed = True

        name_key = company_name_key(company.company_name)
        if company.name_key != name_key:
            company.name_key = name_key
            changed = True

        new_key = build_dedupe_key(
            company.company_name,
            company.tax_id,
            company.email,
            company.phone,
            company.website,
        )
        # Only adopt a stronger key when it is free; colliding with an existing
        # row is the duplicates page's problem, not a reason to fail the run.
        if new_key and new_key != company.dedupe_key:
            clash = self.repo.get_by_dedupe_key(new_key)
            if clash is None or clash.id == company.id:
                company.dedupe_key = new_key
                changed = True
        return changed
