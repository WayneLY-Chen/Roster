"""給一個網站，自己找出哪幾頁是廠商名錄。

:mod:`crawler.discover` 回答的是「**這一頁**要怎麼抓」，前提是使用者已經找到
那一頁。實際上找那一頁往往才是麻煩的部分——會員名錄可能藏在「關於我們 →
組織 → 會員專區」底下三層，網站首頁完全看不出來。

這個模組回答的是另一個問題：「**這個網站**的名錄在哪裡」。作法是從起點出發
在站內走訪，每一頁都丟給 :func:`~crawler.discover.discover_from_html` 問一次
「你看得懂這頁嗎」，看得懂而且抓得到夠多筆的就是候選。

## 對方的伺服器

站內走訪比抓單一頁面重得多，所以這裡的每一個設計都往「少送一點請求」偏：

* **先看 sitemap**。網站自己公告「我有哪些頁面」時，照著看比一頁一頁點連結
  去猜少掉非常多次請求。
* **請求次數有硬上限**，而且是必填的參數，不是可選的優化。
* **連結有優先序**：網址或連結文字裡有「名錄」「會員」「廠商」的先走。
* **同一個名錄的第 2、3 頁不重複探索**——那是分頁，不是新的名錄。

robots.txt 與請求間隔延遲由 fetcher 負責，跟一般爬取完全一樣；被 robots.txt
擋掉的頁面在這裡同樣不會被讀取。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from core.config import AppConfig, get_config
from core.constants import LogCategory
from core.errors import CrawlError, RobotsDisallowedError
from core.logging_setup import get_logger
from crawler.discover import MIN_ITEMS, DiscoveryResult, discover_from_html
from crawler.documents import KIND_BY_KEY, extract_records, is_wanted
from crawler.fetcher import BaseFetcher, build_fetcher, decode_bytes
from crawler.parser import make_soup, sniff_declared_encoding

log = get_logger(LogCategory.CRAWL)

#: 預設最多讀幾頁。每一頁都是對方伺服器的一次請求，而且中間有禮貌延遲，
#: 所以這個數字同時決定了「要等多久」。30 頁大約一到兩分鐘。
DEFAULT_PAGE_BUDGET = 30

#: 找到這麼多個名錄頁就可以收工了——再找下去只是多打擾對方。
DEFAULT_TARGET_CANDIDATES = 5

#: 一頁至少要抓得到這麼多筆才算名錄。低於這個數字的多半是「最新消息」那種
#: 三五則的區塊，不是廠商清單。
MIN_DIRECTORY_ITEMS = MIN_ITEMS

#: 網址或連結文字裡有這些字的優先走。台灣的公協會網站幾乎都用得到其中一個。
_PROMISING = (
    "名錄", "會員", "廠商", "業者", "供應商", "廠商名錄", "會員名錄", "會員廠商",
    "公司", "企業", "工廠", "商家", "店家", "目錄", "清冊", "查詢", "搜尋",
    "directory", "member", "members", "company", "companies", "supplier",
    "suppliers", "vendor", "firm", "list", "search", "query", "catalog",
)

#: 這些一看就不會是名錄，不要浪費預算。
_UNPROMISING = (
    "login", "logout", "signin", "signup", "register", "cart", "checkout",
    "privacy", "terms", "sitemap", "rss", "feed", "comment", "print",
    "登入", "登出", "註冊", "隱私", "條款", "購物車", "留言", "列印",
)

#: 不是網頁的東西。PDF／Word／Excel 目前讀不了，跟進去只是浪費一次請求。
_NON_HTML_SUFFIXES = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".mp4", ".mp3",
    ".css", ".js", ".json", ".xml", ".csv", ".txt", ".exe", ".dmg",
)

#: sitemap 一次最多看這麼多個網址。大型網站的 sitemap 動輒上萬筆，全部評分
#: 只是浪費時間——真正的名錄頁在關鍵字排序之後會排在很前面。
_MAX_SITEMAP_URLS = 2000


@dataclass(slots=True)
class DirectoryCandidate:
    """探索到的一個名錄頁。"""

    url: str
    item_count: int = 0
    fields: list[str] = field(default_factory=list)
    page_count: int = 0
    sample_names: list[str] = field(default_factory=list)
    #: 抓到的內容有多少比例真的像公司名稱。這是分辨「廠商名錄」與「同樣有
    #: 重複結構的問答／新聞列表」唯一有效的訊號。
    company_name_ratio: float = 0.0
    score: float = 0.0
    #: 這個候選是一個檔案時的格式（``pdf``／``excel``／``word``）；
    #: 空字串代表它是一般網頁。
    kind: str = ""

    @property
    def estimated_records(self) -> int:
        """整個名錄爬完大約有幾筆。"""
        return self.item_count * max(self.page_count, 1)


@dataclass
class ExploreResult:
    """一次站內探索的結果。"""

    start_url: str
    candidates: list[DirectoryCandidate] = field(default_factory=list)
    pages_fetched: int = 0
    notes: list[str] = field(default_factory=list)
    #: 用完請求上限才停的（而不是「找完了」）。True 代表再多給一點預算可能還有。
    hit_budget: bool = False
    #: 使用者按了取消。已經找到的候選仍然留著——白等那幾十秒沒有道理。
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.candidates)


# ------------------------------------------------------------------ 網址處理


def normalise_url(url: str) -> str:
    """去掉錨點與結尾斜線，讓同一頁的不同寫法只算一次。"""
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, parts.query, ""))


def same_site(a: str, b: str) -> bool:
    """兩個網址算不算同一個網站（``www.`` 有沒有寫不影響）。"""
    host_a = urlsplit(a).netloc.lower().removeprefix("www.")
    host_b = urlsplit(b).netloc.lower().removeprefix("www.")
    return bool(host_a) and host_a == host_b


def _is_html_url(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return not path.endswith(_NON_HTML_SUFFIXES)


def link_priority(url: str, anchor_text: str = "") -> float:
    """這個連結有多值得走。分數越高越先走，負分代表不要走。"""
    haystack = f"{urlsplit(url).path} {urlsplit(url).query} {anchor_text}".lower()

    if any(word in haystack for word in _UNPROMISING):
        return -1.0

    score = sum(2.0 for word in _PROMISING if word in haystack)
    # 路徑越淺越可能是分類入口而不是某一家公司的明細頁。
    #
    # 只扣分不扣到負的：負分代表「不要走」，那是停用字清單的職責。深層網址
    # 只是比較不優先，不是不能走——名錄藏在第五層的網站真的存在。
    depth = urlsplit(url).path.strip("/").count("/")
    return max(score - depth * 0.3, 0.0)


#: 網址只差一個數字的頁面。一個名錄的第 2、3 頁跟第 1 頁長得一模一樣，
#: 每一頁都分析一次只是把預算用光。
_TRAILING_NUMBER = re.compile(r"\d+")


def _pagination_key(url: str) -> str:
    """把網址裡的所有數字換成佔位符，用來認出「同一個名錄的不同頁」。

    這也會把 ``?cat=1``、``?cat=2`` 這種「一個分類一頁」的網址算成同一個。
    那是刻意的取捨：這裡的任務是**找到名錄在哪**，找到一個入口就夠了，
    剩下的分類與分頁交給精靈的分頁偵測處理。
    """
    parts = urlsplit(url)
    return f"{parts.netloc}{_TRAILING_NUMBER.sub('#', parts.path)}?" + _TRAILING_NUMBER.sub(
        "#", parts.query
    )


# ------------------------------------------------------------------ sitemap


def _sitemap_urls(xml: str, base_url: str) -> tuple[list[str], list[str]]:
    """從 sitemap 拆出 ``(頁面網址, 巢狀 sitemap 網址)``。"""
    try:
        soup = BeautifulSoup(xml, "xml")
    except Exception:
        soup = BeautifulSoup(xml, "html.parser")

    nested = [
        loc.get_text(strip=True)
        for sitemap in soup.find_all("sitemap")
        for loc in sitemap.find_all("loc")
    ]
    nested_set = set(nested)
    pages = [
        text
        for loc in soup.find_all("loc")
        if (text := loc.get_text(strip=True)) and text not in nested_set
    ]
    return (
        [urljoin(base_url, u) for u in pages],
        [urljoin(base_url, u) for u in nested],
    )


# ------------------------------------------------------------------ 探索本體


def _fetch_page(fetcher: BaseFetcher, url: str) -> str | None:
    """讀一頁並用正確的編碼解碼；讀不到就回 None（探索不該因一頁而中止）。"""
    try:
        page = fetcher.fetch(url)
    except RobotsDisallowedError:
        log.debug("robots.txt 不允許 {}，略過", url)
        return None
    except CrawlError as exc:
        log.debug("讀取 {} 失敗：{}", url, exc)
        return None

    html = page.html
    declared = sniff_declared_encoding(page.raw) if page.raw else None
    if declared:
        html = decode_bytes(page.raw, declared)
    return html


#: 抓到的東西裡至少要有這個比例「看起來像公司名稱」才算名錄。
#:
#: 這一關比筆數重要得多。實測 web66 首頁：程式在上面找到 60 筆重複結構、
#: 還抓得到信箱，分數遠高於真正的廠商名錄——但那 60 筆是「住家不鏽鋼門檻
#: 刮傷，需要修復」這種詢價標題。筆數與欄位數都分不出這件事，只有內容可以。
#: 0.4 是實測調出來的：web66 的詢價列表有 29% 的標題碰巧含有「公司」二字
#: （「想請貴公司報價…」），0.25 擋不掉它；而真正的名錄實測都在 60% 以上。
_MIN_COMPANY_NAME_RATIO = 0.4


def _company_name_ratio(result: DiscoveryResult) -> float:
    """預覽資料裡有多少比例真的像公司名稱。"""
    from crawler.discover import _has_company_marker

    names = [record.company_name for record in result.preview if record.company_name]
    if not names:
        return 0.0
    return sum(1 for name in names if _has_company_marker(name)) / len(names)


def _document_candidate(fetcher: BaseFetcher, url: str) -> DirectoryCandidate | None:
    """讀一個 PDF／Excel／Word，讀得出名單就變成一個候選。"""
    from crawler.documents import kind_for

    try:
        page = fetcher.fetch(url)
    except RobotsDisallowedError:
        log.debug("robots.txt 不允許 {}，略過", url)
        return None
    except CrawlError as exc:
        log.debug("讀取 {} 失敗：{}", url, exc)
        return None

    try:
        parsed = extract_records(page.raw or page.html.encode(), url, "explore")
    except CrawlError as exc:
        log.debug("解析 {} 失敗：{}", url, exc)
        return None

    if len(parsed.records) < MIN_DIRECTORY_ITEMS:
        return None

    kind = KIND_BY_KEY.get(kind_for(url) or "")
    return DirectoryCandidate(
        url=url,
        item_count=len(parsed.records),
        fields=sorted(
            name
            for name in ("email", "phone", "fax", "address", "website", "contact_person")
            if any(getattr(record, name, None) for record in parsed.records)
        ),
        page_count=1,
        sample_names=[record.company_name for record in parsed.records[:3]],
        company_name_ratio=1.0,   # extract_records 已經把不像名冊的整份擋掉了
        score=60.0 + min(len(parsed.records), 100) * 0.5,
        kind=kind.key if kind else "",
    )


def _to_candidate(url: str, result: DiscoveryResult) -> DirectoryCandidate | None:
    """把一次分析的結果變成候選，不夠格就回 None。"""
    if not result.ok or result.item_count < MIN_DIRECTORY_ITEMS:
        return None

    company_ratio = _company_name_ratio(result)
    if company_ratio < _MIN_COMPANY_NAME_RATIO:
        log.debug("略過 {}：抓到的內容不像公司名稱（{:.0%}）", url, company_ratio)
        return None

    contact_fields = {"email", "phone", "fax", "address", "website"} & result.fields.keys()

    # 「像不像公司名」擺在最前面且權重最重；其次才是筆數、聯絡欄位、頁數。
    score = (
        company_ratio * 60.0
        + min(result.item_count, 100) * 0.5
        + len(contact_fields) * 8.0
        + min(result.page_count, 50) * 0.5
        + len(result.fields) * 2.0
    )
    return DirectoryCandidate(
        url=url,
        item_count=result.item_count,
        fields=sorted(result.fields),
        page_count=result.page_count,
        sample_names=[r.company_name for r in result.preview[:3]],
        company_name_ratio=company_ratio,
        score=score,
    )


def explore(
    start_url: str,
    config: AppConfig | None = None,
    fetcher: BaseFetcher | None = None,
    page_budget: int = DEFAULT_PAGE_BUDGET,
    target_candidates: int = DEFAULT_TARGET_CANDIDATES,
    on_progress=None,
    cancel_event=None,
    document_kinds=(),
) -> ExploreResult:
    """在 ``start_url`` 所屬的網站裡找出廠商名錄頁。

    ``page_budget`` 是**硬上限**：最多送出這麼多次頁面請求。它同時決定要等
    多久（每次請求之間有禮貌延遲），所以呼叫端一定要讓使用者看得到這個數字。

    ``on_progress`` 會以 ``(已讀頁數, 上限, 目前網址)`` 被呼叫，用來更新進度。
    """
    config = config or get_config()
    start_url = start_url.strip()
    if not start_url:
        raise CrawlError("請輸入網址。")
    if "://" not in start_url:
        start_url = "https://" + start_url

    owned = fetcher is None
    fetcher = fetcher or build_fetcher(config)
    result = ExploreResult(start_url=start_url)

    try:
        _explore_into(
            result, fetcher, start_url, page_budget, target_candidates,
            on_progress, cancel_event, tuple(document_kinds or ()),
        )
    finally:
        if owned:
            fetcher.close()

    result.candidates.sort(key=lambda c: c.score, reverse=True)
    _add_notes(result, page_budget)
    return result


def _explore_into(
    result: ExploreResult,
    fetcher: BaseFetcher,
    start_url: str,
    page_budget: int,
    target_candidates: int,
    on_progress,
    cancel_event=None,
    document_kinds: tuple[str, ...] = (),
) -> None:
    cancelled = False
    seen: set[str] = set()
    seen_pagination: set[str] = set()
    # (優先分數, 網址)。分數高的先走。
    queue: list[tuple[float, str]] = [(1000.0, start_url)]

    for url in _sitemap_seeds(fetcher, start_url, result):
        if _is_html_url(url) and same_site(url, start_url):
            queue.append((link_priority(url), url))

    while queue and result.pages_fetched < page_budget:
        if len(result.candidates) >= target_candidates:
            break
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            log.info("站內探索被取消，已讀 {} 頁", result.pages_fetched)
            break

        queue.sort(key=lambda item: item[0], reverse=True)
        _priority, url = queue.pop(0)

        key = normalise_url(url)
        if key in seen:
            continue
        seen.add(key)

        # 同一個名錄的第 2、3 頁不必再分析一次。
        page_key = _pagination_key(key)
        if page_key in seen_pagination:
            continue

        if on_progress is not None:
            on_progress(result.pages_fetched, page_budget, url)

        # 名冊常常是掛在子頁面上的一個 PDF／Excel 連結，而不是一個網頁。
        # 使用者勾了格式才會走到這裡。
        if is_wanted(url, document_kinds):
            result.pages_fetched += 1
            candidate = _document_candidate(fetcher, url)
            if candidate is not None:
                result.candidates.append(candidate)
                seen_pagination.add(page_key)
            continue

        html = _fetch_page(fetcher, url)
        result.pages_fetched += 1
        if html is None:
            continue

        try:
            discovered = discover_from_html(html, url)
        except Exception as exc:  # 一頁壞掉不該讓整趟探索中止
            log.debug("分析 {} 失敗：{}", url, exc)
            continue

        candidate = _to_candidate(url, discovered)
        if candidate is not None:
            result.candidates.append(candidate)
            seen_pagination.add(page_key)
            log.info("找到名錄頁 {}（{} 筆）", url, candidate.item_count)
            # 名錄頁裡面的連結都是各家公司的明細頁，跟進去對「找名錄」沒有幫助。
            continue

        for link_url, anchor in _links(html, url):
            if not same_site(link_url, start_url):
                continue
            # 檔案連結只有在使用者勾了那個格式時才進佇列。
            if not _is_html_url(link_url) and not is_wanted(link_url, document_kinds):
                continue
            link_key = normalise_url(link_url)
            if link_key in seen:
                continue
            priority = link_priority(link_url, anchor)
            if priority < 0:
                continue
            queue.append((priority, link_url))

    result.cancelled = cancelled
    result.hit_budget = (
        not cancelled and result.pages_fetched >= page_budget and bool(queue)
    )


def _links(html: str, base_url: str) -> list[tuple[str, str]]:
    """頁面上的 ``(絕對網址, 連結文字)``。"""
    soup = make_soup(html)
    found: list[tuple[str, str]] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not isinstance(href, str) or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        found.append((urljoin(base_url, href), anchor.get_text(" ", strip=True)[:60]))
    return found


def _sitemap_seeds(fetcher: BaseFetcher, start_url: str, result: ExploreResult) -> list[str]:
    """從 robots.txt 公告的 sitemap 取出候選網址。

    Sitemap 抓取不計入頁面預算：它是網站自己準備給機器看的索引，讀它正是
    對方希望我們做的事，而且一次就能取代幾十次的連結追蹤。
    """
    try:
        sitemaps = fetcher.robots.sitemaps(start_url)
    except Exception:
        sitemaps = []

    if not sitemaps:
        return []

    collected: list[str] = []
    pending = list(sitemaps[:5])
    seen_sitemaps: set[str] = set()

    while pending and len(collected) < _MAX_SITEMAP_URLS:
        sitemap_url = pending.pop(0)
        if sitemap_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sitemap_url)

        xml = _fetch_page(fetcher, sitemap_url)
        if not xml:
            continue
        pages, nested = _sitemap_urls(xml, sitemap_url)
        collected.extend(pages)
        # 巢狀 sitemap 只再往下一層，避免大型網站無止盡展開。
        pending.extend(n for n in nested[:5] if n not in seen_sitemaps)

    if collected:
        result.notes.append(
            f"這個網站有 sitemap，讀到 {len(collected)} 個網址，"
            "會優先從裡面看起來像名錄的頁面開始找。"
        )
    # 只留看起來有希望的，其餘丟掉——sitemap 動輒上萬筆，全部排進佇列沒有意義。
    return sorted(
        (u for u in collected if link_priority(u) > 0),
        key=link_priority,
        reverse=True,
    )[:200]


def _add_notes(result: ExploreResult, page_budget: int) -> None:
    if result.cancelled:
        result.notes.append(
            f"已停止。停下來之前讀了 {result.pages_fetched} 頁，"
            f"找到 {len(result.candidates)} 個名錄頁。"
        )
        return

    if not result.candidates:
        result.notes.append(
            f"讀了 {result.pages_fetched} 頁，沒有找到看起來像廠商名錄的頁面。"
            "名錄可能需要先登入或送出查詢表單，也可能是用 JavaScript 產生的——"
            "找得到那一頁的話，用「＋ 自訂網址…」直接貼上網址會比較快。"
        )
        return

    best = result.candidates[0]
    result.notes.append(
        f"讀了 {result.pages_fetched} 頁，找到 {len(result.candidates)} 個名錄頁。"
        f"最完整的是每頁 {best.item_count} 筆"
        + (f"、共 {best.page_count} 頁" if best.page_count > 1 else "")
        + "。"
    )
    if result.hit_budget:
        result.notes.append(
            f"已達 {page_budget} 頁的讀取上限就停下來了。把上限調高可以找得更完整，"
            "但也會多花時間、多打擾對方的網站。"
        )
