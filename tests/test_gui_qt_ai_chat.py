"""Tests for gui_qt/pages/ai_chat.py 的「找網站」與「抓資料」兩半。

守的是**中間那兩道確認不能消失**：

* 搜到的候選網站要先變成一張看得見的清單，使用者勾完按下去才真的去抓。少了
  它，一個關鍵字會安靜地展開成幾十個網站、幾百次請求。
* 抽出來的資料要先變成一張看得見的預覽表格，勾掉不要的之後才進得了資料庫。
  少了它，一個把整排導覽選單當成公司名稱的模型可以在三秒內灌兩百筆垃圾進名單。

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
from ai.sites import COMPANY, DIRECTORY, UNRELATED, Candidate, SiteSearchResult  # noqa: E402
from controllers.ai import (  # noqa: E402
    BatchExtractResult,
    ExtractCancelled,
    SaveResult,
)
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

# ------------------------------------------------------------ 用關鍵字找網站
#
# 守的是藍圖裡那一條：**AI 不能自己決定就開始大量請求。** 候選清單一定要
# 使用者確認過才動手——所以這一段測的都是「按下去之前什麼都沒發生」。


CANDIDATES = (
    Candidate(
        url="https://directory.test/members",
        title="某某公會 會員名錄",
        kind=DIRECTORY,
        reason="公會的會員名冊，一頁很多家",
    ),
    Candidate(
        url="https://example-cnc.test/",
        title="精展機械股份有限公司",
        kind=COMPANY,
        reason="看起來是單一公司官網",
    ),
    Candidate(
        url="https://news.test/story",
        title="產業新聞",
        kind=UNRELATED,
        reason="新聞報導，沒有名單",
    ),
)


def _sites(*candidates: Candidate, **kwargs) -> SiteSearchResult:
    items = list(candidates) or list(CANDIDATES)
    kwargs.setdefault("found", len(items))
    return SiteSearchResult(query="台中 CNC 加工", candidates=items, **kwargs)


def test_a_blank_keyword_does_not_start_a_search(qt_app, db_session):
    page = _page(qt_app)
    page.query_input.setText("   ")
    page._find()

    assert not page.sites_task.running
    assert page.app.messages[-1][1] == "warning"


def test_only_the_useful_kinds_are_ticked_by_default(qt_app, db_session):
    """預設全勾的話，模型判斷失準的那幾筆會安靜地變成真的請求。

    「不相關」那一筆仍然列出來、仍然勾得動——只是要使用者自己動手。
    """
    page = _page(qt_app)
    page._on_sites_found(_sites())

    assert page.sites_table.row_count() == 3
    ticked = [row["url"] for row in page.sites_table.checked_rows()]
    assert ticked == ["https://directory.test/members", "https://example-cnc.test/"]
    assert page.crawl_button.isEnabled()


def test_the_model_reason_is_shown_next_to_each_site(qt_app, db_session):
    """那是模型的說法，不是查證過的事實——所以它要擺在使用者眼前。"""
    page = _page(qt_app)
    page._on_sites_found(_sites())

    assert page.sites_table.model.row_at(0)["reason"] == "公會的會員名冊，一頁很多家"
    assert page.sites_table.model.row_at(0)["kind"] == "名錄"


def test_finding_sites_does_not_start_crawling_by_itself(qt_app, db_session, monkeypatch):
    """**這一條是藍圖裡不能妥協的那一條。**

    候選清單出來之後，程式停在這裡等使用者。它不會順手把勾起來的網站抓下去。
    """
    started = []
    page = _page(qt_app)
    monkeypatch.setattr(page.crawl_task, "start", lambda *a: started.append(a))

    page._on_sites_found(_sites())

    assert started == []
    assert page.preview.row_count() == 0


def test_only_the_ticked_sites_get_crawled(qt_app, db_session, monkeypatch):
    page = _page(qt_app)
    page._on_sites_found(_sites())
    page.sites_table.model.row_at(1)[CHECK_KEY] = False   # 取消那家單一公司

    started = []
    monkeypatch.setattr(page.crawl_task, "start", lambda urls, model: started.append(urls))
    page._crawl_checked()

    assert started == [["https://directory.test/members"]]


def test_unticking_everything_says_so_instead_of_crawling(qt_app, db_session, monkeypatch):
    page = _page(qt_app)
    page._on_sites_found(_sites())
    page.sites_table.set_all_checked(False)

    started = []
    monkeypatch.setattr(page.crawl_task, "start", lambda *a: started.append(a))
    page._crawl_checked()

    assert started == []
    assert page.app.messages[-1] == ("一個網站都沒有勾", "warning")


def test_cancelling_the_search_says_nothing_was_crawled(qt_app, db_session):
    """使用者要看得出「按取消之後，那些網站一個都沒有被碰到」。"""
    page = _page(qt_app)
    page._on_sites_error(ExtractCancelled("已取消。"))

    assert "沒有抓任何網站" in page.sites_status.text()
    assert page.find_button.isEnabled()
    assert not page.sites_cancel_button.isEnabled()


def test_no_search_results_suggests_what_to_do(qt_app, db_session):
    page = _page(qt_app)
    page._on_sites_found(SiteSearchResult(query="找不到的東西", found=0))

    assert page.sites_table.row_count() == 0
    assert not page.crawl_button.isEnabled()
    assert "搜不到東西" in page.sites_status.text()


# ------------------------------------------------- 一次抓好幾個網站的結果


def test_records_from_several_sites_land_in_one_preview_with_their_source(
    qt_app, db_session
):
    """一次抓好幾個網站時，「這一筆是哪個網站來的」是判斷對錯唯一的線索。"""
    page = _page(qt_app)
    batch = BatchExtractResult(
        results=[
            (
                "https://directory.test/members",
                ExtractResult(records=[
                    RawCompany(
                        company_name="甲公司",
                        source="ai",
                        source_url="https://directory.test/members",
                    )
                ]),
            ),
            (
                "https://example-cnc.test/",
                ExtractResult(records=[
                    RawCompany(
                        company_name="乙公司",
                        source="ai",
                        source_url="https://www.example-cnc.test/about",
                    )
                ]),
            ),
        ]
    )
    page._on_batch_extracted(batch)

    assert page.preview.row_count() == 2
    hosts = [page.preview.model.row_at(i)["_host"] for i in range(2)]
    assert hosts == ["directory.test", "example-cnc.test"]
    assert page.save_button.isEnabled()


def test_a_site_that_could_not_be_fetched_is_named(qt_app, db_session):
    """五個網站裡有一個被擋，使用者需要知道是哪一個。

    只說「抓到 40 筆」的話，他會以為那五個網站都抓過了。
    """
    page = _page(qt_app)
    batch = BatchExtractResult(
        results=[
            (
                "https://ok.test/",
                ExtractResult(records=[RawCompany(company_name="甲公司", source="ai")]),
            )
        ],
        failures=[("https://blocked.test/", "robots.txt disallows …")],
    )
    page._on_batch_extracted(batch)

    assert "https://blocked.test/" in page.dropped_label.text()
    assert page.preview.row_count() == 1
