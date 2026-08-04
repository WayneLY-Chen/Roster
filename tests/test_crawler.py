"""Tests for the crawler package (rate limiting, parsing, robots, fetching,
the offline sample source, and the end-to-end pipeline)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

import crawler.fetcher as fetcher_module
from core.config import FieldRule
from core.errors import CrawlError, RobotsDisallowedError
from crawler.fetcher import HttpxFetcher, RateLimiter, TransientFetchError
from crawler.parser import (
    extract_field,
    find_next_url,
    harvest_emails,
    harvest_phones,
    make_soup,
    select_items,
)
from crawler.robots import RobotsPolicy
from crawler.sources.sample import TEMPLATES_DIR, SampleSource, sample_source_config
from database.repository import CompanyRepository


# ---------------------------------------------------------------- rate limiter


def test_rate_limiter_sleeps_for_the_remaining_gap(monkeypatch):
    clock = {"t": 1_000_000.0}

    def fake_monotonic() -> float:
        return clock["t"]

    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(fetcher_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(fetcher_module.time, "sleep", fake_sleep)
    monkeypatch.setattr(fetcher_module.random, "uniform", lambda a, b: 0.0)

    limiter = RateLimiter(delay=2.0, jitter=0.5)

    first = limiter.wait()
    assert first == 0.0
    assert sleeps == []

    clock["t"] += 0.1  # only 0.1s has "elapsed" before the next request
    second = limiter.wait()

    assert second == pytest.approx(1.9)
    assert sleeps == [pytest.approx(1.9)]


def test_rate_limiter_never_sleeps_when_delay_is_zero(monkeypatch):
    monkeypatch.setattr(fetcher_module.time, "sleep", lambda s: pytest.fail("must not sleep"))
    limiter = RateLimiter(delay=0.0, jitter=0.0)
    assert limiter.wait() == 0.0
    assert limiter.wait() == 0.0


def test_rate_limiter_honours_a_longer_robots_crawl_delay(monkeypatch):
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(fetcher_module.time, "monotonic", lambda: clock["t"])
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(fetcher_module.time, "sleep", fake_sleep)
    monkeypatch.setattr(fetcher_module.random, "uniform", lambda a, b: 0.0)

    limiter = RateLimiter(delay=1.0, jitter=0.0)
    limiter.wait()
    clock["t"] += 0.05
    limiter.wait(minimum=5.0)

    assert sleeps == [pytest.approx(4.95)]


# ------------------------------------------------------------------ parser.py


@pytest.fixture
def fixture_soup():
    html = (Path(TEMPLATES_DIR) / "sample_directory_page1.html").read_text(encoding="utf-8")
    return make_soup(html)


def test_select_items_returns_every_list_item(fixture_soup):
    items = select_items(fixture_soup, "div.company-card")
    assert len(items) == 5


def test_select_items_invalid_selector_returns_empty_list(fixture_soup):
    assert select_items(fixture_soup, "div[") == []


def test_extract_field_text(fixture_soup):
    item = select_items(fixture_soup, "div.company-card")[0]
    assert extract_field(item, FieldRule(selector="h3.name")) == "宏達精密機械股份有限公司"


def test_extract_field_attr(fixture_soup):
    item = select_items(fixture_soup, "div.company-card")[0]
    value = extract_field(item, FieldRule(selector="a.site", attr="href"))
    assert value == "http://www.hongda-precision.com.tw/?utm_source=directory"


def test_extract_field_regex_capture_group(fixture_soup):
    item = select_items(fixture_soup, "div.company-card")[0]
    value = extract_field(item, FieldRule(selector="span.tax-id", regex=r"(\d{8})"))
    assert value == "69081935"


def test_extract_field_multiple_joins_with_separator():
    soup = make_soup(
        '<div class="item"><span class="a">1</span><span class="a">2</span></div>'
    )
    value = extract_field(soup, FieldRule(selector="span.a", multiple=True, separator=", "))
    assert value == "1, 2"


def test_extract_field_returns_none_when_selector_matches_nothing(fixture_soup):
    item = select_items(fixture_soup, "div.company-card")[0]
    assert extract_field(item, FieldRule(selector="span.does-not-exist")) is None


def test_extract_field_attr_html_returns_inner_markup():
    soup = make_soup('<div class="item"><p class="n">Hello <b>World</b></p></div>')
    value = extract_field(soup, FieldRule(selector="p.n", attr="html"))
    assert value == "Hello <b>World</b>"


def test_extract_field_attr_returning_a_list_is_space_joined():
    soup = make_soup('<div class="item"><span class="a b c">x</span></div>')
    value = extract_field(soup, FieldRule(selector="span", attr="class"))
    assert value == "a b c"


def test_extract_field_invalid_selector_returns_none():
    soup = make_soup('<div class="item"><span>x</span></div>')
    assert extract_field(soup, FieldRule(selector="div[")) is None


def test_extract_field_skips_empty_values_among_multiple_matches():
    soup = make_soup(
        '<div><span class="a"></span><span class="a">real</span></div>'
    )
    value = extract_field(soup, FieldRule(selector="span.a", multiple=True))
    assert value == "real"


def test_extract_field_resolves_href_against_base_url():
    soup = make_soup('<div><a class="link" href="/next">next</a></div>')
    value = extract_field(
        soup, FieldRule(selector="a.link", attr="href"), base_url="https://example.test/base/"
    )
    assert value == "https://example.test/next"


def test_extract_field_regex_no_match_is_skipped():
    soup = make_soup('<div><span class="a">no digits here</span></div>')
    value = extract_field(soup, FieldRule(selector="span.a", regex=r"\d+"))
    assert value is None


def test_page_title_returns_title_text():
    from crawler.parser import page_title

    soup = make_soup("<html><head><title>  My Page  </title></head><body></body></html>")
    assert page_title(soup) == "My Page"


def test_page_title_none_when_missing():
    from crawler.parser import page_title

    soup = make_soup("<html><body>no title here</body></html>")
    assert page_title(soup) is None


def test_find_next_url_resolves_relative_href(fixture_soup):
    url = find_next_url(fixture_soup, "a.next-page", "https://example.test/base/page1.html")
    assert url == "https://example.test/base/sample_directory_page2.html"


def test_find_next_url_none_for_hash_only_or_missing_node():
    soup = make_soup('<a class="next" href="#">x</a><a class="broken">no href</a>')
    assert find_next_url(soup, "a.next", "https://example.test/") is None
    assert find_next_url(soup, "a.broken", "https://example.test/") is None
    assert find_next_url(soup, "a.nonexistent", "https://example.test/") is None


def test_harvest_emails_prefers_mailto_then_body_text():
    soup = make_soup(
        '<div><a href="mailto:sales@example.com">mail us</a>'
        "<p>or write to info@example.com directly</p></div>"
    )
    assert harvest_emails(soup) == ["sales@example.com", "info@example.com"]


def test_harvest_phones_prefers_tel_then_body_text():
    soup = make_soup(
        '<div><a href="tel:+886-2-2723-1234">call</a><p>備用 02-8888-9999</p></div>'
    )
    phones = harvest_phones(soup)
    assert phones[0] == "+886-2-2723-1234"
    assert len(phones) >= 1


def test_harvest_phones_does_not_start_inside_a_longer_digit_run():
    """統一編號接著地址郵遞區號，不可以被讀成電話。

    TAMI 的列表就長這樣：'30878408 110台北市...'。少了數字邊界，比對會從統編
    中間的 0 切進去，得到 '0878408 110'——190 筆裡有 37 筆這樣誤判。
    """
    soup = make_soup("<tr><td>台灣範例股份有限公司</td><td>30878408</td><td>110台北市信義區</td></tr>")
    assert harvest_phones(soup) == []


def test_harvest_phones_still_finds_a_number_next_to_other_fields():
    """加了邊界之後，真的電話仍然要抓得到。"""
    soup = make_soup("<tr><td>台灣範例股份有限公司</td><td>30878408</td><td>02-2723-1234</td></tr>")
    assert harvest_phones(soup) == ["02-2723-1234"]


# ------------------------------------------------------------------- robots.py


class _StubResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _StubClient:
    """Fakes just the ``.get`` surface :class:`RobotsPolicy` relies on."""

    def __init__(self, response=None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.requested: list[str] = []

    def get(self, url: str):
        self.requested.append(url)
        if self._error is not None:
            raise self._error
        return self._response


def test_robots_policy_404_allows_crawling():
    policy = RobotsPolicy("test-agent", client=_StubClient(_StubResponse(404)))
    assert policy.can_fetch("https://example.test/anything") is True


def test_robots_policy_403_disallows_crawling():
    policy = RobotsPolicy("test-agent", client=_StubClient(_StubResponse(403)))
    assert policy.can_fetch("https://example.test/anything") is False


def test_robots_policy_500_disallows_crawling():
    policy = RobotsPolicy("test-agent", client=_StubClient(_StubResponse(500)))
    assert policy.can_fetch("https://example.test/anything") is False


def test_robots_policy_network_failure_disallows_crawling():
    error = httpx.ConnectError("boom")
    policy = RobotsPolicy("test-agent", client=_StubClient(error=error))
    assert policy.can_fetch("https://example.test/anything") is False


def test_robots_policy_parses_real_robots_txt_disallow_and_crawl_delay():
    body = "User-agent: *\nDisallow: /private\nCrawl-delay: 5\n"
    policy = RobotsPolicy("test-agent", client=_StubClient(_StubResponse(200, body)))
    assert policy.can_fetch("https://example.test/private/x") is False
    assert policy.can_fetch("https://example.test/public") is True
    assert policy.crawl_delay("https://example.test/public") == 5.0


def test_robots_policy_caches_per_origin():
    stub = _StubClient(_StubResponse(404))
    policy = RobotsPolicy("test-agent", client=stub)
    policy.can_fetch("https://example.test/a")
    policy.can_fetch("https://example.test/b")
    assert len(stub.requested) == 1  # second call served from the per-origin cache


def test_robots_policy_disabled_always_allows():
    policy = RobotsPolicy("test-agent", enabled=False, client=_StubClient(_StubResponse(403)))
    assert policy.can_fetch("https://example.test/anything") is True
    assert policy.crawl_delay("https://example.test/anything") is None


def test_robots_policy_close_disposes_an_owned_client():
    # No client injected -> RobotsPolicy builds and owns a real httpx.Client
    # (never used over the network here, just constructed and closed).
    policy = RobotsPolicy("test-agent")
    policy._http()  # force lazy construction
    assert policy._owns_client is True
    policy.close()
    assert policy._client is None


def test_robots_policy_context_manager_closes():
    with RobotsPolicy("test-agent") as policy:
        policy._http()
    assert policy._client is None


def test_robots_policy_close_leaves_an_injected_client_open():
    stub = _StubClient(_StubResponse(404))
    policy = RobotsPolicy("test-agent", client=stub)
    policy.close()
    assert policy._client is stub  # never owned, so never closed


def test_robots_policy_malformed_robots_txt_allows_crawling(monkeypatch):
    import crawler.robots as robots_module

    class ExplodingParser:
        def set_url(self, url):
            pass

        def parse(self, lines):
            raise ValueError("boom")

    monkeypatch.setattr(robots_module, "RobotFileParser", ExplodingParser)
    policy = RobotsPolicy("test-agent", client=_StubClient(_StubResponse(200, "garbage")))
    assert policy.can_fetch("https://example.test/anything") is True


# ------------------------------------------------------------------ fetcher.py


def _make_transport_fetcher(tmp_config, handler):
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://example.test")
    robots = RobotsPolicy("test-agent", enabled=False)
    return HttpxFetcher(config=tmp_config, robots=robots, client=client)


def test_httpx_fetcher_fetch_once_success(tmp_config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>ok</html>")

    fetcher = _make_transport_fetcher(tmp_config, handler)
    result = fetcher._fetch_once("https://example.test/ok")
    assert result.ok
    assert result.status_code == 200
    assert "ok" in result.html


def test_httpx_fetcher_fetch_once_retryable_status_raises_transient(tmp_config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="retry me")

    fetcher = _make_transport_fetcher(tmp_config, handler)
    with pytest.raises(TransientFetchError):
        fetcher._fetch_once("https://example.test/retry")


def test_httpx_fetcher_fetch_once_client_error_raises_crawl_error(tmp_config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="missing")

    fetcher = _make_transport_fetcher(tmp_config, handler)
    with pytest.raises(CrawlError):
        fetcher._fetch_once("https://example.test/missing")


def test_httpx_fetcher_fetch_once_timeout_raises_transient(tmp_config):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom", request=request)

    fetcher = _make_transport_fetcher(tmp_config, handler)
    with pytest.raises(TransientFetchError):
        fetcher._fetch_once("https://example.test/timeout")


def test_httpx_fetcher_fetch_once_transport_error_raises_transient(tmp_config):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    fetcher = _make_transport_fetcher(tmp_config, handler)
    with pytest.raises(TransientFetchError):
        fetcher._fetch_once("https://example.test/refused")


def test_httpx_fetcher_fetch_once_other_http_error_raises_crawl_error(tmp_config):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.DecodingError("could not decode response")

    fetcher = _make_transport_fetcher(tmp_config, handler)
    with pytest.raises(CrawlError):
        fetcher._fetch_once("https://example.test/bad-encoding")


def test_httpx_fetcher_fetch_once_retry_after_non_numeric_header_is_ignored(
    tmp_config, monkeypatch
):
    slept: list[float] = []
    monkeypatch.setattr(fetcher_module.time, "sleep", lambda s: slept.append(s))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503, text="wait", headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
        )

    fetcher = _make_transport_fetcher(tmp_config, handler)
    with pytest.raises(TransientFetchError):
        fetcher._fetch_once("https://example.test/retry")
    assert slept == []  # HTTP-date form is not parsed; no sleep happens here


def test_httpx_fetcher_close_only_closes_an_owned_client():
    injected_client = httpx.Client()
    fetcher = HttpxFetcher(
        robots=RobotsPolicy("ua", enabled=False), client=injected_client
    )
    fetcher.close()
    # An injected client is never this fetcher's to close.
    assert injected_client.is_closed is False
    injected_client.close()


def test_httpx_fetcher_fetch_once_retry_after_header_is_capped(tmp_config, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(fetcher_module.time, "sleep", lambda s: slept.append(s))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="wait", headers={"Retry-After": "500"})

    fetcher = _make_transport_fetcher(tmp_config, handler)
    with pytest.raises(TransientFetchError):
        fetcher._fetch_once("https://example.test/retry")
    assert slept == [60.0]  # capped at 60s even though the header said 500


def test_httpx_fetcher_fetch_raises_when_robots_disallows(tmp_config):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be called when robots.txt disallows the URL")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    robots = RobotsPolicy("test-agent", client=_StubClient(_StubResponse(403)))
    fetcher = HttpxFetcher(config=tmp_config, robots=robots, client=client)

    from core.errors import RobotsDisallowedError

    with pytest.raises(RobotsDisallowedError):
        fetcher.fetch("https://example.test/anything")


def test_httpx_fetcher_fetch_full_success_path(tmp_config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>ok</html>")

    fetcher = _make_transport_fetcher(tmp_config, handler)
    result = fetcher.fetch("https://example.test/ok")
    assert result.ok
    assert result.elapsed >= 0.0


def test_httpx_fetcher_fetch_retries_then_succeeds(tmp_config):
    config = tmp_config.model_copy(
        update={
            "crawler": tmp_config.crawler.model_copy(
                update={"max_retries": 2, "retry_backoff": 0.01}
            )
        }
    )
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 2:
            return httpx.Response(503, text="retry")
        return httpx.Response(200, text="ok now")

    fetcher = _make_transport_fetcher(config, handler)
    result = fetcher.fetch("https://example.test/flaky")
    assert result.ok
    assert attempts["n"] == 2


def test_httpx_fetcher_close_closes_owned_client_and_robots():
    fetcher = HttpxFetcher(robots=RobotsPolicy("ua", enabled=False))
    fetcher.close()  # must not raise, even with the default owned client/robots


def test_base_fetcher_context_manager_closes():
    fetcher = HttpxFetcher(robots=RobotsPolicy("ua", enabled=False))
    with fetcher as f:
        assert f is fetcher


# --------------------------------------------------- fetcher.py: POST + encoding


def test_httpx_fetcher_post_sends_form_urlencoded_body(tmp_config):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content.decode()
        return httpx.Response(200, text="<html>ok</html>")

    fetcher = _make_transport_fetcher(tmp_config, handler)
    result = fetcher.fetch(
        "https://example.test/search", method="POST", data={"keyword": "", "page": "1"}
    )

    assert result.ok
    assert captured["method"] == "POST"
    assert "application/x-www-form-urlencoded" in captured["content_type"]
    assert captured["body"] == "keyword=&page=1"


def test_httpx_fetcher_fetch_raises_when_robots_disallows_a_post(tmp_config):
    """robots.txt 檢查要涵蓋 POST，不能只擋 GET。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be called when robots.txt disallows the URL")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    robots = RobotsPolicy("test-agent", client=_StubClient(_StubResponse(403)))
    fetcher = HttpxFetcher(config=tmp_config, robots=robots, client=client)

    with pytest.raises(RobotsDisallowedError):
        fetcher.fetch("https://example.test/search", method="POST", data={"q": "x"})


