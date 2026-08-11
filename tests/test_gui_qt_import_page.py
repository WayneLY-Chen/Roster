"""Integration tests for gui_qt/pages/import_page.py against a real (test) database.

Exercises the whole path this page relies on: ``gui.controllers.ImportController``
-> ``exporter.importer`` -> a real SQLAlchemy session (via ``tests/conftest.py``'s
``db_session`` fixture).

Nothing here writes outside ``tmp_path``: sample/import files are written under
``tmp_path``, and ``db_session``/``patch_config`` route the database at a
temporary file, never the user's real ``crm.db`` or ``config.yaml``.

## Why the actual DB write does *not* run through a real ``QThreadPool`` thread here

An earlier version of this file called ``page._start_import()`` end to end
(through the real :class:`~gui_qt.tasks.BackgroundTask`, i.e. a real
``QThreadPool`` thread doing a real ``session.flush()``). Running the *whole*
test suite several times in a row, that test would intermittently (roughly
every other run) take down the entire Python interpreter with an access
violation inside ``sqlalchemy/orm/session.py``'s ``_flush`` -- not a test
failure, the whole process. This reproduces the same class of problem
documented in ``gui_qt/tasks.py``'s module docstring (Python 3.14 + PySide6
6.11.1 + a ``QThreadPool``-borrowed thread is fragile for certain workloads),
just triggered by a real SQLAlchemy write instead of exception introspection.

The fix mirrors what the crawler page's tests do for the same reason: split
into (1) a test that stubs the controller call to prove the Qt/BackgroundTask
*wiring* (progress -> done -> ``bump_data_version()`` -> UI update) without a
single SQLAlchemy call ever happening off the main thread, and (2) a test that
calls ``ImportController.run()`` directly, synchronously, on the test's own
thread to prove the *actual* database write behaviour -- no ``QThreadPool``
involved there either. Together they cover exactly what the single end-to-end
test did, without ever letting a real SQLAlchemy flush happen on a
``QThreadPool`` thread.
"""

from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog  # noqa: E402

from exporter.importer import ImportSummary  # noqa: E402
from exporter.sample_template import write_sample  # noqa: E402
from controllers.core import ImportController  # noqa: E402
from gui_qt.pages.base import current_data_version  # noqa: E402
from gui_qt.pages.import_page import ImportPage  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeStatusBar:
    def __init__(self) -> None:
        self.progress_started = 0
        self.progress_stopped = 0

    def start_progress(self) -> None:
        self.progress_started += 1

    def stop_progress(self) -> None:
        self.progress_stopped += 1


class _FakeApp:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.status_bar = _FakeStatusBar()

    def set_status(self, message: str, tone: str = "normal") -> None:
        self.messages.append((message, tone))


def _wait_for(qt_app, task, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    while task.running and time.time() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)
    assert not task.running, "background task never completed"
    qt_app.processEvents()  # drain the queued on_done/on_error call itself


# ------------------------------------------------------------------- 建立元件


def test_build_creates_widgets_with_import_disabled(qt_app, db_session):
    app = _FakeApp()
    page = ImportPage(app)
    page.ensure_built()

    assert page.mapping_table.row_count() == 0
    assert not page.import_button.isEnabled()
    assert page.path_label.text() == "尚未選擇檔案"


# --------------------------------------------------------------------- 選檔


def test_choosing_a_file_loads_preview_and_enables_import(qt_app, db_session, tmp_path, monkeypatch):
    sample_path = tmp_path / "sample.csv"
    write_sample(sample_path)

    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(sample_path), ""))
    )

    app = _FakeApp()
    page = ImportPage(app)
    page.ensure_built()

    page._choose_file()

    assert page.selected_path == sample_path
    assert page.import_button.isEnabled()
    assert page.mapping_table.row_count() > 0
    assert "3" in page.total_rows_label.text()  # write_sample() 產生 3 列範例資料


# -------------------------------------------------------------------- 匯入


