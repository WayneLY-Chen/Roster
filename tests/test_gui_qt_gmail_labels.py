"""郵件頁的「Gmail 標籤」區塊：按鈕真的走到背景工作、結果真的顯示出來。

Gmail 是 :class:`tests.test_gmail_labels.FakeGmail`，不連網路。
"""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from database.repository import CompanyRepository  # noqa: E402
from gui_qt.gmail_labels import GmailLabelSection  # noqa: E402
from gui_qt.widgets import CHECK_KEY  # noqa: E402
from tests.test_gmail_labels import ME, FakeGmail  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


class _Page:
    """郵件頁的替身：區塊只借用它的狀態列與錯誤回報。"""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.errors: list[Exception] = []

    def status(self, message: str, tone: str = "normal") -> None:
        self.messages.append(message)

    def report_error(self, exc: Exception) -> None:
        self.errors.append(exc)


@pytest.fixture
def gmail(monkeypatch):
    fake = FakeGmail()
    monkeypatch.setattr("gmail.client.imaplib.IMAP4_SSL", fake)
    monkeypatch.setenv("GMAIL_ADDRESS", ME)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    return fake


@pytest.fixture
def section(qt_app, db_session, patch_config, gmail):
    page = _Page()
    widget = GmailLabelSection(page, patch_config)
    widget.page_stub = page
    yield widget
    widget.deleteLater()


def _wait(qt_app, task, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while task.running and time.time() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)
    assert not task.running, "background task never completed"
    qt_app.processEvents()


def test_previews_are_disabled_until_labels_are_loaded(section):
    assert not section.classify_scan_button.isEnabled()
    assert not section.harvest_scan_button.isEnabled()
    assert section.sync_scan_button.isEnabled()  # 寫回 Gmail 不需要先選標籤


def test_loading_labels_then_classifying_tags_the_company(qt_app, section, gmail, db_session):
    company = CompanyRepository(db_session).create(
        company_name="測試精密", name_key="測試精密", dedupe_key="n:p", email="sales@precision.com.tw"
    )
    db_session.commit()
    gmail.add(1, "sales@precision.com.tw", ME, labels=("客戶",))

    section.load_button.click()
    _wait(qt_app, section.task)
    assert [section.label_combo.itemText(i) for i in range(section.label_combo.count())] == ["供應商", "客戶"]

    section.label_combo.setCurrentText("客戶")
    section.classify_scan_button.click()
    _wait(qt_app, section.task)
    rows = section.classify_table.model._rows
    assert [(r["company_name"], r[CHECK_KEY]) for r in rows] == [("測試精密", True)]

    section.classify_apply_button.click()
    _wait(qt_app, section.task)
    db_session.refresh(company)
    assert {tag.name for tag in company.tags} == {"客戶"}
    assert section.page_stub.errors == []


def test_sync_preview_then_confirmed_apply_labels_gmail(qt_app, section, gmail, db_session, monkeypatch):
    repo = CompanyRepository(db_session)
    company = repo.create(
        company_name="測試精密", name_key="測試精密", dedupe_key="n:p", email="sales@precision.com.tw"
    )
    repo.set_tags(company.id, ["VIP"])
    db_session.commit()
    gmail.add(1, "sales@precision.com.tw", ME)

    section.sync_scan_button.click()
    _wait(qt_app, section.task)
    assert [r["label"] for r in section.sync_table.model._rows] == ["名單匠/VIP"]
    assert gmail.writes == []
    assert section.sync_apply_button.isEnabled()

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    section.sync_apply_button.click()
    _wait(qt_app, section.task)
    assert ("COPY", "名單匠/VIP", "1") in gmail.writes


def test_a_gmail_error_is_reported_not_raised(qt_app, section, monkeypatch):
    monkeypatch.setattr("core.credentials.get_secret", lambda _name: "")

    section.load_button.click()
    _wait(qt_app, section.task)

    assert section.page_stub.errors
    assert section.load_button.isEnabled()
