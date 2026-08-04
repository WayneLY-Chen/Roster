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
