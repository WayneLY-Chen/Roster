"""爬取的兩件保命工作：發現壞掉、以及不要白做工。

整套爬取建立在 CSS 選擇器上。網站一改版，爬取會「成功」地抓到 0 筆，畫面上
寫著完成——而排程是半夜自己跑的，沒有人在看。跟上一次比對是唯一能自動看出來
的方式，而每一次的筆數本來就都存著了。

另一半是續跑：逐項查詢一趟可能好幾個小時，第 80 個條件時網路斷一下，不該整批
從頭再來。
"""

from __future__ import annotations

import pytest

from core.constants import CrawlStatus
from core.schemas import CrawlSummary
from crawler.pipeline import HEALTH_DROP_RATIO, _health_warning


class _Job:
    id = 99


class _Repo:
    """只回答「上一次抓到幾筆」。"""

    def __init__(self, before: int | None) -> None:
        self._before = before
        self.asked: list[str] = []

    def last_harvest_for(self, source: str, before_id: int | None = None):
        self.asked.append(source)
        return self._before


def _summary(found: int, status: str = CrawlStatus.SUCCESS.value, **kwargs) -> CrawlSummary:
    return CrawlSummary(source="某公會", status=status, records_found=found, **kwargs)


# ------------------------------------------------------------ 健康度


def test_zero_records_after_a_good_run_is_reported():
    """最重要的一種：選擇器失效，爬取成功，資料是空的。"""
    warning = _health_warning(_Repo(240), "某公會", _Job(), _summary(0))

    assert warning is not None
    assert "240" in warning


def test_a_cliff_drop_is_reported():
    warning = _health_warning(_Repo(240), "某公會", _Job(), _summary(12))

    assert warning is not None
    assert "12" in warning and "240" in warning


def test_a_normal_fluctuation_is_not_reported():
    """名錄本來就會增減，有些站每個月換一批廠商。抓到六成還在正常範圍——
    每次都跳警告，等於沒有警告。"""
    assert _health_warning(_Repo(240), "某公會", _Job(), _summary(150)) is None


def test_the_threshold_is_where_it_says_it_is():
    before = 100
    just_under = int(before * HEALTH_DROP_RATIO) - 1
    just_over = int(before * HEALTH_DROP_RATIO) + 1

    assert _health_warning(_Repo(before), "某公會", _Job(), _summary(just_under))
    assert _health_warning(_Repo(before), "某公會", _Job(), _summary(just_over)) is None


def test_the_first_ever_run_is_not_reported():
    """沒有東西可以比。第一次就跳警告只會讓人以為程式壞了。"""
    assert _health_warning(_Repo(None), "某公會", _Job(), _summary(0)) is None


def test_a_failed_run_is_not_reported_twice():
    """失敗本來就有自己的錯誤訊息，再加一句「抓得比上次少」只是雜訊。"""
    summary = _summary(0, status=CrawlStatus.FAILED.value)

    assert _health_warning(_Repo(240), "某公會", _Job(), summary) is None


def test_a_cancelled_run_is_not_reported():
    summary = _summary(3, status=CrawlStatus.CANCELLED.value)

    assert _health_warning(_Repo(240), "某公會", _Job(), summary) is None


def test_a_resumed_run_is_not_reported():
    """接續上一次的執行本來就只做剩下的部分，筆數少是正常的。"""
    summary = _summary(5, resumed=True)

    assert _health_warning(_Repo(240), "某公會", _Job(), summary) is None


def test_a_broken_history_lookup_never_breaks_the_crawl():
    """提醒是附加價值。它自己壞掉不該讓一趟跑了三小時的爬取一起失敗。"""

    class _Broken:
        def last_harvest_for(self, source, before_id=None):
            raise RuntimeError("資料表壞了")

    assert _health_warning(_Broken(), "某公會", _Job(), _summary(0)) is None


# ------------------------------------------------------------ 續跑


def test_the_pipeline_resumes_from_the_last_unfinished_run(db_session, tmp_config, monkeypatch):
    """第 80 個條件時斷掉，下一次要從第 81 個開始，不是從頭。"""
    from core.config import PaginationRule, SourceConfig
    from crawler.base import PageBatch
    import crawler.pipeline as pipeline_module
    from database.repository import CrawlJobRepository

    seen: list[str | None] = []

    class _Source:
        requires_network = staticmethod(lambda: False)

        def __init__(self, source_config, fetcher, config) -> None:
            self.source_config = source_config
            self.fetcher = fetcher
            self.resume_from: str | None = None
            self.page_limit = 10

        def iter_pages(self):
            seen.append(self.resume_from)
            yield PageBatch(page_number=1, url="https://a.test/1", records=[], resume_key="81")

    monkeypatch.setattr(pipeline_module, "build_source", _Source)

    source = SourceConfig(
        name="慢慢查",
        type="generic_html",
        start_url="https://a.test/list",
        list_selector="tr",
        fields={"company_name": {"selector": "td"}},
        pagination=PaginationRule(type="none"),
    )

    pipeline = pipeline_module.CrawlPipeline(tmp_config)

    # 第一次：留下進度 81，但當成沒跑完（手動改成取消）。
    pipeline.run_source_config(source)
    job = CrawlJobRepository(db_session).previous_for("慢慢查")
    job.status = CrawlStatus.CANCELLED.value
    job.resume_state = "81"
    db_session.commit()

    # 第二次：應該把進度交給來源。
    summary = pipeline.run_source_config(source)

    assert seen[-1] == "81"
    assert summary.resumed is True


def test_a_finished_run_leaves_nothing_to_resume(db_session, tmp_config, monkeypatch):
    """跑完了還留著進度的話，下一次會從中間開始，前面那幾頁永遠抓不到。"""
    from core.config import PaginationRule, SourceConfig
    from crawler.base import PageBatch
    import crawler.pipeline as pipeline_module
    from database.repository import CrawlJobRepository

    class _Source:
        requires_network = staticmethod(lambda: False)

        def __init__(self, source_config, fetcher, config) -> None:
            self.source_config = source_config
            self.fetcher = fetcher
            self.resume_from: str | None = None
            self.page_limit = 10

        def iter_pages(self):
            yield PageBatch(page_number=1, url="https://a.test/1", records=[], resume_key="7")

    monkeypatch.setattr(pipeline_module, "build_source", _Source)

    source = SourceConfig(
        name="跑完了",
        type="generic_html",
        start_url="https://a.test/list",
        list_selector="tr",
        fields={"company_name": {"selector": "td"}},
        pagination=PaginationRule(type="none"),
    )

    pipeline = pipeline_module.CrawlPipeline(tmp_config)
    pipeline.run_source_config(source)

    job = CrawlJobRepository(db_session).previous_for("跑完了")
    assert job.resume_state is None


@pytest.mark.parametrize("bad", ["", "abc", None])
def test_an_unreadable_resume_marker_starts_from_the_beginning(bad, tmp_config):
    """看不懂的進度不該讓整個來源掛掉，從頭跑一次是安全的那一邊。"""
    from core.config import PaginationRule, QueryLoop, SourceConfig
    from crawler.sources.generic_html import GenericHtmlSource

    source = SourceConfig(
        name="q",
        type="generic_html",
        start_url="https://a.test/q",
        list_selector="tr",
        fields={"company_name": {"selector": "td"}},
        pagination=PaginationRule(type="none"),
        query_loop=QueryLoop(input_selector="#q", submit_selector="#go"),
    )
    crawler = GenericHtmlSource(source, fetcher=object(), config=tmp_config)
    crawler.resume_from = bad

    assert crawler._resume_index() == 0
