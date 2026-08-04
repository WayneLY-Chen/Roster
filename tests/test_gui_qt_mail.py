"""Integration tests for gui_qt/pages/mail.py against a real (test) database.

Exercises the whole path this page relies on: ``gui.controllers_mail.MailController``
and ``gui.controllers.CompanyController`` -> ``gmail.campaign``/``gmail.templates``/
``database.repository`` -> a real SQLAlchemy session, with every DB-backed query
running through ``gui_qt.tasks.BackgroundTask`` exactly like it does in the real
app (see ``gui_qt/pages/mail.py``'s own docstring for why).

This file defines its own ``mail_config``/``db_session`` fixtures (rather than
reusing ``tests/conftest.py``'s plain ``db_session``) because the mail page
needs an isolated ``mailer.templates_dir`` and a fake Gmail app password --
mirroring the same fixtures ``tests/test_gmail_sender.py`` already uses.
"""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import core.config as config_module  # noqa: E402
import database.session as session_module  # noqa: E402
from core.constants import EmailVerdict  # noqa: E402
from database.models import Base  # noqa: E402
from database.repository import CompanyRepository  # noqa: E402
from gmail import templates as templates_module  # noqa: E402
from gui_qt.pages.mail import MailPage, PreviewDialog  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def gmail_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "good-app-password")


@pytest.fixture
def mail_config(tmp_config, tmp_path, gmail_env):
    """``tmp_config`` with a mailer section pointed at an isolated templates dir."""
    templates_dir = tmp_path / "templates" / "mail"
    templates_dir.mkdir(parents=True)
    return tmp_config.model_copy(
        update={
            "mailer": tmp_config.mailer.model_copy(
                update={
                    "templates_dir": str(templates_dir),
                    "dry_run": True,
                    "daily_limit": 100,
                    "delay_seconds": 0.0,
                }
            )
        }
    )


@pytest.fixture
def patch_mail_config(mail_config, monkeypatch: pytest.MonkeyPatch):
    """Route every ``get_config()`` call to ``mail_config`` -- same mechanism as
    ``tests/conftest.py``'s ``patch_config``, just with the mailer overrides above.
    """
    monkeypatch.setattr(config_module, "load_config", lambda path=None: mail_config)
    config_module.reset_config()
    yield mail_config
    config_module.reset_config()


@pytest.fixture
def db_session(patch_mail_config):
    engine = session_module.create_db_engine(
        patch_mail_config, url=patch_mail_config.database.resolved_url
    )
    Base.metadata.create_all(engine)

    session_module._engine = engine
    session_module._session_factory = sessionmaker(
        bind=engine, expire_on_commit=False, future=True
    )

    session = session_module._session_factory()
    try:
        yield session
    finally:
        session.close()
        session_module.reset_engine()


class _FakeStatusBar:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    def start_progress(self) -> None:
        self.started += 1

    def stop_progress(self) -> None:
        self.stopped += 1


class _FakeApp:
    """Just enough of gui_qt.app.MainWindow for MailPage to work with."""

    def __init__(self) -> None:
        self.current_page = MailPage.title
        self.messages: list[tuple[str, str]] = []
        self.status_bar = _FakeStatusBar()
        self.shown_pages: list[str] = []

    def set_status(self, message: str, tone: str = "normal") -> None:
        self.messages.append((message, tone))

    def show_page(self, name: str) -> None:
        self.shown_pages.append(name)


def _wait_for(qt_app, task, timeout: float = 3.0) -> None:
    """Pump the event loop until a BackgroundTask genuinely finishes.

    Mirrors ``tests/test_gui_qt_dashboard.py``'s ``_wait_for_fetch``: the
    query runs on a real QThreadPool thread, so a plain function call would
    not exercise the actual (async) code path this page uses.
    """
    deadline = time.time() + timeout
    while task.running and time.time() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)
    assert not task.running, "background task never completed"
    qt_app.processEvents()  # drain the queued on_done call itself


def _make_company(db_session, **overrides):
    repo = CompanyRepository(db_session)
    fields = dict(
        company_name="測試精密機械股份有限公司",
        name_key="測試精密機械",
        dedupe_key="tax:22099131",
        email="sales@test-precision.example.com",
        email_verdict=EmailVerdict.VALID.value,
        industry="金屬加工",
        source="unit-test",
    )
    fields.update(overrides)
    company = repo.create(**fields)
    db_session.commit()
    return company


def _save_template(config, name: str = "test-template") -> None:
    templates_module.save_template(
        name,
        subject="Hi {company_name}",
        body="您好，{company_name}。",
        config=config,
    )


# ------------------------------------------------------------------- refresh


