"""Integration tests for gui_qt/pages/companies.py against a real (test) database.

Exercises the whole path this page relies on: ``gui.controllers.CompanyController``
-> ``database.repository.CompanyRepository`` -> a real SQLAlchemy session (via the
``db_session`` fixture shared by the rest of the suite), with the query running
through ``gui_qt.tasks.BackgroundTask`` exactly like it does in the real app --
not called synchronously in the test.
"""

from __future__ import annotations

import itertools
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from database.repository import CompanyRepository  # noqa: E402
from gui_qt.pages.base import bump_data_version, current_data_version  # noqa: E402
from gui_qt.pages.companies import CompaniesPage, DuplicatesDialog  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeApp:
    """Just enough of gui_qt.app.MainWindow for CompaniesPage to work with."""

    def __init__(self) -> None:
        self.current_page = CompaniesPage.title
        self.messages: list[tuple[str, str]] = []

    def set_status(self, message: str, tone: str = "normal") -> None:
        self.messages.append((message, tone))


def _wait_for_fetch(qt_app, page: CompaniesPage, timeout: float = 3.0) -> None:
    """Pump the event loop until the page's background fetch has landed."""
    deadline = time.time() + timeout
    while page._fetch_task.running and time.time() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)
    assert not page._fetch_task.running, "background fetch never completed"
    qt_app.processEvents()  # drain the queued on_done call itself


#: ``dedupe_key`` is unique, and several tests seed the database more than once
#: (e.g. to simulate "something changed elsewhere, then bump_data_version()")
#: -- a module-wide counter keeps every seeded row's key distinct regardless of
#: how many times a test calls ``_seed()``.
_seed_counter = itertools.count(1)


def _seed(db_session, count: int = 3) -> list[int]:
    repo = CompanyRepository(db_session)
    ids = []
    for _ in range(count):
        n = next(_seed_counter)
        company = repo.create(
            company_name=f"測試公司{n}號",
            name_key=f"測試公司{n}",
            dedupe_key=f"tax:{1000 + n}",
            email=f"contact{n}@example.com",
            industry="金屬加工" if n % 2 == 0 else "電子零組件",
            source="sample",
        )
        ids.append(company.id)
    db_session.commit()
    return ids


def test_companies_page_shows_real_query_results(qt_app, db_session):
    _seed(db_session, 3)

    app = _FakeApp()
    page = CompaniesPage(app)
    page.ensure_built()

    page.on_show()  # first show: always refreshes, starts the async fetch
    _wait_for_fetch(qt_app, page)

    assert page.table.row_count() == 3
    assert page.count_label.text() == "共 3 家公司"
    assert "已載入 3 家公司" in [message for message, _ in app.messages]

    # Industry dropdown got populated from the same background fetch.
    industry_values = [
        page.filter_combos["industry"].itemText(i)
        for i in range(page.filter_combos["industry"].count())
    ]
    assert "金屬加工" in industry_values
    assert "電子零組件" in industry_values


def test_data_version_skip_avoids_requerying_unchanged_data(qt_app, db_session):
    _seed(db_session, 2)

    app = _FakeApp()
    page = CompaniesPage(app)
    page.ensure_built()

    page.on_show()
    _wait_for_fetch(qt_app, page)
    assert page.table.row_count() == 2

    # No bump_data_version() call in between -> on_show() should skip
    # refresh() entirely and never start a new background fetch.
    seen_version_before = page._seen_version
    page.on_show()
    assert not page._fetch_task.running
    assert page._seen_version == seen_version_before
    assert page.table.row_count() == 2  # unchanged, no requery happened


def test_bump_data_version_forces_a_real_requery(qt_app, db_session):
    _seed(db_session, 2)

    app = _FakeApp()
    page = CompaniesPage(app)
    page.ensure_built()
    page.on_show()
    _wait_for_fetch(qt_app, page)
    assert page.table.row_count() == 2

    _seed(db_session, 1)  # one more company, then tell every page about it
    bump_data_version()

    page.on_show()
    _wait_for_fetch(qt_app, page)
    assert page.table.row_count() == 3


def test_search_filters_by_text(qt_app, db_session):
    repo = CompanyRepository(db_session)
    repo.create(
        company_name="甲科技股份有限公司",
        name_key="甲科技",
        dedupe_key="tax:22099131",
        source="sample",
    )
    repo.create(
        company_name="乙工業有限公司",
        name_key="乙工業",
        dedupe_key="tax:22099132",
        source="sample",
    )
    db_session.commit()

    app = _FakeApp()
    page = CompaniesPage(app)
    page.ensure_built()
    page.on_show()
    _wait_for_fetch(qt_app, page)
    assert page.table.row_count() == 2

    page.search_entry.setText("甲科技")
    page._run_search()  # bypass the debounce timer for a deterministic test
    _wait_for_fetch(qt_app, page)

    assert page.table.row_count() == 1
    assert page.table.model.row_at(0)["company_name"] == "甲科技股份有限公司"