def test_httpx_fetcher_encoding_option_forces_big5_decoding(tmp_config):
    text = "測試股份有限公司"
    raw = text.encode("big5")

    def handler(request: httpx.Request) -> httpx.Response:
        # 故意不宣告 charset，模擬伺服器沒有正確標示編碼的情況。
        return httpx.Response(200, content=raw, headers={"Content-Type": "text/html"})

    fetcher = _make_transport_fetcher(tmp_config, handler)
    result = fetcher.fetch("https://example.test/big5", encoding="big5")
    assert text in result.html


def test_httpx_fetcher_encoding_option_replaces_undecodable_bytes(tmp_config):
    """big5 對應到 big5hkscs 加 errors="replace"：解不出來的位元組不會讓整頁失敗。"""
    raw = b"prefix \xff\xfe suffix"  # 0xFF 不是合法的 Big5/Big5HKSCS 起始位元組

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=raw)

    fetcher = _make_transport_fetcher(tmp_config, handler)
    result = fetcher.fetch("https://example.test/bad-bytes", encoding="big5")
    assert result.ok
    assert "prefix" in result.html and "suffix" in result.html
    assert "�" in result.html


def test_httpx_fetcher_without_encoding_option_behaves_as_before(tmp_config):
    """沒有指定 encoding 時，行為要跟改動前一樣：交給 httpx 自己判斷。"""
    text = "測試股份有限公司"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=text)

    fetcher = _make_transport_fetcher(tmp_config, handler)
    result = fetcher.fetch("https://example.test/utf8")
    assert result.html == text


