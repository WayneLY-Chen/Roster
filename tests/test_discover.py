"""Tests for the automatic discovery engine (``crawler.discover``).

Uses :func:`discover_from_html` throughout so nothing here touches the
network -- the offline directory fixture in ``templates/`` stands in for a
real page.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.config import SourceConfig
from crawler.discover import discover_from_html

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "templates" / "sample_directory_page1.html"
FIXTURE_URL = "https://example.test/directory/page1.html"


@pytest.fixture
def fixture_html() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


def test_discover_finds_the_repeated_company_card(fixture_html):
    result = discover_from_html(fixture_html, FIXTURE_URL)
    assert result.list_selector == "div.company-card"
    assert result.item_count == 5


def test_discover_detects_core_contact_fields(fixture_html):
    result = discover_from_html(fixture_html, FIXTURE_URL)
    assert "company_name" in result.fields
    assert "email" in result.fields
    assert "phone" in result.fields

    assert result.fields["company_name"].hit_rate == pytest.approx(1.0)
    # Two of the five records are missing an email or omit a phone value in a
    # format the guesser cannot parse, so these are found often but not always.
    assert result.fields["email"].hit_rate >= 0.35
    assert result.fields["phone"].hit_rate >= 0.35


def test_discover_preview_has_correct_company_names(fixture_html):
    result = discover_from_html(fixture_html, FIXTURE_URL)
    names = [record.company_name for record in result.preview]
    assert names == [
        "宏達精密機械股份有限公司",
        "日新電子（股）公司",
        "全泰化工有限公司",
        "綠光紡織企業社",
        "海通物流股份有限公司",
    ]


def test_discover_finds_the_next_page_link(fixture_html):
    result = discover_from_html(fixture_html, FIXTURE_URL)
    assert result.next_selector is not None
    assert result.ok is True


def test_discover_reports_failure_when_nothing_repeats():
    html = "<html><body><p>Just one paragraph, nothing repeated here.</p></body></html>"
    result = discover_from_html(html, "https://example.test/none")

    assert result.ok is False
    assert result.list_selector == ""
    assert result.notes  # explains why, in Traditional Chinese, for the GUI


# --------------------------------------------------- 公司名稱不能抓到樣板文字


def _directory_with_boilerplate_links() -> str:
    """每張卡片都有「詳細資料」「加入最愛」——真實名錄幾乎都長這樣。

    這正是舊版會誤抓的形狀：那兩個連結在每一列都出現，命中率是滿分的
    100%，比公司名稱還「可靠」，於是舊的計分方式會選中它們。
    """
    cards = "".join(
        f"""
        <div class="item">
          <a class="name" href="/c/{index}">{name}</a>
          <span class="tel">02-2345-67{index:02d}</span>
          <a class="detail" href="/c/{index}">詳細資料</a>
          <a class="fav" href="#">加入最愛</a>
        </div>
        """
        for index, name in enumerate(
            [
                "宏遠精密工業股份有限公司",
                "台興electronics有限公司",
                "南方化工企業社",
                "東立紡織股份有限公司",
                "北大物流有限公司",
            ],
            start=1,
        )
    )
    return f"<html><body><div class='list'>{cards}</div></body></html>"


def test_company_name_is_not_a_repeated_boilerplate_link():
    """每一列都相同的文字不可能是公司名稱，不管它命中率多高。"""
    result = discover_from_html(
        _directory_with_boilerplate_links(), "https://example.test/dir"
    )

    assert "company_name" in result.fields
    names = [record.company_name for record in result.preview]

    assert "詳細資料" not in names
    assert "加入最愛" not in names
    assert names[0] == "宏遠精密工業股份有限公司"
    # 每一筆都必須是不同的公司，而不是同一段樣板文字重複五次。
    assert len(set(names)) == len(names)


def test_company_name_guess_prefers_the_column_that_reads_like_names():
    """相異率相同時，帶「有限公司」這類組織型態的欄位才是名稱。"""
    cards = "".join(
        f"""
        <div class="item">
          <span class="code">A-{index:04d}</span>
          <span class="title">{name}</span>
        </div>
        """
        for index, name in enumerate(
            ["元大機械有限公司", "正新輪胎股份有限公司", "大同電子企業社",
             "中華食品有限公司", "永豐紙業股份有限公司"],
            start=1,
        )
    )
    html = f"<html><body><div class='list'>{cards}</div></body></html>"

    result = discover_from_html(html, "https://example.test/dir")
    names = [record.company_name for record in result.preview]

    # 流水編號每一列也都不同，相異率一樣是 1.0，只有內容看得出差別。
    assert not any(name.startswith("A-") for name in names)
    assert names[0] == "元大機械有限公司"


def test_small_business_names_without_the_word_company_are_recognised():
    """台灣中小企業常常沒有「公司」二字，只以單一個字結尾。

    這不是假想的情境：實際資料庫的 215 筆裡有 8 筆長這樣，只認
    「有限公司」會讓整個小型商家名錄的評分被低估。
    """
    from crawler.discover import _has_company_marker

    real_names = [
        "祥發包裝材料行", "新發鐵捲門行", "金龍電工機械廠",
        "慶建汽車冷氣材料行", "豫味開封包子店", "香霖農產行",
        "艋舺蒸餾水行", "力業鐵工廠",
    ]
    for name in real_names:
        assert _has_company_marker(name), f"{name} 應該要被認出是公司名稱"

    # 放寬字尾規則不能讓導覽文字跟著過關。
    for noise in ["更多", "本店", "回上頁", "詳細資料"]:
        assert not _has_company_marker(noise), f"{noise} 不該被當成公司名稱"


def test_no_company_name_is_better_than_a_wrong_one():
    """名稱那一欄全是樣板文字時要老實說找不到，不要硬塞。

    卡片本身有電話與地址，所以清單會被正確辨識出來——缺的只有名稱，
    這才是「該回報找不到」而不是「整頁都認不得」的情境。
    """
    cards = "".join(
        f"""
        <div class="item">
          <span class="tel">02-2345-67{index:02d}</span>
          <span class="addr">台北市中正區忠孝東路一段{index}號</span>
          <a class="detail" href="/x">詳細資料</a>
          <a class="fav" href="#">加入最愛</a>
        </div>
        """
        for index in range(1, 6)
    )
    html = f"<html><body><div class='list'>{cards}</div></body></html>"

    result = discover_from_html(html, "https://example.test/dir")

    assert "company_name" not in result.fields
    assert result.ok is False
    assert any("公司名稱" in note for note in result.notes), result.notes


# ------------------------------------------------------------------ 分頁偵測


def _numbered_pagination_page(page_count: int = 24, current: int = 1) -> str:
    """一排「1 2 3 4 5」的數字分頁，沒有任何連結寫著「下一頁」。

    台灣的名錄網站絕大多數長這樣。實測 tpchem.net.tw 的會員名錄就是——
    24 頁、每頁 10 筆。
    """
    cards = "".join(
        f"<div class='item'><h3>公司{n}股份有限公司</h3>"
        f"<span class='tel'>02-2345-67{n:02d}</span></div>"
        for n in range(1, 11)
    )
    links = "".join(
        f"<a href='/directory.php?page={n}'>{n}</a>"
        for n in range(1, page_count + 1)
        if n != current
    )
    return f"<html><body><div class='list'>{cards}</div><nav>{links}</nav></body></html>"


def test_numbered_pagination_is_detected_without_any_next_text():
    """只認「下一頁」文字的話，這種頁面會被存成「只爬第一頁」。

    而且使用者不會知道——他只會看到抓回來的筆數比預期少很多。
    """
    from crawler.discover import find_next_selector, find_query_pagination
    from crawler.parser import make_soup

    soup = make_soup(_numbered_pagination_page())
    url = "http://example.test/directory.php"

    assert find_next_selector(soup) is None, "這個頁面本來就沒有『下一頁』文字"

    template, page_count = find_query_pagination(soup, url)
    assert template == "http://example.test/directory.php?page={page}"
    assert page_count == 24


def test_the_page_count_is_reported_so_the_user_can_set_the_limit():
    """頁數上限原本固定預設 3。一個 24 頁的名錄就這樣只被爬了 3 頁，
    看起來像是程式不會自動翻頁——而使用者沒有辦法知道該調到多少。"""
    result = discover_from_html(
        _numbered_pagination_page(), "http://example.test/directory.php"
    )

    assert result.page_count == 24
    assert result.page_url_template == "http://example.test/directory.php?page={page}"
    assert any("24 頁" in note for note in result.notes), result.notes


def test_a_single_page_directory_still_says_so():
    result = discover_from_html(fixture_html_single_page(), "http://example.test/one")
    assert result.page_count in (0, 1)
    assert any("只爬取這一頁" in note for note in result.notes), result.notes


def fixture_html_single_page() -> str:
    cards = "".join(
        f"<div class='item'><h3>公司{n}有限公司</h3><span class='tel'>02-111{n}2222</span></div>"
        for n in range(1, 6)
    )
    return f"<html><body><div class='list'>{cards}</div></body></html>"


def test_a_link_that_only_changes_one_numeric_parameter_counts_as_pagination():
    """判準是「只有一個查詢參數不同，而且是數字」——連到別的頁面不算。"""
    from crawler.discover import find_query_pagination
    from crawler.parser import make_soup

    html = """
    <html><body>
      <a href="/list.php?cat=5&page=2">2</a>
      <a href="/list.php?cat=5&page=3">3</a>
      <a href="/other.php?page=2">別的頁面</a>
      <a href="/list.php?cat=9&page=2">別的分類</a>
    </body></html>
    """
    result = find_query_pagination(make_soup(html), "http://example.test/list.php?cat=5")

    assert result is not None
    template, count = result
    assert "page={page}" in template
    assert "cat=5" in template
    assert count == 3


def test_one_numbered_link_alone_is_not_pagination():
    """只有一個數字連結可能只是某個帶編號的東西，不是分頁。"""
    from crawler.discover import find_query_pagination
    from crawler.parser import make_soup

    html = "<html><body><a href='/list.php?page=2'>2</a></body></html>"
    assert find_query_pagination(make_soup(html), "http://example.test/list.php") is None


def test_an_icon_only_next_button_is_found_through_aria_label():
    """只有圖示沒有文字的翻頁鈕，可讀性資訊都放在 aria-label 裡。"""
    from crawler.discover import find_next_selector
    from crawler.parser import make_soup

    html = """
    <html><body>
      <a class="pager-next" href="/p2" aria-label="Next page"><i class="icon"></i></a>
    </body></html>
    """
    assert find_next_selector(make_soup(html)) == "a.pager-next"


def test_discover_to_source_config_produces_a_valid_source(fixture_html):
    result = discover_from_html(fixture_html, FIXTURE_URL)
    source = result.to_source_config("my_directory")

    assert isinstance(source, SourceConfig)
    assert source.name == "my_directory"
    assert source.type == "generic_html"
    assert source.start_url == FIXTURE_URL
    assert source.list_selector == "div.company-card"
    assert "company_name" in source.fields
    assert source.pagination.type == "next_link"
    assert source.pagination.next_selector == result.next_selector


# ------------------------------------------- JavaScript 產生的頁面自動重試


def test_a_javascript_page_is_re_analysed_in_a_browser(tmp_config, monkeypatch):
    """後端是 PHP、ASP.NET 還是回 JSON 的介面都沒有差別——差別只在於「原始
    HTML 裡有沒有那些字」。看不到就讓瀏覽器跑完再看，使用者不必知道也不必
    去改任何設定。"""
    import crawler.discover as discover_module
    from crawler.fetcher import FetchResult

    empty = "<html><body><div class='a'>x</div><div class='a'>y</div>" \
            "<div class='a'>z</div></body></html>"
    rendered = """<html><body><div class="list">
      <div class="item"><h3 class="n">甲有限公司</h3><span>02-1234-5678</span></div>
      <div class="item"><h3 class="n">乙股份有限公司</h3><span>02-2234-5678</span></div>
      <div class="item"><h3 class="n">丙實業有限公司</h3><span>02-3234-5678</span></div>
      <div class="item"><h3 class="n">丁企業有限公司</h3><span>02-4234-5678</span></div>
    </div></body></html>"""

    class _Fetcher:
        def __init__(self, html):
            self.html = html

        def fetch(self, url, **_kwargs):
            return FetchResult(url=url, status_code=200, html=self.html)

        def close(self):
            pass

    def fake_build(config=None, robots=None, engine=None):
        return _Fetcher(rendered if engine == "playwright" else empty)

    monkeypatch.setattr(discover_module, "build_fetcher", fake_build)

    result = discover_module.discover("https://a.test/list", tmp_config)

    assert result.engine == "playwright"
    assert result.item_count == 4
    assert "company_name" in result.fields
    assert any("瀏覽器" in note for note in result.notes)


def test_a_plain_page_is_not_re_analysed_in_a_browser(tmp_config, monkeypatch):
    """瀏覽器版慢得多，拿它換一個同樣的結果沒有意義。"""
    import crawler.discover as discover_module
    from crawler.fetcher import FetchResult

    html = """<html><body><div class="list">
      <div class="item"><h3 class="n">甲有限公司</h3><span>02-1234-5678</span></div>
      <div class="item"><h3 class="n">乙股份有限公司</h3><span>02-2234-5678</span></div>
      <div class="item"><h3 class="n">丙實業有限公司</h3><span>02-3234-5678</span></div>
      <div class="item"><h3 class="n">丁企業有限公司</h3><span>02-4234-5678</span></div>
    </div></body></html>"""
    engines: list[str | None] = []

    class _Fetcher:
        def fetch(self, url, **_kwargs):
            return FetchResult(url=url, status_code=200, html=html)

        def close(self):
            pass

    def fake_build(config=None, robots=None, engine=None):
        engines.append(engine)
        return _Fetcher()

    monkeypatch.setattr(discover_module, "build_fetcher", fake_build)

    result = discover_module.discover("https://a.test/list", tmp_config)

    assert result.engine is None
    assert "playwright" not in [e for e in engines if e]


def test_the_source_remembers_it_needs_a_browser(fixture_html):
    """存下去也要記住，否則第一次爬取又會是空的。"""
    result = discover_from_html(fixture_html, FIXTURE_URL)
    result.engine = "playwright"

    assert result.to_source_config("js_site").engine == "playwright"
