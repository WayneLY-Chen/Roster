"""站內自動探索（``crawler.explore``）。

「這一頁怎麼抓」是 :mod:`crawler.discover` 的事；這裡的問題是**名錄在哪一頁**。
名錄常常藏在「關於我們 → 組織 → 會員專區」底下三層，首頁完全看不出來。
"""

from __future__ import annotations

import httpx
import pytest

from crawler.explore import (
    ExploreResult,
    _pagination_key,
    _sitemap_urls,
    _to_candidate,
    explore,
    link_priority,
    normalise_url,
    same_site,
)
from crawler.fetcher import HttpxFetcher
from crawler.robots import RobotsPolicy


# ------------------------------------------------------------ 網址處理


def test_the_same_page_written_two_ways_counts_once():
    assert normalise_url("https://A.com/x/?a=1#top") == normalise_url("https://a.com/x?a=1")


def test_www_does_not_make_it_a_different_site():
    assert same_site("https://www.a.com/x", "https://a.com/y")
    assert not same_site("https://a.com", "https://b.com")


def test_promising_words_are_walked_first():
    assert link_priority("https://a.com/member/list", "會員名錄") > link_priority(
        "https://a.com/about", "關於"
    )


def test_obviously_useless_links_are_skipped():
    """負分代表「不要走」。購物車與登入頁不會有廠商名錄。"""
    assert link_priority("https://a.com/login", "登入") < 0
    assert link_priority("https://a.com/cart", "") < 0


def test_a_deep_url_is_lower_priority_but_still_walkable():
    """名錄藏在第五層的網站真的存在，深度只該扣分、不該一票否決。"""
    assert link_priority("https://a.com/a/b/c/d/e", "") >= 0


def test_pages_of_one_directory_share_a_key():
    assert _pagination_key("https://a.com/list?page=2") == _pagination_key(
        "https://a.com/list?page=7"
    )


# ------------------------------------------------------------ sitemap


def test_a_sitemap_index_yields_nested_sitemaps_not_pages():
    xml = """<?xml version="1.0"?><sitemapindex>
      <sitemap><loc>https://a.com/s1.xml</loc></sitemap>
    </sitemapindex>"""
    pages, nested = _sitemap_urls(xml, "https://a.com/sitemap.xml")
    assert pages == []
    assert nested == ["https://a.com/s1.xml"]


def test_a_normal_sitemap_yields_pages():
    xml = """<?xml version="1.0"?><urlset>
      <url><loc>https://a.com/p1</loc></url>
      <url><loc>https://a.com/p2</loc></url>
    </urlset>"""
    pages, nested = _sitemap_urls(xml, "https://a.com/sitemap.xml")
    assert pages == ["https://a.com/p1", "https://a.com/p2"]
    assert nested == []


# ------------------------------------------------------ 什麼才算名錄


def _cards(names: list[str]) -> str:
    cards = "".join(
        f"<div class='item'><h3 class='n'>{name}</h3>"
        f"<span class='tel'>02-2345-67{i:02d}</span></div>"
        for i, name in enumerate(names)
    )
    return f"<html><body><div class='list'>{cards}</div></body></html>"


def test_a_page_of_company_names_is_a_directory():
    from crawler.discover import discover_from_html

    html = _cards(["甲有限公司", "乙股份有限公司", "丙企業社", "丁實業有限公司", "戊工業社"])
    candidate = _to_candidate("https://a.com/list", discover_from_html(html, "https://a.com/list"))

    assert candidate is not None
    assert candidate.item_count == 5
    assert candidate.company_name_ratio == 1.0


def test_a_page_of_forum_posts_is_not_a_directory():
    """實測踩到的：web66 首頁有 60 筆重複結構、還抓得到信箱，分數遠高於真正的
    廠商名錄——但那 60 筆是「住家不鏽鋼門檻刮傷，需要修復」這種詢價標題。
    筆數與欄位數都分不出這件事，只有內容可以。"""
    from crawler.discover import discover_from_html

    html = _cards([
        "住家不鏽鋼門檻刮傷，需要修復",
        "中員普渡拜拜用棚子",
        "台灣包裹寄送大陸運費",
        "浴室磁磚重鋪報價",
        "想找人幫忙搬家",
    ])
    assert _to_candidate("https://a.com/ask", discover_from_html(html, "https://a.com/ask")) is None


def test_too_few_rows_is_not_a_directory():
    """三五則的區塊多半是「最新消息」，不是廠商清單。"""
    from crawler.discover import discover_from_html

    html = _cards(["甲有限公司", "乙股份有限公司"])
    assert _to_candidate("https://a.com/x", discover_from_html(html, "https://a.com/x")) is None


# ------------------------------------------------------------ 整趟探索


DIRECTORY = _cards(
    ["甲有限公司", "乙股份有限公司", "丙企業社", "丁實業有限公司", "戊工業社", "己貿易有限公司"]
)

HOME = """<html><body>
  <a href="/about">關於我們</a>
  <a href="/login">登入</a>
  <a href="/members">會員名錄</a>
</body></html>"""

ABOUT = "<html><body><p>本會成立於 1970 年。</p></body></html>"