def test_build_fetcher_default_engine_is_httpx(tmp_config):
    from crawler.fetcher import build_fetcher

    fetcher = build_fetcher(tmp_config)
    try:
        assert isinstance(fetcher, HttpxFetcher)
    finally:
        fetcher.close()


def test_build_fetcher_dispatches_to_playwright_engine(tmp_config, monkeypatch):
    import crawler.fetcher as fetcher_module_local

    config = tmp_config.model_copy(
        update={"crawler": tmp_config.crawler.model_copy(update={"engine": "playwright"})}
    )

    sentinel = object()
    monkeypatch.setattr(
        fetcher_module_local, "PlaywrightFetcher", lambda cfg, robots: sentinel
    )
    assert fetcher_module_local.build_fetcher(config) is sentinel


# --------------------------------------------------------------- sample source


def test_sample_source_yields_both_fixture_pages():
    source = SampleSource()
    batches = list(source.iter_pages())
    assert len(batches) == 2
    assert batches[0].page_number == 1
    assert batches[1].page_number == 2
    assert len(batches[0].records) == 5
    assert len(batches[1].records) == 4
    first = batches[0].records[0]
    assert first.company_name == "宏達精密機械股份有限公司"
    assert first.email == "mailto:sales@hongda-precision.com.tw"  # normalized later, not here
    assert first.source == "sample"


