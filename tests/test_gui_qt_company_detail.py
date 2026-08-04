"""Integration tests for gui_qt/company_detail.py (CompanyDetailDialog) against a
real (test) database.

The dialog is built and driven directly (never via ``.exec()``, which would open
a blocking modal event loop) -- exactly like ``tests/test_gui_qt_widgets.py``
drives ``DataTable`` directly without ``.show()``.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from database.repository import CompanyRepository  # noqa: E402
from controllers.core import CompanyController  # noqa: E402
from gui_qt.company_detail import CompanyDetailDialog  # noqa: E402
from gui_qt.pages.base import current_data_version  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def _seed_company(db_session) -> int:
    repo = CompanyRepository(db_session)
    company = repo.create(
        company_name="明細測試公司",
        name_key="明細測試公司",
        dedupe_key="tax:88000001",
        email="detail@example.com",
        phone="02-1111-2222",
        industry="機械設備",
        source="sample",
    )
    db_session.commit()
    return company.id


def test_new_company_dialog_starts_disabled_and_titled(qt_app, db_session):
    controller = CompanyController()
    dialog = CompanyDetailDialog(None, controller, None, on_saved=None)

    assert dialog.windowTitle() == "新增公司"
    assert not dialog.add_contact_button.isEnabled()
    # The dialog is never shown in this test (``.exec()`` would block on a
    # modal event loop), so ``isVisible()`` is always False regardless of our
    # own setVisible() calls -- ``isHidden()`` reflects the explicit
    # show/hide state independently of whether any ancestor was ever shown.
    assert not dialog.hint_label.isHidden()
    dialog.close()


def test_edit_existing_company_loads_its_fields(qt_app, db_session):
    company_id = _seed_company(db_session)
    controller = CompanyController()

    dialog = CompanyDetailDialog(None, controller, company_id, on_saved=None)

    assert dialog.name_entry.get() == "明細測試公司"
    assert dialog.email_entry.get() == "detail@example.com"
    assert dialog.phone_entry.get() == "02-1111-2222"
    assert dialog.industry_entry.get() == "機械設備"
    assert dialog.add_contact_button.isEnabled()
    assert dialog.hint_label.isHidden()
    dialog.close()


def test_save_new_company_bumps_data_version_and_calls_on_saved(qt_app, db_session):
    controller = CompanyController()
    saved_calls: list[int] = []
    dialog = CompanyDetailDialog(None, controller, None, on_saved=lambda: saved_calls.append(1))

    dialog.name_entry.set("新建公司股份有限公司")
    dialog.email_entry.set("new@example.com")
    dialog.tags_entry.set("重要, 潛力客戶")

    version_before = current_data_version()
    dialog._save()

    assert current_data_version() > version_before
    assert saved_calls == [1]
    assert dialog.company_id is not None

    view = controller.get(dialog.company_id)
    assert view is not None
    assert view.company_name == "新建公司股份有限公司"
    assert set(view.tags) == {"重要", "潛力客戶"}
    dialog.close()


def test_save_rejects_a_malformed_follow_up_date(qt_app, db_session):
    controller = CompanyController()
    dialog = CompanyDetailDialog(None, controller, None, on_saved=None)

    dialog.name_entry.set("日期格式錯誤公司")
    dialog.follow_up_entry.set("2026/08/03")  # wrong separator

    version_before = current_data_version()
    dialog._save()

    assert "YYYY-MM-DD" in dialog.error_label.text()
    assert current_data_version() == version_before  # nothing was written
    assert dialog.company_id is None
    dialog.close()


def test_add_and_delete_contact_bumps_data_version(qt_app, db_session, monkeypatch):
    company_id = _seed_company(db_session)
    controller = CompanyController()
    dialog = CompanyDetailDialog(None, controller, company_id, on_saved=None)

    dialog.contact_name_entry.set("聯絡人甲")
    dialog.contact_email_entry.set("contact-a@example.com")
    dialog.contact_primary_check.setChecked(True)

    version_before = current_data_version()
    dialog._add_contact()

    assert current_data_version() > version_before
    assert dialog.contacts_table.row_count() == 1
    assert dialog.contact_name_entry.get() == ""  # form cleared after success

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dialog.contacts_table.view.selectRow(0)
    version_before = current_data_version()
    dialog._delete_contact()

    assert current_data_version() > version_before
    assert dialog.contacts_table.row_count() == 0
    dialog.close()


def test_add_contact_without_a_name_shows_an_error(qt_app, db_session):
    company_id = _seed_company(db_session)
    controller = CompanyController()
    dialog = CompanyDetailDialog(None, controller, company_id, on_saved=None)

    dialog.contact_name_entry.set("")
    version_before = current_data_version()
    dialog._add_contact()

    assert "姓名" in dialog.error_label.text()
    assert current_data_version() == version_before
    assert dialog.contacts_table.row_count() == 0
    dialog.close()


def test_add_activity_bumps_data_version(qt_app, db_session):
    company_id = _seed_company(db_session)
    controller = CompanyController()
    dialog = CompanyDetailDialog(None, controller, company_id, on_saved=None)

    dialog.activity_subject_entry.set("第一次拜訪")
    dialog.activity_body_box.setPlainText("聊得很愉快")

    version_before = current_data_version()
    dialog._add_activity()

    assert current_data_version() > version_before
    assert dialog.activity_table.row_count() == 1
    assert dialog.activity_table.model.row_at(0)["subject"] == "第一次拜訪"
    dialog.close()


def test_attach_and_delete_file(qt_app, db_session, tmp_path, monkeypatch):
    company_id = _seed_company(db_session)
    controller = CompanyController()
    dialog = CompanyDetailDialog(None, controller, company_id, on_saved=None)

    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("hello", encoding="utf-8")

    monkeypatch.setattr(
        "gui_qt.company_detail.QFileDialog.getOpenFileName",
        lambda *a, **k: (str(sample_file), ""),
    )
    version_before = current_data_version()
    dialog._attach_file()

    assert current_data_version() > version_before
    assert dialog.attachments_table.row_count() == 1
    assert dialog.attachments_table.model.row_at(0)["filename"] == "sample.txt"

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dialog.attachments_table.view.selectRow(0)
    version_before = current_data_version()
    dialog._delete_attachment()

    assert current_data_version() > version_before
    assert dialog.attachments_table.row_count() == 0
    dialog.close()


def test_load_reports_error_for_missing_company(qt_app, db_session):
    controller = CompanyController()
    dialog = CompanyDetailDialog(None, controller, 999999, on_saved=None)

    assert "999999" in dialog.error_label.text()
    dialog.close()
