"""Integration tests for gui_qt/pages/settings.py against a real (test) database.

Exercises the whole path this page relies on: ``gui.controllers.SettingsController``
and ``gui.controllers_mail.MailController`` -> ``database``/``core.crypto``/
``core.credentials`` -> a real SQLAlchemy session (via ``tests/conftest.py``'s
``db_session`` fixture), with the page's own read-only refresh running through
``gui_qt.tasks.BackgroundTask`` exactly like it does in the real app (see this
page's module docstring for why).

Nothing here ever touches the user's real ``config.yaml``, ``user_settings.yaml``
or OS credential vault:

    * ``db_session``/``patch_config`` (from ``tests/conftest.py``) route the
      database and every ``config.yaml``-derived path at ``tmp_path``.
    * ``fake_vault`` replaces the OS keyring with an in-memory dict for the
      duration of one test.
    * ``_save_daily_limit`` is tested by monkeypatching
      ``MailController.set_daily_limit`` itself (the same technique
      ``tests/test_gui_qt_mail.py`` uses) rather than calling the real
      ``core.config.save_user_setting()``, which writes to the *real*
      ``user_settings.yaml`` next to the project regardless of ``patch_config``.
"""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from core.errors import CRMError  # noqa: E402
from gui_qt.pages.settings import SettingsPage  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeApp:
    def __init__(self) -> None:
        self.current_page = SettingsPage.title
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


# ------------------------------------------------------------------- 建立元件


def test_build_creates_every_section(qt_app, db_session):
    app = _FakeApp()
    page = SettingsPage(app)
    page.ensure_built()

    assert page.overview_table.row_count() == 0  # 還沒 refresh() 之前是空的
    assert page.backup_table.row_count() == 0
    assert page.appearance_combo.count() == 3
    assert page.daily_limit_entry.get() == ""  # 也還沒 refresh() 過


def test_scroll_area_shows_a_scrollbar_at_the_minimum_supported_window_size(qt_app, db_session):
    from PySide6.QtWidgets import QScrollArea

    app = _FakeApp()
    page = SettingsPage(app)
    page.ensure_built()
    page.resize(940, 600 - 34 - 40)  # 扣掉狀態列/標題列大概高度，模擬視窗最小可用區域
    page.show()
    try:
        qt_app.processEvents()
        scroll = page.findChild(QScrollArea)
        assert scroll is not None
        vbar = scroll.verticalScrollBar()
        assert vbar.maximum() > 0  # 內容比可視區域高，捲軸真的有作用
    finally:
        page.hide()


# ------------------------------------------------------------------- 背景重整


def test_refresh_populates_overview_and_backup_tables(qt_app, db_session, fake_vault):
    app = _FakeApp()
    page = SettingsPage(app)
    page.ensure_built()

    page.controller.create_backup()

    page.on_show()
    _wait_for(qt_app, page._refresh_task)

    assert page.overview_table.row_count() > 0
    assert page.backup_table.row_count() >= 1
    assert page.config_label.text()  # 設定檔路徑那行有內容
    assert page.gmail_status_label.text()
    assert page.encryption_status_label.text()


def test_refresh_skips_when_navigated_away_before_it_lands(qt_app, db_session, fake_vault):
    """跟儀表板同樣的保險：查詢跑的期間使用者已經切走，不該更新看不到的頁面。"""
    app = _FakeApp()
    page = SettingsPage(app)
    page.ensure_built()

    page.refresh()
    app.current_page = "別的頁面"
    _wait_for(qt_app, page._refresh_task)

    assert page.overview_table.row_count() == 0


# ------------------------------------------------------------------- 加密金鑰


def test_export_encryption_key_reports_error_when_no_vault(qt_app, db_session):
    """``tests/__init__.py`` 全域停用了系統憑證保管庫；沒有 fake_vault 時，
    匯出金鑰應該乾淨地報錯，而不是打開對話框或當掉。
    """
    app = _FakeApp()
    page = SettingsPage(app)
    page.ensure_built()

    page._show_encryption_key()  # 內部 export_encryption_key() 應該拋錯後直接 return

    assert app.messages
    kind, tone = app.messages[-1]
    assert tone == "error"


def test_perform_key_restore_round_trip_with_the_same_key_succeeds(
    qt_app, db_session, fake_vault, encryption_on
):
    from core import crypto

    crypto.get_key()  # encryption_on 只保證「可以」加密，金鑰要真的用過才會被建立

    app = _FakeApp()
    page = SettingsPage(app)
    page.ensure_built()

    key = page.controller.export_encryption_key()
    assert key

    ok = page._perform_key_restore(key)

    assert ok is True
    assert any("金鑰已還原" in message for message, _ in app.messages)


def test_perform_key_restore_with_different_key_requires_force_confirmation(
    qt_app, db_session, fake_vault, encryption_on, monkeypatch
):
    import base64
    import secrets

    from core import crypto

    crypto.get_key()  # encryption_on 只保證「可以」加密，金鑰要真的用過才會被建立

    page = SettingsPage(_FakeApp())
    page.ensure_built()

    original_key = page.controller.export_encryption_key()
    different_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    assert different_key != original_key

    # 使用者選「否」：不覆蓋，金鑰維持原樣。
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))
    ok = page._perform_key_restore(different_key)
    assert ok is False
    assert crypto.export_key() == original_key

    # 使用者選「是」：覆蓋成功，之後匯出的金鑰變成新的那把。
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    ok = page._perform_key_restore(different_key)
    assert ok is True
    assert crypto.export_key() == different_key