def test_sample_source_requires_no_network():
    assert SampleSource().requires_network() is False


def test_sample_source_respects_page_limit():
    config = sample_source_config().model_copy(update={"max_pages": 1})
    source = SampleSource(config)
    batches = list(source.iter_pages())
    assert len(batches) == 1


def test_sample_source_missing_fixture_raises(tmp_path):
    from core.errors import SourceConfigError

    source = SampleSource(pages_dir=tmp_path)
    with pytest.raises(SourceConfigError):
        list(source.iter_pages())


def test_sample_source_skips_items_without_name_and_harvests_missing_phone(tmp_path):
    (tmp_path / "sample_directory_page1.html").write_text(
        "<div class='company-card'><p class='industry'>No name here</p></div>"
        "<div class='company-card'><h3 class='name'>Foo Co</h3>"
        "<p>Call us at 02-1234-5678</p></div>",
        encoding="utf-8",
    )
    (tmp_path / "sample_directory_page2.html").write_text(
        "<html><body></body></html>", encoding="utf-8"
    )

    source = SampleSource(pages_dir=tmp_path)
    batches = list(source.iter_pages())

    assert len(batches[0].records) == 1
    record = batches[0].records[0]
    assert record.company_name == "Foo Co"
    assert record.phone == "02-1234-5678"


