"""Config-driven scraper for server-rendered directory listings.

Everything it needs comes from a :class:`~core.config.SourceConfig`: the start
URL, a selector for repeated list items, a CSS rule per field, and a pagination
strategy. That covers the large majority of B2B directories without new code.

Pagination strategies
---------------------
``query``     ``start_url`` (or ``form_data``) contains ``{page}``; the number
              increments.
``next_link`` follow a "next page" anchor until it disappears.
``none``      single page only.

Most directories are GET with UTF-8 responses, which needs nothing beyond the
above. A minority -- typically older trade-association sites -- are POST-only
search forms (``method``/``form_data``) and/or answer in a legacy charset
(``encoding``); see :class:`~core.config.SourceConfig` for both.
"""

from __future__ import annotations

from collections.abc import Iterator

from urllib.parse import urljoin

from core.config import AppConfig, SourceConfig
from core.constants import LogCategory
from core.errors import CrawlError, RobotsDisallowedError, SourceConfigError
from core.logging_setup import get_logger
from core.schemas import RawCompany
from crawler.base import BaseSource, PageBatch
from crawler.fetcher import BaseFetcher
from crawler.parser import (
    extract_field,
    extract_record,
    find_next_url,
    harvest_emails,
    harvest_phones,
    make_soup,
    select_items,
)

log = get_logger(LogCategory.CRAWL)

# Field names that map straight onto RawCompany.
_KNOWN_FIELDS = {
    "company_name",
    "tax_id",
    "email",
    "phone",
    "website",
    "address",
    "industry",
    "english_name",
    "fax",
    "products",
    "contact_person",
}


class GenericHtmlSource(BaseSource):
    """Scrape a paginated HTML listing described entirely in configuration."""

    def __init__(
        self,
        source_config: SourceConfig,
        fetcher: BaseFetcher | None = None,
        config: AppConfig | None = None,
    ) -> None:
        super().__init__(source_config, fetcher, config)
        if not source_config.start_url or not source_config.list_selector:
            raise SourceConfigError(
                f"source {source_config.name!r} needs both start_url and list_selector"
            )
        self._details_fetched = 0

    def iter_pages(self) -> Iterator[PageBatch]:
        if self.fetcher is None:
            raise SourceConfigError(f"source {self.name!r} was given no fetcher")

        strategy = self.source_config.pagination.type
        first_page = self.source_config.page_start
        last_page = self.source_config.page_end

        # Link-based pagination has no addressable page numbers: reaching page
        # 5 means following four "next" links. So a range starting above 1 has
        # to walk the earlier pages, it just does not collect from them.
        if strategy == "next_link":
            url: str | None = self._page_url(1)
            page_number = 1
        else:
            url = self._page_url(first_page)
            page_number = first_page

        yielded = 0
        fetched = 0
        # Skipped pages still cost a request, so they count against the budget.
        budget = self.page_limit + (first_page - 1 if strategy == "next_link" else 0)

        while url is not None and fetched < budget:
            method = self.source_config.method
            form_data = self._form_data(page_number) if method == "POST" else None
            result = self.fetcher.fetch(
                url, method=method, data=form_data, encoding=self.source_config.encoding
            )
            fetched += 1
            soup = make_soup(result.html)

            in_range = page_number >= first_page and (
                last_page is None or page_number <= last_page
            )
            if in_range:
                items = select_items(soup, self.source_config.list_selector or "")
                records = [self._to_record(item, result.url) for item in items]
                records = [r for r in records if r is not None]
                log.info("{}: page {} -> {} records", self.name, page_number, len(records))
                yield PageBatch(page_number=page_number, url=result.url, records=records)
                yielded += 1
            else:
                log.debug("{}: 略過第 {} 頁（不在指定範圍內）", self.name, page_number)

            if last_page is not None and page_number >= last_page:
                log.info("{}: 已到達指定的結束頁 {}", self.name, last_page)
                return
            if yielded >= self.page_limit:
                return
            if strategy == "none":
                return

            if strategy == "next_link":
                selector = self.source_config.pagination.next_selector or ""
                url = find_next_url(soup, selector, result.url)
                if url is None:
                    log.info("{}: no next-page link, stopping", self.name)
                    return
            else:  # query
                url = self._page_url(page_number + 1)
            page_number += 1

    def _page_url(self, page_number: int) -> str:
        template = self.source_config.start_url or ""
        return template.replace("{page}", str(page_number))

    def _form_data(self, page_number: int) -> dict[str, str]:
        """展開 form_data 樣板中的 {page} 佔位，回傳這一頁要送出的表單欄位。

        有些查詢頁（例如 POST 表單的公會網站）固定用同一個網址，換頁只是
        送出的欄位值不同，這時 {page} 就代入 form_data 而不是網址。
        """
        template = self.source_config.form_data or {}
        return {key: value.replace("{page}", str(page_number)) for key, value in template.items()}

    def _detail_url(self, item, page_url: str) -> str | None:
        """Absolute URL of this item's detail page, if the source defines one."""
        rule = self.source_config.detail_link
        if rule is None:
            return None
        href = extract_field(item, rule, page_url)
        if not href:
            return None
        absolute = urljoin(page_url, href)
        return absolute if absolute.startswith(("http://", "https://")) else None

    def _merge_detail(self, record_values: dict, detail_url: str) -> None:
        """Fetch a detail page and fill in whatever the list page lacked.

        List-page values win: they are cheaper and already verified against
        many items, whereas a detail page is a single sample.
        """
        if self.fetcher is None or self._details_fetched >= self.source_config.max_details:
            return
        try:
            # 明細頁一律用 GET（表單方法只套用在列表/分頁請求上），但編碼
            # 設定通常整站一致，所以沿用同一個 encoding。
            page = self.fetcher.fetch(detail_url, encoding=self.source_config.encoding)
        except RobotsDisallowedError:
            raise
        except CrawlError as exc:
            log.debug("明細頁讀取失敗 {}：{}", detail_url, exc)
            return
        self._details_fetched += 1

        soup = make_soup(page.html)
        rules = self.source_config.detail_fields
        detail_values = extract_record(soup, rules, page.url) if rules else {}

        for key, value in detail_values.items():
            if value and not record_values.get(key):
                record_values[key] = value

        # Even without explicit rules, a detail page is the natural place to
        # find the contact address the list page omitted.
        if not record_values.get("email"):
            emails = harvest_emails(soup)
            if emails:
                record_values["email"] = emails[0]
        if not record_values.get("phone"):
            phones = harvest_phones(soup)
            if phones:
                record_values["phone"] = phones[0]

    def _to_record(self, item, page_url: str) -> RawCompany | None:
        """Turn one list item into a record, or ``None`` when it has no name."""
        extracted = extract_record(item, self.source_config.fields, page_url)

        name = (extracted.get("company_name") or "").strip()
        if not name:
            return None

        detail_url = self._detail_url(item, page_url)
        if detail_url:
            self._merge_detail(extracted, detail_url)

        # Configured selectors win; page-wide harvesting only fills gaps.
        if not extracted.get("email"):
            emails = harvest_emails(item)
            if emails:
                extracted["email"] = emails[0]
        if not extracted.get("phone"):
            phones = harvest_phones(item)
            if phones:
                extracted["phone"] = phones[0]

        known = {k: v for k, v in extracted.items() if k in _KNOWN_FIELDS}
        extra = {k: v for k, v in extracted.items() if k not in _KNOWN_FIELDS and v}

        return RawCompany(
            **known,
            source=self.label,
            source_url=page_url,
            extra=extra,
        )
