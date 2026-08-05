"""Controller behind the "paste a URL" source wizard.

Keeps :mod:`gui.pages.source_wizard` from importing ``crawler.*`` or
``core.config`` directly -- the page only ever talks to
:class:`SourceWizardController`, exactly like every other page talks to a
controller in :mod:`gui.controllers`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from core.config import SourceConfig, get_config
from core.errors import CrawlError, SourceConfigError

#: Fields shown (and offered for "add field") in the wizard, in a sane order.
KNOWN_FIELDS: tuple[str, ...] = (
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
)

#: Columns shown in the preview table.
PREVIEW_FIELDS: tuple[str, ...] = (
    "company_name",
    "email",
    "phone",
    "website",
    "address",
    "industry",
)


class SourceWizardController:
    """Discovery, editing, and persistence for one custom crawl source."""

    def analyse(self, url: str, *, report: Callable[[Any], None], cancel_event) -> Any:
        """Fetch ``url`` and guess a scraping recipe. Suitable as a task worker.

        Runs through :func:`crawler.discover.discover`, so robots.txt and the
        configured crawl delay apply exactly as they would to a real crawl.
        """
        from crawler.discover import discover

        report({"stage": "fetching", "url": url})
        result = discover(url)
        report({"stage": "done"})
        return result

    def crawl_delay(self) -> float:
        """設定裡的請求間隔秒數。介面用它估算「這會跑多久」。"""
        from core.config import get_config

        return float(get_config().crawler.delay_seconds)

    def explore(
        self,
        url: str,
        *,
        report: Callable[[Any], None],
        cancel_event,
        page_budget: int | None = None,
        document_kinds: list[str] | None = None,
    ) -> Any:
        """在整個網站裡找名錄頁。Suitable as a task worker。

        跟 :meth:`analyse` 走同一套 fetcher，所以 robots.txt 與請求間隔延遲
        一樣適用。``page_budget`` 是硬上限，同時決定要等多久。
        """
        from crawler.explore import DEFAULT_PAGE_BUDGET, explore

        budget = page_budget or DEFAULT_PAGE_BUDGET

        def progress(done: int, total: int, current: str) -> None:
            report({"stage": "exploring", "done": done, "total": total, "url": current})

        report({"stage": "starting", "total": budget})
        # 取消時回傳已經找到的東西，不是丟掉。使用者按取消多半是「夠了」，
        # 而不是「這些我不要」——把找到的名錄一起丟掉等於白等那幾十秒。
        result = explore(
            url,
            page_budget=budget,
            on_progress=progress,
            cancel_event=cancel_event,
            document_kinds=document_kinds or (),
        )
        report({"stage": "done"})
        return result

    def build_source(
        self,
        url: str,
        name: str,
        list_selector: str,
        field_rules: dict[str, dict[str, Any]],
        next_selector: str | None,
        max_pages: int | None = None,
        detail_link_selector: str | None = None,
        max_details: int | None = None,
        default_industry: str = "",
        collect_fields: list[str] | None = None,
        document_kinds: list[str] | None = None,
        page_actions: list[dict] | None = None,
        page_start: int = 1,
        page_end: int | None = None,
    ) -> SourceConfig:
        """Turn user-edited selectors into a validated :class:`SourceConfig`.

        Raises :class:`SourceConfigError` for anything a human needs to fix
        before this can be saved -- an empty name, no list selector, no
        company-name rule, or a selector pydantic itself rejects.
        """
        from core.config import FieldRule, PageAction, PaginationRule

        clean_name = name.strip()
        if not clean_name:
            raise SourceConfigError("請輸入來源名稱。")
        if not list_selector.strip():
            raise SourceConfigError("請輸入清單（每一筆資料）的 CSS 選擇器。")

        fields: dict[str, FieldRule] = {}
        for field_name, rule in field_rules.items():
            selector = (rule.get("selector") or "").strip()
            if not selector:
                continue
            try:
                fields[field_name] = FieldRule(
                    selector=selector,
                    attr=(rule.get("attr") or "text").strip() or "text",
                    regex=(rule.get("regex") or None),
                )
            except ValidationError as exc:
                raise SourceConfigError(
                    f"欄位「{field_name}」的設定無效：{exc}"
                ) from exc

        if "company_name" not in fields:
            raise SourceConfigError("必須設定「公司名稱」欄位的選擇器才能儲存來源。")

        pagination = (
            PaginationRule(type="next_link", next_selector=next_selector)
            if next_selector
            else PaginationRule(type="none")
        )

        # Detail-page following has to survive an edit. Directories that list
        # only names keep the e-mail one click away, so dropping this silently
        # when the user tweaks a selector would quietly halve what a crawl finds.
        detail_link = (
            FieldRule(selector=detail_link_selector.strip(), attr="href")
            if detail_link_selector and detail_link_selector.strip()
            else None
        )

        try:
            return SourceConfig(
                name=clean_name,
                type="generic_html",
                enabled=True,
                start_url=url,
                list_selector=list_selector.strip(),
                max_pages=max_pages,
                pagination=pagination,
                fields=fields,
                detail_link=detail_link,
                max_details=max_details if max_details is not None else 100,
                label=clean_name,
                default_industry=default_industry.strip(),
                collect_fields=list(collect_fields or []),
                document_kinds=list(document_kinds or []),
                page_actions=[PageAction(**a) for a in (page_actions or [])],
                page_start=page_start,
                page_end=page_end,
            )
        except ValidationError as exc:
            raise SourceConfigError(f"來源設定無效：{exc}") from exc

    def save(self, source_or_result: Any, name: str, enabled: bool = True) -> str:
        """Persist a source built by :meth:`build_source` or ``discover()``.

        ``source_or_result`` may be a ready :class:`SourceConfig` or a
        ``DiscoveryResult`` (in which case ``to_source_config`` builds it).
        """
        from core.config import save_custom_source

        clean_name = name.strip()
        if not clean_name:
            raise SourceConfigError("請輸入來源名稱。")

        if isinstance(source_or_result, SourceConfig):
            source = source_or_result.model_copy(
                update={"name": clean_name, "label": clean_name, "enabled": enabled}
            )
        else:
            source = source_or_result.to_source_config(clean_name, enabled)

        save_custom_source(source)
        return source.name

    def preview_with(
        self,
        url: str,
        list_selector: str,
        field_rules: dict[str, dict[str, Any]],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Re-fetch ``url`` and apply user-edited selectors, for a fresh preview.

        Goes through the normal fetcher (robots.txt, delay) just like
        :meth:`analyse`, then extracts records with the rules the user is
        currently editing rather than the original guess.
        """
        from core.config import FieldRule
        from crawler.fetcher import build_fetcher
        from crawler.parser import extract_record, make_soup, select_items

        if not list_selector.strip():
            raise SourceConfigError("請輸入清單（每一筆資料）的 CSS 選擇器。")

        rules: dict[str, FieldRule] = {}
        for field_name, rule in field_rules.items():
            selector = (rule.get("selector") or "").strip()
            if not selector:
                continue
            rules[field_name] = FieldRule(
                selector=selector,
                attr=(rule.get("attr") or "text").strip() or "text",
                regex=(rule.get("regex") or None),
            )

        config = get_config()
        fetcher = build_fetcher(config)
        try:
            page = fetcher.fetch(url)
        finally:
            fetcher.close()

        soup = make_soup(page.html)
        items = select_items(soup, list_selector.strip())
        if not items:
            raise CrawlError(f"清單選擇器 {list_selector!r} 在頁面上沒有比對到任何區塊。")

        records: list[dict[str, Any]] = []
        for item in items[:limit]:
            values = extract_record(item, rules, page.url)
            if not (values.get("company_name") or "").strip():
                continue
            records.append(values)
        return records

    def custom_sources(self) -> list[dict[str, Any]]:
        from core.config import read_custom_sources

        return read_custom_sources()

    def delete(self, name: str) -> bool:
        from core.config import delete_custom_source

        return delete_custom_source(name)

    def test_run(self, name: str, *, report: Callable[[Any], None], cancel_event) -> Any:
        """Run a real crawl of ``name``, capped to one page, as a smoke test."""
        from controllers.core import CrawlController

        controller = CrawlController()
        return controller.run(name, max_pages=1, report=report, cancel_event=cancel_event)
