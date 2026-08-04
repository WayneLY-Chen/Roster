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
from gui_qt.pages.settings import (  # noqa: E402
    SCHEDULE_ACTIONS,
    SCHEDULE_MODES,
    SettingsPage,
)


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


# --------------------------------------------------------------------- 排程


def _schedule_page(saved: list[dict] | None = None):
    """建好的設定頁，並把儲存動作換成只記錄呼叫的假函式。"""
    page = SettingsPage(_FakeApp())
    page.ensure_built()
    if saved is not None:
        page.controller.save_scheduler_settings = saved.append  # type: ignore[method-assign]
    return page


def test_schedule_form_only_shows_the_fields_that_matter(qt_app, db_session):
    """一次攤開十幾個欄位，使用者得自己判斷哪些跟目前的選擇有關。"""
    page = _schedule_page()
    page.schedule_enabled_check.setChecked(True)

    page.schedule_action_combo.setCurrentText(SCHEDULE_ACTIONS["crawl"])
    page.schedule_mode_combo.setCurrentText(SCHEDULE_MODES["daily"])
    assert page.schedule_sources_list.isVisible() or not page.isVisible()
    assert page.schedule_template_combo.isVisibleTo(page) is False
    assert page.schedule_day_spin.isVisibleTo(page) is False
    assert page.schedule_at_entry.isVisibleTo(page) is True

    page.schedule_mode_combo.setCurrentText(SCHEDULE_MODES["monthly"])
    assert page.schedule_day_spin.isVisibleTo(page) is True

    page.schedule_mode_combo.setCurrentText(SCHEDULE_MODES["interval"])
    assert page.schedule_every_spin.isVisibleTo(page) is True
    assert page.schedule_at_entry.isVisibleTo(page) is False

    page.schedule_action_combo.setCurrentText(SCHEDULE_ACTIONS["send"])
    assert page.schedule_template_combo.isVisibleTo(page) is True
    assert page.schedule_sources_list.isVisibleTo(page) is False


def test_saving_a_schedule_sends_every_field_in_one_call(qt_app, db_session):
    """一次存整組，不是一個欄位存一次。

    逐一儲存會失敗：action 設成寄信時 mail_template 就變成必填，先寫哪一個
    都會在中途產生不合法的設定而被回滾，使用者永遠到不了目標狀態。
    """
    saved: list[dict] = []
    page = _schedule_page(saved)

    page.schedule_enabled_check.setChecked(True)
    page.schedule_action_combo.setCurrentText(SCHEDULE_ACTIONS["crawl_and_send"])
    page.schedule_mode_combo.setCurrentText(SCHEDULE_MODES["monthly"])
    page.schedule_day_spin.setValue(15)
    page.schedule_at_entry.setText("07:45")
    page.schedule_batch_spin.setValue(30)
    page.schedule_campaign_entry.set("每月名單")

    page._save_scheduler()

    assert len(saved) == 1
    values = saved[0]
    assert values["enabled"] is True
    assert values["action"] == "crawl_and_send"
    assert values["mode"] == "monthly"
    assert values["day_of_month"] == 15
    assert values["at"] == "07:45"
    assert values["mail_batch_limit"] == 30
    assert values["mail_campaign"] == "每月名單"


def test_blank_time_falls_back_to_a_valid_default(qt_app, db_session):
    """空字串會讓設定驗證失敗，不能就這樣送出去。"""
    saved: list[dict] = []
    page = _schedule_page(saved)
    page.schedule_at_entry.setText("   ")

    page._save_scheduler()

    assert saved[0]["at"] == "03:00"


def test_a_template_that_no_longer_exists_is_not_saved(qt_app, db_session):
    """下拉選單在沒有樣板時顯示「（尚未建立任何樣板）」，那不是一個樣板名稱。"""
    saved: list[dict] = []
    page = _schedule_page(saved)
    page.schedule_template_combo.clear()
    page.schedule_template_combo.addItem("（尚未建立任何樣板）")

    page._save_scheduler()

    assert saved[0]["mail_template"] == ""


def test_load_scheduler_fills_the_form_from_the_config(qt_app, db_session, monkeypatch):
    page = SettingsPage(_FakeApp())
    page.ensure_built()

    monkeypatch.setattr(
        page.controller,
        "scheduler_settings",
        lambda: {
            "enabled": True, "action": "send", "mode": "monthly", "at": "23:15",
            "every_minutes": 360, "day_of_month": 28, "sources": [],
            "verify_after_crawl": True, "catch_up": False, "mail_template": "",
            "mail_campaign": "季報", "mail_attachments": [], "mail_batch_limit": 77,
            "mail_industry": "", "mail_stage": "", "mail_tag": "",
            "mail_verified_only": False,
        },
    )
    page._load_scheduler()

    assert page.schedule_enabled_check.isChecked() is True
    assert page.schedule_action_combo.currentText() == SCHEDULE_ACTIONS["send"]
    assert page.schedule_mode_combo.currentText() == SCHEDULE_MODES["monthly"]
    assert page.schedule_at_entry.text() == "23:15"
    assert page.schedule_day_spin.value() == 28
    assert page.schedule_batch_spin.value() == 77
    assert page.schedule_campaign_entry.get() == "季報"
    assert page.schedule_catchup_check.isChecked() is False


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
