"""Integration tests for gui_qt/pages/export_page.py against a real (test) database.

Exercises the whole path this page relies on: ``gui.controllers.ExportController``
-> ``exporter.service``/``exporter.base`` -> a real SQLAlchemy session (via
``tests/conftest.py``'s ``db_session`` fixture), with the export itself running
through ``gui_qt.tasks.BackgroundTask`` exactly like it does in the real app.

``tmp_config`` (see ``tests/conftest.py``) already points ``exporter.output_dir``
at a directory under ``tmp_path``, so a "leave path blank" export never writes
anywhere near the user's real ``output/`` folder.
"""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.errors import CRMError  # noqa: E402
from database.repository import CompanyRepository  # noqa: E402
from gui_qt.pages.export_page import ExportPage  # noqa: E402
from gui_qt.widgets import Section  # noqa: E402


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
    qt_app.processEvents()


def _seed(db_session) -> None:
    repo = CompanyRepository(db_session)
    repo.create(
        company_name="甲科技股份有限公司",
        name_key="甲科技",
        dedupe_key="tax:11111111",
        email="a@example.com",
        source="sample",
    )
    repo.create(
        company_name="乙工業有限公司",
        name_key="乙工業",
        dedupe_key="tax:22222222",
        source="sample",
    )
    db_session.commit()


# ------------------------------------------------------------------- 建立元件


def test_build_creates_widgets_with_all_columns_checked(qt_app, db_session):
    app = _FakeApp()
    page = ExportPage(app)
    page.ensure_built()

    assert page.format_combo.count() > 0
    assert len(page.column_checks) > 0
    assert all(checkbox.isChecked() for checkbox in page.column_checks.values())
    assert page._selected_columns() is None  # 全選時視為「不限制欄位」


def test_filters_section_stays_compact_next_to_the_taller_columns_list(qt_app, db_session):
    """使用者截圖回報：篩選條件卡片下半留了一大塊空白，欄位清單卻被裁切。

    修法是讓「篩選條件」用 AlignTop 貼齊頂端、維持自然高度，讓「欄位」清單
    去吃剩下的高度——這裡只驗證欄位清單的可用高度不會比篩選條件矮，這是
    修好之後才會成立的關係（原本兩欄被拉成一樣高，欄位清單反而更常被裁切）。
    """
    app = _FakeApp()
    page = ExportPage(app)
    page.ensure_built()
    page.resize(900, 700)
    page.show()
    try:
        sections = page.findChildren(Section)
        # 依 build() 加入順序：0 = 格式與輸出，1 = 篩選條件，2 = 欄位。
        filters_section, columns_section = sections[1], sections[2]
        assert columns_section.height() >= filters_section.height()
    finally:
        page.hide()


# ------------------------------------------------------------------- 欄位勾選


def test_set_all_columns_toggles_every_checkbox(qt_app, db_session):
    app = _FakeApp()
    page = ExportPage(app)
    page.ensure_built()

    page._set_all_columns(False)
    assert page._selected_columns() == []
    assert ("已全選所有欄位" not in [m for m, _ in app.messages])

    page._set_all_columns(True)
    assert page._selected_columns() is None


# --------------------------------------------------------------------- 篩選


def test_build_filter_reads_form_fields(qt_app, db_session):
    app = _FakeApp()
    page = ExportPage(app)
    page.ensure_built()

    page.text_entry.entry.setText("科技")
    page.industry_entry.entry.setText("電子")
    page.email_only_check.setChecked(True)
    page.limit_entry.entry.setText("10")

    criteria = page._build_filter()
    assert criteria.text == "科技"
    assert criteria.industry == "電子"
    assert criteria.has_email is True
    assert criteria.limit == 10


def test_build_filter_rejects_non_integer_limit(qt_app, db_session):
    app = _FakeApp()
    page = ExportPage(app)
    page.ensure_built()

    page.limit_entry.entry.setText("不是數字")
    with pytest.raises(CRMError):
        page._build_filter()


# --------------------------------------------------------------------- 匯出


def test_start_export_writes_a_file_and_reports_success(qt_app, db_session, tmp_path):
    _seed(db_session)

    app = _FakeApp()
    page = ExportPage(app)
    page.ensure_built()
    page.format_combo.setCurrentIndex(0)  # 目前設定檔開放的第一種格式（excel）

    page._start_export()
    _wait_for(qt_app, page.export_task)

    assert page.last_result is not None
    path, count = page.last_result
    assert path.exists()
    assert count == 2
    assert page.export_button.isEnabled()
    assert page.open_folder_button.isEnabled()
    assert "success" in [tone for _, tone in app.messages]
    assert app.status_bar.progress_started == app.status_bar.progress_stopped == 1


def test_start_export_with_no_columns_checked_is_rejected_before_running(qt_app, db_session):
    app = _FakeApp()
    page = ExportPage(app)
    page.ensure_built()
    page._set_all_columns(False)

    page._start_export()

    assert not page.export_task.running
    kind, tone = app.messages[-1]
    assert tone == "error"
    assert "欄位" in kind
