"""Offline sample source.

Parses the bundled fixtures in ``templates/`` with the same code path as a real
crawl, so ``python main.py crawl --source sample`` exercises extraction,
cleaning, deduplication and storage end to end without touching the network.

Use it to try the app, to demo it, and as the fixture behind the crawler tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from core.config import AppConfig, FieldRule, PaginationRule, SourceConfig
from core.constants import LogCategory
from core.errors import SourceConfigError
from core.logging_setup import get_logger
from core.schemas import RawCompany
from crawler.base import BaseSource, PageBatch
from crawler.fetcher import BaseFetcher
from crawler.parser import (
    extract_record,
    harvest_emails,
    harvest_phones,
    make_soup,
    select_items,
)

log = get_logger(LogCategory.CRAWL)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"

FIXTURE_PAGES = ("sample_directory_page1.html", "sample_directory_page2.html")

LIST_SELECTOR = "div.company-card"

FIELD_RULES: dict[str, FieldRule] = {
    "company_name": FieldRule(selector="h3.name"),
    "tax_id": FieldRule(selector="span.tax-id"),
    "industry": FieldRule(selector="p.industry"),
    "phone": FieldRule(selector="p.tel"),
    "address": FieldRule(selector="p.addr"),
    "website": FieldRule(selector="a.site", attr="href"),
    "email": FieldRule(selector="a[href^='mailto:']", attr="href"),
    "contact_person": FieldRule(selector="p.contact"),
}


def sample_source_config(name: str = "sample") -> SourceConfig:
    """The equivalent :class:`SourceConfig`, useful for tests and docs."""
    return SourceConfig(
        name=name,
        type="sample",
        enabled=True,
        list_selector=LIST_SELECTOR,
        pagination=PaginationRule(type="none"),
        fields=FIELD_RULES,
    )


class SampleSource(BaseSource):
    """Yields records from the bundled HTML fixtures."""

    def __init__(
        self,
        source_config: SourceConfig | None = None,
        fetcher: BaseFetcher | None = None,
        config: AppConfig | None = None,
        pages_dir: Path | None = None,
    ) -> None:
        super().__init__(source_config or sample_source_config(), fetcher, config)
        self.pages_dir = pages_dir or TEMPLATES_DIR

    def requires_network(self) -> bool:
        return False

    def iter_pages(self) -> Iterator[PageBatch]:
        first_page = self.source_config.page_start
        last_page = self.source_config.page_end
        yielded = 0

        for index, filename in enumerate(FIXTURE_PAGES, start=1):
            if yielded >= self.page_limit:
                return
            if index < first_page:
                continue
            if last_page is not None and index > last_page:
                return

            path = self.pages_dir / filename
            if not path.exists():
                raise SourceConfigError(f"sample fixture missing: {path}")

            soup = make_soup(path.read_text(encoding="utf-8"))
            page_url = path.as_uri()
            records: list[RawCompany] = []

            for item in select_items(soup, LIST_SELECTOR):
                extracted = extract_record(item, FIELD_RULES)
                name = (extracted.get("company_name") or "").strip()
                if not name:
                    continue
                if not extracted.get("email"):
                    emails = harvest_emails(item)
                    extracted["email"] = emails[0] if emails else None
                if not extracted.get("phone"):
                    phones = harvest_phones(item)
                    extracted["phone"] = phones[0] if phones else None
                records.append(
                    RawCompany(
                        **extracted,
                        source=self.label,
                        source_url=page_url,
                    )
                )

            log.info("{}: fixture page {} -> {} records", self.name, index, len(records))
            yield PageBatch(page_number=index, url=page_url, records=records)
            yielded += 1
