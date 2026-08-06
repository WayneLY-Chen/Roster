"""要先選條件才有資料的名錄（逐項查詢），與點出來的小視窗。

有一整類名錄不給完整清單，只給一個查詢框：選一個分類、或打一個關鍵字，按下
查詢才會出現結果；點了公司名稱又會跳出一個小視窗，電話與信箱全在裡面。那些
資料在原始 HTML 裡一個字都沒有。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.config import DetailModal, QueryLoop
from crawler.discover import find_query_form
from crawler.parser import make_soup

# ------------------------------------------------------------ 設定模型


def test_a_query_loop_needs_both_selectors():
    """少了任何一個，存下來的來源都只會在爬取時安靜地什麼都不做。"""
    with pytest.raises(ValidationError):
        QueryLoop(input_selector="   ", submit_selector="#go")
    with pytest.raises(ValidationError):
        QueryLoop(input_selector="#q", submit_selector="")


def test_a_detail_modal_needs_both_selectors():
    with pytest.raises(ValidationError):
        DetailModal(click_selector="a", panel_selector="  ")


def test_a_query_loop_has_a_ceiling():
    """一個兩千項的選單可以把一整天用掉，也是對別人網站的基本禮貌。"""
    with pytest.raises(ValidationError):
        QueryLoop(input_selector="#q", submit_selector="#go", max_queries=5000)


# ------------------------------------------------------------ 偵測


_TABBED_QUERY_PAGE = """
<html><body>
  <form id="ctl00">
    <div class="tab-content">
      <div class="tab-pane" id="cname">
        <div class="input-group">
          <input type="text" id="tbCname" class="form-control" />
          <span class="input-group-btn">
            <button class="btn btn-info btnqry" id="btnCname" type="button"></button>
          </span>
        </div>
      </div>
      <div class="tab-pane" id="ptype">
        <div class="input-group">
          <select class="form-control" id="ddlPtype">
            <option value="">--請選擇--</option>
            <option value="01">01 活動物</option>
            <option value="02">02 肉及食用雜碎</option>
            <option value="03">03 魚類</option>
            <option value="04">04 乳製品</option>
            <option value="05">05 未列名動物產品</option>
          </select>
          <span class="input-group-btn">
            <button class="btn btn-info btnqry" type="button"></button>
          </span>
        </div>
      </div>
    </div>
    <div class="modal" id="detlModal">
      <button class="btn btn-primary" id="btnConfirm">確定</button>
    </div>
  </form>
