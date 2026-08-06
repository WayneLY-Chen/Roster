"""Page fetching: rate limiting, retries, and two interchangeable engines.

``HttpxFetcher`` handles server-rendered pages and is the default -- it is an
order of magnitude cheaper on both our side and the site's.
``PlaywrightFetcher`` is for directories that render their listings in
JavaScript.

Both enforce the same contract: robots.txt is consulted before every request,
and a polite delay separates requests to the same host.
"""

from __future__ import annotations

import random
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import httpx
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.config import AppConfig, get_config
from core.constants import LogCategory
from core.errors import CrawlError, RobotsDisallowedError
from core.logging_setup import get_logger
from crawler.robots import RobotsPolicy

log = get_logger(LogCategory.CRAWL)

# Status codes worth trying again; everything else is a settled answer.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

# 部分台灣網站宣告的 Big5 其實不是嚴格標準 Big5（罕見姓名用字、擴充符號常
# 超出範圍），因此指定 "big5" 時一律改用其超集 big5hkscs 解碼，涵蓋範圍更廣
# 且對標準 Big5 完全相容；同時一律加上 errors="replace"，讓少數解不出來的
# 字元變成替代字元，而不是讓整頁解碼失敗、白白浪費一次請求。
_ENCODING_ALIASES = {"big5": "big5hkscs"}


def decode_bytes(raw: bytes, encoding: str) -> str:
    """以指定編碼解碼位元組，未知編碼時退回 UTF-8 而不是讓整頁失敗。"""
    codec = _ENCODING_ALIASES.get(encoding.lower(), encoding)
    try:
        return raw.decode(codec, errors="replace")
    except LookupError:
        log.warning("unknown encoding {!r}; falling back to utf-8", encoding)
        return raw.decode("utf-8", errors="replace")


def _decode_body(response: httpx.Response, encoding: str | None) -> str:
    """依指定編碼解碼回應內容；未指定時交給 httpx 自己判斷（標頭或自動偵測）。"""
    if not encoding:
        return response.text
    return decode_bytes(response.content, encoding)


def _encode_form_body(data: dict[str, str], encoding: str) -> bytes:
    """依指定編碼組出 x-www-form-urlencoded 的請求內容（位元組）。

    老舊的 Big5 站台（多半是舊版 ASP/PHP）通常整頁——包含表單提交——都只認
    同一種編碼；httpx 的 ``data=`` 參數固定以 UTF-8 編碼中文字，對這類站台
    送出查詢關鍵字會變成亂碼、查不到資料。指定 ``encoding`` 時改用同一套
    編碼組出請求內容，讓查詢字串與頁面本身用的是同一種編碼。
    """
    codec = _ENCODING_ALIASES.get(encoding.lower(), encoding)
    return urlencode(data, encoding=codec).encode("ascii")


class TransientFetchError(CrawlError):
    """A failure that may resolve on retry (timeout, 503, connection reset)."""


#: 一次 ``click_all`` 最多按幾個元素。一頁 200 列、每列一顆「顯示電話」是常
#: 態，全部按完要花不少時間，但如果一頁有上千個相符元素，那多半是選擇器寫錯了。
MAX_CLICKS_PER_ACTION = 300


def _run_page_actions(page: Any, actions: Sequence[Any]) -> None:
    """在擷取之前，把使用者設定的動作在頁面上做一遍。

    每一個動作都獨立處理失敗：「同意 cookie」那顆按鈕第二頁就不會再出現，
    那是正常的，不該讓整趟爬取停下來。只有標了 ``required`` 的動作失敗才會
    往上丟——那代表使用者說「沒做到這件事，這一頁的資料就是不完整的」。
    """
    for action in actions:
        try:
            _run_one_action(page, action)
        except CrawlError:
            raise
        except Exception as exc:
            if getattr(action, "required", False):
                raise CrawlError(
                    f"頁面動作 {action.type}（{action.selector}）失敗：{exc}"
                ) from exc
            log.debug("頁面動作 {} 略過：{}", action.type, exc)


