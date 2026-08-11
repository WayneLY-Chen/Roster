"""用公司名稱找官網的搜尋來源。

這個模組有兩種完全不同的東西，測試方式也不一樣：

* **解析別人的 HTML**（DuckDuckGo）——對方改版就會壞掉，所以解析本身是純
  函式，用實際回應的形狀直接測。這裡的 HTML 片段照著真的 ``html.duckduckgo.com``
  回來的結構寫，包含那個一定要拆的轉址連結與夾在結果裡的廣告。
* **挑哪一家 provider**——純粹是設定與金鑰的邏輯，一次網路都不用碰。

整份測試不連任何網路。
"""

from __future__ import annotations

import pytest

from core.config import AppConfig
from crawler.fetcher import FetchResult
from crawler.websearch import (
    BRAVE_KEY_SECRET,
    GOOGLE_CX_SECRET,
    GOOGLE_KEY_SECRET,
    BraveProvider,
    DuckDuckGoProvider,
    GoogleCseProvider,
    SearchHit,
    SearchUnavailable,
    _unwrap_ddg_link,
    build_search_provider,
    parse_ddg_results,
)

#: ``html.duckduckgo.com`` 回來的結構。第三筆是廣告（``/y.js``，沒有 uddg），
#: 第四筆跟第一筆是同一個網址——兩個都必須被丟掉。
DDG_HTML = """
<div class="results">
  <div class="result results_links">
    <h2><a class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.tsmc.com%2Fchinese&amp;rut=abc">
       台積電 TSMC</a></h2>
    <a class="result__snippet">台灣積體電路製造股份有限公司官方網站</a>
  </div>
  <div class="result results_links">
    <h2><a class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.104.com.tw%2Fcompany%2Fabc&amp;rut=def">
       台積電 職缺</a></h2>
    <a class="result__snippet">104 人力銀行</a>
  </div>
  <div class="result results_links result--ad">
    <h2><a class="result__a" href="//duckduckgo.com/y.js?ad_provider=xyz">贊助商連結</a></h2>
  </div>
  <div class="result results_links">
    <h2><a class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.tsmc.com%2Fchinese&amp;rut=ghi">
       台積電（重複）</a></h2>
  </div>
</div>
"""

THROTTLED_HTML = "<html><body><p>If this error persists, ... anomaly detected ...</p></body></html>"


class _Fetcher:
    """記下被要求的網址，吐出預先準備好的頁面。"""

    def __init__(self, html: str = DDG_HTML) -> None:
        self.html = html
        self.urls: list[str] = []

    def fetch(self, url: str, **_kwargs) -> FetchResult:
        self.urls.append(url)
        return FetchResult(url=url, status_code=200, html=self.html)

    def close(self) -> None:  # pragma: no cover - 介面完整性
        pass


# ------------------------------------------------------- 轉址連結的還原


@pytest.mark.parametrize(
    "href, expected",
    [
        (
            "//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.tsmc.com%2Fchinese&rut=abc",
            "https://www.tsmc.com/chinese",
        ),
        # 有些結果不經過轉址，直接就是目標網址。
        ("https://example.com.tw/", "https://example.com.tw/"),
        # 廣告：是 duckduckgo.com 的網址但沒有 uddg。
        ("//duckduckgo.com/y.js?ad_provider=xyz", None),
        ("", None),
        ("javascript:void(0)", None),
    ],
)
def test_the_redirect_wrapper_is_unwrapped(href, expected):
    assert _unwrap_ddg_link(href) == expected


# --------------------------------------------------------------- 解析


def test_results_are_parsed_in_order():
    hits = parse_ddg_results(DDG_HTML)
    assert [hit.url for hit in hits] == [
        "https://www.tsmc.com/chinese",
        "https://www.104.com.tw/company/abc",
    ]


def test_the_title_and_snippet_come_along():
    first = parse_ddg_results(DDG_HTML)[0]
    assert first.title == "台積電 TSMC"
    assert "台灣積體電路製造股份有限公司" in first.snippet


def test_an_ad_without_a_target_is_dropped():
    """廣告是 ``/y.js``，拆不出目標網址。留著它會變成一個爬不動的候選。"""
    assert not any("y.js" in hit.url for hit in parse_ddg_results(DDG_HTML))


def test_the_same_url_twice_is_only_one_hit():
    urls = [hit.url for hit in parse_ddg_results(DDG_HTML)]
    assert len(urls) == len(set(urls))


def test_the_limit_is_honoured():
    assert len(parse_ddg_results(DDG_HTML, limit=1)) == 1


def test_an_empty_page_is_not_an_error():
    """搜尋不到東西是一個正常的答案，不是故障。"""
    assert parse_ddg_results("") == []
    assert parse_ddg_results("<html><body>nothing here</body></html>") == []


# ------------------------------------------------------- DuckDuckGo 來源


