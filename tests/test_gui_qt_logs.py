"""Integration tests for gui_qt/pages/logs.py against a real (test) log directory.

``tmp_config`` (see ``tests/conftest.py``) already points ``logging.dir`` at a
directory under ``tmp_path``, so nothing here ever reads or truncates the
user's real ``logs/`` folder.

The actual file read runs through ``gui_qt.tasks.BackgroundTask`` (see the
module docstring of ``gui_qt/pages/logs.py`` for why: ``LogController.tail()``
reads the whole file into memory, which is too slow to do on the UI thread
once a log file has grown large), so tests that call ``refresh()`` need to
pump the Qt event loop until the background fetch lands, same as
``tests/test_gui_qt_dashboard.py``.
"""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core.constants import LogCategory  # noqa: E402
from core.logging_setup import log_file_path  # noqa: E402
from gui_qt.pages.logs import LogsPage  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeApp:
    def __init__(self) -> None:
        self.current_page = LogsPage.title
        self.messages: list[tuple[str, str]] = []

    def set_status(self, message: str, tone: str = "normal") -> None:
        self.messages.append((message, tone))


def _wait_for(qt_app, task, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    while task.running and time.time() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)
    assert not task.running, "background task never completed"
    qt_app.processEvents()


def _write_log(patch_config, category: LogCategory, content: str) -> None:
    path = log_file_path(category, patch_config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ------------------------------------------------------------------- 建立元件


def test_build_creates_one_button_per_category_with_the_first_checked(qt_app, patch_config):
    app = _FakeApp()
    page = LogsPage(app)
    page.ensure_built()

    categories = [c.value for c in LogCategory]
    assert list(page._category_buttons.keys()) == categories
    assert page._category_buttons[categories[0]].isChecked()
    assert page._current_category() == categories[0]


# --------------------------------------------------------------------- 讀取


def test_on_show_reads_the_selected_category_asynchronously(qt_app, patch_config):
    _write_log(patch_config, LogCategory.GUI, "第一行\n第二行\n第三行\n")

    app = _FakeApp()
    page = LogsPage(app)
    page.ensure_built()

    # 預設分類是 crawl；切成 gui 才會讀到我們剛寫的檔案。
    page._category_buttons[LogCategory.GUI.value].click()
    _wait_for(qt_app, page._refresh_task)

    assert "第一行" in page.log_box.toPlainText()
    assert "第三行" in page.log_box.toPlainText()


def test_lines_entry_limits_how_many_lines_are_requested(qt_app, patch_config, monkeypatch):
    _write_log(patch_config, LogCategory.GUI, "\n".join(f"line-{i}" for i in range(10)))

    app = _FakeApp()
    page = LogsPage(app)
    page.ensure_built()
    page._category_buttons[LogCategory.GUI.value].click()
    _wait_for(qt_app, page._refresh_task)

    page.lines_entry.setText("2")
    page.on_show(force=True)
    _wait_for(qt_app, page._refresh_task)

    shown = page.log_box.toPlainText().splitlines()
    assert shown == ["line-8", "line-9"]


def test_invalid_lines_entry_falls_back_to_default(qt_app, patch_config):
    app = _FakeApp()
    page = LogsPage(app)
    page.ensure_built()

    page.lines_entry.setText("不是數字")
    assert page._current_lines() == 400


# ----------------------------------------------------------------- 清除日誌


def test_clear_log_truncates_file_after_confirmation(qt_app, patch_config, monkeypatch):
    _write_log(patch_config, LogCategory.GUI, "會被清掉的內容\n")
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))

    app = _FakeApp()
    page = LogsPage(app)
    page.ensure_built()
    page._category_buttons[LogCategory.GUI.value].click()
    _wait_for(qt_app, page._refresh_task)
    assert "會被清掉的內容" in page.log_box.toPlainText()

    page._clear_log()
    _wait_for(qt_app, page._refresh_task)

    assert page.log_box.toPlainText() == ""
    assert log_file_path(LogCategory.GUI, patch_config).read_text(encoding="utf-8") == ""
    assert any("已清除" in message for message, _ in app.messages)


def test_clear_log_does_nothing_when_user_declines_confirmation(qt_app, patch_config, monkeypatch):
    _write_log(patch_config, LogCategory.GUI, "保留這段內容\n")
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))

    app = _FakeApp()
    page = LogsPage(app)
    page.ensure_built()
    page._category_buttons[LogCategory.GUI.value].click()
    _wait_for(qt_app, page._refresh_task)

    page._clear_log()

    assert log_file_path(LogCategory.GUI, patch_config).read_text(encoding="utf-8") == "保留這段內容\n"


# ----------------------------------------------------------------- 自動整理


def test_auto_refresh_toggle_starts_and_stops_the_timer(qt_app, patch_config):
    app = _FakeApp()
    page = LogsPage(app)
    page.ensure_built()

    assert not page._timer.isActive()
    page.auto_check.setChecked(True)
    assert page._timer.isActive()
    page.auto_check.setChecked(False)
    assert not page._timer.isActive()


def test_on_hide_stops_the_auto_refresh_timer_even_if_left_on(qt_app, patch_config):
    app = _FakeApp()
    page = LogsPage(app)
    page.ensure_built()
    page.auto_check.setChecked(True)
    assert page._timer.isActive()

    page.on_hide()

    assert not page._timer.isActive()