# ------------------------------------------------------------------- pipeline


def test_crawl_pipeline_run_source_sample_end_to_end(db_session):
    from crawler.pipeline import CrawlPipeline

    pipeline = CrawlPipeline()
    summary = pipeline.run_source("sample")

    assert summary.status == "Success"
    assert summary.pages_crawled == 2
    assert summary.records_found == 9
    assert summary.records_new == 7
    assert summary.records_updated == 2
    assert summary.records_duplicate == 2
    assert summary.records_invalid == 0

    repo = CompanyRepository(db_session)
    companies = repo.all()
    assert len(companies) == 7

    by_name = {c.company_name: c for c in companies}
    # The two cross-page duplicates merged into a single row each.
    merged_hongda = by_name["宏達精密機械股份有限公司"]
    assert merged_hongda.tax_id == "69081935"
    assert merged_hongda.address is not None  # filled in from the page-2 record

    merged_chuantai = by_name["全泰化工有限公司"]
    assert merged_chuantai.email == "service@chuantai-chem.com.tw"
    assert merged_chuantai.website is not None


def test_crawl_pipeline_run_all_runs_every_enabled_source(db_session, tmp_config):
    from core.config import SourceConfig
    from crawler.pipeline import CrawlPipeline

    config = tmp_config.model_copy(
        update={
            "crawler": tmp_config.crawler.model_copy(
                update={
                    "sources": [
                        SourceConfig(name="sample", type="sample", enabled=True),
                        SourceConfig(name="disabled-one", type="sample", enabled=False),
                    ]
                }
            )
        }
    )
    pipeline = CrawlPipeline(config)
    summaries = pipeline.run_all()
    assert [s.source for s in summaries] == ["sample"]


def test_crawl_pipeline_run_all_stops_when_cancelled_before_first_source(
    db_session, tmp_config
):
    import threading

    from crawler.pipeline import CrawlPipeline

    pipeline = CrawlPipeline(tmp_config)
    cancelled = threading.Event()
    cancelled.set()
    summaries = pipeline.run_all(cancel_event=cancelled)
    assert summaries == []


def test_crawl_pipeline_cancel_event_stops_mid_run(db_session, tmp_config):
    import threading

    from crawler.pipeline import CrawlPipeline

    # The sample source only has 2 pages; cancelling immediately still lets the
    # first page through (the cancel check happens at the top of the loop).
    cancelled = threading.Event()
    cancelled.set()
    pipeline = CrawlPipeline(tmp_config)
    summary = pipeline.run_source("sample", cancel_event=cancelled)
    assert summary.status == "Cancelled"
    assert summary.pages_crawled == 0


def test_crawl_pipeline_max_pages_override(db_session, tmp_config):
    from crawler.pipeline import CrawlPipeline

    pipeline = CrawlPipeline(tmp_config)
    summary = pipeline.run_source("sample", max_pages=1)
    assert summary.pages_crawled == 1


def test_crawl_pipeline_unknown_source_raises(db_session, tmp_config):
    from core.errors import ConfigError
    from crawler.pipeline import CrawlPipeline

    pipeline = CrawlPipeline(tmp_config)
    with pytest.raises(ConfigError):
        pipeline.run_source("does-not-exist")


def test_crawl_pipeline_context_manager_closes_owned_fetcher():
    from crawler.pipeline import CrawlPipeline

    with CrawlPipeline() as pipeline:
        assert pipeline._owns_fetcher is True
    assert pipeline._fetcher is None


def test_crawl_convenience_function_single_source(db_session, tmp_config):
    from crawler.pipeline import crawl

    summaries = crawl(source="sample", config=tmp_config)
    assert len(summaries) == 1
    assert summaries[0].source == "sample"


def test_crawl_convenience_function_all_sources(db_session, tmp_config):
    from crawler.pipeline import crawl

    summaries = crawl(config=tmp_config)
    assert [s.source for s in summaries] == ["sample"]


def test_last_crawl_time_none_when_no_jobs(db_session, tmp_config):
    from crawler.pipeline import last_crawl_time

    assert last_crawl_time() is None


def test_last_crawl_time_after_a_run(db_session, tmp_config):
    from crawler.pipeline import crawl, last_crawl_time

    crawl(source="sample", config=tmp_config)
    assert last_crawl_time() is not None


class _BoomSource:
    """A fake source that raises to exercise the pipeline's failure handling."""

    page_limit = 10

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.fetcher = None

    def requires_network(self) -> bool:
        return False

    def iter_pages(self):
        raise self._exc
        yield  # pragma: no cover - never reached, makes this a generator


@pytest.mark.parametrize(
    "exc",
    [
        RobotsDisallowedError("https://example.test", "ua"),
        CrawlError("boom"),
        RuntimeError("unexpected"),
    ],
)
def test_crawl_pipeline_records_failure_status_for_every_error_kind(
    db_session, tmp_config, monkeypatch, exc
):
    import crawler.pipeline as pipeline_module

    monkeypatch.setattr(
        pipeline_module, "build_source", lambda *_a, **_k: _BoomSource(exc)
    )
    pipeline = pipeline_module.CrawlPipeline(tmp_config)
    summary = pipeline.run_source("sample")
    assert summary.status == "Failed"
    assert summary.error is not None


