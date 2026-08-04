"""Integration tests for gui_qt/pages/contacts.py against a real (test) database.

Exercises the whole path this page relies on: ``gui.controllers.ContactController``
-> ``database.repository.ContactRepository`` -> a real SQLAlchemy session, with the
query running through ``gui_qt.tasks.BackgroundTask`` exactly like it does in the
real app -- not called synchronously in the test.
"""

from __future__ import annotations

import itertools
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from database.repository import CompanyRepository, ContactRepository  # noqa: E402
from gui_qt.pages.base import bump_data_version, current_data_version  # noqa: E402
from gui_qt.pages.contacts import ContactsPage  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeApp:
    def __init__(self) -> None:
        self.current_page = ContactsPage.title
        self.messages: list[tuple[str, str]] = []

    def set_status(self, message: str, tone: str = "normal") -> None:
        self.messages.append((message, tone))


def _wait_for_fetch(qt_app, page: ContactsPage, timeout: float = 3.0) -> None:
    deadline = time.time() + timeout
    while page._fetch_task.running and time.time() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)
    assert not page._fetch_task.running, "background fetch never completed"
    qt_app.processEvents()


#: ``dedupe_key`` is unique, and several tests seed the database more than once
#: within a single test -- a module-wide counter keeps every seeded company's
#: key distinct regardless of how many times a test calls ``_seed_contacts()``.
_seed_counter = itertools.count(1)


def _seed_contacts(db_session, count: int = 4) -> list[int]:
    n = next(_seed_counter)
    company_repo = CompanyRepository(db_session)
    company = company_repo.create(
        company_name=f"聯絡人測試公司{n}號",
        name_key=f"聯絡人測試公司{n}",
        dedupe_key=f"tax:9900{n:04d}",
        source="sample",
    )
    contact_repo = ContactRepository(db_session)
    ids = []
    for i in range(count):
        contact = contact_repo.add(
            company.id,
            name=f"聯絡人{n}-{i}號",
            title="業務",
            email=f"person{n}-{i}@example.com",
            phone=f"02-1234-{n:03d}{i}",
            mobile=f"09{n:02d}-{i}00-000",
            is_primary=(i == 0),
        )
        ids.append(contact.id)
    db_session.commit()
    return ids


def test_contacts_page_shows_real_query_results(qt_app, db_session):
    _seed_contacts(db_session, 4)

    app = _FakeApp()
    page = ContactsPage(app)
    page.ensure_built()

    page.on_show()
    _wait_for_fetch(qt_app, page)

    assert page.table.row_count() == 4
    assert page.count_label.text() == "共 4 位聯絡人"
    assert "已載入 4 位聯絡人" in [message for message, _ in app.messages]


def test_data_version_skip_avoids_requerying_unchanged_data(qt_app, db_session):
    _seed_contacts(db_session, 2)

    app = _FakeApp()
    page = ContactsPage(app)
    page.ensure_built()
    page.on_show()
    _wait_for_fetch(qt_app, page)
    assert page.table.row_count() == 2

    page.on_show()  # nothing bumped the data version in between
    assert not page._fetch_task.running
    assert page.table.row_count() == 2


def test_bump_data_version_forces_a_real_requery(qt_app, db_session):
    _seed_contacts(db_session, 2)

    app = _FakeApp()
    page = ContactsPage(app)
    page.ensure_built()
    page.on_show()
    _wait_for_fetch(qt_app, page)
    assert page.table.row_count() == 2

    _seed_contacts(db_session, 1)
    bump_data_version()

    page.on_show()
    _wait_for_fetch(qt_app, page)
    assert page.table.row_count() == 3


def test_search_filters_by_text(qt_app, db_session):
    company_repo = CompanyRepository(db_session)
    company = company_repo.create(
        company_name="聯絡人搜尋測試",
        name_key="聯絡人搜尋測試",
        dedupe_key="tax:99000002",
        source="sample",
    )
    contact_repo = ContactRepository(db_session)
    contact_repo.add(company.id, name="王小明", email="wang@example.com")
    contact_repo.add(company.id, name="陳小華", email="chen@example.com")
    db_session.commit()

    app = _FakeApp()
    page = ContactsPage(app)
    page.ensure_built()
    page.on_show()
    _wait_for_fetch(qt_app, page)

    page.search_entry.setText("王小明")
    page._run_search()  # bypass the debounce timer for a deterministic test
    _wait_for_fetch(qt_app, page)

    assert page.table.row_count() == 1
    assert page.table.model.row_at(0)["name"] == "王小明"


def test_delete_selected_bumps_data_version_and_requeries(qt_app, db_session, monkeypatch):
    _seed_contacts(db_session, 2)

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)

    app = _FakeApp()
    page = ContactsPage(app)
    page.ensure_built()
    page.on_show()
    _wait_for_fetch(qt_app, page)
    assert page.table.row_count() == 2

    page.table.view.selectRow(0)
    version_before = current_data_version()
    page._delete_selected()
    _wait_for_fetch(qt_app, page)

    assert current_data_version() == version_before + 1
    assert page.table.row_count() == 1
    assert any("已刪除" in message for message, _ in app.messages)


def test_open_company_and_delete_without_a_selection_report_status(qt_app, db_session):
    _seed_contacts(db_session, 1)

    app = _FakeApp()
    page = ContactsPage(app)
    page.ensure_built()
    page.on_show()
    _wait_for_fetch(qt_app, page)

    page.table.view.clearSelection()
    page._open_company()
    page._delete_selected()

    assert app.messages[-2] == ("請先選擇一位聯絡人", "muted")
    assert app.messages[-1] == ("請先選擇一位聯絡人", "muted")


def test_on_hide_stops_the_search_debounce_timer(qt_app, db_session):
    _seed_contacts(db_session, 1)

    app = _FakeApp()
    page = ContactsPage(app)
    page.ensure_built()
    page.on_show()
    _wait_for_fetch(qt_app, page)

    page._schedule_search()
    assert page._search_timer.isActive()
    page.on_hide()
    assert not page._search_timer.isActive()
