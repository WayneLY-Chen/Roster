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
        self.progress_events: list[tuple[int, int | None]] = []
        self.progress_total: int | None = None
        self.status_bar = self

    def set_status(self, message: str, tone: str = "normal") -> None:
        self.messages.append((message, tone))

    # status_bar stand-in
    #
    # 簽章必須跟 gui_qt.widgets.StatusBar 一模一樣，由
    # test_the_fake_status_bar_matches_the_real_one 盯著。
    #
    # 對不上的代價不是「少測到一點東西」：``advance_progress`` 以前根本不存在
    # 於這個替身上，於是每一次爬取進度都在 ``_handle_progress`` 裡丟
    # AttributeError。Qt 會把 slot 裡的例外吞掉（印出 traceback 之後繼續），
    # 所以測試照樣是綠的——但整份測試報告裡混著幾十份 traceback，而且進度
    # 回報那條路實際上一行都沒被驗證過。更糟的是那個例外是在「pool 執行緒
    # 正在送訊號」的當下逼 Python 組 traceback，正是 gui_qt/tasks.py 開頭
    # 記載會讓整個直譯器以 access violation 收場的那種情境。
    def start_progress(self, total: int | None = None) -> None:
        self.progress_total = total

    def advance_progress(self, done: int, total: int | None = None) -> None:
        self.progress_events.append((done, total))

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


def test_the_fake_status_bar_matches_the_real_one():
    """替身的進度介面必須跟 :class:`gui_qt.widgets.StatusBar` 一模一樣。

    這一條是補寫的，因為對不上這件事真的發生過而且沒有人發現：``_FakeApp``
    上沒有 ``advance_progress``，於是爬取的每一次進度回報都在
    ``BackgroundTask._handle_progress`` 裡丟 ``AttributeError``。

    為什麼沒被發現：Qt 會把 slot 裡的例外印出來然後**繼續跑**，所以測試
    全都是綠的。代價有三個——測試報告裡混著幾十份 traceback、進度回報那條
    路實際上完全沒被驗證、以及那個例外是在 pool 執行緒送訊號的當下逼 Python
    組 traceback，正好命中 ``gui_qt/tasks.py`` 開頭記載的那個會讓整個直譯器
    以 access violation 收場的情境。

    比對簽章而不只是比對「有沒有這個方法」：``start_progress`` 真的那一支
    收一個 ``total``，替身少收一個參數一樣會炸。
    """
    import inspect

    from gui_qt.widgets import StatusBar

    fake = _FakeApp().status_bar
    for name in ("start_progress", "advance_progress", "stop_progress"):
        assert hasattr(fake, name), f"替身少了 StatusBar.{name}"
        real_params = list(inspect.signature(getattr(StatusBar, name)).parameters)
        fake_params = list(inspect.signature(getattr(type(fake), name)).parameters)
        assert fake_params == real_params, (
            f"StatusBar.{name} 的簽章是 {real_params}，替身是 {fake_params}"
        )


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
    app = _FakeApp()
    page = CrawlerPage(app)
    page.ensure_built()

    before = current_data_version()
    page._start_crawl()
    assert page.start_button.isEnabled() is False

    _wait_for_task(qt_app, page.crawl_task)

    assert page.results_table.row_count() == 1
    row = page.results_table.model.row_at(0)
    assert row["source"] == "sample"
    assert row["found"] > 0

    assert page.start_button.isEnabled() is True
    assert "完成" in page.log_box.toPlainText()

    # 進度真的送到 status bar 了。這一段是補寫的：``advance_progress`` 以前
    # 根本不在 ``_FakeApp`` 上，所以每一次進度回報都在丟 AttributeError，
    # 而 Qt 把它吞掉了——這條路從來沒有被驗證過，改壞了也不會有人知道。
    assert app.progress_events, "爬完一輪卻沒有任何進度事件送到 status bar"
    done, _total = app.progress_events[0]
    assert done >= 1

    # 每一次進度都會 bump 一次，最後完成時再 bump 一次。
    #
    # 這裡原本寫的是 ``== before + 1``，而它會過**是因為上面那個 bug**：
    # ``_on_crawl_progress`` 先呼叫 ``advance_progress`` 才呼叫
    # ``bump_data_version()``，前者一丟 AttributeError，後者整個不會執行。
    # 所以那句斷言驗證的是「進度不會 bump」——正好跟
    # ``gui_qt/pages/crawler.py`` 裡寫明的用意相反（爬一趟可能一個多小時，
    # 公司頁要能跟著長出來，不能等到最後）。
    assert current_data_version() == before + len(app.progress_events) + 1


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