def test_refresh_loads_templates_and_status_via_background_task(qt_app, db_session, mail_config):
    _save_template(mail_config)
    _make_company(db_session)

    app = _FakeApp()
    page = MailPage(app)
    page.ensure_built()

    page.on_show()  # kicks off the async status/filter fetch
    _wait_for(qt_app, page.status_task)

    assert page.template_combo.count() == 1
    assert page.template_combo.currentText() == "test-template"
    assert page.subject_entry.get() == "Hi {company_name}"

    # gmail_env 設了假的 GMAIL_ADDRESS/GMAIL_APP_PASSWORD 環境變數，
    # get_secret() 讀得到，所以帳號會顯示成「已就緒」。
    assert page.account_card.value_text == "me@example.com"
    assert page.account_card.hint_text == "帳號已就緒"
    assert "封（今日）" in page.daily_card.hint_text
    assert page.daily_limit_spin.value() == 100
    assert page.industry_combo.count() >= 2  # "全部" + 至少一個真實產業


def test_on_show_again_still_refreshes_despite_data_version_skip(
    qt_app, db_session, mail_config
):
    """啟用/演練開關與每日上限的變更不會呼叫 bump_data_version()，所以這頁
    永遠強制重整，不套用 BasePage 的資料版本跳過機制。"""
    _save_template(mail_config)
    app = _FakeApp()
    page = MailPage(app)
    page.ensure_built()
    page.on_show()
    _wait_for(qt_app, page.status_task)

    page.on_show()  # 沒有任何人呼叫 bump_data_version()
    _wait_for(qt_app, page.status_task)
    assert page.status_task.running is False  # 第二次仍然真的查了一次，沒有卡住


# --------------------------------------------------------------- build_plan


def test_build_plan_populates_table_and_enables_buttons(qt_app, db_session, mail_config):
    _save_template(mail_config)
    _make_company(db_session)

    app = _FakeApp()
    page = MailPage(app)
    page.ensure_built()
    page.on_show()
    _wait_for(qt_app, page.status_task)

    page.template_combo.setCurrentText("test-template")
    page._start_build_plan()
    _wait_for(qt_app, page.build_task)

    assert page.plan is not None
    assert page.plan.sendable == 1
    assert page.table.row_count() == 1
    row = page.table.model.row_at(0)
    assert row["status"] == "會寄送"
    assert page.preview_button.isEnabled()
    assert page.dry_run_button.isEnabled()
    # 演練模式為 True，「開始寄送」需要帳號就緒 -- 這個測試環境沒有 keyring，
    # 所以 send_button 仍應維持關閉。
    assert not page.send_button.isEnabled()
    assert app.status_bar.started == app.status_bar.stopped == 1


def test_build_plan_without_template_selected_shows_error(qt_app, db_session, mail_config):
    app = _FakeApp()
    page = MailPage(app)
    page.ensure_built()
    page.template_combo.clear()
    page._start_build_plan()
    assert app.messages[-1][1] == "error"


# ---------------------------------------------------------------- templates


def test_save_template_persists_body_and_subject(qt_app, db_session, mail_config):
    _save_template(mail_config)
    app = _FakeApp()
    page = MailPage(app)
    page.ensure_built()
    page.on_show()
    _wait_for(qt_app, page.status_task)

    page.subject_entry.set("新的主旨")
    page.body_editor.set_body("新的內文")
    page._save_template()

    subject, body = page.controller.load("test-template")
    assert subject == "新的主旨"
    assert body == "新的內文"


def test_new_template_adds_to_dropdown_and_selects_it(qt_app, db_session, mail_config, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    app = _FakeApp()
    page = MailPage(app)
    page.ensure_built()
    page.on_show()
    _wait_for(qt_app, page.status_task)

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("新樣板", True)))
    page._new_template()

    assert "新樣板" in page.controller.templates()
    assert page.template_combo.currentText() == "新樣板"


# ------------------------------------------------------------------ 插入變數


def test_placeholder_dropdown_inserts_token_at_cursor(qt_app, db_session, mail_config):
    app = _FakeApp()
    page = MailPage(app)
    page.ensure_built()
    page.body_editor.set_body("")

    page.placeholder_combo.setCurrentText("{company_name} 公司名稱")
    page._insert_selected_placeholder()

    assert "{company_name}" in page.body_editor.to_body_string()


# -------------------------------------------------------------- 每日寄送上限


def test_save_daily_limit_calls_controller_and_reports_success(
    qt_app, db_session, mail_config, monkeypatch
):
    """The actual persist-and-roll-back-on-invalid-value behaviour of
    ``MailController.set_daily_limit()`` (backed by
    ``core.config.save_user_setting()``) is already covered end to end by
    ``tests/test_mail_page.py``. What this page needs to prove is that
    clicking "儲存上限" calls the controller with the value in the spin box
    and reports success -- not re-derive ``get_config()``'s own merge
    behaviour, which the ``patch_mail_config``/``load_config`` monkeypatch
    used for DB isolation here deliberately bypasses (same as
    ``tests/conftest.py``'s own ``patch_config``).
    """
    app = _FakeApp()
    page = MailPage(app)
    page.ensure_built()
    page.on_show()
    _wait_for(qt_app, page.status_task)

    calls: list[int] = []
    monkeypatch.setattr(page.controller, "set_daily_limit", calls.append)

    page.daily_limit_spin.setValue(250)
    page._save_daily_limit()
    _wait_for(qt_app, page.status_task)

    assert calls == [250]
    assert ("每日寄送上限已更新為 250 封", "success") in app.messages