def test_perform_key_restore_rejects_garbage_input(qt_app, db_session, fake_vault):
    app = _FakeApp()
    page = SettingsPage(app)
    page.ensure_built()

    ok = page._perform_key_restore("這不是一把有效的金鑰")

    assert ok is False
    kind, tone = app.messages[-1]
    assert tone == "error"


def test_dialogs_build_without_crashing(qt_app, db_session, fake_vault, encryption_on, monkeypatch):
    """對話框本身（不是抽出去的邏輯）也要能正常組出來、不當掉——用
    monkeypatch 讓 ``QDialog.exec()`` 立刻回傳，不會真的卡住測試。
    """
    from core import crypto

    crypto.get_key()  # 讓 export_encryption_key() 真的有金鑰可以匯出
    monkeypatch.setattr(QDialog, "exec", lambda self: 0)

    app = _FakeApp()
    page = SettingsPage(app)
    page.ensure_built()

    page._show_encryption_key()
    page._restore_encryption_key()


# --------------------------------------------------------------------- Gmail


def test_save_and_clear_gmail_credentials_round_trip(qt_app, db_session, fake_vault, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))

    app = _FakeApp()
    page = SettingsPage(app)
    page.ensure_built()

    page.gmail_address_entry.set("me@example.com")
    page.gmail_password_entry.set("app-password")
    page._save_gmail_credentials()

    assert page.gmail_address_entry.get() == ""  # 密碼一經處理絕不留在輸入框
    assert page.gmail_password_entry.get() == ""
    assert "系統憑證保管庫" in page.gmail_status_label.text()
    assert "success" in [tone for _, tone in app.messages]

    page._clear_gmail_credentials()

    assert "尚未設定" in page.gmail_status_label.text()
    assert page.controller.credential_status("gmail_address").is_set is False


def test_save_gmail_credentials_with_nothing_entered_is_rejected(qt_app, db_session, fake_vault):
    app = _FakeApp()
    page = SettingsPage(app)
    page.ensure_built()

    page._save_gmail_credentials()

    kind, tone = app.messages[-1]
    assert tone == "error"


# ------------------------------------------------------------------- 郵件寄送


def test_save_daily_limit_calls_mail_controller_and_reports_success(qt_app, db_session, monkeypatch):
    app = _FakeApp()
    page = SettingsPage(app)
    page.ensure_built()

    calls: list[int] = []
    monkeypatch.setattr(page.mail_controller, "set_daily_limit", calls.append)

    page.daily_limit_entry.set("250")
    page._save_daily_limit()

    assert calls == [250]
    assert page.daily_limit_entry.get() == "250"
    assert "success" in [tone for _, tone in app.messages]


def test_save_daily_limit_rejects_non_integer_without_touching_controller(
    qt_app, db_session, monkeypatch
):
    app = _FakeApp()
    page = SettingsPage(app)
    page.ensure_built()

    calls: list[int] = []
    monkeypatch.setattr(page.mail_controller, "set_daily_limit", calls.append)

    page.daily_limit_entry.set("不是數字")
    page._save_daily_limit()

    assert calls == []
    kind, tone = app.messages[-1]
    assert tone == "error"


def test_save_daily_limit_reports_error_from_controller(qt_app, db_session, monkeypatch):
    app = _FakeApp()
    page = SettingsPage(app)
    page.ensure_built()

    def _raise(value: int) -> None:
        raise CRMError("上限必須介於 1 到 2000 之間")

    monkeypatch.setattr(page.mail_controller, "set_daily_limit", _raise)

    page.daily_limit_entry.set("9999")
    page._save_daily_limit()

    kind, tone = app.messages[-1]
    assert tone == "error"
    assert "上限必須介於" in kind


# ---------------------------------------------------------------------- 外觀


def test_changing_appearance_applies_the_theme(qt_app, db_session, monkeypatch):
    import gui_qt.theme as theme

    calls: list[str] = []
    monkeypatch.setattr(theme, "apply_theme", lambda app, mode: calls.append(mode) or mode)

    app = _FakeApp()
    page = SettingsPage(app)
    page.ensure_built()

    page.appearance_combo.setCurrentText("深色")

    assert calls == ["dark"]
    assert "success" not in [t for _, t in app.messages]  # 純粹是 normal 語氣的狀態列訊息


# ---------------------------------------------------------------------- 備份


def test_restore_selected_without_a_selection_is_rejected(qt_app, db_session):
    app = _FakeApp()
    page = SettingsPage(app)
    page.ensure_built()

    page._restore_selected()

    kind, tone = app.messages[-1]
    assert tone == "error"
    assert "請先選擇" in kind


def test_create_backup_refreshes_the_backup_table(qt_app, db_session):
    app = _FakeApp()
    page = SettingsPage(app)
    page.ensure_built()

    page._create_backup()

    assert page.backup_table.row_count() == 1
    assert "success" in [tone for _, tone in app.messages]
