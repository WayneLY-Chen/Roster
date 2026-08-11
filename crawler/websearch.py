"""用公司名稱找到它的官網——可抽換的搜尋來源。

## 為什麼需要這個

:mod:`crawler.enrich` 能從公司自己的網站上找到公開刊登的信箱，但它的前提是
「已經知道網址」。名錄爬回來的資料通常帶著網址，所以以前不需要這一步；
使用者自己匯入的 Excel 就不一定了——很多名單只有公司名稱和一支電話。

沒有網址，補完就到此為止。這個模組補的就是「公司名稱 → 官網網址」那一段。

## 三個來源，優先順序由設定決定

======================  ========  ==========================================
provider                要金鑰？  說明
======================  ========  ==========================================
``duckduckgo``          否        預設。零設定、零費用，但對方會限流。
``brave``               是        Brave Search API，免費方案每月 2000 次。
``google``              是        Google Custom Search JSON API，每日 100 次。
======================  ========  ==========================================

設定寫 ``auto``（預設）時：有 Brave 金鑰就用 Brave，其次 Google，都沒有就用
DuckDuckGo。所以使用者什麼都不設定也能用，之後量大了再去申請金鑰，程式碼
與設定檔都不用改。

## 關於「爬搜尋引擎」這件事

這支程式唯一在強制執行的授權訊號是 robots.txt（見 :mod:`crawler.robots`），
而各家搜尋引擎在這件事上的立場**不一樣**，實際查過的結果：

    google.com/robots.txt          Disallow: /search
    bing.com/robots.txt            Disallow: /search
    mojeek.com/robots.txt          Disallow: /search
    duckduckgo.com/robots.txt      Disallow: /html
    html.duckduckgo.com/robots.txt Allow: /          ← 明文允許
    lite.duckduckgo.com/robots.txt Allow: /          ← 明文允許

所以這裡只用 ``html.duckduckgo.com``。前面那幾個不是「我們選擇不用」——是
這支程式的 fetcher 每次請求前都會查 robots.txt，那些網址**它自己就會擋下來**
並丟出 :class:`~core.errors.RobotsDisallowedError`。要用它們就得關掉 robots
檢查，而那正是這個專案從第一天起就不做的事。

DuckDuckGo 仍然會對高頻查詢回一頁「異常流量」而不是結果。遇到就丟出
:class:`SearchUnavailable`，由呼叫端決定要不要停——**不會**改頭換面重送、
不會換 User-Agent、不會繞過。那是對方在說「夠了」，照做就是。

## 這個模組不做的事

它只回傳「搜尋結果長什麼樣」。判斷哪一筆才是公司官網是 :mod:`crawler.complete`
的事——那需要知道公司叫什麼名字，而搜尋來源不需要知道。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from urllib.parse import parse_qs, quote_plus, urlsplit

import httpx

from core.config import AppConfig, get_config
from core.constants import LogCategory
from core.credentials import get_secret
from core.errors import CRMError, CrawlError
from core.logging_setup import get_logger
from crawler.fetcher import BaseFetcher
from crawler.parser import make_soup

log = get_logger(LogCategory.CRAWL)

#: 唯一一個 robots.txt 明文允許自動存取的搜尋端點。見模組說明。
DDG_ENDPOINT = "https://html.duckduckgo.com/html/"

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
GOOGLE_ENDPOINT = "https://www.googleapis.com/customsearch/v1"

#: 憑證保管庫裡的金鑰名稱。
BRAVE_KEY_SECRET = "brave_search_key"
GOOGLE_KEY_SECRET = "google_search_key"
GOOGLE_CX_SECRET = "google_search_cx"

#: DuckDuckGo 被打太兇時回的那一頁上會有的字樣。
_ANOMALY_MARKERS = (
    "anomaly",
    "unusual traffic",
    "detected unusual",
    "blocked",
)


class SearchUnavailable(CRMError):
    """這次查不了：對方限流、金鑰無效，或服務暫時不通。

    跟「查了但沒有結果」不同——那是回傳空 list，是一個正常的答案。
    """


@dataclass(frozen=True, slots=True)
class SearchHit:
    """一筆搜尋結果。"""

    url: str
    title: str = ""
    snippet: str = ""


class SearchProvider(ABC):
    """搜尋來源的共同介面。"""

    #: 設定檔與畫面上顯示的名字。
    name: str = "unknown"

    #: 使用者看得懂的說明，設定頁與日誌用。
    label: str = ""

    @abstractmethod
    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        """送出一次查詢。查得到就回結果，查不到回空 list。

        :raises SearchUnavailable: 對方限流、金鑰無效或服務不通。
        """

    def close(self) -> None:  # pragma: no cover - 預設沒有資源要收
        """釋放自己開的連線。用別人傳進來的 fetcher 時不該關掉它。"""


# --------------------------------------------------------------- DuckDuckGo


def _unwrap_ddg_link(href: str) -> str | None:
    """把 DuckDuckGo 的轉址連結還原成真正的網址。

    結果連結長這樣：``//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.tsmc.com...``。
    廣告走的是 ``/y.js``，那個沒有 ``uddg``，回 ``None`` 讓呼叫端丟掉。
    """
    if not href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    parts = urlsplit(href)
    if parts.netloc.endswith("duckduckgo.com"):
        target = parse_qs(parts.query).get("uddg")
        return target[0] if target else None
    return href if parts.scheme in ("http", "https") else None


class DuckDuckGoProvider(SearchProvider):
    """免金鑰的預設來源，走 ``html.duckduckgo.com``。

    刻意使用呼叫端傳進來的 :class:`~crawler.fetcher.BaseFetcher`，而不是自己
    開一個 httpx client——這樣 robots.txt 檢查、請求間隔、誠實的 User-Agent
    這三件事全部沿用爬名錄時同一套，不會因為「這是搜尋引擎」就鬆一格。
    """

    name = "duckduckgo"
    label = "DuckDuckGo（免金鑰）"

    def __init__(self, fetcher: BaseFetcher) -> None:
        self.fetcher = fetcher

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        url = f"{DDG_ENDPOINT}?q={quote_plus(query)}"
        try:
            result = self.fetcher.fetch(url)
        except CrawlError as exc:
            raise SearchUnavailable(f"DuckDuckGo 查詢失敗：{exc}") from exc

        hits = parse_ddg_results(result.html, limit)
        if not hits and _looks_throttled(result.html):
            raise SearchUnavailable(
                "DuckDuckGo 回的是異常流量頁而不是搜尋結果——查詢送太密了。"
                "請稍後再試，或到設定頁填入 Brave／Google 的搜尋金鑰。"
            )
        return hits


def _looks_throttled(html: str) -> bool:
    lowered = (html or "").lower()
    return any(marker in lowered for marker in _ANOMALY_MARKERS)


def parse_ddg_results(html: str, limit: int = 10) -> list[SearchHit]:
    """從 ``html.duckduckgo.com`` 的頁面上取出結果。

    這是解析別人的 HTML，對方改版就會失效——所以它是一支能單獨測試的純函式，
    壞掉時只要換這裡，不必碰到補完流程。
    """
    soup = make_soup(html or "")
    hits: list[SearchHit] = []
    seen: set[str] = set()

    for anchor in soup.select("a.result__a"):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        target = _unwrap_ddg_link(href)
        if not target or target in seen:
            continue
        seen.add(target)

        snippet = ""
        # 摘要跟標題在同一個 .result 區塊裡，往上找一層再往下取。
        block = anchor.find_parent(class_="result") or anchor.parent
        if block is not None:
            node = block.select_one(".result__snippet")
            if node is not None:
                snippet = node.get_text(" ", strip=True)

        hits.append(
            SearchHit(
                url=target,
                title=anchor.get_text(" ", strip=True),
                snippet=snippet,
            )
        )
        if len(hits) >= limit:
            break
    return hits


# --------------------------------------------------------- 需要金鑰的來源


class _ApiProvider(SearchProvider):
    """走官方 API 的來源共同部分。

    這些不經過 :class:`~crawler.fetcher.BaseFetcher`：API 端點不是「爬取」，
    對方發金鑰給你就是允許你程式化查詢，robots.txt 管的是爬蟲抓網頁。但逾時
    仍然沿用設定檔的值，不另外定一套。
    """

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()
        self._client: httpx.Client | None = None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.config.crawler.request_timeout,
                follow_redirects=True,
                headers={"User-Agent": self.config.crawler.resolved_user_agent()},
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _get(self, url: str, *, params: dict, headers: dict | None = None) -> dict:
        try:
            response = self._http().get(url, params=params, headers=headers or {})
        except httpx.HTTPError as exc:
            raise SearchUnavailable(f"{self.label} 連線失敗：{exc}") from exc

        if response.status_code in (401, 403):
            raise SearchUnavailable(f"{self.label} 拒絕這把金鑰（HTTP {response.status_code}），請檢查設定。")
        if response.status_code == 429:
            raise SearchUnavailable(f"{self.label} 已達用量上限（HTTP 429），請稍後再試。")
        if response.status_code >= 400:
            raise SearchUnavailable(f"{self.label} 回應 HTTP {response.status_code}。")

        try:
            return json.loads(response.text)
        except ValueError as exc:
            raise SearchUnavailable(f"無法解讀 {self.label} 的回應：{exc}") from exc


class BraveProvider(_ApiProvider):
    """Brave Search API。免費方案每月 2000 次查詢。"""

    name = "brave"
    label = "Brave Search API"

    def __init__(self, api_key: str, config: AppConfig | None = None) -> None:
        super().__init__(config)
        self.api_key = api_key

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        payload = self._get(
            BRAVE_ENDPOINT,
            params={"q": query, "count": min(limit, 20), "country": "tw"},
            headers={"Accept": "application/json", "X-Subscription-Token": self.api_key},
        )
        results = (payload.get("web") or {}).get("results") or []
        return [
            SearchHit(
                url=str(item.get("url") or ""),
                title=str(item.get("title") or ""),
                snippet=str(item.get("description") or ""),
            )
            for item in results
            if isinstance(item, dict) and item.get("url")
        ][:limit]


class GoogleCseProvider(_ApiProvider):
    """Google Custom Search JSON API。免費額度每日 100 次。"""

    name = "google"
    label = "Google Custom Search API"

    def __init__(self, api_key: str, cx: str, config: AppConfig | None = None) -> None:
        super().__init__(config)
        self.api_key = api_key
        self.cx = cx

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        payload = self._get(
            GOOGLE_ENDPOINT,
            params={
                "key": self.api_key,
                "cx": self.cx,
                "q": query,
                # 這支 API 一次最多給 10 筆，要更多得自己翻頁。補完只看前幾筆，
                # 不值得為了第 11 筆多送一次請求。
                "num": min(limit, 10),
                "hl": "zh-TW",
            },
        )
        return [
            SearchHit(
                url=str(item.get("link") or ""),
                title=str(item.get("title") or ""),
                snippet=str(item.get("snippet") or ""),
            )
            for item in (payload.get("items") or [])
            if isinstance(item, dict) and item.get("link")
        ][:limit]


# ------------------------------------------------------------------ 選擇


def available_providers() -> dict[str, str]:
    """``名稱 -> 說明``，設定頁的下拉選單用。"""
    return {
        "auto": "自動（有金鑰就用金鑰，沒有就用 DuckDuckGo）",
        DuckDuckGoProvider.name: DuckDuckGoProvider.label,
        BraveProvider.name: BraveProvider.label,
        GoogleCseProvider.name: GoogleCseProvider.label,
        "none": "不搜尋（只用檔案裡已有的網址）",
    }


def configured_keys() -> dict[str, bool]:
    """哪幾把金鑰已經設定好了。給設定頁顯示狀態用，不回傳金鑰內容。"""
    return {
        BraveProvider.name: bool(get_secret(BRAVE_KEY_SECRET)),
        GoogleCseProvider.name: bool(
            get_secret(GOOGLE_KEY_SECRET) and get_secret(GOOGLE_CX_SECRET)
        ),
    }


def build_search_provider(
    config: AppConfig | None = None,
    fetcher: BaseFetcher | None = None,
) -> SearchProvider | None:
    """依設定挑一個搜尋來源。設定成 ``none`` 時回 ``None``。

    ``auto`` 的順序是 Brave → Google → DuckDuckGo。指名某一家但金鑰沒設定
    時**不會**安靜地退回 DuckDuckGo——那會讓使用者以為自己在用付費額度，
    實際上在打免費端點。這種情況丟 :class:`SearchUnavailable`。
    """
    config = config or get_config()
    choice = (config.completion.search_provider or "auto").strip().lower()

    if choice == "none":
        return None

    brave_key = get_secret(BRAVE_KEY_SECRET)
    google_key = get_secret(GOOGLE_KEY_SECRET)
    google_cx = get_secret(GOOGLE_CX_SECRET)

    if choice == "auto":
        if brave_key:
            return BraveProvider(brave_key, config)
        if google_key and google_cx:
            return GoogleCseProvider(google_key, google_cx, config)
        choice = DuckDuckGoProvider.name

    if choice == BraveProvider.name:
        if not brave_key:
            raise SearchUnavailable(
                "設定選的是 Brave Search API，但還沒有填入金鑰。"
                "請到「設定」頁填入，或把搜尋來源改回「自動」。"
            )
        return BraveProvider(brave_key, config)

    if choice == GoogleCseProvider.name:
        if not (google_key and google_cx):
            raise SearchUnavailable(
                "設定選的是 Google Custom Search API，但金鑰或搜尋引擎 ID 還沒填。"
                "請到「設定」頁填入，或把搜尋來源改回「自動」。"
            )
        return GoogleCseProvider(google_key, google_cx, config)

    if choice == DuckDuckGoProvider.name:
        if fetcher is None:
            raise SearchUnavailable("DuckDuckGo 來源需要一個 fetcher。")
        return DuckDuckGoProvider(fetcher)

    raise SearchUnavailable(f"不認得的搜尋來源：{choice}")