class _SequenceSource:
    """A fake source yielding pre-built pages, for pipeline-loop edge cases."""

    page_limit = 10

    def __init__(self, batches) -> None:
        self._batches = batches
        self.fetcher = None

    def requires_network(self) -> bool:
        return False

    def iter_pages(self):
        yield from self._batches


def test_crawl_pipeline_stops_early_on_empty_page(db_session, tmp_config, monkeypatch):
    from core.schemas import RawCompany
    from crawler.base import PageBatch
    import crawler.pipeline as pipeline_module

    batches = [
        PageBatch(page_number=1, url="u1", records=[RawCompany(company_name="Foo Co")]),
        PageBatch(page_number=2, url="u2", records=[]),
        PageBatch(page_number=3, url="u3", records=[RawCompany(company_name="Never Seen")]),
    ]
    monkeypatch.setattr(
        pipeline_module, "build_source", lambda *_a, **_k: _SequenceSource(batches)
    )
    pipeline = pipeline_module.CrawlPipeline(tmp_config)
    summary = pipeline.run_source("sample")

    assert summary.pages_crawled == 2  # stopped after the empty page, never reached page 3
    assert summary.records_found == 1


def test_crawl_pipeline_invokes_progress_callback(db_session, tmp_config, monkeypatch):
    from core.schemas import RawCompany
    from crawler.base import PageBatch
    import crawler.pipeline as pipeline_module

    batches = [PageBatch(page_number=1, url="u1", records=[RawCompany(company_name="Foo Co")])]
    monkeypatch.setattr(
        pipeline_module, "build_source", lambda *_a, **_k: _SequenceSource(batches)
    )
    seen = []
    pipeline = pipeline_module.CrawlPipeline(tmp_config)
    pipeline.run_source("sample", progress=lambda *args: seen.append(args))
    assert seen == [("sample", 1, 1)]


def test_crawl_pipeline_lazily_builds_and_closes_a_network_fetcher(
    db_session, tmp_config, monkeypatch
):
    from core.config import PaginationRule, SourceConfig
    from crawler.fetcher import FetchResult
    import crawler.pipeline as pipeline_module

    class FakeFetcher:
        def __init__(self) -> None:
            self.closed = False

        def fetch(self, url, *, method="GET", data=None, encoding=None):
            return FetchResult(
                url=url, status_code=200,
                html="<div class='item'><h3 class='n'>Foo</h3></div>",
            )

        def close(self) -> None:
            self.closed = True

    fake = FakeFetcher()
    monkeypatch.setattr(pipeline_module, "build_fetcher", lambda cfg: fake)

    config = tmp_config.model_copy(
        update={
            "crawler": tmp_config.crawler.model_copy(
                update={
                    "sources": [
                        SourceConfig(
                            name="generic",
                            type="generic_html",
                            start_url="https://example.test/list",
                            list_selector="div.item",
                            fields={"company_name": {"selector": "h3.n"}},
                            pagination=PaginationRule(type="none"),
                        )
                    ]
                }
            )
        }
    )
    pipeline = pipeline_module.CrawlPipeline(config)
    summary = pipeline.run_source("generic")
    assert summary.records_found == 1

    pipeline.close()
    assert fake.closed is True


def test_crawl_pipeline_run_source_config_for_an_ad_hoc_source(db_session, tmp_config):
    from core.config import SourceConfig
    from crawler.pipeline import CrawlPipeline

    ad_hoc = SourceConfig(name="ad-hoc", type="sample")
    pipeline = CrawlPipeline(tmp_config)
    summary = pipeline.run_source_config(ad_hoc)
    assert summary.source == "ad-hoc"
    assert summary.status == "Success"


# --------------------------------------------------------- generic_html source


def test_generic_html_source_requires_start_url_and_list_selector(tmp_config):
    from core.config import PaginationRule, SourceConfig
    from crawler.sources.generic_html import GenericHtmlSource

    # Bypass SourceConfig's own pydantic validation (which already forbids
    # this) to exercise GenericHtmlSource's defensive check directly.
    bad_config = SourceConfig.model_construct(
        name="x",
        type="generic_html",
        enabled=True,
        start_url=None,
        page_start=1,
        max_pages=None,
        list_selector=None,
        pagination=PaginationRule(type="none"),
        fields={},
        label=None,
    )
    from core.errors import SourceConfigError

    with pytest.raises(SourceConfigError):
        GenericHtmlSource(bad_config)


def test_base_source_defaults_require_network_and_have_a_repr(tmp_config):
    from core.config import PaginationRule, SourceConfig
    from crawler.sources.generic_html import GenericHtmlSource

    config = SourceConfig(
        name="generic",
        type="generic_html",
        start_url="https://example.test/{page}",
        list_selector=".item",
        fields={"company_name": {"selector": ".n"}},
        pagination=PaginationRule(type="query"),
    )
    source = GenericHtmlSource(config, config=tmp_config)
    assert source.requires_network() is True
    assert repr(source) == "<GenericHtmlSource name='generic'>"
    assert source.page_limit == tmp_config.crawler.max_pages