def _run_one_action(page: Any, action: Any) -> None:
    wait_ms = getattr(action, "wait_ms", 400)
    times = getattr(action, "times", 1)
    selector = (getattr(action, "selector", None) or "").strip()

    if action.type == "wait":
        page.wait_for_timeout(wait_ms)
        return

    if action.type == "scroll":
        for _ in range(times):
            page.mouse.wheel(0, 20_000)
            page.wait_for_timeout(wait_ms)
        return

    if action.type == "click":
        for _ in range(times):
            element = page.query_selector(selector)
            if element is None:
                break          # 「載入更多」按完就消失了，那是做完了不是失敗
            element.click()
            page.wait_for_timeout(wait_ms)
        return

    if action.type == "click_all":
        elements = page.query_selector_all(selector)[:MAX_CLICKS_PER_ACTION]
        for element in elements:
            try:
                element.click()
            except Exception as exc:
                # 一顆按不動（被蓋住、已經展開）不該讓其餘 199 顆都不按。
                log.debug("click_all 有一個元素按不動：{}", exc)
        if elements:
            page.wait_for_timeout(wait_ms)
        return

    log.warning("不認得的頁面動作：{}", action.type)


# ---------------------------------------------------------------- 逐項查詢


def _is_a_select(page: Any, selector: str) -> bool:
    try:
        tag = page.eval_on_selector(selector, "el => el.tagName.toLowerCase()")
    except Exception:                       # noqa: BLE001 - 找不到元素
        return False
    return str(tag).lower() == "select"


def _option_values(page: Any, selector: str) -> list[str]:
    """下拉選單裡每一個真正可以查的選項值。

    第一個通常是「--請選擇--」，值是空的；那不是一個查詢條件，是提示文字。
    """
    if not _is_a_select(page, selector):
        return []
    try:
        values = page.eval_on_selector_all(
            f"{selector} option", "els => els.map(e => e.value)"
        )
    except Exception as exc:                # noqa: BLE001
        log.warning("讀不到 {} 的選項：{}", selector, exc)
        return []
    return [str(v) for v in (values or []) if str(v).strip()]


def _close_modal(page: Any, modal: Any) -> None:
    close_selector = (getattr(modal, "close_selector", None) or "").strip()
    if close_selector:
        button = page.query_selector(close_selector)
        if button is not None:
            button.click()
            page.wait_for_timeout(200)
            return
    # 沒指定關閉鈕就按 Esc。多數彈出視窗都吃這一招，而且不會誤按到別的東西。
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)


def _collect_modal_details(page: Any, modal: Any, list_selector: str) -> list[str]:
    """把每一列點開，讀出小視窗裡的內容。

    回傳的順序與清單上的每一列**一一對應**，點不開的那一筆留一個空字串。
    這件事非做不可：資料是靠位置對回去的，失敗時如果直接跳過不放，後面每一
    筆的聯絡資訊都會錯位到別人家去——而且看起來完全正常。
    """
    rows = page.query_selector_all(list_selector)[: modal.max_rows]
    details: list[str] = []
    for index, row in enumerate(rows):
        try:
            target = row.query_selector(modal.click_selector)
            if target is None:
                details.append("")
                continue
            target.click()
            page.wait_for_timeout(modal.wait_ms)
            panel = page.query_selector(modal.panel_selector)
            details.append(panel.inner_html() if panel is not None else "")
            _close_modal(page, modal)
        except Exception as exc:              # noqa: BLE001 - 一筆點不開
            log.debug("第 {} 列的詳細視窗打不開：{}", index + 1, exc)
            details.append("")
    return details


def _submit_one_query(page: Any, loop: Any, value: str) -> None:
    """填一個條件並按下查詢。"""
    selector = loop.input_selector
    if _is_a_select(page, selector):
        # force=True 是必要的。很多網站把原生的 <select> 藏起來，畫面上那個好看
        # 的下拉是自己用 div 做的——原生的那個永遠「看不見」，不加這個參數會一直
        # 等到逾時。我們要改的本來就是原生元素的值，選好之後照樣會送出 change
        # 事件，網站的程式收得到。
        page.select_option(selector, value, force=True)
    else:
        page.fill(selector, value, force=True)

    button = page.query_selector(loop.submit_selector)
    if button is None:
        raise CrawlError(f"找不到查詢按鈕：{loop.submit_selector}")
    _click_even_if_hidden(button)
    page.wait_for_timeout(loop.wait_ms)


