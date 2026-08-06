"""Tests for gui_qt/pages/crawler.py and its two dialogs.

Everything here runs against the offline ``sample`` crawl source (bundled
HTML fixtures under ``templates/``) -- never real network. The crawl/verify
runs go through the real ``gui.controllers`` classes and a real (temp)
SQLAlchemy session (``db_session``/``patch_config`` fixtures shared by the
rest of the suite), with the actual work happening on a ``QThreadPool``
thread via ``gui_qt.tasks.BackgroundTask`` -- exactly like the real app --
not called synchronously in the test.
"""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import core.config as config_module  # noqa: E402
from core.config import SourceConfig, save_custom_source  # noqa: E402
from gui_qt.pages.base import current_data_version  # noqa: E402
from gui_qt.pages.crawler import ALL_ENABLED, CrawlerPage, CustomSourcesDialog  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _custom_sources_path(monkeypatch, tmp_path):
    """Redirect the module-level custom-sources file into ``tmp_path``.

    ``core.config.CUSTOM_SOURCES_PATH`` is a plain module constant, not part
    of ``AppConfig`` -- left unpatched, saving/deleting a source in a test
    would write into the real project's ``custom_sources.yaml``.
    """
    path = tmp_path / "custom_sources.yaml"
    monkeypatch.setattr(config_module, "CUSTOM_SOURCES_PATH", path)
    return path


class _FakeApp:
    """Just enough of gui_qt.app.MainWindow for CrawlerPage to work with."""

    def __init__(self) -> None:
        self.current_page = CrawlerPage.title
        self.messages: list[tuple[str, str]] = []
        self.status_bar = self

    def set_status(self, message: str, tone: str = "normal") -> None:
        self.messages.append((message, tone))

    # status_bar stand-in
    def start_progress(self) -> None:
        pass

    def stop_progress(self) -> None:
        pass


def _wait_for_task(qt_app, task, timeout: float = 5.0) -> None:
    """Pump the event loop until a BackgroundTask genuinely finishes."""
    deadline = time.time() + timeout
    while task is not None and task.running and time.time() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)
    assert task is None or not task.running, "background task never completed"
    qt_app.processEvents()


def test_build_lists_offline_sample_source(qt_app, patch_config):
    page = CrawlerPage(_FakeApp())
    page.ensure_built()

    values = [page.source_combo.combo.itemText(i) for i in range(page.source_combo.combo.count())]
    assert values == [ALL_ENABLED, "sample"]


def test_ethics_notice_text_is_preserved(qt_app, patch_config):
    """The robots.txt / delay disclosure must survive the port, word for word."""
    from PySide6.QtWidgets import QLabel

    page = CrawlerPage(_FakeApp())
    page.ensure_built()

    texts = [label.text() for label in page.findChildren(QLabel)]
    joined = "\n".join(texts)
    assert "robots.txt" in joined
    assert "請求間隔延遲" in joined


def test_optional_int_rejects_non_integers_and_non_positive():
    with pytest.raises(ValueError, match="必須是整數"):
        CrawlerPage._optional_int("abc", "最多幾頁")
    with pytest.raises(ValueError, match="必須大於 0"):
        CrawlerPage._optional_int("0", "最多幾頁")
    assert CrawlerPage._optional_int("", "最多幾頁") is None
    assert CrawlerPage._optional_int("5", "最多幾頁") == 5


def test_the_crawl_page_does_not_duplicate_the_source_settings(qt_app, patch_config):
    """頁數範圍與欄位選擇只住在「自訂網址」精靈裡，不在這一頁重複一份。

    同一個概念散在兩個地方，使用者會不確定哪個才算數；更實際的問題是
    ——寫在這一頁的東西自動排程根本看不到，排程跑的時候沒有人在這裡填。
    """
    page = CrawlerPage(_FakeApp())
    page.ensure_built()

    for removed in ("from_page_entry", "to_page_entry", "max_pages_entry", "field_checks"):
        assert not hasattr(page, removed), f"{removed} 應該只存在於自訂網址精靈裡"


def test_starting_a_crawl_uses_the_sources_own_settings(qt_app, patch_config, monkeypatch):
    """這一頁只挑來源、按下去，頁數範圍一律照來源自己的設定。"""
    page = CrawlerPage(_FakeApp())
    page.ensure_built()

    started: list[tuple] = []
    monkeypatch.setattr(
        page, "_clear_log", lambda: None
    )

    class _FakeTask:
        running = False

        def start(self, *args):
            started.append(args)

    monkeypatch.setattr("gui_qt.pages.crawler.BackgroundTask", lambda *a, **k: _FakeTask())
    page._start_crawl()

    assert started, "應該要真的啟動一次爬取"
    # (source, max_pages, from_page, to_page) —— 後三個一律 None。
    assert started[0][1:] == (None, None, None)


