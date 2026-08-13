"""Source abstraction.

A *source* knows how to walk one directory and yield pages of records. It does
not know about databases, cleaning or deduplication -- :mod:`crawler.pipeline`
owns all of that. Adding a new site means implementing :meth:`BaseSource.iter_pages`
(or, more often, just adding YAML for :class:`~crawler.sources.generic_html.GenericHtmlSource`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field

from core.config import AppConfig, SourceConfig, get_config
from core.schemas import RawCompany
from crawler.fetcher import BaseFetcher


@dataclass(slots=True)
class PageBatch:
    """Records harvested from a single page."""

    page_number: int
    url: str
    records: list[RawCompany] = field(default_factory=list)
    #: 「到這一批為止，已經完成到哪裡」，意義由來源自己定義（第幾頁、第幾個
    #: 查詢條件）。``None`` 代表這個來源沒辦法從中間接續。
    #:
    #: 只在**完全做完**的進度上前進：一個查詢條件底下還有好幾層要點的時候，
    #: 中途那幾批要回報的是「前一個條件」，否則接續時會跳過還沒做完的部分。
    resume_key: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.records


@dataclass(frozen=True, slots=True)
class KnownCompanies:
    """已經在資料庫裡的公司，用來略過重複的明細頁請求。

    ## 這在省什麼

    名錄的列表頁上一頁有幾十筆，每一筆的細節（信箱、傳真、負責人）都在各自的
    明細頁上——所以真正花時間的不是「幾頁」，是**每一筆一次請求**。一個 2000
    家的名錄第二次再爬，是 2000 次幾乎確定拿回同樣內容的請求，而且每一次都要
    等禮貌延遲。

    :meth:`~database.repository.CompanyRepository.upsert` 合併時只填空欄位，
    所以那 2000 次請求對已經存在的公司幾乎不會改到任何東西。既然如此，不要送
    出去比較好——對對方的伺服器也是。

    ## 為什麼只看名稱與統編

    比對必須便宜（列表頁上每一筆都要問一次），所以只用資料庫裡**沒有加密**的
    兩個識別欄位：``name_key`` 與 ``tax_id``。信箱、電話那些是加密欄位，SQL
    看到的是密文，比不了；而且列表頁上本來也常常沒有。

    整份一次撈進記憶體，不是每一筆查一次資料庫。一個名錄幾千家，兩個 set 的
    成本可以忽略，而幾千次 SQL 往返不行。

    ## 代價，講清楚

    略過的那一筆**不會**被更新。已經存在但缺信箱的公司，就算明細頁上有，這一
    次也不會補回來——要補那個缺口用的是「補抓信箱」與「補齊公司資料」，那兩支
    本來就是為此存在的，而且它們只挑真的缺欄位的公司，比整批重爬精準得多。

    不要這個行為的話，把 ``crawler.skip_known`` 設成 false。
    """

    name_keys: frozenset[str] = frozenset()
    tax_ids: frozenset[str] = frozenset()

    def has(self, company_name: str, tax_id: str | None = None) -> bool:
        """這家公司已經在資料庫裡了嗎？"""
        digits = "".join(ch for ch in str(tax_id or "") if ch.isdigit())
        if len(digits) == 8 and digits in self.tax_ids:
            return True
        # 延後 import：crawler.base 會被 core.preload 在很早的時候載入。
        from verifier.normalize import company_name_key

        key = company_name_key(company_name or "")
        # 太短的鍵不能當識別。company_name_key 會把「股份有限公司」這類後綴
        # 拿掉，所以一兩個字的鍵在幾千家裡撞到別人是遲早的事，而撞到的代價是
        # **整筆被略過、永遠不會被爬**——那比多送一次請求糟得多。
        return len(key) >= 2 and key in self.name_keys

    @property
    def size(self) -> int:
        return len(self.name_keys)


class BaseSource(ABC):
    """One crawl target.

    Subclasses receive an already-configured fetcher; they must not create
    their own HTTP clients, or they would bypass the rate limiter and the
    robots.txt policy.
    """

    def __init__(
        self,
        source_config: SourceConfig,
        fetcher: BaseFetcher | None = None,
        config: AppConfig | None = None,
    ) -> None:
        self.source_config = source_config
        self.config = config or get_config()
        self.fetcher = fetcher
        #: 上一次沒跑完時留下的進度。管線在 :meth:`iter_pages` 之前設好，
        #: 來源自己決定要怎麼跳過已經做完的部分。
        self.resume_from: str | None = None
        #: 已經在資料庫裡的公司。管線在開跑前設好，來源用它省下明細頁的請求。
        #: ``None`` 代表不做這件事（設定關掉了，或呼叫端沒有資料庫）。
        self.known: KnownCompanies | None = None
        #: 因為「已經在資料庫裡」而沒有去讀明細頁的筆數，由管線讀出來回報。
        self.skipped_known = 0

    @property
    def name(self) -> str:
        return self.source_config.name

    @property
    def label(self) -> str:
        """Value written to ``Company.source``."""
        return self.source_config.source_label

    @property
    def page_limit(self) -> int:
        """這一次最多收錄幾頁。

        ``crawler.max_pages`` 是**預設值**，不是天花板——只有在來源自己沒有
        指定時才生效。以前是三者取最小，結果是：一個 24 頁的名錄，使用者在
        來源上明確填了 24，卻只被爬了 10 頁（全域預設值），而且畫面上顯示
        「完成」，看不出被截斷過。使用者明確講了 24 就是 24。

        頁碼範圍（``page_start``/``page_end``）不一樣，那是使用者對「這一次
        要爬哪幾頁」的直接指定，仍然要生效。
        """
        loop = self.source_config.query_loop
        if loop is not None:
            # 逐項查詢沒有「頁」這種東西——它的一趟就是「換一組條件查一次」，
            # 而要查幾組是使用者在 query_loop.max_queries 直接講的。
            #
            # 這裡不能退回 max_pages。實際發生過：使用者在選單那一格填了 97，
            # 但「最多爬幾頁」還停在預設的 3（他從頭到尾沒去動過那一格，那一格
            # 對這種來源也沒有意義）。兩個數字都在講「總共跑幾趟」，取最小的
            # 結果是使用者沒填的那個贏了——查到第 3 個就停，畫面上寫著完成。
            #
            # 中間還要再點一層的話，一組條件底下的每一列各自算一趟，所以要
            # 乘上去；真正的煞車是 max_queries 與 drill.max_rows 自己。
            rows = loop.drill.max_rows if loop.drill is not None else 1
            return max(1, loop.max_queries * rows)

        limits: list[int] = [
            self.source_config.max_pages or self.config.crawler.max_pages
        ]
        page_count = self.source_config.page_count
        if page_count is not None:
            limits.append(page_count)
        return max(1, min(limits))

    @abstractmethod
    def iter_pages(self) -> Iterator[PageBatch]:
        """Yield one :class:`PageBatch` per page, in order.

        Implementations stop on their own when there are no more pages; the
        pipeline additionally enforces :attr:`page_limit` and cancellation.
        """

    def requires_network(self) -> bool:
        """False for offline fixtures, which lets tests skip network guards."""
        return True

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"
