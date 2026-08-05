"""點了才看得到的資料（``PageAction`` 與自動偵測）。

有一類名錄把電話與信箱藏在「顯示電話」按鈕後面，或是只先載入前 20 筆、要按
「載入更多」才出現其餘的。那些資料在原始 HTML 裡根本不存在——不做這個動作就
永遠抓不到。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.config import PageAction
from core.errors import CrawlError
from crawler.discover import discover_from_html, find_page_actions
from crawler.fetcher import _run_page_actions
from crawler.parser import make_soup


# ------------------------------------------------------------ 設定模型


def test_a_click_without_a_selector_is_rejected():
    """點什麼？沒有選擇器的點擊沒有意義，存下來只會在爬取時安靜失敗。"""
    with pytest.raises(ValidationError):
        PageAction(type="click")
    with pytest.raises(ValidationError):
        PageAction(type="click_all", selector="   ")


def test_scroll_and_wait_need_no_selector():
    assert PageAction(type="scroll", times=3).selector is None
    assert PageAction(type="wait", wait_ms=1000).selector is None


# ------------------------------------------------------------ 動作執行


class _FakeElement:
    def __init__(self, page: "_FakePage", name: str, breaks: bool = False) -> None:
        self._page = page
        self._name = name
        self._breaks = breaks

    def click(self) -> None:
        if self._breaks:
            raise RuntimeError("這個元素被蓋住了")
        self._page.clicked.append(self._name)


class _FakePage:
    """只實作 :func:`_run_page_actions` 真正會用到的那幾個方法。"""

    def __init__(self, elements: dict[str, list[_FakeElement]] | None = None) -> None:
        self.clicked: list[str] = []
        self.waited: list[int] = []
        self.scrolled = 0
        self._elements = elements or {}

    def query_selector(self, selector: str):
        found = self._elements.get(selector) or []
        return found[0] if found else None

    def query_selector_all(self, selector: str):
        return list(self._elements.get(selector) or [])

    def wait_for_timeout(self, ms: int) -> None:
        self.waited.append(ms)

    @property
    def mouse(self):
        page = self

        class _Mouse:
            def wheel(self, _dx: int, _dy: int) -> None:
                page.scrolled += 1

        return _Mouse()


def _page_with(selector: str, count: int, breaks_at: int | None = None) -> _FakePage:
    elements = [
        _FakeElement(None, f"{selector}#{i}", breaks=(i == breaks_at))
        for i in range(count)
    ]
    page = _FakePage({selector: elements})
    for element in elements:
        element._page = page
    return page


def test_click_all_presses_every_matching_button():
    """每一列各有一顆「顯示電話」——只按第一顆等於只拿到第一家的電話。"""
    page = _page_with("button.phone", 5)
    _run_page_actions(page, [PageAction(type="click_all", selector="button.phone")])

    assert len(page.clicked) == 5


def test_one_stuck_button_does_not_stop_the_others():
    """一顆按不動（被蓋住、已經展開）不該讓其餘的都不按。"""
    page = _page_with("button.phone", 5, breaks_at=2)
    _run_page_actions(page, [PageAction(type="click_all", selector="button.phone")])

    assert len(page.clicked) == 4


def test_click_all_has_a_ceiling():
    """一頁有上千個相符元素多半是選擇器寫錯了。"""
    from crawler.fetcher import MAX_CLICKS_PER_ACTION

    page = _page_with("a", MAX_CLICKS_PER_ACTION + 50)
    _run_page_actions(page, [PageAction(type="click_all", selector="a")])

    assert len(page.clicked) == MAX_CLICKS_PER_ACTION


def test_click_repeats_until_the_button_disappears():
    """「載入更多」按完就不見了，那是做完了，不是失敗。"""

    class _VanishingPage(_FakePage):
        def __init__(self) -> None:
            super().__init__()
            self.left = 3

        def query_selector(self, selector: str):
            if self.left <= 0:
                return None
            self.left -= 1
            element = _FakeElement(self, selector)
            return element

    page = _VanishingPage()
    _run_page_actions(page, [PageAction(type="click", selector="button.more", times=10)])

    assert len(page.clicked) == 3


def test_scroll_repeats_the_requested_number_of_times():
    page = _FakePage()
    _run_page_actions(page, [PageAction(type="scroll", times=4)])

    assert page.scrolled == 4


def test_a_missing_optional_button_is_not_an_error():
    """「同意 cookie」那顆按鈕第二頁就不會再出現，那是正常的。"""
    page = _FakePage()
    _run_page_actions(page, [PageAction(type="click", selector="button.cookie")])

    assert page.clicked == []


def test_a_required_action_that_fails_stops_the_crawl():
    """標成必要，代表「沒做到這件事，這一頁的資料就是不完整的」。"""

    class _BrokenPage(_FakePage):
        def query_selector_all(self, selector: str):
            raise RuntimeError("選擇器無效")

    with pytest.raises(CrawlError):
        _run_page_actions(
            _BrokenPage(),
            [PageAction(type="click_all", selector="button.x", required=True)],
        )


# ------------------------------------------------------------ 自動偵測


def _listing(button_text: str) -> str:
    cards = "".join(
        f"""<div class="item">
              <h3 class="n">第{i}有限公司</h3>
              <span class="addr">台北市中山區中山北路二段45號{i}樓</span>
              <button class="reveal">{button_text}</button>
            </div>"""
        for i in range(6)
    )
    return f"<html><body><div class='list'>{cards}</div></body></html>"


def test_a_show_phone_button_on_every_row_suggests_clicking_them_all():
    soup = make_soup(_listing("顯示電話"))
    items = soup.select("div.item")

    actions = find_page_actions(soup, items)

    assert {"type": "click_all", "selector": "button.reveal"} in actions


def test_a_load_more_button_suggests_repeated_clicks():
    html = "<html><body><button class='more'>載入更多</button></body></html>"
    soup = make_soup(html)

    actions = find_page_actions(soup, [])

    assert actions == [{"type": "click", "selector": "button.more", "times": 10}]


def test_an_ordinary_page_suggests_nothing():
    """沒有這種按鈕就不要建議——給一個按了沒作用的選項比不給更糟。"""
    html = "<html><body><div class='item'><h3>甲有限公司</h3></div></body></html>"
    soup = make_soup(html)

    assert find_page_actions(soup, soup.select("div.item")) == []


def test_a_button_on_only_one_row_is_not_a_per_row_button():
    """六列裡只有一列有，那不是「每一列都有一顆」，是別的東西。"""
    cards = "".join(f"<div class='item'><h3>第{i}有限公司</h3></div>" for i in range(6))
    html = (
        "<html><body><div class='list'>"
        + cards
        + "<div class='item'><h3>第七有限公司</h3>"
        "<button class='reveal'>顯示電話</button></div></div></body></html>"
    )
    soup = make_soup(html)

    assert find_page_actions(soup, soup.select("div.item")) == []


def test_the_analysis_tells_the_user_the_data_is_behind_a_button():
    """使用者要知道「這一頁抓不到電話」不是程式壞了，是資料還沒出現。"""
    result = discover_from_html(_listing("顯示電話"), "https://a.test/list")

    assert result.suggested_actions
    assert any("按下去才會出現" in note for note in result.notes)


# ------------------------------------------------- 引擎不對時要講出來


def test_using_httpx_with_page_actions_warns_instead_of_failing_silently(
    tmp_config, caplog
):
    """httpx 拿到的是伺服器吐出來的原始 HTML，上面沒有任何東西可以按。
    安靜地忽略設定，使用者只會看到「怎麼還是抓不到電話」。"""
    import logging

    from core.config import PaginationRule, SourceConfig
    from crawler.sources.generic_html import GenericHtmlSource

    source = SourceConfig(
        name="js",
        type="generic_html",
        start_url="https://a.test/list",
        list_selector="div.item",
        fields={"company_name": {"selector": "h3"}},
        pagination=PaginationRule(type="none"),
        page_actions=[PageAction(type="click_all", selector="button.reveal")],
    )

    messages: list[str] = []
    from core.logging_setup import get_logger
    from core.constants import LogCategory

    handler_id = None
    try:
        from loguru import logger

        handler_id = logger.add(lambda message: messages.append(str(message)), level="WARNING")
        GenericHtmlSource(source, fetcher=None, config=tmp_config)
    finally:
        if handler_id is not None:
            from loguru import logger

            logger.remove(handler_id)

    assert any("playwright" in message for message in messages)
    assert logging and get_logger and LogCategory      # 保持匯入被使用


# --------------------------------------------- 這一頁有哪些檔案可以讀


def test_the_analysis_reports_which_file_types_the_page_links_to():
    """介面靠這個決定哪些格式勾得動。一個頁面上根本沒有 PDF，卻讓使用者勾
    「讀 PDF」，勾了不會發生任何事——那比沒有那個選項更讓人困惑。"""
    from crawler.discover import find_document_links

    html = """<html><body>
      <a href="/會員名冊.pdf">名冊</a>
      <a href="/附件.PDF">附件</a>
      <a href="/統計.xlsx">統計</a>
      <a href="/簡報.pptx">簡報</a>
      <a href="/about.html">關於</a>
      <a href="mailto:a@b.test">寄信</a>
    </body></html>"""

    found = find_document_links(make_soup(html), "https://a.test/list")

    assert found == {"pdf": 2, "excel": 1, "powerpoint": 1}


def test_the_same_file_linked_twice_counts_once():
    from crawler.discover import find_document_links

    html = """<html><body>
      <a href="/名冊.pdf">下載</a><a href="/名冊.pdf">再下載一次</a>
    </body></html>"""

    assert find_document_links(make_soup(html), "https://a.test/") == {"pdf": 1}


def test_a_page_with_no_files_reports_nothing():
    from crawler.discover import find_document_links

    html = "<html><body><a href='/about'>關於</a></body></html>"
    assert find_document_links(make_soup(html), "https://a.test/") == {}