def test_the_query_goes_to_the_endpoint_robots_txt_allows():
    """一定要打 ``html.duckduckgo.com``。

    ``duckduckgo.com`` 本站的 robots.txt 寫著 ``Disallow: /html``，只有
    ``html.`` 這個子網域是 ``Allow: /``。打錯的話這支程式的 fetcher 會自己
    把請求擋下來，而且是在使用者按下按鈕之後才擋。
    """
    fetcher = _Fetcher()
    DuckDuckGoProvider(fetcher).search("台積電")

    assert len(fetcher.urls) == 1
    assert fetcher.urls[0].startswith("https://html.duckduckgo.com/html/?q=")


def test_a_throttled_page_is_reported_not_swallowed():
    """被限流時回空 list 會讓上層以為「這家公司搜不到」，那是兩件事。"""
    with pytest.raises(SearchUnavailable, match="異常流量"):
        DuckDuckGoProvider(_Fetcher(THROTTLED_HTML)).search("台積電")


def test_a_genuinely_empty_result_is_not_treated_as_throttling():
    provider = DuckDuckGoProvider(_Fetcher("<html><body>No results.</body></html>"))
    assert provider.search("不存在的公司") == []


# ----------------------------------------------------- 需要金鑰的來源


def test_brave_responses_are_mapped(monkeypatch):
    provider = BraveProvider("key", AppConfig())
    monkeypatch.setattr(
        provider,
        "_get",
        lambda *a, **k: {
            "web": {
                "results": [
                    {"url": "https://a.tw", "title": "A", "description": "說明 A"},
                    {"title": "沒有網址的就丟掉"},
                ]
            }
        },
    )
    assert provider.search("x") == [SearchHit("https://a.tw", "A", "說明 A")]


def test_google_responses_are_mapped(monkeypatch):
    provider = GoogleCseProvider("key", "cx", AppConfig())
    monkeypatch.setattr(
        provider,
        "_get",
        lambda *a, **k: {"items": [{"link": "https://b.tw", "title": "B", "snippet": "說明 B"}]},
    )
    assert provider.search("x") == [SearchHit("https://b.tw", "B", "說明 B")]


# ------------------------------------------------------------ 挑來源


@pytest.fixture
def no_keys(monkeypatch):
    """保證環境裡一把金鑰都沒有。

    ``tests/__init__`` 已經把系統保管庫關掉了，但 :func:`core.credentials.get_secret`
    還會退回環境變數——開發者自己機器上剛好設了 ``BRAVE_SEARCH_KEY`` 的話，
    測試結果就會因人而異。
    """
    for name in ("BRAVE_SEARCH_KEY", "GOOGLE_SEARCH_KEY", "GOOGLE_SEARCH_CX"):
        monkeypatch.delenv(name, raising=False)


def _config(provider: str) -> AppConfig:
    return AppConfig.model_validate({"completion": {"search_provider": provider}})


def test_auto_without_any_key_falls_back_to_the_free_one(no_keys):
    """零設定就要能用。這是整個功能「不用申請任何東西」的那個承諾。"""
    provider = build_search_provider(_config("auto"), fetcher=_Fetcher())
    assert isinstance(provider, DuckDuckGoProvider)


def test_auto_prefers_a_key_when_there_is_one(no_keys, monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_KEY", "secret")
    provider = build_search_provider(_config("auto"), fetcher=_Fetcher())
    assert isinstance(provider, BraveProvider)
    provider.close()


def test_auto_uses_google_when_only_google_is_configured(no_keys, monkeypatch):
    monkeypatch.setenv("GOOGLE_SEARCH_KEY", "secret")
    monkeypatch.setenv("GOOGLE_SEARCH_CX", "cx-id")
    provider = build_search_provider(_config("auto"), fetcher=_Fetcher())
    assert isinstance(provider, GoogleCseProvider)
    provider.close()


def test_google_without_its_search_engine_id_is_not_usable(no_keys, monkeypatch):
    """金鑰有、ID 沒有，那支 API 一定回 400。當成沒設定。"""
    monkeypatch.setenv("GOOGLE_SEARCH_KEY", "secret")
    provider = build_search_provider(_config("auto"), fetcher=_Fetcher())
    assert isinstance(provider, DuckDuckGoProvider)


def test_naming_a_provider_without_its_key_says_so(no_keys):
    """不要安靜地退回免費來源。

    使用者以為自己在用付費額度，實際上在打免費端點——然後被限流，然後
    完全不知道為什麼。這種情況要當場講出來。
    """
    with pytest.raises(SearchUnavailable, match="Brave"):
        build_search_provider(_config("brave"), fetcher=_Fetcher())


def test_none_means_do_not_search(no_keys):
    assert build_search_provider(_config("none"), fetcher=_Fetcher()) is None


def test_the_secret_names_are_the_ones_credentials_knows_about():
    """設定頁存進去的名字，跟這裡讀出來的名字必須是同一個。

    這兩邊各寫一份字串的話，使用者會填了金鑰、按了儲存、然後發現完全沒有
    生效——而且畫面上不會有任何錯誤。
    """
    from core.credentials import SECRET_ENV_VARS

    for name in (BRAVE_KEY_SECRET, GOOGLE_KEY_SECRET, GOOGLE_CX_SECRET):
        assert name in SECRET_ENV_VARS