def test_clear_filters_reruns_search_with_no_criteria(qt_app, db_session):
    repo = CompanyRepository(db_session)
    repo.create(
        company_name="甲科技股份有限公司",
        name_key="甲科技",
        dedupe_key="tax:33099131",
        source="sample",
    )
    db_session.commit()

    app = _FakeApp()
    page = CompaniesPage(app)
    page.ensure_built()
    page.on_show()
    _wait_for_fetch(qt_app, page)

    page.search_entry.setText("找不到的關鍵字xyz")
    page._run_search()
    _wait_for_fetch(qt_app, page)
    assert page.table.row_count() == 0

    page._clear_filters()
    _wait_for_fetch(qt_app, page)
    assert page.search_entry.text() == ""
    assert page.table.row_count() >= 1


def test_delete_selected_bumps_data_version_and_requeries(qt_app, db_session, monkeypatch):
    ids = _seed(db_session, 2)

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)

    app = _FakeApp()
    page = CompaniesPage(app)
    page.ensure_built()
    page.on_show()
    _wait_for_fetch(qt_app, page)
    assert page.table.row_count() == 2

    page.table.view.selectRow(0)
    version_before = current_data_version()
    page._delete_selected()
    _wait_for_fetch(qt_app, page)

    assert current_data_version() > version_before
    assert page.table.row_count() == 1
    assert any("已刪除" in message for message, _ in app.messages)


def test_edit_and_delete_buttons_without_a_selection_report_status(qt_app, db_session):
    _seed(db_session, 1)

    app = _FakeApp()
    page = CompaniesPage(app)
    page.ensure_built()
    page.on_show()
    _wait_for_fetch(qt_app, page)

    page.table.view.clearSelection()
    page._edit_selected()
    page._delete_selected()

    assert app.messages[-2] == ("請先選擇一家公司", "muted")
    assert app.messages[-1] == ("請先選擇一家公司", "muted")


def test_on_hide_stops_the_search_debounce_timer(qt_app, db_session):
    _seed(db_session, 1)

    app = _FakeApp()
    page = CompaniesPage(app)
    page.ensure_built()
    page.on_show()
    _wait_for_fetch(qt_app, page)

    page._schedule_search()
    assert page._search_timer.isActive()
    page.on_hide()
    assert not page._search_timer.isActive()


def test_find_duplicates_opens_dialog_with_real_groups(qt_app, db_session):
    repo = CompanyRepository(db_session)
    repo.create(
        company_name="重複公司A",
        name_key="重複公司",
        dedupe_key="tax:44099131",
        email="dup@example.com",
        source="sample",
    )
    repo.create(
        company_name="重複公司B",
        name_key="重複公司",
        dedupe_key="tax:44099132",
        email="dup@example.com",
        source="sample",
    )
    db_session.commit()

    app = _FakeApp()
    page = CompaniesPage(app)
    page.ensure_built()

    groups = page.controller.duplicate_groups()
    assert len(groups) >= 1

    dialog = DuplicatesDialog(None, page.controller, groups, on_merged=lambda: None)
    assert dialog.scroll.widget() is not None
    dialog.close()


def test_duplicates_dialog_merge_bumps_data_version(qt_app, db_session):
    repo = CompanyRepository(db_session)
    keep = repo.create(
        company_name="合併保留",
        name_key="合併保留",
        dedupe_key="tax:55099131",
        email="merge@example.com",
        source="sample",
    )
    drop = repo.create(
        company_name="合併被吃",
        name_key="合併保留",
        dedupe_key="tax:55099132",
        email="merge@example.com",
        source="sample",
    )
    db_session.commit()

    from PySide6.QtWidgets import QComboBox

    from controllers.core import CompanyController

    controller = CompanyController()
    groups = controller.duplicate_groups()
    target_group = next(g for g in groups if {c.id for c in g} == {keep.id, drop.id})

    merged_calls: list[int] = []
    dialog = DuplicatesDialog(
        None, controller, [target_group], on_merged=lambda: merged_calls.append(1)
    )

    combo = QComboBox()
    combo.addItems([str(keep.id), str(drop.id)])
    combo.setCurrentText(str(keep.id))

    version_before = current_data_version()
    dialog._merge(target_group, combo)

    assert current_data_version() > version_before
    assert merged_calls == [1]
    assert controller.get(drop.id) is None
    dialog.close()
