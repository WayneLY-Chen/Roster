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
