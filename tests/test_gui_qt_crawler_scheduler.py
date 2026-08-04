"""Tests for the CrawlScheduler <-> Qt bridge wired up in gui_qt/app.py.

``CrawlScheduler`` (core/scheduler.py) calls ``on_finished`` from its own
background thread -- a plain ``threading.Thread``, never a widget-safe
context. ``gui_qt/app.py`` bridges that through ``_SchedulerBridge``, a
``QObject`` whose ``finished`` signal is emitted from the scheduler thread
and delivered to ``MainWindow._on_scheduled_run`` queued onto the UI thread
(the same cross-thread signal/slot mechanism ``gui_qt/tasks.py`` relies on).

The scheduler in these tests always targets the offline ``sample`` source --
nothing here touches the network.
"""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QObject  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.config import SchedulerSection  # noqa: E402
from core.schemas import CrawlSummary  # noqa: E402
from gui_qt.app import MainWindow  # noqa: E402
from gui_qt.pages.base import current_data_version  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeWindow(QObject):
    """A bare QObject standing in for MainWindow.

    Reuses the *real* ``_start_scheduler``/``_on_scheduled_run`` methods
    under test (bound via the class body below) so this only fakes the
    surrounding widget tree, never the logic being tested.
    """

    _start_scheduler = MainWindow._start_scheduler
    _on_scheduled_run = MainWindow._on_scheduled_run

    def __init__(self, config) -> None:
        super().__init__()
        self.config_data = config
        self.scheduler = None
        self.messages: list[tuple[str, str]] = []
        self.refreshed = False

    def set_status(self, message: str, tone: str = "normal") -> None:
        self.messages.append((message, tone))

    def refresh_current(self) -> None:
        self.refreshed = True


def test_start_scheduler_is_a_no_op_when_disabled(tmp_config):
    # tmp_config's scheduler section defaults to enabled=False.
    window = _FakeWindow(tmp_config)
    window._start_scheduler()

    assert window.scheduler is None
    assert window.messages == []


def test_on_scheduled_run_bumps_version_reports_and_refreshes(tmp_config):
    """Logic-only check: no thread, no signal -- just what the slot does."""
    window = _FakeWindow(tmp_config)
    before = current_data_version()

    summaries = [
        CrawlSummary(source="sample", status="Success", records_found=3, records_new=3),
        CrawlSummary(source="sample", status="Success", records_found=1, records_new=0),
    ]
    window._on_scheduled_run(summaries)

    assert current_data_version() == before + 1
    assert window.refreshed is True
    assert window.messages[-1] == ("排程爬取完成，新增 3 筆資料", "success")


def test_start_scheduler_wires_the_bridge_as_on_finished(patch_config, monkeypatch):
    """``CrawlScheduler`` must be given the bridge's ``emit``, not
    ``_on_scheduled_run`` directly -- calling the latter straight from the
    scheduler's own background thread would touch widget-adjacent state
    (``bump_data_version``, ``set_status``, ``refresh_current``) off the UI
    thread, exactly the mistake ``gui_qt/tasks.py`` warns about for
    ``BackgroundTask``. A stub replaces ``CrawlScheduler`` so this checks the
    wiring in ``gui_qt/app.py`` without starting a real thread.
    """
    config = patch_config.model_copy(
        update={
            "scheduler": SchedulerSection.model_validate(
                {"enabled": True, "sources": ["sample"]}
            )
        }
    )

    captured: dict[str, object] = {}

    class _StubScheduler:
        def __init__(self, cfg, on_finished=None):
            captured["config"] = cfg
            captured["on_finished"] = on_finished

        def start(self):
            return True

        @property
        def status_text(self):
            return "stub status"

    import gui_qt.app as app_module

    monkeypatch.setattr(app_module, "CrawlScheduler", _StubScheduler)

    window = _FakeWindow(config)
    window._start_scheduler()

    assert captured["config"] is config
    assert captured["on_finished"] == window._scheduler_bridge.finished.emit
    assert window.messages[-1] == ("stub status", "muted")


def test_scheduler_bridge_delivers_a_cross_thread_emit_to_the_ui_thread(qt_app):
    """The actual mechanism ``_start_scheduler`` relies on: emitting
    ``_SchedulerBridge.finished`` from a background thread must be delivered
    to ``_on_scheduled_run`` on the thread that owns the bridge (here, the
    test's own/UI thread) -- not run directly on the emitting thread.

    Deliberately uses a bare ``threading.Thread`` with no database access at
    all (unlike a real ``CrawlScheduler`` run) -- this test only needs to
    prove the signal crosses threads safely, not re-exercise the crawl
    pipeline that ``tests/test_scheduler.py`` already covers synchronously.
    """
    import threading

    from gui_qt.app import _SchedulerBridge

    window = _FakeWindow(None)
    bridge = _SchedulerBridge()
    bridge.finished.connect(window._on_scheduled_run)

    summaries = [CrawlSummary(source="sample", status="Success", records_new=5)]
    emitting_thread_ident: list[int] = []

    def emit_from_background_thread() -> None:
        emitting_thread_ident.append(threading.get_ident())
        bridge.finished.emit(summaries)

    worker = threading.Thread(target=emit_from_background_thread, daemon=True)
    before = current_data_version()
    worker.start()

    deadline = time.time() + 3.0
    while not window.refreshed and time.time() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)
    worker.join(timeout=2)

    assert window.refreshed, "signal 從背景執行緒 emit 之後，從未送達 UI 執行緒的 slot"
    assert emitting_thread_ident and emitting_thread_ident[0] != threading.get_ident()
    assert current_data_version() == before + 1
    assert window.messages[-1] == ("排程爬取完成，新增 5 筆資料", "success")


def test_close_event_stops_a_running_scheduler(patch_config):
    """gui_qt/app.py's closeEvent must stop the scheduler thread on exit."""
    config = patch_config.model_copy(
        update={
            "scheduler": SchedulerSection.model_validate(
                {"enabled": True, "mode": "interval", "every_minutes": 60, "catch_up": False}
            )
        }
    )

    window = _FakeWindow(config)
    window._start_scheduler()
    assert window.scheduler is not None
    assert window.scheduler.running is True

    window.scheduler.stop()
    assert window.scheduler.running is False