def _click_even_if_hidden(element: Any) -> None:
    """按下去，元素被藏起來也要按到。

    查詢頁常常做成分頁籤，沒被選到的那一頁是 ``display:none``——裡面的按鈕在
    畫面上不存在，一般的點擊會一直等到逾時。改用送出 click 事件的方式，網站
    自己的程式收到的東西是一樣的。
    """
    try:
        element.click()
    except Exception as exc:                  # noqa: BLE001
        log.debug("一般點擊失敗，改用送出事件的方式：{}", exc)
        element.dispatch_event("click")


@dataclass(slots=True)
class FetchResult:
    """One retrieved page."""

    url: str
    status_code: int
    html: str
    elapsed: float = 0.0
    from_cache: bool = False
    #: 未經解碼的原始回應內容。分析網址時用得到：頁面可能在 HTML 裡宣告了
    #: 一個跟 HTTP 標頭不同的編碼（老舊的 Big5 站台幾乎都是這樣），留著原始
    #: 位元組就能直接換編碼重解一次，不必為了同一頁再送一次請求。
    #: Playwright 引擎沒有這個東西——它交出來的是瀏覽器解碼後的 DOM。
    raw: bytes = b""
    #: 逐列點開的小視窗內容，順序與清單上的每一列一一對應。
    #: 只有來源設了 ``detail_modal`` 時才會有東西。
    details: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


@dataclass
class RateLimiter:
    """Enforces a minimum gap between requests, with jitter.

    Jitter matters: a perfectly periodic crawler looks like an attack to rate
    limiters, and it synchronizes badly with server-side bucket refills.
    """

    delay: float = 2.0
    jitter: float = 0.5
    _last_request: float = field(default=0.0, init=False)

    def wait(self, minimum: float | None = None) -> float:
        """Sleep until the next request is due. Returns seconds slept."""
        target = max(self.delay, minimum or 0.0)
        if target <= 0:
            self._last_request = time.monotonic()
            return 0.0

        target += random.uniform(0, self.jitter)
        elapsed = time.monotonic() - self._last_request
        remaining = target - elapsed
        if self._last_request and remaining > 0:
            time.sleep(remaining)
            slept = remaining
        else:
            slept = 0.0
        self._last_request = time.monotonic()
        return slept