def test_start_import_wiring_bumps_data_version_and_updates_ui(
    qt_app, db_session, tmp_path, monkeypatch
):
    """``BackgroundTask`` 的接線本身：進度 -> 完成 -> ``bump_data_version()``
    -> UI 更新，全部走真正的 ``QThreadPool`` 執行緒——但 worker 換成一個
    不碰 SQLAlchemy 的 stub（見檔案開頭的說明，理由是避免在 ``QThreadPool``
    執行緒上跑真正的 ``session.flush()``）。真正的資料庫寫入行為由下面
    ``test_import_controller_run_writes_expected_companies_to_the_database``
    直接同步呼叫 controller 驗證。
    """
    sample_path = tmp_path / "sample.csv"
    write_sample(sample_path)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(sample_path), ""))
    )

    fake_summary = ImportSummary(
        file=str(sample_path), rows_read=3, records_new=2, records_merged=0,
        records_duplicate=0, records_invalid=1, unmapped_columns=[],
    )

    def _stub_run(path, label=None, *, report, cancel_event):
        report({"stage": "reading"})  # 純 Python callable，不碰任何 widget 或 DB
        return fake_summary

    app = _FakeApp()
    page = ImportPage(app)
    page.ensure_built()
    page._choose_file()
    monkeypatch.setattr(page.import_task, "worker", _stub_run)

    version_before = current_data_version()
    page._start_import()
    _wait_for(qt_app, page.import_task)

    assert current_data_version() > version_before  # bump_data_version() 有被呼叫
    assert "success" in [tone for _, tone in app.messages]
    assert "匯入完成" in page.import_status.text()
    assert "新增：2" in page.summary_label.text()
    assert page.import_button.isEnabled()
    assert page.choose_button.isEnabled()
    assert app.status_bar.progress_started == app.status_bar.progress_stopped == 1


# --------------------------------------------------------- 匯入後自動補齊


@pytest.fixture(autouse=True)
def _user_settings_in_tmp_path(monkeypatch, tmp_path):
    """把 ``user_settings.yaml`` 導到 ``tmp_path``。

    「匯入後自動補齊」那個勾選框一被切換就會存檔（``_save_auto_complete``），
    而 ``core.config.USER_SETTINGS_PATH`` 是模組層級的常數、不在 ``AppConfig``
    裡——``patch_config`` 管不到它。不導開的話，任何一個碰到那個勾選框的測試
    都會改寫**使用者真正的設定檔**。

    做成 autouse 而不是逐個測試套用：以後只要有人在這個檔案裡多寫一個測試、
    不小心碰到那個勾選框，就會安靜地再污染一次。
    """
    import core.config as config_module

    target = tmp_path / "user_settings.yaml"
    monkeypatch.setattr(config_module, "USER_SETTINGS_PATH", target)
    return target


def _with_completion_settings(monkeypatch, config, **values):
    """讓 ``get_config()`` 回傳一份改過 ``completion`` 區段的設定。

    ``patch_config`` 把 ``load_config`` 換成一個永遠回傳同一個物件的函式，
    所以寫進 ``user_settings.yaml`` 不會反映到 ``get_config()`` 上。要改
    「程式讀到的設定」就得換掉那個物件本身——``AppConfig`` 是 frozen 的，
    所以用 ``model_copy``。
    """
    import core.config as config_module

    updated = config.model_copy(
        update={"completion": config.completion.model_copy(update=values)}
    )
    monkeypatch.setattr(config_module, "load_config", lambda path=None: updated)
    config_module.reset_config()
    return updated


def _completion_summary(**overrides):
    from crawler.complete import CompletionSummary

    summary = CompletionSummary(considered=2, updated=2)
    summary.filled = {"tax_id": 2, "email": 1}
    for name, value in overrides.items():
        setattr(summary, name, value)
    return summary


def _prepare_import(page, tmp_path, monkeypatch, summary: ImportSummary):
    """把頁面帶到「匯入剛完成」的狀態，中間完全不碰資料庫。"""
    sample_path = tmp_path / "sample.csv"
    write_sample(sample_path)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(sample_path), ""))
    )
    page.ensure_built()
    page._choose_file()
    monkeypatch.setattr(
        page.import_task,
        "worker",
        lambda path, label=None, *, report, cancel_event: summary,
    )
    return sample_path


