"""Tests for gui_qt/tasks.py -- BackgroundTask over QThreadPool.

The point being tested is the contract that gui/controllers.py depends on:
the worker is called with ``report=<callable>`` and ``cancel_event=<Event>``,
progress/result/error all arrive back on the thread that created the task
(here, the test's own thread, driven by a local Qt event loop), and
``cancel()`` sets the same ``threading.Event`` the worker was given.

No real controller is exercised here -- see test_gui_qt_dashboard.py for a
BackgroundTask wired to a real gui.controllers.DashboardController.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QThreadPool, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gui_qt.tasks import BackgroundTask  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    """The shared Qt application instance.

    Always ``QApplication`` (not the lighter ``QCoreApplication``), even
    though nothing in this module builds a widget: other test modules in the
    same pytest process do, and only one ``QCoreApplication``-family instance
    can exist per process. Creating a plain ``QCoreApplication`` here first
    and a ``QApplication`` later (or vice versa) leaves the *first* type as
    the process-wide singleton and crashed the interpreter in practice --
    using ``QApplication`` everywhere avoids the mismatch entirely.
    """
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _drain_thread_pool():
    """Let every pooled thread from the previous test actually finish first.

    Tests submit work to the process-wide ``QThreadPool.globalInstance()``.
    Without this, a runnable from one test can still be mid-teardown (Qt
    delivering its queued signals) while the next test starts submitting its
    own -- observed to occasionally crash the interpreter when several tests
    with different worker functions land back to back on the same pool.
    """
    yield
    QThreadPool.globalInstance().waitForDone(2000)


def _run_until_finished(task: BackgroundTask, *args, timeout_ms: int = 3000, **kwargs):
    """Start ``task`` and pump a local event loop until it finishes.

    Returns ``("done", result)`` or ``("error", exc)``. Raises on timeout --
    a hang here means a signal never made it back to the UI thread.
    """
    loop = QEventLoop()
    outcome: dict[str, object] = {}

    original_done = task.on_done
    original_error = task.on_error

    def on_done(result):
        outcome["kind"] = "done"
        outcome["value"] = result
        if original_done:
            original_done(result)
        loop.quit()

    def on_error(exc):
        outcome["kind"] = "error"
        outcome["value"] = exc
        if original_error:
            original_error(exc)
        loop.quit()

    task.on_done = on_done
    task.on_error = on_error

    QTimer.singleShot(timeout_ms, loop.quit)
    task.start(*args, **kwargs)
    loop.exec()

    if "kind" not in outcome:
        raise AssertionError("background task never finished within the timeout")
    return outcome["kind"], outcome["value"]


def test_worker_receives_report_and_cancel_event(qt_app):
    """The exact contract gui/controllers.py's ``run()`` methods rely on."""
    seen: dict[str, object] = {}

    def worker(x, *, report, cancel_event):
        seen["cancel_event_type"] = type(cancel_event)
        report({"stage": "working"})
        return x * 2

    progress_messages = []
    task = BackgroundTask(None, worker, on_progress=progress_messages.append)

    kind, value = _run_until_finished(task, 21)

    assert kind == "done"
    assert value == 42
    assert seen["cancel_event_type"] is threading.Event
    assert progress_messages == [{"stage": "working"}]


def test_error_is_reported_not_raised(qt_app):
    def worker(*, report, cancel_event):
        raise ValueError("boom")

    task = BackgroundTask(None, worker)
    kind, exc = _run_until_finished(task)

    assert kind == "error"
    assert isinstance(exc, ValueError)
    assert str(exc) == "boom"


def test_cancel_sets_the_event_the_worker_sees(qt_app):
    def worker(*, report, cancel_event):
        for _ in range(200):
            if cancel_event.is_set():
                return "cancelled"
            time.sleep(0.005)
        return "completed"

    task = BackgroundTask(None, worker)

    loop = QEventLoop()
    outcome = {}

    def on_done(result):
        outcome["value"] = result
        loop.quit()

    task.on_done = on_done
    task.start()
    task.cancel()  # ask it to stop almost immediately after starting
    QTimer.singleShot(3000, loop.quit)
    loop.exec()

    assert outcome.get("value") == "cancelled"


def test_running_reflects_task_state(qt_app):
    """``running`` flips synchronously inside ``start()``/on completion.

    Deliberately does *not* poll with a raw ``processEvents()`` + ``sleep()``
    loop to observe the worker "mid-flight": mixing that pattern with a
    QThreadPool runnable emitting cross-thread signals turned out to be the
    one combination that could crash the interpreter (rare, but reproducible
    over enough runs) on this Qt/Python combination. A single
    ``QEventLoop.exec()`` is the Qt-native way to wait and never showed the
    same problem, so that is all this test relies on.
    """
    release = threading.Event()

    def worker(*, report, cancel_event):
        release.wait(timeout=2)
        return "ok"

    task = BackgroundTask(None, worker)
    assert task.running is False

    loop = QEventLoop()
    task.on_done = lambda result: loop.quit()

    task.start()
    # `_running` is set synchronously inside start(), before the pool thread
    # necessarily begins executing the worker -- no need to wait for that.
    assert task.running is True

    release.set()
    QTimer.singleShot(3000, loop.quit)
    loop.exec()
    assert task.running is False


def test_duplicate_start_while_running_is_ignored(qt_app):
    release = threading.Event()
    call_count = {"n": 0}

    def worker(*, report, cancel_event):
        call_count["n"] += 1
        release.wait(timeout=2)
        return call_count["n"]

    task = BackgroundTask(None, worker)
    loop = QEventLoop()
    task.on_done = lambda result: loop.quit()

    task.start()
    task.start()  # should be a no-op: the first one is still running
    release.set()
    QTimer.singleShot(3000, loop.quit)
    loop.exec()

    assert call_count["n"] == 1
