"""爬取的兩件保命工作：發現壞掉、以及不要白做工。

整套爬取建立在 CSS 選擇器上。網站一改版，爬取會「成功」地抓到 0 筆，畫面上
寫著完成——而排程是半夜自己跑的，沒有人在看。跟以前比對是唯一能自動看出來
的方式，而每一次的筆數本來就都存著了。

另一半是續跑：逐項查詢一趟可能好幾個小時，第 80 個條件時網路斷一下，不該整批
從頭再來。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from core.constants import CrawlStatus
from core.schemas import CrawlSummary
from crawler.health import (
    DROP_RATIO,
    Baseline,
    HealthState,
    assess,
    summarise,
)
from crawler.pipeline import _health_warning
from database.models import CrawlJob, now
from database.repository import CrawlJobRepository


class _Job:
    id = 99


def _summary(found: int, status: str = CrawlStatus.SUCCESS.value, **kwargs) -> CrawlSummary:
    return CrawlSummary(source="某公會", status=status, records_found=found, **kwargs)


def _check(before, found: int, **kwargs):
    """這個來源前幾次抓到 ``before``、這次抓到 ``found``，判斷結果是什麼。"""
    counts = (before,) if isinstance(before, int) else tuple(before)
    return assess("某公會", _summary(found, **kwargs), Baseline(counts))


def _warning(before, found: int, **kwargs) -> str | None:
    return _check(before, found, **kwargs).warning


# ------------------------------------------------------------ 健康度


def test_zero_records_after_a_good_run_is_reported():
    """最重要的一種：選擇器失效，爬取成功，資料是空的。"""
    warning = _warning(240, 0)

    assert warning is not None
    assert "240" in warning


def test_a_cliff_drop_is_reported():
    warning = _warning(240, 12)

    assert warning is not None
    assert "12" in warning and "240" in warning


def test_a_normal_fluctuation_is_not_reported():
    """名錄本來就會增減，有些站每個月換一批廠商。抓到六成還在正常範圍——
    每次都跳警告，等於沒有警告。"""
    assert _warning(240, 150) is None


def test_the_threshold_is_where_it_says_it_is():
    before = 100

    assert _warning(before, int(before * DROP_RATIO) - 1)
    assert _warning(before, int(before * DROP_RATIO) + 1) is None


def test_the_first_ever_run_is_not_reported():
    """沒有東西可以比。第一次就跳警告只會讓人以為程式壞了。"""
    report = _check((), 0)

    assert report.warning is None
    assert report.state is HealthState.NO_BASELINE


def test_a_failed_run_is_not_reported_twice():
    """失敗本來就有自己的錯誤訊息，再加一句「抓得比上次少」只是雜訊。"""
    report = _check(240, 0, status=CrawlStatus.FAILED.value)

    assert report.warning is None
    # 但狀態要標出來：半夜排程失敗的話，這是隔天早上唯一看得到的線索。
    assert report.state is HealthState.FAILED
    assert report.needs_attention


def test_a_cancelled_run_is_not_reported():
    report = _check(240, 3, status=CrawlStatus.CANCELLED.value)

    assert report.warning is None
    # 使用者自己按的停止。留一個紅字在那裡，他唯一能做的事是再跑一次。
    assert not report.needs_attention


def test_a_resumed_run_is_not_reported():
    """接續上一次的執行本來就只做剩下的部分，筆數少是正常的。"""
    report = _check(240, 5, resumed=True)

    assert report.warning is None
    assert report.state is HealthState.RESUMED


def test_a_run_where_everything_was_rejected_is_reported():
    """抓到了東西、卻一筆都沒存進去——選擇器指到了導覽列或表頭。

    這跟「抓不到」是完全不同的毛病，而且第一次跑就可能發生，所以它不需要
    基準也要講。
    """
    warning = _warning((), 21, records_invalid=21)

    assert warning is not None and "21" in warning


# --------------------------------------------- 為什麼基準是中位數而不是上一次


def test_a_source_that_is_still_broken_is_still_reported():
    """**這是這一版的重點。**

    只跟上一次比的話，警告只會出現一次：禮拜一壞掉抓回 12 筆（跳警告），
    禮拜二又是 12 筆——跟上一次比完全正常，於是不再警告。偏偏禮拜二這個來源
    已經壞了一整天。

    ``harvest_history`` 給的是新的排前面，所以這裡的 12 就是「昨天那次壞掉
    的」。中位數拉不動，警告要留著。
    """
    assert _warning([12, 380, 400, 390], 12) is not None


def test_a_source_that_really_shrank_stops_warning_by_itself():
    """名錄是真的變小了的話，警告要自己停掉。

    會員退掉一半是會發生的事。連續幾次都是新的數字之後中位數自己會跟上來
    ——不必請使用者去按「不要再提醒我」，而那種開關一旦按下去，真的壞掉的
    那天也不會有人通知他。
    """
    assert _warning([120, 120, 120, 380, 380], 120) is None


def test_one_freak_run_does_not_become_the_baseline():
    """有一次抓了兩倍（來源自己重複刊登）也不該讓之後每一次都跳警告。"""
    assert _warning([800, 380, 400, 390, 385], 380) is None


def test_the_baseline_speaks_differently_when_it_only_has_one_run():
    """只有一次可以比的時候不要硬講成「平常」——使用者看得出來程式只有一筆
    資料，講得太篤定反而讓人不信任後面那句建議。"""
    assert "上一次" in Baseline((240,)).describe()
    assert "平常" in Baseline((240, 250, 230)).describe()


def test_the_baseline_is_the_median_not_the_average():
    assert Baseline((380, 400, 12)).value == 380
    assert Baseline(()).value is None


# ------------------------------------------------------------ 其他狀態


def test_a_source_that_was_never_crawled_says_so():
    report = assess("某公會", None)

    assert report.state is HealthState.NEVER
    assert not report.needs_attention
    assert report.found is None


def test_a_disabled_source_is_not_judged():
    """停用的來源排程不會去爬。留一個紅字在那裡，使用者唯一能做的事是把它
    重新啟用——那不是提醒，是嘮叨。"""
    report = assess("某公會", _summary(0), Baseline((240,)), enabled=False)

    assert report.state is HealthState.DISABLED
    assert not report.needs_attention
    assert report.warning is None


def test_a_failed_run_shows_its_own_error_in_the_note():
    report = assess(
        "某公會",
        _summary(0, status=CrawlStatus.FAILED.value, error="robots.txt 不允許"),
        Baseline((240,)),
    )

    assert "robots.txt" in report.note


def test_the_summary_line_names_the_broken_sources():
    reports = [
        assess("好的", _summary(200), Baseline((210,))),
        assess("壞的", _summary(0), Baseline((240,))),
    ]

    line = summarise(reports)
    assert "壞的" in line and "好的" not in line


def test_the_summary_line_says_so_when_everything_is_fine():
    reports = [assess("好的", _summary(200), Baseline((210,)))]

    assert "正常" in summarise(reports)


# ------------------------------------------------- 爬完那一刻用的是同一份判斷


class _Repo:
    """只回答「以前抓到幾筆」。"""

    def __init__(self, counts) -> None:
        self._counts = list(counts)
        self.asked: list[str] = []

    def harvest_history(self, source: str, limit: int = 5, before_id: int | None = None):
        self.asked.append(source)
        return self._counts[:limit]


def test_the_pipeline_asks_for_the_history_and_uses_the_same_verdict():
    repo = _Repo([380, 400])
    warning = _health_warning(repo, "某公會", _Job(), _summary(0))

    assert repo.asked == ["某公會"]
    assert warning == _warning([380, 400], 0)


def test_a_broken_history_lookup_never_breaks_the_crawl():
    """提醒是附加價值。它自己壞掉不該讓一趟跑了三小時的爬取一起失敗。"""

    class _Broken:
        def harvest_history(self, source, limit=5, before_id=None):
            raise RuntimeError("資料表壞了")

    assert _health_warning(_Broken(), "某公會", _Job(), _summary(0)) is None


# ------------------------------------------------------------ 基準怎麼撈出來


def _record(session, source: str, found: int, days_ago: int, **kwargs) -> CrawlJob:
    job = CrawlJob(
        source=source,
        status=kwargs.pop("status", CrawlStatus.SUCCESS.value),
        records_found=found,
        started_at=now() - timedelta(days=days_ago),
        **kwargs,
    )
    session.add(job)
    session.flush()
    return job


def test_the_history_is_newest_first_and_capped(db_session):
    for index, found in enumerate([100, 200, 300, 400, 500, 600]):
        _record(db_session, "某公會", found, days_ago=index)

    counts = CrawlJobRepository(db_session).harvest_history("某公會", limit=3)

    assert counts == [100, 200, 300]


@pytest.mark.parametrize(
    "bad",
    [
        {"status": CrawlStatus.FAILED.value},
        {"status": CrawlStatus.CANCELLED.value},
        {"records_found": 0},
        {"resumed": True},
    ],
    ids=["失敗", "取消", "本來就零筆", "接續跑"],
)
def test_the_history_skips_the_runs_that_cannot_be_a_baseline(db_session, bad):
    """這四種執行的筆數都不代表「這個來源平常抓得到多少」。

    尤其是接續跑的：它只做剩下的一截。一次半夜斷線留下的 30 筆混進基準裡，
    會把「平常有 380 筆」拉成一個沒有意義的數字。
    """
    found = bad.pop("records_found", 380)
    _record(db_session, "某公會", found, days_ago=1, **bad)
    _record(db_session, "某公會", 400, days_ago=2)

    counts = CrawlJobRepository(db_session).harvest_history("某公會")

    assert counts == [400]


def test_the_history_never_includes_the_run_that_is_asking(db_session):
    """不排掉自己的話，比到的是自己，永遠不會有警告。"""
    _record(db_session, "某公會", 400, days_ago=2)
    mine = _record(db_session, "某公會", 3, days_ago=0)

    counts = CrawlJobRepository(db_session).harvest_history("某公會", before_id=mine.id)

    assert counts == [400]


def test_the_history_does_not_mix_sources(db_session):
    _record(db_session, "某公會", 400, days_ago=1)
    _record(db_session, "別的公會", 900, days_ago=1)

    assert CrawlJobRepository(db_session).harvest_history("某公會") == [400]


def test_a_finished_run_records_whether_it_was_resumed(db_session):
    """存不下來的話，健康度事後根本分不出「只跑了一截」跟「抓不到」。"""
    repo = CrawlJobRepository(db_session)
    job = repo.start("某公會")
    repo.finish(job, _summary(30, resumed=True))

    assert job.resumed is True
    assert repo.to_summary(job).resumed is True


# ------------------------------------------------------------ 「爬取」頁那張表


def test_the_controller_reports_every_configured_source(db_session, patch_config):
    from controllers.core import CrawlHealthController

    names = {source.name for source in patch_config.crawler.sources}
    reports = CrawlHealthController(patch_config).check()

    assert {report.source for report in reports} == names
    # 從來沒爬過的來源也要列出來，狀態是「還沒爬過」而不是消失。
    assert all(r.state is HealthState.NEVER for r in reports)


def test_the_controller_puts_the_broken_sources_first(db_session, patch_config):
    from controllers.core import CrawlHealthController

    source = patch_config.crawler.sources[0].name
    _record(db_session, source, 380, days_ago=3)
    _record(db_session, source, 400, days_ago=2)
    _record(db_session, source, 2, days_ago=1)
    db_session.commit()

    reports = CrawlHealthController(patch_config).check()

    assert reports[0].source == source
    assert reports[0].state is HealthState.DROPPED
    assert reports[0].baseline.value == 390


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
