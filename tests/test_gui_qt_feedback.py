"""反饋頁：兩條寄送路徑，以及提示文字不能騙人。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog  # noqa: E402

import core.config as config_module  # noqa: E402
from gui_qt.app import PAGE_CLASSES  # noqa: E402
from gui_qt.pages.feedback import FeedbackPage  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


class _FakeStatusBar:
    def start_progress(self):
        pass

    def stop_progress(self):
        pass


class _FakeApp:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []
        self.status_bar = _FakeStatusBar()
        self.current_page = "反饋"

    def set_status(self, message, tone="normal"):
        self.messages.append((message, tone))


@pytest.fixture
def feedback_config(tmp_config, monkeypatch):
    config = tmp_config.model_copy(
        update={
            "app": tmp_config.app.model_copy(
                update={"feedback_email": "author@example.com"}
            )
        }
    )
    monkeypatch.setattr(config_module, "load_config", lambda path=None: config)
    config_module.reset_config()
    yield config
    config_module.reset_config()


@pytest.fixture
def database(feedback_config):
    """附件庫的索引存在資料庫，加截圖需要它。"""
    from sqlalchemy.orm import sessionmaker

    import database.session as session_module
    from database.models import Base

    engine = session_module.create_db_engine(
        feedback_config, url=feedback_config.database.resolved_url
    )
    Base.metadata.create_all(engine)
    session_module._engine = engine
    session_module._session_factory = sessionmaker(
        bind=engine, expire_on_commit=False, future=True
    )
    yield
    session_module.reset_engine()


@pytest.fixture
def page(qt_app, feedback_config, database):
    built = FeedbackPage(_FakeApp())
    built.ensure_built()
    return built


def test_feedback_is_reachable_from_the_sidebar():
    """藏在設定頁最下面等於沒有——回報問題不是一種設定。"""
    assert FeedbackPage in PAGE_CLASSES
    assert PAGE_CLASSES[-1] is FeedbackPage       # 排最後，在「設定」之後


def test_the_hint_never_uses_markdown(page):
    """QLabel 預設是純文字，寫 **粗體** 會把星號原封不動印在畫面上。"""
    page.refresh()
    assert "**" not in page.hint_label.text()


def test_without_gmail_the_hint_says_the_screenshot_must_be_attached_by_hand(
    page, monkeypatch
):
    """mailto: 沒有帶附件的標準做法。

    讓使用者以為截圖跟著寄出去了，比一開始就說沒辦法更糟——他會以為作者
    看得到畫面，然後等一個不會來的回覆。
    """
    monkeypatch.setattr("core.feedback.can_send_directly", lambda config=None: False)
    page.refresh()
    text = page.hint_label.text()
    assert "郵件軟體" in text
    assert "截圖" in text


def test_with_gmail_configured_the_hint_says_it_will_send_directly(page, monkeypatch):
    monkeypatch.setattr("core.feedback.can_send_directly", lambda config=None: True)
    page.refresh()
    assert "Gmail" in page.hint_label.text()


def test_sending_clears_the_form(page, monkeypatch):
    """送出後留著舊內容，使用者會不確定到底寄出去了沒。"""
    monkeypatch.setattr("core.feedback.can_send_directly", lambda config=None: True)
    sent: list[object] = []
    monkeypatch.setattr("core.feedback.send", lambda fb, config=None: sent.append(fb))

    page.message_box.setPlainText("匯出的時候會當掉")
    page.reply_entry.set("me@example.com")
    page._send()

    assert len(sent) == 1
    assert sent[0].message == "匯出的時候會當掉"
    assert page.message_box.toPlainText() == ""
    assert page.app.messages[-1][1] == "success"


def test_an_empty_report_is_reported_as_an_error_not_sent(page, monkeypatch):
    monkeypatch.setattr("core.feedback.can_send_directly", lambda config=None: True)
    sent: list[object] = []
    monkeypatch.setattr("core.feedback.send", lambda fb, config=None: sent.append(fb))

    page.message_box.setPlainText("   ")
    page._send()

    assert sent == []
    assert page.app.messages[-1][1] == "error"


def test_attachments_go_through_the_shared_attachment_library(page, monkeypatch, tmp_path):
    """借用郵件的附件庫——複製進 attachments/、驗大小、擋路徑穿越都已經寫好了。"""
    source = tmp_path / "截圖.png"
    source.write_bytes(b"x" * 40)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileNames", staticmethod(lambda *a, **k: ([str(source)], ""))
    )

    page._add_attachment()

    assert page._attachments == ["截圖.png"]
    assert "截圖.png" in page.attachment_label.text()

    page._clear_attachments()
    assert page._attachments == []
