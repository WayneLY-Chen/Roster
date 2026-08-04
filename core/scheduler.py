"""Unattended crawl scheduling.

This is a desktop application, not a service. There is no daemon, so a job can
only run while the window is open -- the scheduler says so plainly rather than
pretending otherwise, and :attr:`CrawlScheduler.status_text` always reports the
next run in terms the user can check against a clock.

The worker thread wakes once a second, which is cheap and keeps ``stop()``
responsive; it never spins on the database.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

from core.config import AppConfig, SchedulerSection, get_config
from core.constants import LogCategory
from core.logging_setup import get_logger
from core.schemas import CrawlSummary

log = get_logger(LogCategory.CRAWL)

STATE_FILENAME = "scheduler_state.json"
_TICK_SECONDS = 1.0


@dataclass
class SchedulerState:
    """Persisted across restarts so ``catch_up`` can tell a job was missed."""

    last_run: datetime | None = None
    last_status: str = ""
    last_error: str | None = None
    run_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "run_count": self.run_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "SchedulerState":
        raw = data.get("last_run")
        parsed: datetime | None = None
        if isinstance(raw, str):
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                parsed = None
        return cls(
            last_run=parsed,
            last_status=str(data.get("last_status") or ""),
            last_error=data.get("last_error") if isinstance(data.get("last_error"), str) else None,
            run_count=int(data.get("run_count") or 0),
        )


def _state_path(config: AppConfig) -> Path:
    sqlite_path = config.database.sqlite_path
    base = sqlite_path.parent if sqlite_path else Path.cwd()
    return base / STATE_FILENAME


def load_state(config: AppConfig | None = None) -> SchedulerState:
    config = config or get_config()
    path = _state_path(config)
    if not path.exists():
        return SchedulerState()
    try:
        return SchedulerState.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("排程狀態檔讀取失敗，視為從未執行過：{}", exc)
        return SchedulerState()


def save_state(state: SchedulerState, config: AppConfig | None = None) -> None:
    config = config or get_config()
    path = _state_path(config)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        log.warning("排程狀態寫入失敗：{}", exc)


def parse_at(value: str) -> dtime:
    """``"03:00"`` -> :class:`datetime.time`. Validated already by config."""
    hour, _, minute = value.partition(":")
    return dtime(hour=int(hour), minute=int(minute or 0))


def next_run_after(
    reference: datetime, settings: SchedulerSection, last_run: datetime | None = None
) -> datetime:
    """When the next job is due, given the mode and the last completed run."""
    if settings.mode == "interval":
        base = last_run or reference
        due = base + timedelta(minutes=settings.every_minutes)
        return due if due > reference else reference + timedelta(seconds=_TICK_SECONDS)

    if settings.mode == "hourly":
        due = (reference + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        return due

    target = parse_at(settings.at)
    due = reference.replace(
        hour=target.hour, minute=target.minute, second=0, microsecond=0
    )
    if due <= reference:
        due += timedelta(days=1)
    return due


def is_overdue(
    reference: datetime, settings: SchedulerSection, last_run: datetime | None
) -> bool:
    """True when a run should already have happened but did not.

    Used by ``catch_up`` at start-up: the machine may simply have been off at
    3 a.m., and silently skipping a night of collection is worse than running
    it a few hours late.
    """
    if not settings.catch_up:
        return False
    if last_run is None:
        return True

    if settings.mode == "interval":
        return reference - last_run >= timedelta(minutes=settings.every_minutes)
    if settings.mode == "hourly":
        return reference - last_run >= timedelta(hours=1)

    target = parse_at(settings.at)
    due_today = reference.replace(
        hour=target.hour, minute=target.minute, second=0, microsecond=0
    )
    return reference >= due_today > last_run


class CrawlScheduler:
    """Runs crawls on a timer for as long as the application is open."""

    def __init__(
        self,
        config: AppConfig | None = None,
        on_finished: Callable[[list[CrawlSummary]], None] | None = None,
    ) -> None:
        self.config = config or get_config()
        self.settings = self.config.scheduler
        self.on_finished = on_finished
        self.state = load_state(self.config)

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._job_cancel = threading.Event()
        self._running_job = False
        self._next_run: datetime | None = None

    # ------------------------------------------------------------ lifecycle

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def next_run(self) -> datetime | None:
        return self._next_run

    @property
    def status_text(self) -> str:
        """One line for the Settings page. Never says "on" when it is not."""
        if not self.settings.enabled:
            return "排程已關閉"
        if not self.running:
            return "排程已設定，但尚未啟動"
        if self._running_job:
            return "排程任務執行中…"
        if self._next_run is None:
            return "排程執行中，正在計算下次時間"
        return f"下次執行：{self._next_run:%Y-%m-%d %H:%M}（僅在本程式開啟時執行）"

    def start(self) -> bool:
        """Start the timer thread. Returns False when scheduling is disabled."""
        if not self.settings.enabled:
            log.info("排程未啟用，不啟動排程執行緒")
            return False
        if self.running:
            return True

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="crawl-scheduler", daemon=True
        )
        self._thread.start()
        log.info(
            "排程已啟動（模式 {}，{}）",
            self.settings.mode,
            self.settings.at if self.settings.mode == "daily"
            else f"每 {self.settings.every_minutes} 分鐘",
        )
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the timer and ask any running job to finish early."""
        self._stop.set()
        self._job_cancel.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        log.info("排程已停止")

    def run_now(self) -> None:
        """Trigger a run immediately, without waiting for the next slot."""
        threading.Thread(target=self._run_job, name="crawl-now", daemon=True).start()

    # --------------------------------------------------------------- internal

    def _loop(self) -> None:
        now = datetime.now()

        if is_overdue(now, self.settings, self.state.last_run):
            log.info("偵測到錯過的排程，立即補跑一次")
            self._run_job()

        self._next_run = next_run_after(datetime.now(), self.settings, self.state.last_run)

        while not self._stop.is_set():
            if self._stop.wait(_TICK_SECONDS):
                break
            if self._next_run is None:
                continue
            if datetime.now() >= self._next_run:
                self._run_job()
                self._next_run = next_run_after(
                    datetime.now(), self.settings, self.state.last_run
                )
                log.info("下次排程時間：{:%Y-%m-%d %H:%M}", self._next_run)

    def _run_job(self) -> None:
        """One scheduled pass: crawl the configured sources, then verify."""
        if self._running_job:
            log.warning("上一次排程任務尚未結束，略過這一次")
            return

        targets = self.settings.sources or [
            s.name for s in self.config.crawler.enabled_sources()
        ]
        if not targets:
            # Nothing to do is not a successful run: recording it as one would
            # move last_run forward and stop catch_up from ever firing.
            log.warning("排程沒有可執行的來源，這次不算執行")
            return

        self._running_job = True
        self._job_cancel.clear()
        summaries: list[CrawlSummary] = []
        error: str | None = None

        try:
            from crawler.pipeline import CrawlPipeline

            log.info("排程任務開始，來源：{}", ", ".join(targets))
            with CrawlPipeline(self.config) as pipeline:
                for name in targets:
                    if self._stop.is_set():
                        break
                    summaries.append(
                        pipeline.run_source(name, cancel_event=self._job_cancel)
                    )

            if self.settings.verify_after_crawl and not self._stop.is_set():
                self._verify()

        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            log.exception("排程任務失敗")
        finally:
            self._running_job = False
            self.state.last_run = datetime.now()
            self.state.run_count += 1
            self.state.last_error = error
            self.state.last_status = (
                "失敗" if error else
                ("成功" if all(s.status == "Success" for s in summaries) else "部分成功")
            )
            save_state(self.state, self.config)

            total_new = sum(s.records_new for s in summaries)
            log.info(
                "排程任務結束：{}，共新增 {} 筆", self.state.last_status, total_new
            )
            if self.on_finished is not None:
                try:
                    self.on_finished(summaries)
                except Exception:  # a UI callback must not kill the scheduler
                    log.exception("排程完成回呼發生錯誤")

    def _verify(self) -> None:
        from database.repository import CompanyRepository
        from database.session import session_scope
        from verifier.service import VerificationService

        with session_scope() as session:
            targets = CompanyRepository(session).all()
            if targets:
                VerificationService(session).run(targets)
