"""Crawl orchestration: fetch -> parse -> clean -> dedupe -> store.

The pipeline owns everything a source deliberately does not: the page budget,
cancellation, the database transaction, and the run record that the Dashboard
reads.

Records are committed **per page**, so a long crawl that is cancelled or fails
half-way keeps what it already collected. Cross-page duplicates are resolved by
:meth:`~database.repository.CompanyRepository.upsert`, which matches on several
identity signals rather than on the dedupe key alone.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from datetime import datetime

from core.config import AppConfig, SourceConfig, get_config
from core.constants import CrawlStatus, LogCategory
from core.errors import CrawlError, RobotsDisallowedError
from core.logging_setup import get_logger
from core.schemas import CrawlSummary, RawCompany
from crawler.base import BaseSource
from crawler.fetcher import BaseFetcher, build_fetcher
from crawler.sources import build_source
from database.models import now
from database.repository import CompanyRepository, CrawlJobRepository
from database.session import session_scope
from verifier.dedupe import deduplicate_batch
from verifier.mx import MXChecker
from verifier.service import CleaningService

log = get_logger(LogCategory.CRAWL)

ProgressCallback = Callable[[str, int, int, int], None]
"""``(source_name, page_number, records_stored_so_far, total_pages)``。

``total_pages`` 是這一次的頁數預算。有它畫面上才做得出真的進度條——不定進度
那條來回跑的橫槓只回答「有沒有當掉」，而逐項查詢一趟可能一個多小時。
"""


#: 使用者可以勾選要不要收集的欄位。``company_name`` 不在裡面——它是必填的，
#: 不收集就等於整筆不要，那是「不要爬」而不是「不要這個欄位」。
COLLECTABLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("tax_id", "統一編號"),
    ("email", "電子信箱"),
    ("phone", "電話"),
    ("website", "網站"),
    ("address", "地址"),
    ("industry", "產業"),
    ("english_name", "英文名稱"),
    ("fax", "傳真"),
    ("products", "主要產品"),
    ("contact_person", "聯絡人"),
)


def _keep_only(records: list[RawCompany], keep: set[str] | None) -> None:
    """把沒有被勾選的欄位清空。

    存在的理由跟匯出頁可以挑欄位一樣：使用者只想要公司名稱與信箱時，
    多抓回來的地址與統編只是雜訊，還會讓「疑似重複」的比對多出無謂的維度。

    在這裡清空而不是在解析階段跳過：解析規則是來源設定的一部分（會被存進
    custom_sources.yaml 重複使用），不該被「這一次執行想要什麼」改寫。
    """
    if keep is None:
        return
    for record in records:
        for field, _label in COLLECTABLE_FIELDS:
            if field not in keep:
                setattr(record, field, None)


#: 抓到的筆數掉到上一次的這個比例以下，就當成「這個網站可能改版了」。
#:
#: 0.4 是刻意寬鬆的。名錄本來就會增減，有些站每個月換一批廠商；抓到六成
#: 還在正常範圍。真正要攔的是 0 筆、以及「240 筆變 12 筆」那種斷崖。
HEALTH_DROP_RATIO = 0.4


def _health_warning(job_repo, source: str, job, summary: CrawlSummary) -> str | None:
    """跟上一次比，這次是不是掉得不合理。

    這是整套爬取最難發現的一種壞掉：網站改版之後選擇器失效，爬取會「成功」
    地抓到 0 筆，畫面上寫著完成，而排程是半夜自己跑的，沒有人在看。跟上一次
    比對是唯一能自動看出來的方式，而每一次的筆數本來就都存著了。
    """
    # 失敗與取消本來就有自己的訊息，再加一句「抓得比上次少」只是雜訊。
    if summary.status not in (CrawlStatus.SUCCESS.value, CrawlStatus.PARTIAL.value):
        return None

    # 抓到了東西、卻一筆都沒存進去。這跟「抓不到」是完全不同的毛病：頁面讀到
    # 了、重複區塊也找對了，是「公司名稱」那一欄指到了別的位置（子分類代號、
    # 表頭、導覽列）。以前這種情形畫面上只寫「找到 21、新增 0、拒絕 21」，
    # 三個數字擺在一起沒有人看得出那是壞的——這一句要直接講出來。
    #
    # 放在「跟上一次比」的前面：第一次跑就抓錯的話根本沒有上一次可以比，而
    # 那正是最需要被提醒的時候。
    if summary.records_found and summary.records_new == 0 and summary.records_updated == 0:
        if summary.records_invalid >= summary.records_found:
            return (
                f"抓到 {summary.records_found} 筆，但全部都不是公司資料，一筆都沒有存進去。"
                "通常是「公司名稱」抓到了別的位置——到日誌頁看它實際抓到什麼文字，"
                "再重新分析一次這個來源。"
            )

    # 接續上一次的執行本來就只做剩下的部分，筆數少是正常的。
    if summary.resumed:
        return None

    try:
        before = job_repo.last_harvest_for(source, before_id=job.id)
    except Exception as exc:                  # noqa: BLE001 - 提醒不該弄壞爬取
        log.debug("讀不到上一次的筆數：{}", exc)
        return None
    if not before:
        return None                            # 第一次跑，沒有東西可以比

    now_found = summary.records_found
    if now_found == 0:
        return (
            f"這次一筆都沒抓到，上一次有 {before} 筆。"
            "這個網站很可能改版了，請重新分析一次這個來源。"
        )
    if now_found < before * HEALTH_DROP_RATIO:
        return (
            f"這次只抓到 {now_found} 筆，上一次有 {before} 筆。"
            "掉這麼多通常代表網站的版面變了，建議重新分析確認一次。"
        )
    return None


def _apply_default_industry(records: list[RawCompany], default: str) -> int:
    """把來源宣告的產業補到沒有產業的紀錄上，回傳補了幾筆。

    台灣的名錄網站幾乎都是「一個分類一個頁面」——工業會依公會分類、iyp 依
    產業分類。分類本身就是產業，但它寫在麵包屑或頁面標題裡，不在每一列的
    資料中，所以逐列抓的欄位規則抓不到它，產業欄就永遠是空的。

    只補空的。頁面自己有寫產業時那個比較準，不要蓋掉。
    """
    default = (default or "").strip()
    if not default:
        return 0
    filled = 0
    for record in records:
        if not (record.industry or "").strip():
            record.industry = default
            filled += 1
    return filled


def _with_page_range(
    source_config: SourceConfig, page_start: int | None, page_end: int | None
) -> SourceConfig:
    """Apply a per-run page range without mutating the stored source."""
    if page_start is None and page_end is None:
        return source_config
    return source_config.model_copy(
        update={
            "page_start": page_start if page_start is not None else source_config.page_start,
            "page_end": page_end if page_end is not None else source_config.page_end,
        }
    )


class CrawlPipeline:
    """Runs configured sources and persists what they find."""

    def __init__(
        self,
        config: AppConfig | None = None,
        fetcher: BaseFetcher | None = None,
    ) -> None:
        self.config = config or get_config()
        self._fetcher = fetcher
        self._owns_fetcher = fetcher is None
        # 每一種引擎各一個，重複使用。以前只有一個共用的，因為引擎是全域設定；
        # 現在來源可以自己指定「這個網站要用瀏覽器」，一次執行就可能同時用到
        # 兩種。仍然是每種一個而不是每個來源一個——開瀏覽器很貴。
        self._fetchers: dict[str, BaseFetcher] = {}

    @staticmethod
    def _fields_for(source_config: SourceConfig) -> set[str] | None:
        """這個來源要收集哪些欄位。``None`` 代表全部。

        設定住在來源上而不是「每次執行時勾一勾」：排程爬取跑的時候沒有人在
        介面前面勾選，放在畫面上的話對排程完全沒有作用。

        公司名稱一律保留——那是必填欄位，丟掉整筆資料就沒有意義了。
        """
        chosen = source_config.collect_fields
        return set(chosen) | {"company_name"} if chosen else None

    # ------------------------------------------------------------------ api

    def run_source(
        self,
        source_name: str,
        progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        max_pages: int | None = None,
        page_start: int | None = None,
        page_end: int | None = None,
    ) -> CrawlSummary:
        """Crawl one configured source and store the results.

        ``page_start``/``page_end`` override the source's own range for this
        run only -- the saved source definition is left untouched.
        """
        source_config = self.config.crawler.source(source_name)
        source_config = _with_page_range(source_config, page_start, page_end)
        return self._run(source_config, progress, cancel_event, max_pages)

    def run_source_config(
        self,
        source_config: SourceConfig,
        progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        max_pages: int | None = None,
    ) -> CrawlSummary:
        """Crawl a source definition that is not in config.yaml.

        This is what the URL wizard and ``crawl --url`` use: a recipe worked
        out on the spot can be run without being saved first.
        """
        return self._run(source_config, progress, cancel_event, max_pages)

    def run_all(
        self,
        progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        max_pages: int | None = None,
        page_start: int | None = None,
        page_end: int | None = None,
    ) -> list[CrawlSummary]:
        """Crawl every enabled source, one after another.

        A failure in one source is recorded and the run continues -- one broken
        directory should not abort the rest of the night's work.
        """
        summaries: list[CrawlSummary] = []
        for source_config in self.config.crawler.enabled_sources():
            if cancel_event is not None and cancel_event.is_set():
                log.warning("crawl cancelled before {}", source_config.name)
                break
            summaries.append(
                self._run(
                    _with_page_range(source_config, page_start, page_end),
                    progress,
                    cancel_event,
                    max_pages,
                )
            )
        return summaries

    def close(self) -> None:
        if self._owns_fetcher and self._fetcher is not None:
            self._fetcher.close()
            self._fetcher = None
        for fetcher in self._fetchers.values():
            fetcher.close()
        self._fetchers.clear()

    def __enter__(self) -> "CrawlPipeline":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -------------------------------------------------------------- internal

    def _get_fetcher(self, source: BaseSource) -> BaseFetcher | None:
        """Lazily build a fetcher for this source; offline sources need none."""
        if not source.requires_network():
            return None
        # 外面塞進來的優先。測試與命令列會這樣做，那時候「用哪一種引擎」已經
        # 由呼叫端決定了，這裡不該再自己造一個。
        if self._fetcher is not None:
            return self._fetcher

        engine = source.source_config.engine or self.config.crawler.engine
        fetcher = self._fetchers.get(engine)
        if fetcher is None:
            fetcher = build_fetcher(self.config, engine=engine)
            self._fetchers[engine] = fetcher
        return fetcher

    def _run(
        self,
        source_config: SourceConfig,
        progress: ProgressCallback | None,
        cancel_event: threading.Event | None,
        max_pages: int | None,
    ) -> CrawlSummary:
        started = now()
        summary = CrawlSummary(
            source=source_config.name,
            status=CrawlStatus.RUNNING.value,
            started_at=started,
        )

        source = build_source(source_config, None, self.config)
        source.fetcher = self._get_fetcher(source)

        page_budget = min(max_pages, source.page_limit) if max_pages else source.page_limit
        log.info("crawl started: {} (up to {} pages)", source_config.name, page_budget)

        with session_scope() as session:
            job_repo = CrawlJobRepository(session)
            job = job_repo.start(source_config.name)
            session.commit()

            # 上一次沒跑完的話從那裡接下去。逐項查詢一趟可能好幾個小時，第 80
            # 個條件時網路斷一下就整批重來，那是白做工。
            previous = job_repo.previous_for(source_config.name, before_id=job.id)
            if previous is not None and previous.resume_state:
                source.resume_from = previous.resume_state
                summary.resumed = True
                log.info(
                    "{}: 接續上一次未完成的執行（進度 {}）",
                    source_config.name, previous.resume_state,
                )

            repo = CompanyRepository(session)
            mx = MXChecker(self.config, session) if self.config.verifier.check_mx else None
            cleaner = CleaningService(self.config, mx)

            try:
                for batch in source.iter_pages():
                    if cancel_event is not None and cancel_event.is_set():
                        summary.status = CrawlStatus.CANCELLED.value
                        log.warning("crawl cancelled during {}", source_config.name)
                        break

                    summary.pages_crawled += 1
                    summary.records_found += len(batch.records)

                    _apply_default_industry(batch.records, source_config.default_industry)
                    _keep_only(batch.records, self._fields_for(source_config))
                    self._store_page(batch.records, repo, cleaner, summary)
                    # 進度跟資料同一個交易寫入。分開寫的話，兩者之間斷電就會
                    # 出現「進度說做完了、資料卻沒存進去」的空洞。
                    if batch.resume_key is not None:
                        job.resume_state = batch.resume_key
                    session.commit()

                    if progress is not None:
                        progress(
                            source_config.name,
                            batch.page_number,
                            summary.records_new,
                            page_budget,
                        )

                    if batch.is_empty and self.config.crawler.stop_on_empty_page:
                        log.info("{}: empty page, stopping early", source_config.name)
                        break
                    if summary.pages_crawled >= page_budget:
                        log.info("{}: page budget reached", source_config.name)
                        break

                if summary.status == CrawlStatus.RUNNING.value:
                    summary.status = (
                        CrawlStatus.SUCCESS.value
                        if summary.records_invalid == 0
                        else CrawlStatus.PARTIAL.value
                    )

            except RobotsDisallowedError as exc:
                summary.status = CrawlStatus.FAILED.value
                summary.error = str(exc)
                log.error("crawl blocked by robots.txt: {}", exc)
            except CrawlError as exc:
                summary.status = CrawlStatus.FAILED.value
                summary.error = str(exc)
                log.error("crawl failed for {}: {}", source_config.name, exc)
            except Exception as exc:  # unexpected: record it, do not lose the run
                summary.status = CrawlStatus.FAILED.value
                summary.error = f"{type(exc).__name__}: {exc}"
                log.exception("unexpected error crawling {}", source_config.name)

            summary.warning = _health_warning(job_repo, source_config.name, job, summary)
            if summary.warning:
                log.warning("{}: {}", source_config.name, summary.warning)

            summary.finished_at = now()
            job_repo.finish(job, summary)
            # 跑完了就沒有東西可以接續。留著的話下一次會從中間開始，永遠抓不到
            # 前面那幾頁。
            if summary.status in (CrawlStatus.SUCCESS.value, CrawlStatus.PARTIAL.value):
                job.resume_state = None

        log.info(
            "crawl finished: {} [{}] {} pages, {} found, {} new, {} merged, "
            "{} duplicates, {} rejected in {:.1f}s",
            summary.source,
            summary.status,
            summary.pages_crawled,
            summary.records_found,
            summary.records_new,
            summary.records_updated,
            summary.records_duplicate,
            summary.records_invalid,
            summary.duration_seconds,
        )
        return summary

    @staticmethod
    def _store_page(
        records: Iterable[RawCompany],
        repo: CompanyRepository,
        cleaner: CleaningService,
        summary: CrawlSummary,
    ) -> None:
        """Dedupe within the page, clean, then upsert each record."""
        records = list(records)
        unique, dropped_in_page = deduplicate_batch(records)
        summary.records_duplicate += dropped_in_page

        cleaned, dropped = cleaner.clean_batch(unique)
        summary.records_invalid += len(dropped)

        if dropped:
            # 被丟掉的**那幾筆的公司名稱欄長什麼樣**，就是「選擇器指到了什麼
            # 位置」最直接的證據。以前這裡只把數字加一加，使用者看到的是
            # 「拒絕 21」，完全不知道被丟掉的是什麼、為什麼。
            #
            # 一筆都沒留下來跟只丟掉幾筆是兩件事：前者代表整個抓錯位置（要
            # 重新分析），後者常常是正常的（同一頁上混著表頭、分類代號）。
            # 所以講法不一樣，嚴重度也不一樣。
            names = ", ".join(
                repr((record.company_name or "").strip()[:24]) for record in dropped[:5]
            )
            more = f"…等 {len(dropped)} 筆" if len(dropped) > 5 else ""
            if not cleaned:
                log.warning(
                    "這一頁的 {} 筆全部不是公司資料，被丟掉了。"
                    "「公司名稱」抓到的是：{}{}",
                    len(dropped), names, more,
                )
            else:
                log.info(
                    "這一頁有 {} 筆不是公司資料，被丟掉了（另外 {} 筆有存進去）。"
                    "被丟掉的「公司名稱」是：{}{}",
                    len(dropped), len(cleaned), names, more,
                )

        for record in cleaned:
            _, merged = repo.upsert(record)
            if merged:
                summary.records_updated += 1
                summary.records_duplicate += 1
            else:
                summary.records_new += 1


def crawl(
    source: str | None = None,
    config: AppConfig | None = None,
    progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
    max_pages: int | None = None,
    page_start: int | None = None,
    page_end: int | None = None,
) -> list[CrawlSummary]:
    """Convenience entry point: crawl one source, or every enabled source."""
    with CrawlPipeline(config) as pipeline:
        if source:
            return [
                pipeline.run_source(
                    source, progress, cancel_event, max_pages, page_start, page_end
                )
            ]
        return pipeline.run_all(progress, cancel_event, max_pages, page_start, page_end)


def last_crawl_time() -> datetime | None:
    """When the most recent crawl started, or ``None`` if there has been none."""
    with session_scope() as session:
        job = CrawlJobRepository(session).last()
        return job.started_at if job else None
