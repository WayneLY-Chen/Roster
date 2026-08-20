"""Tests for gui_qt/pages/ai_chat.py 的「從網址抓資料」那一半。

守的是**中間那一步不能消失**：抽出來的東西要先變成一張看得見的預覽表格，使用
者勾掉不要的之後才進得了資料庫。少了它，一個把整排導覽選單當成公司名稱的模型
可以在三秒內灌兩百筆垃圾進名單。

跟 ``test_gui_qt_import_page.py`` 一樣，資料庫那一段刻意**不**經過真的
``QThreadPool`` 執行緒——理由寫在那個檔案的模組說明裡（Python 3.14 + PySide6
6.11.1 + 執行緒池借來的執行緒 + 真正的 SQLAlchemy flush，整個直譯器會偶爾
access violation）。這裡的分法一樣：Qt 接線用假的 controller 驗，真正的寫入
在 ``tests/test_ai_controller.py`` 裡同步呼叫。
"""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from PySide6.QtWidgets import QApplication  # noqa: E402

from ai.extract import DroppedValue, ExtractResult  # noqa: E402
from controllers.ai import ExtractCancelled, SaveResult  # noqa: E402
from core.errors import RobotsDisallowedError  # noqa: E402
from core.schemas import RawCompany  # noqa: E402
from gui_qt.pages.ai_chat import AIChatPage  # noqa: E402
from gui_qt.widgets import CHECK_KEY  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeStatusBar:
    def start_progress(self) -> None: ...

    def stop_progress(self) -> None: ...


class _FakeApp:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.status_bar = _FakeStatusBar()

    def set_status(self, message: str, tone: str = "normal") -> None:
        self.messages.append((message, tone))

    def refresh_all(self) -> None: ...


def _wait_for(qt_app, task, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    while task.running and time.time() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)
    assert not task.running, "background task never completed"
    qt_app.processEvents()


def _page(qt_app) -> AIChatPage:
    page = AIChatPage(_FakeApp())
    page.ensure_built()
    return page


def _result(*records: RawCompany, **kwargs) -> ExtractResult:
    return ExtractResult(records=list(records), **kwargs)


TWO = (
    RawCompany(company_name="大安精密工業股份有限公司", phone="02-27407278", source="ai"),
    RawCompany(company_name="臺中鑄造有限公司", email="info@taichung.test", source="ai"),
)


# ------------------------------------------------------------------ 初始狀態


def test_nothing_can_be_saved_before_anything_has_been_extracted(qt_app, db_session):
    page = _page(qt_app)

    assert page.preview.row_count() == 0
    assert not page.save_button.isEnabled()
    assert not page.dropped_button.isEnabled()
    assert not page.cancel_button.isEnabled()


def test_a_blank_url_does_not_start_a_fetch(qt_app, db_session):
    page = _page(qt_app)
    page.url_input.setText("   ")
    page._fetch()

    assert not page.extract_task.running
    assert page.app.messages[-1][1] == "warning"


# ------------------------------------------------------------------- 預覽表格


def test_extracted_records_land_in_the_preview_all_ticked(qt_app, db_session):
    """預設全部勾起來：這張表的用途是「把不要的勾掉」。

    預設全不勾的話使用者要按幾十次才回到起點，而那正是他一開始就想要的狀態。
    """
    page = _page(qt_app)
    page._on_extracted(_result(*TWO))

    assert page.preview.row_count() == 2
    assert len(page.preview.checked_rows()) == 2
    assert page.save_button.isEnabled()
    # 表格上看得到的就是即將被存進去的東西。
    assert page.preview.model.row_at(0)["company_name"] == TWO[0].company_name
    assert page.preview.model.row_at(0)["phone"] == "02-27407278"


def test_unticking_a_row_keeps_it_out_of_what_gets_saved(qt_app, db_session, monkeypatch):
    """這一步就是這張表存在的理由。"""
    page = _page(qt_app)
    page._on_extracted(_result(*TWO))

    page.preview.model.row_at(1)[CHECK_KEY] = False

    sent: list[list[RawCompany]] = []
    monkeypatch.setattr(page.save_task, "start", lambda records: sent.append(records))
    page._save_checked()

    assert [record.company_name for record in sent[0]] == [TWO[0].company_name]


def test_untick_everything_and_the_save_button_says_so_instead_of_saving(
    qt_app, db_session, monkeypatch
):
    page = _page(qt_app)
    page._on_extracted(_result(*TWO))
    page.preview.set_all_checked(False)

    started = []
    monkeypatch.setattr(page.save_task, "start", lambda *a: started.append(a))
    page._save_checked()

    assert started == []
    assert page.app.messages[-1] == ("一筆都沒有勾", "warning")


