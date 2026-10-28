"""背景爬取。一次只跑一個，網頁用輪詢看進度。

爬一個名錄動輒幾分鐘，不能讓一個 HTTP 請求等那麼久，所以按下「開始」只是
起一條背景執行緒，畫面每秒來問一次 :meth:`CrawlJob.status`。
"""

from __future__ import annotations

import threading
from typing import Any

from controllers import CrawlController
from core.constants import LogCategory
from core.logging_setup import get_logger

log = get_logger(LogCategory.GUI)


class CrawlJob:
    """目前（或上一次）的爬取工作。所有方法都可以從任何執行緒呼叫。"""

    def __init__(self, controller: CrawlController) -> None:
        self._controller = controller
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._state: dict[str, Any] = _idle_state()

    def start(self, source: str | None) -> bool:
        """開始爬 ``source``（``None`` 是全部啟用的來源）。已經在跑就回 ``False``。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._cancel = threading.Event()
            self._state = {**_idle_state(), "running": True, "source": source}
            self._thread = threading.Thread(
                target=self._run, args=(source,), name="web-crawl", daemon=True
            )
            self._thread.start()
            return True

    def cancel(self) -> None:
        self._cancel.set()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def wait(self, timeout: float | None = None) -> None:
        """等背景工作結束。給測試用。"""
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self, source: str | None) -> None:
        try:
            results = self._controller.run(
                source, report=self._on_progress, cancel_event=self._cancel
            )
            self._update(results=[result.model_dump(mode="json") for result in results])
        except Exception as exc:  # noqa: BLE001 - 錯誤要顯示在畫面上，不能讓執行緒默默死掉
            log.exception("網頁版爬取失敗")
            self._update(error=str(exc))
        finally:
            self._update(running=False)

    def _on_progress(self, payload: dict[str, Any]) -> None:
        self._update(progress=payload)

    def _update(self, **changes: Any) -> None:
        with self._lock:
            self._state.update(changes)


def _idle_state() -> dict[str, Any]:
    return {"running": False, "source": None, "progress": None, "results": [], "error": None}
