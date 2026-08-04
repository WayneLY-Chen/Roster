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
from dataclasses import dataclass, field
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


def _decode_body(response: httpx.Response, encoding: str | None) -> str:
    """依指定編碼解碼回應內容；未指定時交給 httpx 自己判斷（標頭或自動偵測）。"""
    if not encoding:
        return response.text
    codec = _ENCODING_ALIASES.get(encoding.lower(), encoding)
    return response.content.decode(codec, errors="replace")


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


@dataclass(slots=True)
class FetchResult:
    """One retrieved page."""

    url: str
    status_code: int
    html: str
    elapsed: float = 0.0
    from_cache: bool = False

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
        result = retryer(self._fetch_once, url, method=method, data=data, encoding=encoding)
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
    ) -> FetchResult:
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
            return FetchResult(url=page.url, status_code=status or 200, html=page.content())
        except PlaywrightTimeout as exc:
            raise TransientFetchError(f"timeout loading {url}") from exc
        except PlaywrightError as exc:
            raise TransientFetchError(f"browser error loading {url}: {exc}") from exc
        finally:
            page.close()

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
    config: AppConfig | None = None, robots: RobotsPolicy | None = None
) -> BaseFetcher:
    """Instantiate the engine named in ``crawler.engine``."""
    config = config or get_config()
    if config.crawler.engine == "playwright":
        return PlaywrightFetcher(config, robots)
    return HttpxFetcher(config, robots)