def test_unchecked_means_nothing_happens_after_an_import(
    qt_app, db_session, tmp_path, monkeypatch
):
    """預設不自動補齊。補齊會連網，那件事該由使用者自己決定。"""
    app = _FakeApp()
    page = ImportPage(app)
    summary = ImportSummary(file="x.csv", rows_read=2, records_new=2, company_ids=[1, 2])
    _prepare_import(page, tmp_path, monkeypatch, summary)
    page.auto_complete_check.setChecked(False)

    called: list = []
    monkeypatch.setattr(page.completion_task, "worker", lambda **kw: called.append(kw))

    page._start_import()
    _wait_for(qt_app, page.import_task)

    assert called == []
    assert not page.completion_task.running


def test_checked_starts_completion_for_the_imported_companies_only(
    qt_app, db_session, tmp_path, monkeypatch
):
    """只補這一批。把資料庫裡既有的幾千家一起重跑是另一個決定。"""
    app = _FakeApp()
    page = ImportPage(app)
    summary = ImportSummary(file="x.csv", rows_read=2, records_new=2, company_ids=[7, 9])
    _prepare_import(page, tmp_path, monkeypatch, summary)
    page.auto_complete_check.setChecked(True)

    seen: dict = {}

    def _stub(*, company_ids=None, report, cancel_event, **_kw):
        seen["company_ids"] = company_ids
        report({"done": 1, "total": 2, "name": "測試公司"})
        return _completion_summary()

    monkeypatch.setattr(page.completion_task, "worker", _stub)

    page._start_import()
    _wait_for(qt_app, page.import_task)
    _wait_for(qt_app, page.completion_task)

    assert seen["company_ids"] == [7, 9]
    assert "自動補齊完成" in page.import_status.text()
    # 匯入的結果不能被補齊的訊息蓋掉——那兩件事使用者都要看到。
    assert "新增：2" in page.summary_label.text()
    assert "補上 3 個欄位" in page.summary_label.text()
    assert page.import_button.isEnabled()


def test_the_batch_is_capped_so_a_huge_file_is_not_a_silent_multi_hour_job(
    qt_app, db_session, patch_config, tmp_path, monkeypatch
):
    """勾了自動補齊之後匯入三千筆，不該變成一個跑好幾小時、而且使用者不知道
    自己按下去了的動作。"""
    _with_completion_settings(monkeypatch, patch_config, auto_after_import_limit=2)

    app = _FakeApp()
    page = ImportPage(app)
    summary = ImportSummary(
        file="x.csv", rows_read=5, records_new=5, company_ids=[1, 2, 3, 4, 5]
    )
    _prepare_import(page, tmp_path, monkeypatch, summary)
    page.auto_complete_check.setChecked(True)

    seen: dict = {}
    monkeypatch.setattr(
        page.completion_task,
        "worker",
        lambda *, company_ids=None, report, cancel_event, **_kw: (
            seen.update(company_ids=company_ids) or _completion_summary()
        ),
    )

    page._start_import()
    _wait_for(qt_app, page.import_task)
    _wait_for(qt_app, page.completion_task)

    assert seen["company_ids"] == [1, 2]
    # 「有 3 家沒被處理」必須留在跑完之後還看得到的地方。只寫在開跑那一句
    # 的話，它會被完成訊息蓋掉，使用者只看到它閃一下。
    assert "另外 3 家" in page.summary_label.text()


def test_a_failed_completion_does_not_claim_the_import_failed(
    qt_app, db_session, tmp_path, monkeypatch
):
    """匯入已經成功寫進資料庫了。這一段失敗不該讓使用者以為資料沒進去。"""
    app = _FakeApp()
    page = ImportPage(app)
    summary = ImportSummary(file="x.csv", rows_read=1, records_new=1, company_ids=[1])
    _prepare_import(page, tmp_path, monkeypatch, summary)
    page.auto_complete_check.setChecked(True)

    def _boom(**_kw):
        raise RuntimeError("網路不通")

    monkeypatch.setattr(page.completion_task, "worker", _boom)
    monkeypatch.setattr(page, "report_error", lambda exc: None)

    page._start_import()
    _wait_for(qt_app, page.import_task)
    _wait_for(qt_app, page.completion_task)

    assert "匯入已完成" in page.import_status.text()
    assert page.import_button.isEnabled()


