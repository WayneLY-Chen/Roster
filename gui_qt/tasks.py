"""跑背景工作而不卡住視窗——PySide6 版。

對應 ``gui/tasks.py`` 的 ``BackgroundTask``，公開介面刻意做成一樣：
``start(*args, **kwargs)``、``cancel()``、``.running``。差別只在底層機制：

    Tk 版：worker 跑在 ``threading.Thread``，用一個 ``queue.Queue`` 把訊息
           丟回來，UI 執行緒再用 ``after()`` 輪詢那個 queue（每 100ms 一次）。

    Qt 版：worker 跑在 ``QThreadPool`` 借出來的執行緒，直接用 Qt signal 把
           訊息送回 UI 執行緒，不需要輪詢——``QObject`` 的 signal/slot 只要
           收送雙方所在的執行緒不同，Qt 就會自動把呼叫排進接收端那個執行緒
           的事件迴圈（也就是 slot 永遠在建立 ``BackgroundTask`` 的那個執行
           緒，即 UI 執行緒，被執行），這正是「進度只能從主執行緒碰 widget」
           的保證方式。

## 為什麼是 ``QThreadPool``，不是每次都開一個新的 ``QThread``

一開始這裡是每次 ``start()`` 都 new 一個 ``QThread``、跑完就丟掉。量儀表板
頁的換頁延遲時發現：query 本身很快時（甚至整個 mock 成立即回傳），量到的
中位數反而是 20ms 起跳，比同一個 query 同步在 UI 執行緒上跑還要慢一截——
瓶頸不是 Python 也不是這支專案的程式碼，是 Windows 每次建立/回收一個真正
的作業系統執行緒本身的固定成本，量出來就是那個等級。``QThreadPool`` 會保留
一批已經建立好、閒置中的執行緒供重複借用，同一個工作再跑一次不需要重新
跟作業系統要一個執行緒——這正是 Qt 官方建議「重複、短命工作」該用的機制。
換成 ``QThreadPool`` 之後，儀表板換頁中位數從「有時比同步還慢」穩定回到
Qt 換頁本身該有的量級（個位數毫秒）。

最關鍵的一點：worker 收到的仍然是 ``report`` 這個 callable 和一個
``threading.Event`` 當 ``cancel_event``，跟 Tk 版一模一樣。這是因為
``gui/controllers.py``（``CrawlController.run()``、``VerifyController.run()``
……）都是照這個簽章寫的，兩套介面共用同一個控制器層，這個簽章不能變。
``cancel_event`` 用普通的 ``threading.Event``、不是任何 Qt 專屬的東西，
所以 controller 那邊完全不需要知道自己是被 Tk 還是 Qt 呼叫。

## 已知的坑：不要在 worker 執行緒裡處理例外的 traceback

開發時實際踩到過一次：``_Runnable.run()`` 原本在 ``except`` 區塊裡就地呼叫
``log.error(...)`` 並附上 ``traceback.format_exc()``，在自動化測試裡反覆
跑幾十次之後，偶爾（不是每次、時間點也不固定）會讓 Python 直譯器整個
access violation 當掉，而且完全不會拋出任何 Python 例外可以攔——是那種
會讓「這支程式偶爾無聲無息當掉」的問題。反覆隔離後確認：問題只在
「``QThreadPool`` 借出來的執行緒 + 在該執行緒內組出例外的 traceback 字串
（``traceback.format_exc()``）」同時出現時才會發生；單純的
``log.info()``/``log.exception()`` 呼叫（不牽涉在同一個執行緒內另外組
traceback 字串）大量重複測試都沒有重現。目前的做法是：worker 執行緒裡的
``except`` 區塊只 ``emit`` 例外物件本身（``Exception.__traceback__`` 會
原封不動跟著過去），真正要記錄、要組 traceback 字串，一律留到
``_on_failed()``——也就是 UI 執行緒、一個正常的 Python 執行緒——才做。
**之後任何 worker 或這個模組本身，都不要在 worker 執行緒裡呼叫
``traceback.format_exc()``／``sys.exc_info()`` 之類的例外內省，一律把例外
物件整個回拋到 UI 執行緒再處理。**
"""

from __future__ import annotations

import threading
import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from core.constants import LogCategory
from core.logging_setup import get_logger

log = get_logger(LogCategory.GUI)


class _Signals(QObject):
    """``QRunnable`` 本身不是 ``QObject``、不能發 signal，所以另外配一個。

    每次 :meth:`BackgroundTask.start` 都會建立一份新的，隨那一次執行的結果
    一起被回呼消化掉，不需要手動清理。
    """

    progress = Signal(object)
    succeeded = Signal(object)
    failed = Signal(object)


