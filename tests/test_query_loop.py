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
        return ["", "01", "02", "03"]

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