def test_generic_html_source_iter_pages_requires_a_fetcher(tmp_config):
    from core.config import PaginationRule, SourceConfig
    from crawler.sources.generic_html import GenericHtmlSource
    from core.errors import SourceConfigError

    config = SourceConfig(
        name="x",
        type="generic_html",
        start_url="https://example.test/{page}",
        list_selector=".item",
        fields={"company_name": {"selector": ".n"}},
        pagination=PaginationRule(type="query"),
    )
    source = GenericHtmlSource(config, fetcher=None, config=tmp_config)
    with pytest.raises(SourceConfigError):
        list(source.iter_pages())


def _make_generic_fetcher(tmp_config, pages: dict[str, str]):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=pages.get(str(request.url), ""))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpxFetcher(config=tmp_config, robots=RobotsPolicy("ua", enabled=False), client=client)


def test_generic_html_source_query_pagination(tmp_config):
    from core.config import PaginationRule, SourceConfig
    from crawler.sources.generic_html import GenericHtmlSource

    pages = {
        "https://example.test/list?page=1": (
            "<div class='item'><h3 class='n'>Foo</h3></div>"
            "<div class='item'><h3 class='n'>Bar</h3></div>"
        ),
        "https://example.test/list?page=2": "<div class='item'><h3 class='n'>Baz</h3></div>",
        "https://example.test/list?page=3": "<html><body>no items</body></html>",
    }
    fetcher = _make_generic_fetcher(tmp_config, pages)
    config = SourceConfig(
        name="generic",
        type="generic_html",
        start_url="https://example.test/list?page={page}",
        list_selector="div.item",
        fields={"company_name": {"selector": "h3.n"}},
        pagination=PaginationRule(type="query"),
        max_pages=3,
    )
    source = GenericHtmlSource(config, fetcher=fetcher, config=tmp_config)
    batches = list(source.iter_pages())

    assert [len(b.records) for b in batches] == [2, 1, 0]
    assert batches[0].records[0].company_name == "Foo"
    assert batches[0].records[0].source == "generic"
    assert batches[0].records[0].source_url == "https://example.test/list?page=1"


def test_generic_html_source_next_link_pagination_stops_without_next(tmp_config):
    from core.config import PaginationRule, SourceConfig
    from crawler.sources.generic_html import GenericHtmlSource

    pages = {
        "https://example.test/p1": (
            "<div class='item'><h3 class='n'>Foo</h3></div>"
            "<a class='next' href='/p2'>Next</a>"
        ),
        "https://example.test/p2": "<div class='item'><h3 class='n'>Bar</h3></div>",
    }
    fetcher = _make_generic_fetcher(tmp_config, pages)
    config = SourceConfig(
        name="generic",
        type="generic_html",
        start_url="https://example.test/p1",
        list_selector="div.item",
        fields={"company_name": {"selector": "h3.n"}},
        pagination=PaginationRule(type="next_link", next_selector="a.next"),
        max_pages=5,
    )
    source = GenericHtmlSource(config, fetcher=fetcher, config=tmp_config)
    batches = list(source.iter_pages())

    assert len(batches) == 2
    assert batches[0].records[0].company_name == "Foo"
    assert batches[1].records[0].company_name == "Bar"


def test_generic_html_source_none_pagination_stops_after_one_page(tmp_config):
    from core.config import PaginationRule, SourceConfig
    from crawler.sources.generic_html import GenericHtmlSource

    pages = {
        "https://example.test/single": (
            "<div class='item'><h3 class='n'>Foo</h3></div>"
            "<a class='next' href='/never-followed'>Next</a>"
        ),
    }
    fetcher = _make_generic_fetcher(tmp_config, pages)
    config = SourceConfig(
        name="generic",
        type="generic_html",
        start_url="https://example.test/single",
        list_selector="div.item",
        fields={"company_name": {"selector": "h3.n"}},
        pagination=PaginationRule(type="none"),
        max_pages=5,
    )
    source = GenericHtmlSource(config, fetcher=fetcher, config=tmp_config)
    batches = list(source.iter_pages())
    assert len(batches) == 1


def test_generic_html_source_fills_gaps_from_page_wide_harvest_and_keeps_extras(tmp_config):
    from core.config import PaginationRule, SourceConfig
    from crawler.sources.generic_html import GenericHtmlSource

    pages = {
        "https://example.test/single": (
            "<div class='item'>"
            "<h3 class='n'>Foo</h3>"
            "<a href='mailto:foo@example.com'>mail</a>"
            "<a href='tel:02-1234-5678'>call</a>"
            "<span class='note'>VIP client</span>"
            "</div>"
        ),
    }
    fetcher = _make_generic_fetcher(tmp_config, pages)
    config = SourceConfig(
        name="generic",
        type="generic_html",
        start_url="https://example.test/single",
        list_selector="div.item",
        fields={
            "company_name": {"selector": "h3.n"},
            "note": {"selector": "span.note"},
        },
        pagination=PaginationRule(type="none"),
    )
    source = GenericHtmlSource(config, fetcher=fetcher, config=tmp_config)
    batches = list(source.iter_pages())

    record = batches[0].records[0]
    assert record.email == "foo@example.com"
    assert record.phone == "02-1234-5678"
    assert record.extra == {"note": "VIP client"}