def test_a_new_fetch_clears_the_previous_preview(qt_app, db_session, monkeypatch):
    """上一頁的資料留在表格上、而新的一頁抓失敗，使用者會把舊的存進去。"""
    page = _page(qt_app)
    page._on_extracted(_result(*TWO))

    monkeypatch.setattr(page.extract_task, "start", lambda *a: None)
    page.url_input.setText("https://example.test/other")
    page._fetch()

    assert page.preview.row_count() == 0
    assert not page.save_button.isEnabled()


# ------------------------------------------------------ 誠實地講出丟掉了什麼


def test_dropped_values_are_shown_not_swallowed(qt_app, db_session):
    """那份清單就是「這個模型在這個網站上可不可信」的證據。

    只把它記進日誌等於沒有講：使用者不會去看日誌，他看的是這一頁。
    """
    page = _page(qt_app)
    page._on_extracted(
        _result(
            TWO[0],
            dropped=[
                DroppedValue("大安精密工業股份有限公司", "email", "編的@example.test"),
                DroppedValue("", "company_name", "不存在公司", whole_record=True),
            ],
            returned=2,
        )
    )

    assert page.dropped_label.isVisible() or page.dropped_label.text()
    text = page.dropped_label.text()
    assert "1 個值在原始頁面上找不到" in text
    assert "整筆丟棄" in text
    assert page.dropped_button.isEnabled()


def test_a_truncated_page_says_so(qt_app, db_session):
    page = _page(qt_app)
    page._on_extracted(
        _result(TWO[0], page_chars=40_000, sent_chars=24_000, truncated=True)
    )

    assert "只送了前" in page.dropped_label.text()


def test_nothing_found_is_reported_as_nothing_found(qt_app, db_session):
    """頁面上沒有公司時不會硬湊，而且要說得出可能的原因。"""
    page = _page(qt_app)
    page._on_extracted(_result())

    assert page.preview.row_count() == 0
    assert not page.save_button.isEnabled()
    assert "沒有抓到任何公司" in page.extract_status.text()


# ---------------------------------------------------------------- 失敗與取消


def test_robots_disallowed_explains_why_and_does_not_look_like_a_crash(qt_app, db_session):
    """使用者要看得出「不是壞了，是那個網站說不要」。"""
    page = _page(qt_app)
    page._on_extract_error(RobotsDisallowedError("https://blocked.test/x", "Roster/1.0"))

    message = page.extract_status.text()
    assert "robots.txt" in message
    assert "https://blocked.test/x" in message
    assert page.fetch_button.isEnabled()
    assert not page.cancel_button.isEnabled()


def test_cancelling_is_not_reported_as_an_error(qt_app, db_session):
    """他自己按的。跳錯誤視窗會讓人以為按取消把東西弄壞了。"""
    page = _page(qt_app)
    page._on_extract_error(ExtractCancelled("已取消。"))

    assert "已取消" in page.extract_status.text()
    assert page.app.messages[-1] == ("已取消", "warning")


# ------------------------------------------------------------------ Qt 接線


def test_the_whole_extract_wiring_runs_off_the_ui_thread(qt_app, db_session, monkeypatch):
    """進度回報 → 完成 → 填表格，全部要真的走一次 BackgroundTask。

    這裡把 controller 換掉，所以整趟不連網、也不碰資料庫——驗的是接線本身。
    """
    seen: dict[str, object] = {}

    def fake_extract(url, *, model=None, report=None, cancel_event=None):
        seen["url"] = url
        seen["model"] = model
        report("模型正在回覆…")
        return _result(*TWO)

    page = _page(qt_app)
    monkeypatch.setattr(page.controller, "extract_url", fake_extract)

    page.url_input.setText("https://example.test/members")
    page.model_combo.setCurrentText("some-model")
    page._fetch()

    assert not page.fetch_button.isEnabled()   # 跑的時候不能再按一次
    assert page.cancel_button.isEnabled()

    _wait_for(qt_app, page.extract_task)

    assert seen["url"] == "https://example.test/members"
    assert seen["model"] == "some-model"
    assert page.preview.row_count() == 2
    assert page.fetch_button.isEnabled()
    assert not page.cancel_button.isEnabled()


def test_saving_bumps_the_data_version_so_the_companies_page_sees_it(qt_app, db_session):
    """存完了但「公司資訊」頁沒有變，使用者會以為沒有存進去。"""
    from gui_qt.pages.base import current_data_version

    page = _page(qt_app)
    before = current_data_version()
    page._on_saved(SaveResult(new=2))

    assert current_data_version() != before
    assert "新增 2 筆" in page.extract_status.text()