def test_toggling_the_checkbox_is_remembered(
    qt_app, db_session, monkeypatch, _user_settings_in_tmp_path
):
    """下次打開程式還記得使用者的選擇。

    直接看檔案內容，不看 ``get_config()``——``patch_config`` 把設定物件釘死
    了，從它身上讀不出「有沒有存進去」這件事。
    """
    app = _FakeApp()
    page = ImportPage(app)
    page.ensure_built()

    page.auto_complete_check.setChecked(True)

    saved = _user_settings_in_tmp_path.read_text(encoding="utf-8")
    assert "auto_after_import: true" in saved


def test_a_settings_file_that_cannot_be_written_does_not_cancel_the_choice(
    qt_app, db_session, monkeypatch
):
    """存不起來時這一次仍然照做——真正決定要不要跑的是勾選框當下的狀態。

    把勾勾取消掉才是最糟的：使用者明明按了，畫面卻自己彈回去。
    """
    import gui_qt.pages.import_page as page_module
    from core.errors import CRMError

    def _boom(*_a, **_k):
        raise CRMError("磁碟唯讀")

    monkeypatch.setattr(page_module, "save_user_setting", _boom)

    app = _FakeApp()
    page = ImportPage(app)
    page.ensure_built()

    page.auto_complete_check.setChecked(True)

    assert page.auto_complete_check.isChecked()
    assert app.messages[-1][1] == "warning"


def test_import_controller_run_writes_expected_companies_to_the_database(db_session, tmp_path):
    """真正的資料庫寫入行為——直接同步呼叫 controller，不透過任何背景執行緒。

    ``ImportController.run()`` 本身就是 ``import_page.py`` 唯一會呼叫的東西
    （見 ``ImportPage.__init__`` 把它原封不動交給 ``BackgroundTask``），這裡
    這樣呼叫，等於驗證了頁面「按下匯入之後真正會發生的事」，只是不透過
    ``QThreadPool`` 執行緒。
    """
    sample_path = tmp_path / "sample.csv"
    write_sample(sample_path)

    controller = ImportController()
    summary = controller.run(
        sample_path, None, report=lambda payload: None, cancel_event=threading.Event()
    )

    assert summary.rows_read == 3
    assert summary.records_new + summary.records_merged >= 1

    from database.repository import CompanyRepository

    companies = CompanyRepository(db_session).search_views(None)
    names = {c.company_name for c in companies}
    assert "範例科技股份有限公司" in names


def test_start_import_reports_error_on_bad_file(qt_app, db_session, tmp_path, monkeypatch):
    bad_path = tmp_path / "not-a-real-spreadsheet.csv"
    bad_path.write_text("", encoding="utf-8")  # 空檔案，preview() 應該會失敗或給空欄位

    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(bad_path), ""))
    )

    app = _FakeApp()
    page = ImportPage(app)
    page.ensure_built()
    page._choose_file()

    # 空檔案沒有欄位可對應，匯入按鈕不該被打開，也不該有預覽列。
    if page.preview is not None:
        assert page.mapping_table.row_count() == 0


# ---------------------------------------------------------------- 下載範例檔


def test_download_sample_writes_file_and_updates_status(qt_app, db_session, tmp_path, monkeypatch):
    target = tmp_path / "匯入範例.xlsx"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), ""))
    )

    app = _FakeApp()
    page = ImportPage(app)
    page.ensure_built()

    page._download_sample()

    assert target.exists()
    assert ("success" in [tone for _, tone in app.messages])
    assert str(target) in app.messages[-1][0]
    assert "範例檔已存到" in page.import_status.text()


def test_download_sample_user_cancels_dialog_does_nothing(qt_app, db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("", "")))

    app = _FakeApp()
    page = ImportPage(app)
    page.ensure_built()

    page._download_sample()

    assert app.messages == []
    assert page.import_status.text() == ""


def test_download_sample_reports_error_for_unsupported_suffix(
    qt_app, db_session, tmp_path, monkeypatch
):
    target = tmp_path / "範例.txt"  # write_sample() 不支援 .txt，會丟 ExportError
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), ""))
    )

    app = _FakeApp()
    page = ImportPage(app)
    page.ensure_built()

    page._download_sample()

    assert not target.exists()
    assert app.messages
    kind, tone = app.messages[-1]
    assert tone == "error"
    assert "ExportError" in kind