def _site_fetcher(tmp_config, pages: dict[str, str], robots: str = ""):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200 if robots else 404, text=robots)
        body = pages.get(path)
        if body is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text=body, headers={"Content-Type": "text/html"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpxFetcher(
        config=tmp_config,
        robots=RobotsPolicy("ua", enabled=False, client=client),
        client=client,
    )


@pytest.fixture
def quick_config(tmp_config):
    """測試不要真的等禮貌延遲——那是對真實網站的規矩，不是對假的。"""
    return tmp_config.model_copy(
        update={"crawler": tmp_config.crawler.model_copy(update={"delay_seconds": 0.0})}
    )


def test_a_directory_two_clicks_deep_is_found(quick_config):
    fetcher = _site_fetcher(
        quick_config, {"/": HOME, "/about": ABOUT, "/members": DIRECTORY}
    )
    result = explore("https://a.test/", config=quick_config, fetcher=fetcher, page_budget=10)

    assert [c.url for c in result.candidates] == ["https://a.test/members"]
    assert result.candidates[0].item_count == 6


def test_the_page_budget_is_a_hard_ceiling(quick_config):
    """站內走訪是對別人的伺服器送請求，上限必須說到做到。"""
    pages = {f"/p{i}": f"<html><body><a href='/p{i + 1}'>下一個 廠商</a></body></html>"
             for i in range(50)}
    pages["/"] = "<html><body><a href='/p0'>廠商清單</a></body></html>"
    fetcher = _site_fetcher(quick_config, pages)

    result = explore("https://a.test/", config=quick_config, fetcher=fetcher, page_budget=5)

    assert result.pages_fetched <= 5
    assert result.hit_budget


#: 名字各自不同的名錄頁。路徑刻意不帶數字——帶數字的會被當成同一個名錄的
#: 不同頁而合併，那是另一個測試在管的事。
_MANY = ["members", "suppliers", "vendors", "factories", "importers", "exporters"]


def test_it_stops_once_it_has_enough(quick_config):
    pages = {"/": "".join(f"<a href='/{name}'>會員名錄</a>" for name in _MANY)}
    pages.update({f"/{name}": DIRECTORY for name in _MANY})
    fetcher = _site_fetcher(quick_config, pages)

    result = explore(
        "https://a.test/", config=quick_config, fetcher=fetcher,
        page_budget=30, target_candidates=2,
    )

    assert len(result.candidates) == 2
    assert result.pages_fetched < 30


def test_the_other_pages_of_one_directory_are_not_analysed_again(quick_config):
    """``?page=2`` 跟第 1 頁長得一模一樣，每一頁都分析一次只是把預算用光。
    翻頁交給精靈的分頁偵測，探索只要找到入口。"""
    pages = {"/": "<a href='/list?page=1'>廠商名錄</a>"}
    pages.update({"/list": DIRECTORY})
    fetcher = _site_fetcher(quick_config, pages)

    result = explore(
        "https://a.test/", config=quick_config, fetcher=fetcher,
        page_budget=20, target_candidates=5,
    )

    assert len(result.candidates) == 1


def test_cancelling_keeps_what_was_already_found(quick_config):
    """按取消多半是「夠了」，不是「這些我不要」。白等那幾十秒沒有道理。"""

    class StopAfterFirstPage:
        def __init__(self) -> None:
            self._fired = False

        def is_set(self) -> bool:
            was = self._fired
            self._fired = True
            return was

    fetcher = _site_fetcher(quick_config, {"/": HOME, "/members": DIRECTORY})
    result = explore(
        "https://a.test/", config=quick_config, fetcher=fetcher,
        page_budget=10, cancel_event=StopAfterFirstPage(),
    )

    assert result.cancelled
    assert not result.hit_budget


def test_links_to_other_sites_are_not_followed(quick_config):
    """探索的範圍是「這個網站」。跟出去就變成漫遊整個網際網路了。"""
    home = "<html><body><a href='https://elsewhere.test/members'>會員名錄</a></body></html>"
    fetcher = _site_fetcher(quick_config, {"/": home})
    result = explore("https://a.test/", config=quick_config, fetcher=fetcher, page_budget=10)

    assert result.pages_fetched == 1


def test_files_we_cannot_read_are_not_fetched(quick_config):
    """PDF、Word、圖片現在讀不了，跟進去只是浪費一次請求。"""
    home = "<html><body><a href='/members.pdf'>會員名錄 PDF</a></body></html>"
    fetcher = _site_fetcher(quick_config, {"/": home})
    result = explore("https://a.test/", config=quick_config, fetcher=fetcher, page_budget=10)

    assert result.pages_fetched == 1


def test_a_site_with_no_directory_says_so_instead_of_guessing(quick_config):
    fetcher = _site_fetcher(quick_config, {"/": HOME, "/about": ABOUT})
    result = explore("https://a.test/", config=quick_config, fetcher=fetcher, page_budget=10)

    assert not result.ok
    assert any("沒有找到" in note for note in result.notes)


def test_an_empty_url_is_rejected_before_any_request(quick_config):
    from core.errors import CrawlError

    with pytest.raises(CrawlError):
        explore("   ", config=quick_config)


def test_the_result_reports_how_many_records_a_directory_would_yield():
    from crawler.explore import DirectoryCandidate

    assert DirectoryCandidate(url="x", item_count=24, page_count=147).estimated_records == 3528
    # 沒偵測到頁數時當成一頁，不要回傳 0。
    assert DirectoryCandidate(url="x", item_count=24, page_count=0).estimated_records == 24


def test_notes_never_claim_success_when_nothing_was_found():
    result = ExploreResult(start_url="https://a.test/", pages_fetched=9)
    from crawler.explore import _add_notes

    _add_notes(result, 30)
    assert not result.ok
    assert "9" in result.notes[0]