</body></html>
"""


def test_a_dropdown_with_many_options_is_a_query_form():
    form = find_query_form(make_soup(_TABBED_QUERY_PAGE))

    assert form is not None
    assert form["input_selector"] == "#ddlPtype"
    # 空值的「--請選擇--」是提示文字，不是一個查詢條件。
    assert form["option_count"] == 5
    assert form["sample"][0] == "01 活動物"


def test_the_submit_button_selector_matches_exactly_one_button():
    """四個分頁籤各有一顆長得一模一樣的查詢鈕。挑到錯的那一顆，按下去什麼都
    不會發生——而且完全不會報錯，只會看到「查了 97 次，一筆都沒有」。"""
    soup = make_soup(_TABBED_QUERY_PAGE)
    form = find_query_form(soup)

    assert form is not None
    assert len(soup.select(str(form["submit_selector"]))) == 1


def test_a_confirm_button_inside_a_modal_is_not_a_search_button():
    """彈出視窗裡的「確定」長得跟查詢鈕一樣，但按下去只會關掉一個對話框。"""
    soup = make_soup(_TABBED_QUERY_PAGE)
    form = find_query_form(soup)

    assert form is not None
    assert "btnConfirm" not in str(form["submit_selector"])


def test_a_keyword_box_is_reported_alongside_the_dropdown():
    """有些網站的選單要再點好幾層才看得到廠商，關鍵字反而一步到位。"""
    form = find_query_form(make_soup(_TABBED_QUERY_PAGE))

    assert form is not None
    assert form["text_input_selector"] == "#tbCname"


def test_a_short_dropdown_is_not_a_query_form():
    """「每頁顯示 10／25／50 筆」永遠只有三四個選項，那不是查詢條件。"""
    html = """<html><body><div>
      <select id="perPage">
        <option value="10">10</option><option value="25">25</option>
      </select>
      <button>查詢</button>
    </div></body></html>"""

    assert find_query_form(make_soup(html)) is None


def test_a_dropdown_with_no_search_button_is_not_a_query_form():
    html = """<html><body><div>
      <select id="sort">
        <option value="a">甲</option><option value="b">乙</option>
        <option value="c">丙</option><option value="d">丁</option>
        <option value="e">戊</option>
      </select>
    </div></body></html>"""

    assert find_query_form(make_soup(html)) is None


def test_an_ordinary_listing_page_has_no_query_form():
    html = "<html><body><div class='item'><h3>甲有限公司</h3></div></body></html>"
    assert find_query_form(make_soup(html)) is None


# ------------------------------------------------------------ 執行


class _FakeElement:
    def __init__(self, page: "_FakePage", name: str, visible: bool = True) -> None:
        self._page = page
        self._name = name
        self._visible = visible
        self.inner = f"<div>{name} 的詳細</div>"

    def click(self) -> None:
        if not self._visible:
            raise RuntimeError("element is not visible")
        self._page.clicked.append(self._name)

    def dispatch_event(self, _event: str) -> None:
        self._page.dispatched.append(self._name)

    def inner_html(self) -> str:
        return self.inner

    def query_selector(self, _selector: str):
        return self


class _FakePage:
    """只實作逐項查詢真正會用到的那幾個方法。"""

    def __init__(self, rows: int = 0, button_visible: bool = True) -> None:
        self.clicked: list[str] = []
        self.dispatched: list[str] = []
        self.selected: list[str] = []
        self.filled: list[str] = []
        self.escaped = 0
        self._rows = [_FakeElement(self, f"第{i}列") for i in range(rows)]
        self._button = _FakeElement(self, "查詢鈕", visible=button_visible)

    def eval_on_selector(self, _selector: str, _script: str):
        return "select"

    def eval_on_selector_all(self, _selector: str, _script: str):
        # 形狀要跟真的瀏覽器一樣：``[值, 顯示文字]``。只回值的話，這個替身就
        # 不再是它要模擬的那個東西，而「起訖用選項文字指定」那條路完全測不到。
        # 第一個是「--請選擇--」，值是空的。
        return [
            ["", "--請選擇--"],
            ["01", "01 活動物"],
            ["02", "02 肉及食用雜碎"],
            ["03", "03 魚類、甲殼類"],
        ]

    def select_option(self, _selector: str, value: str, force: bool = False) -> None:
        self.selected.append(value)

    def fill(self, _selector: str, value: str, force: bool = False) -> None:
        self.filled.append(value)

    def query_selector(self, selector: str):
        if "modal" in selector.lower() or "panel" in selector.lower():
            return _FakeElement(self, "小視窗")
        return self._button

    def query_selector_all(self, _selector: str):
        return list(self._rows)

    def wait_for_timeout(self, _ms: int) -> None:
        pass

    def close(self) -> None:
        pass

    def goto(self, _url: str, **_kwargs) -> None:
        pass

    def content(self) -> str:
        return "<html></html>"

    @property
    def url(self) -> str:
        return "https://a.test/q"

    @property
    def keyboard(self):
        page = self

        class _Keyboard:
            def press(self, _key: str) -> None:
                page.escaped += 1

        return _Keyboard()


def test_the_real_options_skip_the_please_choose_placeholder():
    from crawler.fetcher import _option_values

    assert _option_values(_FakePage(), "#ddlPtype") == ["01", "02", "03"]


def test_a_hidden_search_button_is_still_pressed():
    """查詢頁常常做成分頁籤，沒被選到的那一頁是隱藏的——裡面的按鈕在畫面上
    不存在，一般的點擊會一直等到逾時。"""
    from crawler.fetcher import _submit_one_query

    page = _FakePage(button_visible=False)
    loop = QueryLoop(input_selector="#ddlPtype", submit_selector="#go")
    _submit_one_query(page, loop, "01")

    assert page.dispatched == ["查詢鈕"]


def test_every_row_gets_its_own_modal_in_the_same_order():
    """資料是靠位置對回去的。點不開的那一筆如果直接跳過不放，後面每一筆的
    聯絡資訊都會錯位到別人家去——而且看起來完全正常。"""
    from crawler.fetcher import _collect_modal_details

    page = _FakePage(rows=4)
    modal = DetailModal(click_selector="a", panel_selector="#detlModal")

    details = _collect_modal_details(page, modal, "tr")

    assert len(details) == 4
    assert all("小視窗" in html for html in details)


def test_a_row_that_will_not_open_leaves_a_blank_not_a_gap():
    from crawler.fetcher import _collect_modal_details

    class _StubbornPage(_FakePage):
        def query_selector_all(self, _selector: str):
            rows = list(self._rows)
            rows[1] = _FakeElement(self, "壞掉的列", visible=False)
            return rows

    page = _StubbornPage(rows=3)
    modal = DetailModal(click_selector="a", panel_selector="#detlModal")

    details = _collect_modal_details(page, modal, "tr")

    assert len(details) == 3


def test_the_modal_is_closed_with_escape_when_no_button_is_given():
    from crawler.fetcher import _collect_modal_details

    page = _FakePage(rows=2)
    modal = DetailModal(click_selector="a", panel_selector="#detlModal")

    _collect_modal_details(page, modal, "tr")

    assert page.escaped == 2


# ------------------------------------------------- 引擎不對時要講出來


def test_a_query_loop_without_a_browser_says_so(tmp_config):
    """httpx 拿到的是伺服器吐出來的原始 HTML，上面沒有查詢框可以填。"""
    from core.config import PaginationRule, SourceConfig
    from core.errors import SourceConfigError
    from crawler.sources.generic_html import GenericHtmlSource

    class _PlainFetcher:
        def fetch(self, url, **_kwargs):
            raise AssertionError("不該走到這裡")

    source = SourceConfig(
        name="q",
        type="generic_html",
        start_url="https://a.test/query",
        list_selector="tr",
        fields={"company_name": {"selector": "td"}},
        pagination=PaginationRule(type="none"),
        query_loop=QueryLoop(input_selector="#q", submit_selector="#go"),
    )

    crawler = GenericHtmlSource(source, fetcher=_PlainFetcher(), config=tmp_config)
    with pytest.raises(SourceConfigError):
        list(crawler.iter_pages())


# ------------------------------------------- 中間還要再點一層（三層網站）


def test_a_result_drill_needs_a_row_selector():
    from core.config import ResultDrill

    with pytest.raises(ValidationError):
        ResultDrill(row_selector="   ")


def test_drilling_clicks_each_row_and_yields_one_page_each():
    """有些網站是三層的：選一個大分類 → 出來一張子分類清單 → 點其中一項才
    看得到廠商。中間那一層看起來很像資料，裡面卻一家公司都沒有。"""
    from core.config import ResultDrill
    from crawler.fetcher import PlaywrightFetcher

    class _Page(_FakePage):
        def __init__(self) -> None:
            super().__init__()
            self.sub_rows = [_FakeElement(self, f"子分類{i}") for i in range(3)]
            self.content_calls = 0

        def query_selector_all(self, selector: str):
            return list(self.sub_rows) if selector == "#tbSub tr" else []

        def content(self) -> str:
            self.content_calls += 1
            return f"<html>第{self.content_calls}次</html>"

        @property
        def url(self) -> str:
            return "https://a.test/q"

    page = _Page()
    fetcher = PlaywrightFetcher.__new__(PlaywrightFetcher)   # 不啟動真的瀏覽器
    fetcher.limiter = _NoWait()
    fetcher.robots = _AllowAll()
    fetcher.user_agent = "test"
    fetcher._settings = _Settings()
    fetcher._context = _Context(page)

    loop = QueryLoop(
        input_selector="#q",
        submit_selector="#go",
        values=["甲"],
        drill=ResultDrill(row_selector="#tbSub tr", click_selector="a", max_rows=10),
    )

    pages = list(fetcher.iter_query_pages("https://a.test/q", loop))

    # 一個查詢條件 × 三列子分類 = 三份結果，不是一份。
    assert len(pages) == 3
    assert page.clicked.count("子分類0") == 1


class _LeavingElement(_FakeElement):
    """點下去就會把畫面帶離查詢結果頁的那種列（子分類）。"""

    def click(self) -> None:
        super().click()
        self._page.leave_results()


def test_drilling_reruns_the_query_before_each_row():
    """點完一列要先把查詢重做一次，才點得到下一列。

    點第一列會把畫面換成那個子分類底下的廠商清單。留在那一頁去點「第 2 列」，
    點到的是**廠商**，開出來的是明細小視窗，不是下一個子分類——結果是第 2 批
    之後每一筆都是同一家公司，而且一個欄位都沒有。實測遇過。
    """
    from core.config import ResultDrill
    from crawler.fetcher import PlaywrightFetcher

    class _Page(_FakePage):
        def __init__(self) -> None:
            super().__init__()
            self.sub_rows = [_LeavingElement(self, f"子分類{i}") for i in range(3)]
            #: 有沒有停在「剛查完」的那一頁。點一列就會離開，重查才回得來。
            self.on_results = False

        def select_option(self, selector, value, force=False):
            super().select_option(selector, value, force)
            self.on_results = True

        def query_selector_all(self, selector: str):
            if selector != "#tbSub tr":
                return []
            # 已經點進去了就看不到子分類清單了。
            return list(self.sub_rows) if self.on_results else []

        def leave_results(self) -> None:
            """點進某一個子分類之後就離開查詢結果頁了。"""
            self.on_results = False

    page = _Page()
    fetcher = PlaywrightFetcher.__new__(PlaywrightFetcher)
    fetcher.limiter = _NoWait()
    fetcher.robots = _AllowAll()
    fetcher.user_agent = "test"
    fetcher._settings = _Settings()
    fetcher._context = _Context(page)

    loop = QueryLoop(
        input_selector="#q",
        submit_selector="#go",
        values=["甲"],
        drill=ResultDrill(row_selector="#tbSub tr", click_selector="a", max_rows=3),
    )

    pages = list(fetcher.iter_query_pages("https://a.test/q", loop))

    # 三列都點得到——沒有重查的話第 2 列開始就找不到子分類清單，只會有 1 份。
    assert len(pages) == 3
    # 一個條件查了 3 次：第一次進來，之後每一列各回去一次。
    assert page.selected == ["甲", "甲", "甲"]


def test_drilling_stops_at_the_row_ceiling():
    from core.config import ResultDrill
    from crawler.fetcher import PlaywrightFetcher

    class _Page(_FakePage):
        def __init__(self) -> None:
            super().__init__()
            self.sub_rows = [_FakeElement(self, f"列{i}") for i in range(50)]

        def query_selector_all(self, selector: str):
            return list(self.sub_rows) if selector == "tr" else []

        def content(self) -> str:
            return "<html></html>"

        @property
        def url(self) -> str:
            return "https://a.test/q"

    page = _Page()
    fetcher = PlaywrightFetcher.__new__(PlaywrightFetcher)
    fetcher.limiter = _NoWait()
    fetcher.robots = _AllowAll()
    fetcher.user_agent = "test"
    fetcher._settings = _Settings()
    fetcher._context = _Context(page)

    loop = QueryLoop(
        input_selector="#q",
        submit_selector="#go",
        values=["甲"],
        drill=ResultDrill(row_selector="tr", max_rows=4),
    )

    assert len(list(fetcher.iter_query_pages("https://a.test/q", loop))) == 4


class _NoWait:
    def wait(self, minimum=None):
        return 0.0


class _AllowAll:
    def can_fetch(self, _url: str) -> bool:
        return True

    def crawl_delay(self, _url: str):
        return None


class _Settings:
    wait_until = "load"
    nav_timeout_ms = 1000


class _Context:
    def __init__(self, page) -> None:
        self._page = page

    def new_page(self):
        return self._page


# ------------------------------------------------------------ 續跑


def _fetcher_with(page):
    from crawler.fetcher import PlaywrightFetcher

    fetcher = PlaywrightFetcher.__new__(PlaywrightFetcher)   # 不啟動真的瀏覽器
    fetcher.limiter = _NoWait()
    fetcher.robots = _AllowAll()
    fetcher.user_agent = "test"
    fetcher._settings = _Settings()
    fetcher._context = _Context(page)
    return fetcher


class _CountingPage(_FakePage):
    def content(self) -> str:
        return "<html></html>"

    @property
    def url(self) -> str:
        return "https://a.test/q"


def test_resuming_skips_the_conditions_already_done():
    page = _CountingPage()
    loop = QueryLoop(
        input_selector="#q",
        submit_selector="#go",
        values=["甲", "乙", "丙", "丁"],
    )

    list(_fetcher_with(page).iter_query_pages("https://a.test/q", loop, skip_values=2))

    # 假的頁面把輸入欄位回報成下拉選單，所以走的是 select_option 那一條。
    assert page.selected == ["丙", "丁"]


#: 假頁面回報的選單內容，跟 ``_FakePage.eval_on_selector_all`` 一致。
#: 「--請選擇--」的值是空的，所以真正查得到的是後面三個。


def test_a_starting_position_skips_the_earlier_conditions():
    """97 個分類跑完要好幾個小時，使用者本來就會分次跑。沒有起點的話，想爬
    第 3 個分類的唯一辦法是從第 1 個重跑。"""
    page = _CountingPage()
    loop = QueryLoop(input_selector="#q", submit_selector="#go", start_at=2)

    list(_fetcher_with(page).iter_query_pages("https://a.test/q", loop))

    assert page.selected == ["02", "03"]


def test_a_range_given_as_numbers_is_honoured():
    page = _CountingPage()
    loop = QueryLoop(
        input_selector="#q", submit_selector="#go", start_at=2, max_queries=1
    )

    list(_fetcher_with(page).iter_query_pages("https://a.test/q", loop))

    assert page.selected == ["02"]


def test_a_range_can_be_given_as_the_text_on_the_options():
    """使用者看著畫面說的是「從 02 肉類 爬到 03」，沒有人會去數那是第幾個。"""
    page = _CountingPage()
    loop = QueryLoop(
        input_selector="#q",
        submit_selector="#go",
        start_value="02 肉",
        end_value="03",
    )

    list(_fetcher_with(page).iter_query_pages("https://a.test/q", loop))

    assert page.selected == ["02", "03"]


def test_matching_the_option_text_also_works_on_the_value():
    """有些選單的顯示文字跟值一樣，有些不一樣。兩邊都比對，使用者打哪一種
    都認得。"""
    page = _CountingPage()
    loop = QueryLoop(input_selector="#q", submit_selector="#go", start_value="03")

    list(_fetcher_with(page).iter_query_pages("https://a.test/q", loop))

    assert page.selected == ["03"]


def test_a_range_that_matches_nothing_falls_back_instead_of_crawling_the_wrong_part():
    """打錯字的代價不該是「安靜地爬了完全不同的一段」——那要跑完才發現。"""
    page = _CountingPage()
    loop = QueryLoop(
        input_selector="#q", submit_selector="#go", start_value="沒有這個分類"
    )

    list(_fetcher_with(page).iter_query_pages("https://a.test/q", loop))

    assert page.selected == ["01", "02", "03"]


def test_a_starting_position_and_resuming_do_not_cancel_each_other_out():
    """起點是使用者指定的，續跑的進度是在那個起點之後往前推的。兩者互相
    蓋掉的話，接續會跳到完全不對的地方。"""
    page = _CountingPage()
    loop = QueryLoop(input_selector="#q", submit_selector="#go", start_at=2)

    list(_fetcher_with(page).iter_query_pages("https://a.test/q", loop, skip_values=1))

    assert page.selected == ["03"]


def test_the_progress_marker_only_moves_when_a_condition_is_finished():
    """一個條件底下還有好幾列要往下點的時候，中途那幾批回報的必須是前一個
    條件——不然接續時會把還沒點完的那一個整個跳過。"""
    from core.config import ResultDrill

    class _Page(_CountingPage):
        def __init__(self) -> None:
            super().__init__()
            self.sub_rows = [_FakeElement(self, f"列{i}") for i in range(2)]

        def query_selector_all(self, selector: str):
            return list(self.sub_rows) if selector == "tr" else []

    loop = QueryLoop(
        input_selector="#q",
        submit_selector="#go",
        values=["甲", "乙"],
        drill=ResultDrill(row_selector="tr", max_rows=5),
    )

    pages = list(_fetcher_with(_Page()).iter_query_pages("https://a.test/q", loop))

    # 甲的兩批都還在 0（甲自己還沒做完），乙的兩批才是 1。
    assert [p.completed_values for p in pages] == [0, 0, 1, 1]


def test_a_condition_that_fails_is_not_retried_forever():
    """查壞的那一個也算走過了，續跑不要每次都卡在它身上。"""

    class _BrokenPage(_CountingPage):
        def select_option(self, _selector: str, value: str, force: bool = False) -> None:
            raise RuntimeError("這一個查不動")

    loop = QueryLoop(
        input_selector="#q", submit_selector="#go", values=["甲", "乙"]
    )

    pages = list(_fetcher_with(_BrokenPage()).iter_query_pages("https://a.test/q", loop))

    assert pages == []
