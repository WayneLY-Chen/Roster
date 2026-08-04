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

    @property
    def name(self) -> str:
        return self.source_config.name

    @property
    def label(self) -> str:
        """Value written to ``Company.source``."""
        return self.source_config.source_label

    @property
    def page_limit(self) -> int:
        """收錄頁數上限：來源自訂上限、頁碼範圍與全域上限三者取最小。"""
        limits = [self.config.crawler.max_pages]
        if self.source_config.max_pages:
            limits.append(self.source_config.max_pages)
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