def test_daily_limit_out_of_range_is_rejected_by_spinbox_before_it_reaches_the_controller(
    qt_app, db_session, mail_config
):
    app = _FakeApp()
    page = MailPage(app)
    page.ensure_built()
    page.daily_limit_spin.setValue(5000)  # 超出 QSpinBox 的 1..2000 範圍
    assert page.daily_limit_spin.value() == 2000


# ------------------------------------------------------------ 演練開關確認


def test_disabling_dry_run_without_confirmation_leaves_setting_untouched(
    qt_app, db_session, mail_config, monkeypatch
):
    app = _FakeApp()
    page = MailPage(app)
    page.ensure_built()
    page.on_show()
    _wait_for(qt_app, page.status_task)

    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
    )
    page.live_check.setChecked(True)

    assert page.controller.mailer_status()["dry_run"] == "是"
    assert page.live_check.isChecked() is False  # 被撥回去了


def test_disabling_dry_run_with_confirmation_calls_controller(
    qt_app, db_session, mail_config, monkeypatch
):
    """See the docstring on ``test_save_daily_limit_calls_controller_and_reports_success``
    for why this asserts against a spy rather than round-tripping through
    ``get_config()``."""
    app = _FakeApp()
    page = MailPage(app)
    page.ensure_built()
    page.on_show()
    _wait_for(qt_app, page.status_task)

    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        page.controller, "set_mailer_option", lambda key, value: calls.append((key, value))
    )

    page.live_check.setChecked(True)
    _wait_for(qt_app, page.status_task)

    assert calls == [("dry_run", False)]
    # 注意：因為 set_mailer_option 被換成一個只記錄呼叫、不真的寫入的假函式，
    # 底下 _refresh_status() 查到的 mailer_status() 仍然是原本沒改過的
    # dry_run=True，所以 live_check 會被 _apply_status() 正確地依照「真實
    # （未被 mock 影響）設定值」撥回去——這正是頁面該有的行為：勾選框永遠
    # 反映查詢回來的真實狀態，不是使用者剛才點擊的樂觀結果。


# ---------------------------------------------------------------------- 預覽


def test_preview_dialog_shows_subject_and_renders_body(qt_app, db_session, mail_config):
    _save_template(mail_config)
    _make_company(db_session)

    app = _FakeApp()
    page = MailPage(app)
    page.ensure_built()
    page.on_show()
    _wait_for(qt_app, page.status_task)
    page.template_combo.setCurrentText("test-template")
    page._start_build_plan()
    _wait_for(qt_app, page.build_task)

    subject, body = page.controller.preview_first(page.plan)
    assert subject == "Hi 測試精密機械股份有限公司"

    dialog = PreviewDialog(page, subject, body, page.controller.config)
    assert dialog.windowTitle() == "預覽第一封"
    assert dialog.body_view.isReadOnly()
    assert "測試精密機械股份有限公司" in dialog.body_view.toPlainText()


def test_preview_without_a_plan_shows_error(qt_app, db_session, mail_config):
    app = _FakeApp()
    page = MailPage(app)
    page.ensure_built()
    page._preview_first()
    assert app.messages[-1] == ("請先產生名單", "error")


# -------------------------------------------------------------------- 寄送


def test_dry_run_send_reports_success(qt_app, db_session, mail_config):
    _save_template(mail_config)
    _make_company(db_session)

    app = _FakeApp()
    page = MailPage(app)
    page.ensure_built()
    page.on_show()
    _wait_for(qt_app, page.status_task)
    page.template_combo.setCurrentText("test-template")
    page._start_build_plan()
    _wait_for(qt_app, page.build_task)

    page._start_send(force_dry_run=True)
    _wait_for(qt_app, page.send_task)
    # _on_send_done() 結尾會再呼叫一次 _refresh_status()（背景查詢），把它也
    # 等完，才不會跟下面的斷言起競態——但斷言本身刻意檢查 app.messages
    # （在 _on_send_done() 當下、_refresh_status() 觸發的非同步查詢回來
    # 「之前」就已經寫入），不是 result_label 的文字（那個之後可能被
    # 「寄件狀態」的非同步查詢結果蓋掉，見 _apply_status() 對演練模式的處理）。
    _wait_for(qt_app, page.status_task)

    assert any("演練完成" in message for message, _tone in app.messages)
    assert not page.cancel_send_button.isEnabled()


def test_send_without_a_plan_shows_error(qt_app, db_session, mail_config):
    app = _FakeApp()
    page = MailPage(app)
    page.ensure_built()
    page._start_send(force_dry_run=True)
    assert app.messages[-1] == ("請先產生一份包含可寄送對象的名單", "error")