def test_generic_html_source_post_pagination_substitutes_page_in_form_data(tmp_config):
    """POST 表單分頁：網址固定不變，換頁靠 form_data 裡的 {page} 欄位。"""
    from urllib.parse import parse_qs

    from core.config import PaginationRule, SourceConfig
    from crawler.sources.generic_html import GenericHtmlSource

    seen_bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        seen_bodies.append(body)
        page = parse_qs(body).get("page", ["1"])[0]
        if page == "1":
            return httpx.Response(200, text="<div class='item'><h3 class='n'>Foo</h3></div>")
        if page == "2":
            return httpx.Response(200, text="<div class='item'><h3 class='n'>Bar</h3></div>")
        return httpx.Response(200, text="<html>no items</html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = HttpxFetcher(config=tmp_config, robots=RobotsPolicy("ua", enabled=False), client=client)

    config = SourceConfig(
        name="tca",
        type="generic_html",
        start_url="https://example.test/search",
        list_selector="div.item",
        fields={"company_name": {"selector": "h3.n"}},
        pagination=PaginationRule(type="query"),
        method="POST",
        form_data={"keyword": "", "page": "{page}"},
        max_pages=3,
    )
    source = GenericHtmlSource(config, fetcher=fetcher, config=tmp_config)
    batches = list(source.iter_pages())

    assert [len(b.records) for b in batches] == [1, 1, 0]
    assert batches[0].records[0].company_name == "Foo"
    assert batches[1].records[0].company_name == "Bar"
    assert seen_bodies == ["keyword=&page=1", "keyword=&page=2", "keyword=&page=3"]


def test_generic_html_source_encoding_option_decodes_big5_response(tmp_config):
    from core.config import PaginationRule, SourceConfig
    from crawler.sources.generic_html import GenericHtmlSource

    html = "<div class='item'><h3 class='n'>測試股份有限公司</h3></div>"
    raw = html.encode("big5")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=raw, headers={"Content-Type": "text/html"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = HttpxFetcher(config=tmp_config, robots=RobotsPolicy("ua", enabled=False), client=client)

    config = SourceConfig(
        name="tca",
        type="generic_html",
        start_url="https://example.test/search",
        list_selector="div.item",
        fields={"company_name": {"selector": "h3.n"}},
        pagination=PaginationRule(type="none"),
        encoding="big5",
    )
    source = GenericHtmlSource(config, fetcher=fetcher, config=tmp_config)
    batches = list(source.iter_pages())

    assert batches[0].records[0].company_name == "測試股份有限公司"


def test_generic_html_source_default_method_and_encoding_unchanged(tmp_config):
    """沒設定 method/encoding 的既有來源行為要維持一致（純 GET、交給 httpx 判斷編碼）。"""
    from core.config import PaginationRule, SourceConfig
    from crawler.sources.generic_html import GenericHtmlSource

    seen_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_methods.append(request.method)
        return httpx.Response(200, text="<div class='item'><h3 class='n'>Foo</h3></div>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = HttpxFetcher(config=tmp_config, robots=RobotsPolicy("ua", enabled=False), client=client)

    config = SourceConfig(
        name="generic",
        type="generic_html",
        start_url="https://example.test/single",
        list_selector="div.item",
        fields={"company_name": {"selector": "h3.n"}},
        pagination=PaginationRule(type="none"),
    )
    source = GenericHtmlSource(config, fetcher=fetcher, config=tmp_config)
    batches = list(source.iter_pages())

    assert seen_methods == ["GET"]
    assert batches[0].records[0].company_name == "Foo"


def test_generic_html_source_skips_items_without_a_company_name(tmp_config):
    from core.config import PaginationRule, SourceConfig
    from crawler.sources.generic_html import GenericHtmlSource

    pages = {
        "https://example.test/single": "<div class='item'><p>no name here</p></div>",
    }
    fetcher = _make_generic_fetcher(tmp_config, pages)
    config = SourceConfig(
        name="generic",
        type="generic_html",
        start_url="https://example.test/single",
        list_selector="div.item",
        fields={"company_name": {"selector": "h3.n"}},
        pagination=PaginationRule(type="none"),
    )
    source = GenericHtmlSource(config, fetcher=fetcher, config=tmp_config)
    batches = list(source.iter_pages())
    assert batches[0].records == []


# ------------------------------------------------------------- source registry


def test_registered_types_includes_the_builtins():
    from crawler.sources import registered_types

    assert {"sample", "generic_html"} <= set(registered_types())


def test_build_source_unknown_type_raises(tmp_config):
    from core.config import PaginationRule, SourceConfig
    from core.errors import SourceConfigError
    from crawler.sources import build_source

    bad_config = SourceConfig.model_construct(
        name="x",
        type="totally-unknown",
        enabled=True,
        start_url=None,
        page_start=1,
        max_pages=None,
        list_selector=None,
        pagination=PaginationRule(type="none"),
        fields={},
        label=None,
    )
    with pytest.raises(SourceConfigError):
        build_source(bad_config, config=tmp_config)


def test_build_source_builds_sample_and_generic_html(tmp_config):
    from core.config import PaginationRule, SourceConfig
    from crawler.sources import build_source
    from crawler.sources.generic_html import GenericHtmlSource
    from crawler.sources.sample import SampleSource

    sample = build_source(SourceConfig(name="sample", type="sample"), config=tmp_config)
    assert isinstance(sample, SampleSource)

    generic = build_source(
        SourceConfig(
            name="generic",
            type="generic_html",
            start_url="https://example.test/{page}",
            list_selector=".item",
            fields={"company_name": {"selector": ".n"}},
            pagination=PaginationRule(type="query"),
        ),
        config=tmp_config,
    )
    assert isinstance(generic, GenericHtmlSource)
