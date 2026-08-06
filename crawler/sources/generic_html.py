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
from crawler.documents import extract_records, is_wanted
from crawler.fetcher import BaseFetcher
from crawler.labels import MIN_PAIRS, parse_record, split_cjk_english
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
        # 設了頁面動作卻用 httpx，動作會被安靜地忽略——使用者只會看到「怎麼
        # 還是抓不到電話」。這種安靜失敗一定要講出來。
        engine = self.source_config.engine or self.config.crawler.engine
        if self.source_config.page_actions and engine != "playwright":
            log.warning(
                "{}: 這個來源設定了頁面動作（點按、捲動），但取頁面的方式是 "
                "{}，動作不會被執行。要讓它生效，這個來源的引擎要設成 "
                "playwright。",
                self.name, engine,
            )

        self._details_fetched = 0
        self._documents_fetched = 0
        # 同一份名冊 PDF 常常每一頁都掛同一個連結；下載一次就夠了。
        self._seen_documents: set[str] = set()

    def iter_pages(self) -> Iterator[PageBatch]:
        if self.fetcher is None:
            raise SourceConfigError(f"source {self.name!r} was given no fetcher")

        if self.source_config.query_loop is not None:
            yield from self._iter_query_loop_pages()
            return

        strategy = self.source_config.pagination.type
        first_page = self.source_config.page_start
        last_page = self.source_config.page_end

        # 接續上一次。只有網址帶頁碼的分頁（``query``）跳得過去；``next_link``
        # 沒有可定址的頁碼，第 5 頁的意思是「跟著下一頁連結走四次」，跳不了。
        if strategy == "query":
            resume_page = self._resume_index()
            if resume_page >= first_page:
                log.info("{}: 接續上一次，從第 {} 頁開始", self.name, resume_page + 1)
                first_page = resume_page + 1

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
                url,
                method=method,
                data=form_data,
                encoding=self.source_config.encoding,
                actions=self.source_config.page_actions,
                **self._modal_kwargs(),
            )
            fetched += 1
            soup = make_soup(result.html)

            in_range = page_number >= first_page and (
                last_page is None or page_number <= last_page
            )
            if in_range:
                items = select_items(soup, self.source_config.list_selector or "")
                records = self._records_for(items, result)
                records.extend(self._records_from_documents(soup, result.url))
                log.info("{}: page {} -> {} records", self.name, page_number, len(records))
                yield PageBatch(
                    page_number=page_number,
                    url=result.url,
                    records=records,
                    resume_key=str(page_number) if strategy == "query" else None,
                )
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

    def _iter_query_loop_pages(self) -> Iterator[PageBatch]:
        """逐項查詢：同一個查詢表單，換一組條件就查一次，每一次算一「頁」。

        這一類名錄不給完整清單，只給一個查詢框。每一組條件的結果就是它能給的
        全部——把條件逐一查過去，才是它的「翻頁」。
        """
        loop = self.source_config.query_loop
        assert loop is not None
        fetcher = self.fetcher
        assert fetcher is not None

        iterate = getattr(fetcher, "iter_query_pages", None)
        if iterate is None:
            raise SourceConfigError(
                f"來源 {self.name!r} 設了逐項查詢，但取頁面的方式不會操作網頁。"
                "這個來源的引擎要設成 playwright。"
            )

        skip = self._resume_index()
        if skip:
            log.info("{}: 接續上一次，跳過前 {} 個查詢條件", self.name, skip)

        page_number = 0
        for result in iterate(
            self.source_config.start_url or "",
            loop,
            actions=self.source_config.page_actions,
            skip_values=skip,
            **self._modal_kwargs(),
        ):
            page_number += 1
            soup = make_soup(result.html)
            items = select_items(soup, self.source_config.list_selector or "")
            records = self._records_for(items, result)
            log.info(
                "{}: 第 {} 個查詢狀態 -> {} 筆", self.name, page_number, len(records)
            )
            # ``done`` 是「已經整個做完的查詢條件數」。一個條件底下還有好幾列要
            # 往下點的時候，中途這幾批回報的仍然是前一個條件——不然接續時會把
            # 還沒點完的那一個整個跳過。
            done = getattr(result, "completed_values", None)
            yield PageBatch(
                page_number=page_number,
                url=result.url,
                records=records,
                resume_key=None if done is None else str(skip + done),
            )
            if page_number >= self.page_limit:
                return

    def _resume_index(self) -> int:
        """上一次做完的查詢條件數；沒有就是 0。"""
        try:
            return max(0, int(self.resume_from or 0))
        except (TypeError, ValueError):
            log.warning("{}: 看不懂的續跑進度 {!r}，從頭開始", self.name, self.resume_from)
            return 0

    def _modal_kwargs(self) -> dict:
        """設了「點開小視窗」才多傳這兩個參數。

        沒設就一個字都不多傳——取頁面的東西五花八門（測試裡的假物件、
        以後可能有的其他引擎），不該為了一個少見的設定要求全部都跟著改。
        """
        modal = self.source_config.detail_modal
        if modal is None:
            return {}
        return {"modal": modal, "list_selector": self.source_config.list_selector}

    def _records_for(self, items, result) -> list[RawCompany]:
        """把這一頁的每一列變成一筆資料，需要的話配上它自己的小視窗內容。

        ``details`` 是照列的順序來的，所以用位置對回去；長度不足時補空字串，
        絕不讓某一筆去撿到別人家的電話。
        """
        details = list(getattr(result, "details", []) or [])
        records = []
        for index, item in enumerate(items):
            modal_html = details[index] if index < len(details) else ""
            record = self._to_record(item, result.url, modal_html)
            if record is not None:
                records.append(record)
        return records

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

    def _merge_detail(
        self, record_values: dict, detail_url: str, extra_fields: dict[str, str]
    ) -> None:
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

        # 明細頁才是「標籤︰值」排版最常出現的地方——列表頁只給名稱，明細頁
        # 才把負責人、傳真、統編一項一項列出來。
        self._harvest_labels(soup, record_values, extra_fields)

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

    def _records_from_documents(self, soup, page_url: str) -> list[RawCompany]:
        """讀頁面上連出去的 PDF／Excel／Word，把裡面的名單也收進來。

        只有使用者在來源上勾選了格式才會執行；沒勾就一個檔案都不下載。
        下載並解析別人的檔案跟讀網頁不是同一件事，該由使用者明確決定。
        """
        wanted = self.source_config.document_kinds
        if not wanted or self.fetcher is None:
            return []

        collected: list[RawCompany] = []
        for anchor in soup.find_all("a", href=True):
            if self._documents_fetched >= self.source_config.max_documents:
                log.info("{}: 已達檔案讀取上限 {}", self.name,
                         self.source_config.max_documents)
                break

            href = anchor["href"]
            if not isinstance(href, str) or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            target = urljoin(page_url, href)
            if not is_wanted(target, wanted) or target in self._seen_documents:
                continue
            self._seen_documents.add(target)

            try:
                page = self.fetcher.fetch(target)
            except RobotsDisallowedError:
                raise
            except CrawlError as exc:
                log.debug("檔案讀取失敗 {}：{}", target, exc)
                continue
            self._documents_fetched += 1

            try:
                result = extract_records(page.raw or page.html.encode(), target, self.label)
            except CrawlError as exc:
                log.warning("{} 解析失敗：{}", target, exc)
                continue

            for record in result.records:
                record.source_url = target
            collected.extend(result.records)

        return collected

    def _harvest_labels(self, scope, values: dict, extra_fields: dict[str, str]) -> None:
        """從「標籤︰值」的文字排版補欄位，並收下沒有對應欄位的部分。

        只補選擇器沒抓到的欄位——設定好的規則永遠優先，這裡是補漏不是覆蓋。

        為什麼要有這一段：舊式的公會名錄整頁只有 ``<table>`` 與 ``<font>``，
        沒有 class 也沒有標題標籤，「負責人」那一欄根本沒有 CSS 選擇器指得到。
        唯一穩定的線索是標籤就寫在值前面，所以只能從文字解析。
        """
        if not self.source_config.label_fields:
            return

        parsed = parse_record(scope.get_text(" ", strip=True))
        if parsed.pair_count < MIN_PAIRS:
            return          # 一兩個冒號可能只是內文，不是這種排版

        for name, value in parsed.fields.items():
            if name in _KNOWN_FIELDS and not values.get(name):
                values[name] = value

        # 標題列常常是「中文名稱　English Name」並排，兩個都要。
        if not values.get("english_name"):
            _chinese, english = split_cjk_english(parsed.heading)
            if english:
                values["english_name"] = english

        for label, value in parsed.extra.items():
            extra_fields.setdefault(label, value)

    def _merge_modal(self, record_values: dict, html: str, extra_fields: dict) -> None:
        """把點開的小視窗裡的內容補進這一筆。

        列表頁的值優先——它已經在幾十筆上驗證過，小視窗只有一個樣本。
        彈出視窗幾乎都是「負責人：…／連絡電話：…」這種排版，所以主力是
        標籤解析，跟明細頁走的是同一條路。
        """
        soup = make_soup(html)
        rules = self.source_config.detail_fields
        if rules:
            for key, value in extract_record(soup, rules, "").items():
                if value and not record_values.get(key):
                    record_values[key] = value

        self._harvest_labels(soup, record_values, extra_fields)

        if not record_values.get("email"):
            emails = harvest_emails(soup)
            if emails:
                record_values["email"] = emails[0]
        if not record_values.get("phone"):
            phones = harvest_phones(soup)
            if phones:
                record_values["phone"] = phones[0]

    def _to_record(
        self, item, page_url: str, modal_html: str = ""
    ) -> RawCompany | None:
        """Turn one list item into a record, or ``None`` when it has no name."""
        extracted = extract_record(item, self.source_config.fields, page_url)

        name = (extracted.get("company_name") or "").strip()
        if not name:
            return None

        extra_fields: dict[str, str] = {}
        detail_url = self._detail_url(item, page_url)
        if detail_url:
            self._merge_detail(extracted, detail_url, extra_fields)
        if modal_html:
            self._merge_modal(extracted, modal_html, extra_fields)

        self._harvest_labels(item, extracted, extra_fields)

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
            extra_fields=extra_fields,
        )
