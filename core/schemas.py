"""Pydantic DTOs that move data between layers.

Three shapes, in pipeline order:

``RawCompany``    what a crawl source yields -- untrusted, unnormalized text.
``CleanCompany``  after :mod:`verifier` -- normalized, validated, dedupe-keyed.
``CompanyView``   read model handed to the GUI and exporters.

ORM entities live in :mod:`database.models` and never leave the repository.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.constants import (
    ActivityType,
    EmailVerdict,
    PipelineStage,
    Priority,
    RecordStatus,
)


class RawCompany(BaseModel):
    """A single record as scraped, before any cleaning."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    company_name: str = ""
    tax_id: str | None = None
    email: str | None = None
    phone: str | None = None
    website: str | None = None
    address: str | None = None
    industry: str | None = None
    english_name: str | None = None
    fax: str | None = None
    products: str | None = None
    contact_person: str | None = None
    source: str = "unknown"
    source_url: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    def is_empty(self) -> bool:
        """True when there is nothing worth keeping."""
        return not self.company_name.strip()


class CleanCompany(BaseModel):
    """Normalized record ready to be written to the database."""

    model_config = ConfigDict(extra="forbid")

    company_name: str
    name_key: str                  # normalized name, used for fuzzy dedupe
    dedupe_key: str                # strongest available identity key
    tax_id: str | None = None
    email: str | None = None
    phone: str | None = None
    website: str | None = None
    address: str | None = None
    industry: str | None = None
    english_name: str | None = None
    fax: str | None = None
    products: str | None = None
    contact_person: str | None = None
    source: str = "unknown"
    source_url: str | None = None
    email_verdict: EmailVerdict = EmailVerdict.UNKNOWN
    email_checked_at: datetime | None = None
    status: RecordStatus = RecordStatus.ACTIVE
    remark: str | None = None


class CompanyView(BaseModel):
    """Flat read model. Everything the GUI tables and exporters need."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    company_name: str
    tax_id: str | None = None
    email: str | None = None
    phone: str | None = None
    website: str | None = None
    address: str | None = None
    industry: str | None = None
    english_name: str | None = None
    fax: str | None = None
    products: str | None = None
    contact_person: str | None = None
    source: str | None = None
    source_url: str | None = None
    status: str = RecordStatus.ACTIVE.value
    pipeline_stage: str = PipelineStage.NEW.value
    priority: str = Priority.MEDIUM.value
    email_verdict: str = EmailVerdict.UNKNOWN.value
    follow_up_date: date | None = None
    remark: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ContactView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    company_id: int
    company_name: str | None = None
    name: str
    title: str | None = None
    email: str | None = None
    phone: str | None = None
    mobile: str | None = None
    is_primary: bool = False
    remark: str | None = None
    created_at: datetime | None = None


class ActivityView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    company_id: int
    type: str = ActivityType.NOTE.value
    subject: str | None = None
    body: str | None = None
    occurred_at: datetime | None = None
    created_at: datetime | None = None


class CompanyFilter(BaseModel):
    """Search/filter criteria for the Companies page and exports.

    ``text`` is the full-text term, matched across name, email, phone, website,
    address, industry, contact person, tax id and remark. The remaining fields
    are AND-ed narrowing filters.
    """

    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    company_name: str | None = None
    email: str | None = None
    phone: str | None = None
    website: str | None = None
    address: str | None = None
    industry: str | None = None
    contact_person: str | None = None
    tags: list[str] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=list)
    stages: list[str] = Field(default_factory=list)
    priorities: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    email_verdicts: list[str] = Field(default_factory=list)
    has_email: bool | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    follow_up_before: date | None = None
    limit: int | None = Field(default=None, ge=1)
    offset: int = Field(default=0, ge=0)
    order_by: str = "updated_at"
    descending: bool = True

    def is_empty(self) -> bool:
        """True when nothing would be narrowed (ignoring paging/ordering)."""
        ignored = {"limit", "offset", "order_by", "descending"}
        dumped = self.model_dump(exclude=ignored, exclude_none=True)
        return not any(value for value in dumped.values())


class CrawlSummary(BaseModel):
    """Result of one source's crawl run."""

    source: str
    status: str
    pages_crawled: int = 0
    records_found: int = 0
    records_new: int = 0
    records_updated: int = 0
    records_duplicate: int = 0
    records_invalid: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None

    @property
    def duration_seconds(self) -> float:
        if not (self.started_at and self.finished_at):
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()


class VerifySummary(BaseModel):
    """Result of a verification pass."""

    checked: int = 0
    valid: int = 0
    invalid_syntax: int = 0
    no_mx: int = 0
    disposable: int = 0
    empty: int = 0
    updated: int = 0


class DashboardStats(BaseModel):
    """Numbers rendered on the Dashboard page."""

    total_companies: int = 0
    total_emails: int = 0
    verified_emails: int = 0
    total_contacts: int = 0
    new_this_week: int = 0
    new_today: int = 0
    duplicates: int = 0
    follow_ups_due: int = 0
    by_stage: dict[str, int] = Field(default_factory=dict)
    by_source: dict[str, int] = Field(default_factory=dict)
    last_crawl: CrawlSummary | None = None
