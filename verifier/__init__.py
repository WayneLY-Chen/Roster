"""Data cleaning, validation and duplicate detection."""

from verifier.classify import Verdict, classify, is_probably_company
from verifier.dedupe import build_dedupe_key, deduplicate_batch, key_confidence
from verifier.mx import MXChecker, MXLookupUnavailable
from verifier.normalize import (
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
    website_host,
)
from verifier.service import CleaningService, VerificationService
from verifier.validators import (
    is_disposable_email,
    is_role_address,
    is_valid_company_name,
    is_valid_email,
    is_valid_phone,
    is_valid_tax_id,
    is_valid_website,
)

__all__ = [
    "CleaningService",
    "Verdict",
    "classify",
    "is_probably_company",
    "MXChecker",
    "MXLookupUnavailable",
    "VerificationService",
    "build_dedupe_key",
    "company_name_key",
    "deduplicate_batch",
    "extract_emails",
    "is_disposable_email",
    "is_role_address",
    "is_valid_company_name",
    "is_valid_email",
    "is_valid_phone",
    "is_valid_tax_id",
    "is_valid_website",
    "key_confidence",
    "normalize_address",
    "normalize_company_name",
    "normalize_email",
    "normalize_industry",
    "normalize_person_name",
    "normalize_phone",
    "normalize_tax_id",
    "normalize_website",
    "website_host",
]