def test_crawl_against_sample_source_completes_and_bumps_version(
    qt_app, db_session, patch_config
):
    page = CrawlerPage(_FakeApp())
    page.ensure_built()

    before = current_data_version()
    page._start_crawl()
    assert page.start_button.isEnabled() is False

    _wait_for_task(qt_app, page.crawl_task)

    assert page.results_table.row_count() == 1
    row = page.results_table.model.row_at(0)
    assert row["source"] == "sample"
    assert row["found"] > 0

    assert current_data_version() == before + 1
    assert page.start_button.isEnabled() is True
    assert "完成" in page.log_box.toPlainText()


def test_verify_runs_against_real_controller(qt_app, db_session, patch_config):
    page = CrawlerPage(_FakeApp())
    page.ensure_built()

    page._start_verify()
    _wait_for_task(qt_app, page.verify_task)

    assert "驗證完成" in page.log_box.toPlainText()
    assert page.verify_button.isEnabled() is True


def test_enrich_with_nothing_pending_reports_status_without_blocking_dialog(
    qt_app, db_session, patch_config
):
    """An empty database has zero enrichable companies -- no confirmation dialog,
    which matters for the test: a real dialog would block the event loop."""
    page = CrawlerPage(_FakeApp())
    page.ensure_built()

    page._start_enrich()

    assert page.enrich_task is None
    assert page.app.messages[-1] == ("沒有「有網址、缺信箱」的公司需要補抓", "success")


def test_on_source_saved_refreshes_the_source_combo(qt_app, patch_config, monkeypatch):
    """``_on_source_saved`` must drop the config cache and re-read the source list.

    ``patch_config`` replaces ``core.config.load_config`` with a function that
    always returns the same fixed ``AppConfig`` (custom-source merging is
    ``load_config``'s own job, already covered by ``tests/test_config.py``),
    so this test monkeypatches ``CrawlController.source_names`` directly to
    check the *wiring* -- reset, rebuild the controller, refresh the combo --
    without depending on that merge happening to also be exercised here.
    """
    from controllers.core import CrawlController

    page = CrawlerPage(_FakeApp())
    page.ensure_built()

    monkeypatch.setattr(CrawlController, "source_names", lambda self: ["sample", "my_custom"])

    page._on_source_saved("my_custom")

    values = [page.source_combo.combo.itemText(i) for i in range(page.source_combo.combo.count())]
    assert values == [ALL_ENABLED, "sample", "my_custom"]


# --------------------------------------------------------- CustomSourcesDialog


def test_custom_sources_dialog_lists_and_deletes(qt_app, patch_config, monkeypatch):
    source = SourceConfig(
        name="my_custom",
        type="generic_html",
        enabled=True,
        start_url="https://example.test/companies",
        list_selector="div.card",
        pagination={"type": "none"},
        fields={"company_name": {"selector": "h3"}},
    )
    save_custom_source(source)

    from controllers.source import SourceWizardController

    changed: list[str] = []
    dialog = CustomSourcesDialog(
        None, SourceWizardController(), on_changed=changed.append
    )

    assert dialog.table.row_count() == 1
    row = dialog.table.model.row_at(0)
    assert row["name"] == "my_custom"

    # A real QMessageBox.question() would block the test's event loop waiting
    # for a click that will never come -- answer "yes" programmatically instead.
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    dialog.table.view.selectRow(0)
    dialog._delete_selected()

    assert dialog.table.row_count() == 0
    assert changed == ["my_custom"]


def test_the_new_source_button_opens_an_empty_wizard(qt_app, patch_config):
    """按「＋ 自訂網址…」要開一個空白的精靈，不是去編輯某個來源。

    這是實際炸過的當機。``clicked`` 的完整簽章是 ``clicked(bool checked=false)``
    ——直接把它接到 ``_open_source_wizard(entry=None)`` 的話，Qt 會把那個布林值
    當成 ``entry`` 送進來，而 ``False is not None`` 成立，於是程式拿 ``False``
    去當一份來源設定用：

        AttributeError: 'bool' object has no attribute 'get'

    所以這個測試一定要**真的發訊號**，不能直接呼叫 ``_open_source_wizard()``
    ——直接呼叫的話這個 bug 完全測不到，那正是它當初漏掉的原因。
    """
    page = CrawlerPage(_FakeApp())
    page.ensure_built()

    page.custom_source_button.click()

    wizard = page._wizard
    assert wizard is not None
    # 空白精靈：沒有在編輯任何既有來源，網址欄也是空的。
    assert wizard._editing_source is None
    assert wizard.url_entry.text() == ""
    wizard.close()