class _Runnable(QRunnable):
    """實際執行 worker 的工作單元，丟進 ``QThreadPool`` 借來的執行緒裡跑。"""

    def __init__(
        self,
        worker: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        cancel_event: threading.Event,
        signals: _Signals,
    ) -> None:
        super().__init__()
        self._worker = worker
        self._args = args
        self._kwargs = kwargs
        self._cancel_event = cancel_event
        self._signals = signals

    def run(self) -> None:  # noqa: D102 - QRunnable 的入口點，跑在借來的執行緒上
        def report(payload: Any) -> None:
            self._signals.progress.emit(payload)

        try:
            result = self._worker(
                *self._args,
                report=report,
                cancel_event=self._cancel_event,
                **self._kwargs,
            )
            self._signals.succeeded.emit(result)
        except Exception as exc:  # noqa: BLE001 - 背景工作的例外一律回報，不能讓執行緒吞掉
            # 特意不在這裡呼叫 log.error()：這個執行緒是 QThreadPool 借出來
            # 的、不是 Python 標準的 threading.Thread，實測跟 loguru 這支
            # 專案設定的 enqueue=True（背後是 multiprocessing.Queue）寫入
            # 執行緒交手時，偶爾會讓直譯器整個當掉（access violation），而且
            # 不是每次、也不是當下就炸，很難重現、更難用一般的 try/except
            # 接住。把記錄動作留到 _on_failed()（UI 執行緒，一個普通的
            # Python 執行緒）處理，例外物件本身連同 ``__traceback__`` 會原封
            # 不動地跟著 emit 過去，不會漏掉任何診斷資訊。
            self._signals.failed.emit(exc)


class BackgroundTask(QObject):
    """一個可取消、會回報進度的背景工作單元。

    用法跟 ``gui.tasks.BackgroundTask`` 完全一樣::

        self.task = BackgroundTask(
            self, controller.run,
            on_progress=self._on_progress,
            on_done=self._on_done,
            on_error=self._on_error,
        )
        self.task.start(source_name, max_pages=10)
        ...
        self.task.cancel()

    ``worker`` 必須接受關鍵字參數 ``report`` 與 ``cancel_event``——
    ``gui/controllers.py`` 裡每一個要跑很久的方法都是這樣寫的。
    """

    def __init__(
        self,
        parent: QObject,
        worker: Callable[..., Any],
        on_progress: Callable[[Any], None] | None = None,
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.worker = worker
        self.on_progress = on_progress
        self.on_done = on_done
        self.on_error = on_error
        self.cancel_event = threading.Event()
        self._running = False
        #: 目前這一次執行用的 signals 物件，Python 這邊留一份參照讓它撐到
        #: 回呼跑完為止（否則沒有其他地方引用它，可能提早被回收）。
        self._signals: _Signals | None = None

    @property
    def running(self) -> bool:
        return self._running

    def start(self, *args: Any, **kwargs: Any) -> None:
        if self._running:
            log.warning("task already running; ignoring duplicate start")
            return

        self.cancel_event = threading.Event()  # 每次開跑都用一個全新的旗標
        signals = _Signals()
        # AutoConnection：signals 物件本身住在 UI 執行緒（誰建立它就住哪），
        # 但 emit 是從 run() 內、也就是執行緒池借出來的背景執行緒呼叫的——
        # Qt 偵測到訊號的 receiver（self，也住在 UI 執行緒）跟目前執行緒
        # 不同，會自動排進 UI 執行緒的事件迴圈，所以 _handle_*/_on_* 一定
        # 在 UI 執行緒被呼叫，worker 本身完全碰不到任何 widget。
        signals.progress.connect(self._handle_progress)
        signals.succeeded.connect(self._on_succeeded)
        signals.failed.connect(self._on_failed)
        self._signals = signals

        self._running = True
        runnable = _Runnable(self.worker, args, kwargs, self.cancel_event, signals)
        QThreadPool.globalInstance().start(runnable)

    def cancel(self) -> None:
        """請 worker 停下來，實際會在它下一次檢查 ``cancel_event`` 時停。"""
        self.cancel_event.set()

    # ------------------------------------------------------------- signal 處理

    def _handle_progress(self, payload: Any) -> None:
        if self.on_progress:
            self.on_progress(payload)

    def _on_succeeded(self, result: Any) -> None:
        self._running = False
        if self.on_done:
            self.on_done(result)

    def _on_failed(self, exc: Exception) -> None:
        self._running = False
        # 記錄放在這裡（UI 執行緒），不是 worker 執行緒——理由見 _Runnable.run()。
        formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        log.error("background task failed: {}\n{}", exc, formatted)
        if self.on_error:
            self.on_error(exc)
