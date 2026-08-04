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

ProgressCallback = Callable[[str, int, int], None]
"""``(source_name, page_number, records_stored_so_far)``."""


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

    def __enter__(self) -> "CrawlPipeline":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -------------------------------------------------------------- internal

    def _get_fetcher(self, source: BaseSource) -> BaseFetcher | None:
        """Lazily build the shared fetcher; offline sources need none."""
        if not source.requires_network():
            return None
        if self._fetcher is None:
            self._fetcher = build_fetcher(self.config)
        return self._fetcher

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

                    self._store_page(batch.records, repo, cleaner, summary)
                    session.commit()

                    if progress is not None:
                        progress(source_config.name, batch.page_number, summary.records_new)

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

            summary.finished_at = now()
            job_repo.finish(job, summary)

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
        unique, dropped_in_page = deduplicate_batch(records)
        summary.records_duplicate += dropped_in_page

        cleaned, rejected = cleaner.clean_many(unique)
        summary.records_invalid += rejected

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