def test_completion_with_nothing_pending_reports_status_without_blocking_dialog(
    qt_app, db_session, patch_config
):
    """空資料庫沒有東西可補，所以不會跳確認視窗——這對測試很重要，
    真的跳出來會把事件迴圈卡住。"""
    page = CrawlerPage(_FakeApp())
    page.ensure_built()

    page._start_completion()

    assert page.completion_task is None
    assert page.app.messages[-1] == ("每一家公司的資料都齊了，沒有需要補的", "success")


def test_the_completion_button_says_what_it_needs_and_what_it_finds(qt_app, patch_config):
    """三顆補完按鈕的前提各不相同，使用者要看得出來該按哪一顆。

    「補抓信箱」要先有網址、「補公司登記資料」要先有統編，只有名字的名單
    按那兩顆都會得到「沒有需要處理的公司」——那句話讀起來像是「已經很完整
    了」。這一顆的說明必須講清楚它不需要那些前提。
    """
    page = CrawlerPage(_FakeApp())
    page.ensure_built()

    assert page.completion_button.text() == "補齊公司資料"
    tip = page.completion_button.toolTip()
    assert "只有公司名稱" in tip
    assert "商業司" in tip


def test_completion_done_reports_the_rejected_sites_rather_than_hiding_them(
    qt_app, db_session, patch_config
):
    """通不過驗證的網址是刻意留白，不是搜尋壞了。

    不講的話使用者只會看到「找到 0 個官網」，然後以為功能沒用。
    """
    from crawler.complete import CompletionSummary

    page = CrawlerPage(_FakeApp())
    page.ensure_built()

    summary = CompletionSummary(considered=3, updated=1, rejected_unconfirmed=2)
    summary.filled = {"tax_id": 1}
    page._on_completion_done(summary)

    log_text = page.log_box.toPlainText()
    assert "沒有提到" in log_text
    assert "統一編號 1" in log_text  # 欄位名要翻成中文，不是印 tax_id
    assert page.app.messages[-1][1] == "success"


def test_the_batch_size_box_asks_how_many_not_where_to_start(qt_app, patch_config):
    """使用者只該被問「這次幾家」。

    問「從第幾筆開始」是把記帳的責任丟回給人：填錯一個數字就跳過一整段，
    而且填錯完全看不出來——程式照樣回報「處理 200 家」。從第幾家開始由
    ``completion_checked_at`` 決定，見 crawler/complete.py 的 queue_position()。
    """
    page = CrawlerPage(_FakeApp())
    page.ensure_built()

    assert page.completion_count.value() == patch_config.completion.batch_size
    tip = page.completion_count.toolTip()
    assert "從上次停下來的地方" in tip
    assert "從第幾家開始" in tip


def test_completion_done_reports_what_is_left_for_the_next_batch(
    qt_app, db_session, patch_config
):
    """分批跑的人真正要看的是「還剩幾家、下次按下去會不會有進展」。

    「待補」跟「還沒跑過」要分開講：跑完一批之後待補幾乎不會動（補不到的
    公司仍然缺欄位），會動的是「還沒跑過」。只顯示前者的話，使用者按了五次
    看到同一個數字，會以為按鈕沒有用。
    """
    from crawler.complete import CompletionSummary

    page = CrawlerPage(_FakeApp())
    page.ensure_built()

    page._on_completion_done(
        CompletionSummary(
            considered=200, updated=43, marked_done=200,
            remaining=2499, remaining_untried=2299,
        )
    )

    log_text = page.log_box.toPlainText()
    assert "標記完成 200 家" in log_text
    assert "還剩 2499 家" in log_text
    assert "2299 家還沒跑過" in log_text
    assert "可以再按一次" in page.app.messages[-1][0]


def test_completion_says_the_throttled_companies_will_come_back(
    qt_app, db_session, patch_config
):
    """被限流時沒搜尋到的公司不會被記成跑過——這件事必須講出來。

    不講的話，使用者看到「搜尋已停止」會以為那一批公司白跑了，而實際上
    下次按下去拿到的還是它們。
    """
    from crawler.complete import CompletionSummary

    page = CrawlerPage(_FakeApp())
    page.ensure_built()

    page._on_completion_done(
        CompletionSummary(considered=50, updated=3, marked_done=3,
                          search_stopped="額度用完了")
    )

    assert "下次按下去還是它們" in page.log_box.toPlainText()


def test_completion_done_warns_when_the_search_stopped_partway(
    qt_app, db_session, patch_config
):
    """限流跑到一半停掉，結果就不完整——那不是 success。"""
    from crawler.complete import CompletionSummary

    page = CrawlerPage(_FakeApp())
    page.ensure_built()

    page._on_completion_done(CompletionSummary(considered=5, updated=1, search_stopped="額度用完了"))

    assert "額度用完了" in page.log_box.toPlainText()
    assert page.app.messages[-1][1] == "warning"


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