class BaseFetcher(ABC):
    """Common retry, rate-limit and robots handling for both engines."""

    def __init__(self, config: AppConfig | None = None, robots: RobotsPolicy | None = None) -> None:
        self.config = config or get_config()
        self.user_agent = self.config.crawler.resolved_user_agent()
        self.robots = robots or RobotsPolicy(
            user_agent=self.user_agent,
            timeout=self.config.crawler.request_timeout,
            enabled=self.config.crawler.respect_robots,
        )
        self.limiter = RateLimiter(
            delay=self.config.crawler.delay_seconds,
            jitter=self.config.crawler.delay_jitter,
        )

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        data: dict[str, str] | None = None,
        encoding: str | None = None,
        actions: Sequence[Any] = (),
        modal: Any = None,
        list_selector: str | None = None,
    ) -> FetchResult:
        """Fetch one URL, honouring robots.txt, the delay, and the retry budget.

        ``method``/``data`` cover POST-only search forms (robots.txt is still
        consulted first, exactly as for GET); ``encoding`` forces decoding the
        response with a specific charset for sites whose headers are wrong or
        absent, and -- for POST -- also encodes the outgoing form body with
        the same charset, since old same-charset-both-ways sites often reject
        (or silently mismatch) a UTF-8 query string.
        """
        if not self.robots.can_fetch(url):
            raise RobotsDisallowedError(url, self.user_agent)

        self.limiter.wait(minimum=self.robots.crawl_delay(url))

        retryer = Retrying(
            stop=stop_after_attempt(self.config.crawler.max_retries + 1),
            wait=wait_exponential(
                multiplier=self.config.crawler.retry_backoff, min=1, max=60
            ),
            retry=retry_if_exception_type(TransientFetchError),
            reraise=True,
            before_sleep=lambda state: log.warning(
                "retry {}/{} for {}: {}",
                state.attempt_number,
                self.config.crawler.max_retries,
                url,
                state.outcome.exception() if state.outcome else "unknown",
            ),
        )
        started = time.monotonic()
        result = retryer(
            self._fetch_once, url, method=method, data=data,
            encoding=encoding, actions=actions,
            modal=modal, list_selector=list_selector,
        )
        result.elapsed = time.monotonic() - started
        log.debug("fetched {} [{}] in {:.2f}s", url, result.status_code, result.elapsed)
        return result

    @abstractmethod
    def _fetch_once(
        self,
        url: str,
        *,
        method: str = "GET",
        data: dict[str, str] | None = None,
        encoding: str | None = None,
        actions: Sequence[Any] = (),
        modal: Any = None,
        list_selector: str | None = None,
    ) -> FetchResult:
        """Single attempt. Raise :class:`TransientFetchError` to trigger retry."""

    def close(self) -> None:
        self.robots.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class HttpxFetcher(BaseFetcher):
    """HTTP fetcher for server-rendered pages."""

    def __init__(
        self,
        config: AppConfig | None = None,
        robots: RobotsPolicy | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(config, robots)
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=self.config.crawler.request_timeout,
            follow_redirects=True,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            },
        )

    def _fetch_once(
        self,
        url: str,
        *,
        method: str = "GET",
        data: dict[str, str] | None = None,
        encoding: str | None = None,
        actions: Sequence[Any] = (),
        modal: Any = None,
        list_selector: str | None = None,
    ) -> FetchResult:
        if modal is not None:
            # 安靜地忽略等於使用者永遠看不到「怎麼還是沒有電話」的原因。
            log.warning(
                "這個來源要點開小視窗才看得到詳細資料，但取頁面的方式是 httpx，"
                "點不了。這個來源的引擎要設成 playwright。"
            )
        try:
            if method == "POST":
                # 表單以 application/x-www-form-urlencoded 送出，這是傳統
                # ASP/PHP 查詢頁最常見的格式。有指定 encoding 時，連同送出
                # 的表單內容也用同一種編碼組——像 TCA 這類 Big5 年代的舊站，
                # 查詢關鍵字若用 httpx 預設的 UTF-8 送出會直接查不到資料，
                # 因為伺服器是拿 Big5 位元組去比對資料庫。沒指定 encoding
                # 時維持原本用 httpx 預設（UTF-8）的行為，不影響既有來源。
                if encoding:
                    response = self._client.post(
                        url,
                        content=_encode_form_body(data or {}, encoding),
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
                else:
                    response = self._client.post(url, data=data or {})
            else:
                response = self._client.get(url)
        except httpx.TimeoutException as exc:
            raise TransientFetchError(f"timeout fetching {url}") from exc
        except httpx.TransportError as exc:
            raise TransientFetchError(f"transport error fetching {url}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise CrawlError(f"failed to fetch {url}: {exc}") from exc

        if response.status_code in _RETRYABLE_STATUS:
            self._honour_retry_after(response)
            raise TransientFetchError(f"{url} returned {response.status_code}")

        if response.status_code >= 400:
            raise CrawlError(f"{url} returned {response.status_code}")

        return FetchResult(
            url=str(response.url),
            status_code=response.status_code,
            html=_decode_body(response, encoding),
            raw=response.content,
        )

    @staticmethod
    def _honour_retry_after(response: httpx.Response) -> None:
        """Sleep for a server-specified Retry-After, capped so we never hang."""
        header = response.headers.get("Retry-After")
        if not header:
            return
        try:
            seconds = float(header)
        except ValueError:
            return  # HTTP-date form; the exponential backoff covers it
        wait = min(max(seconds, 0.0), 60.0)
        log.info("server asked us to wait {:.0f}s before retrying", wait)
        time.sleep(wait)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
        super().close()


class PlaywrightFetcher(BaseFetcher):
    """Headless-browser fetcher for JavaScript-rendered listings.

    Requires a one-off browser download::

        python -m playwright install chromium
    """

    def __init__(self, config: AppConfig | None = None, robots: RobotsPolicy | None = None) -> None:
        super().__init__(config, robots)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise CrawlError(
                "playwright is not installed; run: pip install playwright"
            ) from exc

        self._settings = self.config.crawler.playwright
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=self._settings.headless
            )
        except Exception as exc:
            raise CrawlError(
                "could not start Chromium. Install the browser once with: "
                f"python -m playwright install chromium ({exc})"
            ) from exc

        self._context = self._browser.new_context(
            user_agent=self.user_agent,
            locale="zh-TW",
        )
        self._context.set_default_navigation_timeout(self._settings.nav_timeout_ms)

    def _fetch_once(
        self,
        url: str,
        *,
        method: str = "GET",
        data: dict[str, str] | None = None,
        encoding: str | None = None,
        actions: Sequence[Any] = (),
        modal: Any = None,
        list_selector: str | None = None,
    ) -> FetchResult:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        if method != "GET":
            # 瀏覽器只會「導覽」到網址；沒有簡單的方式送出任意 POST 表單並
            # 維持一般的頁面渲染流程，所以需要 POST 表單的來源請改用
            # engine: httpx。
            raise CrawlError(
                "Playwright 引擎不支援 method='POST'；此來源請改用 engine: httpx"
            )

        page = self._context.new_page()
        try:
            response = page.goto(url, wait_until=self._settings.wait_until)
            status = response.status if response else 0
            if status in _RETRYABLE_STATUS:
                raise TransientFetchError(f"{url} returned {status}")
            if status >= 400:
                raise CrawlError(f"{url} returned {status}")
            if actions:
                _run_page_actions(page, actions)
            details = (
                _collect_modal_details(page, modal, list_selector or "")
                if modal is not None and list_selector
                else []
            )
            return FetchResult(
                url=page.url,
                status_code=status or 200,
                html=page.content(),
                details=details,
            )
        except PlaywrightTimeout as exc:
            raise TransientFetchError(f"timeout loading {url}") from exc
        except PlaywrightError as exc:
            raise TransientFetchError(f"browser error loading {url}: {exc}") from exc
        finally:
            page.close()

    # --------------------------------------------------------- 逐項查詢

    def fetch_with_first_query(
        self,
        url: str,
        input_selector: str,
        submit_selector: str,
        *,
        value: str | None = None,
        drill_row_selector: str | None = None,
        drill_click_selector: str = "a",
    ) -> FetchResult:
        """開頁面、送出**一次**查詢，回傳結果的 HTML。

        分析查詢型名錄時用得到：還沒查詢的頁面上一筆資料都沒有，只看那一頁是
        猜不出「一筆資料長什麼樣」的。送一次查詢再看，跟人打開網頁隨便選一個
        分類按下去是完全一樣的動作。

        ``drill_row_selector`` 再往下點一層：有些網站查出來的是子分類，點其中
        一項才是廠商。給了就會點第一列，回傳那之後的頁面。
        """
        if not self.robots.can_fetch(url):
            raise RobotsDisallowedError(url, self.user_agent)

        page = self._context.new_page()
        try:
            self.limiter.wait(minimum=self.robots.crawl_delay(url))
            page.goto(url, wait_until=self._settings.wait_until)

            chosen = value
            if chosen is None:
                options = _option_values(page, input_selector)
                if not options:
                    raise CrawlError(f"{input_selector} 沒有可以選的選項")
                chosen = options[0]

            loop = SimpleNamespace(
                input_selector=input_selector,
                submit_selector=submit_selector,
                wait_ms=1500,
            )
            self.limiter.wait(minimum=self.robots.crawl_delay(url))
            _submit_one_query(page, loop, chosen)

            if drill_row_selector:
                rows = page.query_selector_all(drill_row_selector)
                target = None
                for row in rows:
                    target = row.query_selector(drill_click_selector)
                    if target is not None:
                        break
                if target is None:
                    raise CrawlError(f"{drill_row_selector} 裡面沒有可以點的東西")
                self.limiter.wait(minimum=self.robots.crawl_delay(url))
                _click_even_if_hidden(target)
                page.wait_for_timeout(1500)

            return FetchResult(url=page.url, status_code=200, html=page.content())
        finally:
            page.close()

    def iter_query_pages(
        self,
        url: str,
        loop: Any,
        *,
        actions: Sequence[Any] = (),
        cancel_event: Any = None,
        modal: Any = None,
        list_selector: str | None = None,
    ) -> Iterator[FetchResult]:
        """開一次頁面，把每一組查詢條件各查一次，每查一次交出一份結果 HTML。

        為什麼是一個產生器而不是「查完全部再回傳」：一輪就是幾百家公司，全部
        累積在記憶體裡等到最後才處理，中途取消或出錯就整批損失。

        整段只導覽一次網址——查詢是在同一個頁面裡進行的，這也是它比「一頁一次
        請求」對別人的伺服器更客氣的地方。每一輪之間仍然照設定的間隔等待。
        """
        if not self.robots.can_fetch(url):
            raise RobotsDisallowedError(url, self.user_agent)

        page = self._context.new_page()
        try:
            self.limiter.wait(minimum=self.robots.crawl_delay(url))
            page.goto(url, wait_until=self._settings.wait_until)
            if actions:
                _run_page_actions(page, actions)

            values = list(loop.values) or _option_values(page, loop.input_selector)
            if not values:
                raise CrawlError(
                    f"逐項查詢找不到任何可以查的值：{loop.input_selector} "
                    "既不是下拉選單，來源也沒有指定要查哪些值。"
                )

            for value in values[: loop.max_queries]:
                if cancel_event is not None and cancel_event.is_set():
                    return
                self.limiter.wait(minimum=self.robots.crawl_delay(url))
                try:
                    _submit_one_query(page, loop, value)
                except Exception as exc:      # noqa: BLE001 - 一個條件查壞了
                    # 不要讓 98 個分類裡的第 7 個失敗，害其餘 91 個都收不到。
                    log.warning("逐項查詢「{}」失敗：{}", value, exc)
                    continue
                drill = getattr(loop, "drill", None)
                if drill is None:
                    yield self._snapshot(page, modal, list_selector)
                    continue

                # 查詢結果還不是名單，中間要再點一層。
                for index in range(drill.max_rows):
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    # 每一次都重新取一遍列：點下去之後整張表常常被重畫，
                    # 先存起來的那些元素會全部失效（stale）。
                    rows = page.query_selector_all(drill.row_selector)
                    if index >= len(rows):
                        break
                    self.limiter.wait(minimum=self.robots.crawl_delay(url))
                    try:
                        target = rows[index].query_selector(drill.click_selector)
                        if target is None:
                            continue
                        _click_even_if_hidden(target)
                        page.wait_for_timeout(drill.wait_ms)
                    except Exception as exc:  # noqa: BLE001 - 一列點不開
                        log.debug("往下點第 {} 列失敗：{}", index + 1, exc)
                        continue
                    yield self._snapshot(page, modal, list_selector)
        finally:
            page.close()

    def _snapshot(self, page: Any, modal: Any, list_selector: str | None) -> FetchResult:
        """把頁面目前的狀態交出去（需要的話連同每一列點開的小視窗）。"""
        details = (
            _collect_modal_details(page, modal, list_selector or "")
            if modal is not None and list_selector
            else []
        )
        return FetchResult(
            url=page.url, status_code=200, html=page.content(), details=details
        )

    def close(self) -> None:
        for closer in (
            getattr(self, "_context", None),
            getattr(self, "_browser", None),
        ):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # pragma: no cover - best-effort teardown
                pass
        try:
            if getattr(self, "_playwright", None) is not None:
                self._playwright.stop()
        except Exception:  # pragma: no cover
            pass
        super().close()


def build_fetcher(
    config: AppConfig | None = None,
    robots: RobotsPolicy | None = None,
    engine: str | None = None,
) -> BaseFetcher:
    """Instantiate a fetcher.

    ``engine`` 是這一個來源自己指定的引擎；留空才回頭看全域的
    ``crawler.engine``。「這個網站要不要用瀏覽器」是網站的性質，不是使用者的
    偏好設定——把它綁在來源上，一個需要瀏覽器的網站就不會拖慢其他所有來源。
    """
    config = config or get_config()
    chosen = engine or config.crawler.engine
    if chosen == "playwright":
        return PlaywrightFetcher(config, robots)
    return HttpxFetcher(config, robots)
