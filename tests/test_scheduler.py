"""Tests for unattended crawl scheduling.

The timing logic is tested directly against a fixed reference time rather than
by waiting on a real clock -- a scheduler test that sleeps is a slow test that
fails on a loaded machine.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from core.config import SchedulerSection
from core.scheduler import (
    CrawlScheduler,
    SchedulerState,
    is_overdue,
    load_state,
    next_run_after,
    parse_at,
    save_state,
)


def _settings(**overrides) -> SchedulerSection:
    return SchedulerSection.model_validate({"enabled": True, **overrides})


# ------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    ("value", "hour", "minute"),
    [("03:00", 3, 0), ("00:00", 0, 0), ("23:59", 23, 59), ("9:05", 9, 5)],
)
def test_parse_at(value: str, hour: int, minute: int) -> None:
    parsed = parse_at(value)
    assert (parsed.hour, parsed.minute) == (hour, minute)


@pytest.mark.parametrize("value", ["24:00", "12:60", "abc", "", "-1:00"])
def test_invalid_at_is_rejected(value: str) -> None:
    with pytest.raises(Exception):
        SchedulerSection.model_validate({"at": value})


# ------------------------------------------------------------- next_run_after


def test_daily_schedules_later_today_when_time_has_not_passed() -> None:
    now = datetime(2026, 8, 3, 1, 0)
    due = next_run_after(now, _settings(mode="daily", at="03:00"))
    assert due == datetime(2026, 8, 3, 3, 0)


def test_daily_rolls_over_to_tomorrow_once_the_time_has_passed() -> None:
    now = datetime(2026, 8, 3, 5, 0)
    due = next_run_after(now, _settings(mode="daily", at="03:00"))
    assert due == datetime(2026, 8, 4, 3, 0)


def test_hourly_lands_on_the_next_whole_hour() -> None:
    now = datetime(2026, 8, 3, 5, 37, 12)
    due = next_run_after(now, _settings(mode="hourly"))
    assert due == datetime(2026, 8, 3, 6, 0)


def test_interval_counts_from_the_last_run() -> None:
    now = datetime(2026, 8, 3, 12, 0)
    last = datetime(2026, 8, 3, 11, 0)
    due = next_run_after(now, _settings(mode="interval", every_minutes=90), last)
    assert due == datetime(2026, 8, 3, 12, 30)


def test_interval_that_is_already_due_is_scheduled_immediately() -> None:
    now = datetime(2026, 8, 3, 12, 0)
    last = datetime(2026, 8, 3, 9, 0)
    due = next_run_after(now, _settings(mode="interval", every_minutes=60), last)
    assert due <= now + timedelta(seconds=2)


# ------------------------------------------------------------------- overdue


def test_never_run_is_overdue_when_catch_up_is_on() -> None:
    assert is_overdue(datetime(2026, 8, 3, 12, 0), _settings(catch_up=True), None)


def test_never_run_is_not_overdue_when_catch_up_is_off() -> None:
    assert not is_overdue(datetime(2026, 8, 3, 12, 0), _settings(catch_up=False), None)


def test_daily_job_missed_while_the_machine_was_off_is_overdue() -> None:
    # 03:00 passed while the last run was yesterday evening.
    now = datetime(2026, 8, 3, 9, 0)
    last = datetime(2026, 8, 2, 20, 0)
    assert is_overdue(now, _settings(mode="daily", at="03:00"), last)


def test_daily_job_already_run_today_is_not_overdue() -> None:
    now = datetime(2026, 8, 3, 9, 0)
    last = datetime(2026, 8, 3, 3, 0)
    assert not is_overdue(now, _settings(mode="daily", at="03:00"), last)


def test_interval_overdue_only_after_the_interval_elapses() -> None:
    settings = _settings(mode="interval", every_minutes=60)
    now = datetime(2026, 8, 3, 12, 0)
    assert not is_overdue(now, settings, datetime(2026, 8, 3, 11, 30))
    assert is_overdue(now, settings, datetime(2026, 8, 3, 10, 0))


# --------------------------------------------------------------------- state


def test_state_round_trips_through_disk(patch_config) -> None:
    state = SchedulerState(
        last_run=datetime(2026, 8, 3, 3, 0), last_status="成功", run_count=4
    )
    save_state(state, patch_config)

    restored = load_state(patch_config)
    assert restored.last_run == state.last_run
    assert restored.last_status == "成功"
    assert restored.run_count == 4


def test_missing_state_file_reads_as_never_run(patch_config) -> None:
    assert load_state(patch_config).last_run is None


def test_corrupt_state_file_does_not_raise(patch_config) -> None:
    path = patch_config.database.sqlite_path.parent / "scheduler_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")

    state = load_state(patch_config)
    assert state.last_run is None


def test_unparseable_timestamp_is_treated_as_never_run(patch_config) -> None:
    path = patch_config.database.sqlite_path.parent / "scheduler_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_run": "not-a-date"}), encoding="utf-8")

    assert load_state(patch_config).last_run is None


# ----------------------------------------------------------------- scheduler


def test_disabled_scheduler_refuses_to_start(patch_config) -> None:
    scheduler = CrawlScheduler(patch_config)
    assert scheduler.start() is False
    assert scheduler.running is False
    assert "關閉" in scheduler.status_text


def test_status_text_never_claims_to_be_running_when_it_is_not(tmp_config) -> None:
    config = tmp_config.model_copy(
        update={"scheduler": SchedulerSection.model_validate({"enabled": True})}
    )
    scheduler = CrawlScheduler(config)
    assert "尚未啟動" in scheduler.status_text


def test_run_job_executes_the_configured_sources(db_session, patch_config, monkeypatch):
    """One scheduled pass crawls the offline sample source and records state."""
    config = patch_config.model_copy(
        update={
            "scheduler": SchedulerSection.model_validate(
                {"enabled": True, "sources": ["sample"], "verify_after_crawl": False}
            )
        }
    )
    scheduler = CrawlScheduler(config)
    scheduler._run_job()

    assert scheduler.state.run_count == 1
    assert scheduler.state.last_run is not None
    assert scheduler.state.last_error is None

    from database.repository import CompanyRepository

    assert CompanyRepository(db_session).count() > 0


def test_failure_is_recorded_rather_than_raised(patch_config, monkeypatch) -> None:
    config = patch_config.model_copy(
        update={
            "scheduler": SchedulerSection.model_validate(
                {"enabled": True, "sources": ["sample"]}
            )
        }
    )

    class Boom:
        def __init__(self, *_a, **_k):
            raise RuntimeError("pipeline exploded")

    monkeypatch.setattr("crawler.pipeline.CrawlPipeline", Boom)

    scheduler = CrawlScheduler(config)
    scheduler._run_job()          # must not propagate

    assert scheduler.state.last_status == "失敗"
    assert "pipeline exploded" in (scheduler.state.last_error or "")


def test_no_configured_sources_is_a_no_op(patch_config, monkeypatch) -> None:
    empty = patch_config.model_copy(
        update={
            "crawler": patch_config.crawler.model_copy(update={"sources": []}),
            "scheduler": SchedulerSection.model_validate({"enabled": True}),
        }
    )
    scheduler = CrawlScheduler(empty)
    scheduler._run_job()

    assert scheduler.state.run_count == 0


def test_stop_is_safe_when_never_started(patch_config) -> None:
    CrawlScheduler(patch_config).stop()


# ------------------------------------------------------------------ 每月排程


def test_monthly_runs_on_the_configured_day() -> None:
    settings = _settings(mode="monthly", day_of_month=15, at="09:30")
    assert next_run_after(datetime(2026, 3, 1, 8, 0), settings) == datetime(2026, 3, 15, 9, 30)


def test_monthly_rolls_into_next_month_once_the_day_has_passed() -> None:
    settings = _settings(mode="monthly", day_of_month=5, at="09:00")
    assert next_run_after(datetime(2026, 3, 5, 10, 0), settings) == datetime(2026, 4, 5, 9, 0)


def test_monthly_rolls_over_the_year_boundary() -> None:
    settings = _settings(mode="monthly", day_of_month=1, at="00:30")
    assert next_run_after(datetime(2026, 12, 1, 1, 0), settings) == datetime(2027, 1, 1, 0, 30)


@pytest.mark.parametrize(
    ("year", "month", "expected"),
    [(2026, 2, 28), (2028, 2, 29), (2026, 4, 30), (2026, 1, 31)],
)
def test_day_31_falls_back_to_the_last_day_of_short_months(year, month, expected) -> None:
    """設定「每月 31 號」的人要的是月底。

    二月沒有 31 號時整個月跳過不執行，絕對不是他的本意——那會安靜地少跑
    一個月，而且沒有任何錯誤訊息可看。
    """
    from core.scheduler import _clamp_day

    assert _clamp_day(year, month, 31) == expected


def test_monthly_on_day_31_actually_fires_in_february() -> None:
    settings = _settings(mode="monthly", day_of_month=31, at="03:00")
    assert next_run_after(datetime(2026, 2, 1, 0, 0), settings) == datetime(2026, 2, 28, 3, 0)


def test_monthly_overdue_detects_a_missed_run() -> None:
    settings = _settings(mode="monthly", day_of_month=1, at="03:00", catch_up=True)
    reference = datetime(2026, 3, 2, 10, 0)
    assert is_overdue(reference, settings, datetime(2026, 2, 1, 3, 0)) is True
    assert is_overdue(reference, settings, datetime(2026, 3, 1, 3, 5)) is False


# ---------------------------------------------------------------- 排程動作


@pytest.mark.parametrize(
    ("action", "crawls", "sends"),
    [("crawl", True, False), ("send", False, True), ("crawl_and_send", True, True)],
)
def test_action_flags(action, crawls, sends) -> None:
    settings = _settings(action=action, mail_template="t")
    assert settings.crawls is crawls
    assert settings.sends_mail is sends


def test_enabling_a_mail_schedule_without_a_template_is_rejected() -> None:
    """時間到了才發現不能跑，是最糟的失敗時機。"""
    with pytest.raises(Exception, match="mail_template"):
        SchedulerSection.model_validate(
            {"enabled": True, "action": "send", "mail_template": ""}
        )


def test_an_incomplete_mail_schedule_is_allowed_while_disabled() -> None:
    """使用者可能先切成「寄信」再去挑樣板，中間那一刻不該讓設定檔壞掉。"""
    settings = SchedulerSection.model_validate(
        {"enabled": False, "action": "send", "mail_template": ""}
    )
    assert settings.sends_mail is True


# ------------------------------------------------------------- 排程寄信


class _FakeSendResult:
    sent = 3
    failed = 0


def _fake_plan(count: int):
    class _Recipient:
        def __init__(self) -> None:
            self.will_send = True

    class _Plan:
        def __init__(self) -> None:
            self.recipients = [_Recipient() for _ in range(count)]
            self.sendable = count
            self.attachments: list[str] = []

    return _Plan()


def _mail_scheduler(tmp_config, **scheduler_overrides) -> CrawlScheduler:
    config = tmp_config.model_copy(
        update={
            "scheduler": SchedulerSection.model_validate(
                {"action": "send", "mail_template": "月報", **scheduler_overrides}
            )
        }
    )
    return CrawlScheduler(config)


def test_send_mail_goes_through_the_same_build_plan_as_the_mail_page(
    tmp_config, monkeypatch
) -> None:
    """排程不是另一條寄信路徑。

    每日上限、重複寄送間隔、只寄已驗證信箱、強制附上退訂聲明，全都住在
    build_plan/send_campaign 裡——排程照樣走它們，才不會有「排程寄的信
    沒有退訂連結」這種事。
    """
    scheduler = _mail_scheduler(tmp_config, mail_campaign="每月開發信")
    seen: dict[str, object] = {}

    def _build(criteria, template, campaign, cfg, attachments):
        seen.update(template=template, campaign=campaign, attachments=attachments)
        return _fake_plan(2)

    monkeypatch.setattr("gmail.campaign.build_plan", _build)
    monkeypatch.setattr("gmail.campaign.send_campaign", lambda *a, **k: _FakeSendResult())

    assert scheduler._send_mail() == 3
    assert seen["template"] == "月報"
    # 活動名稱接上執行日期，事後在紀錄裡才分得出是哪一次跑的。
    assert str(seen["campaign"]).startswith("每月開發信-")


def test_batch_limit_caps_an_unattended_run(tmp_config, monkeypatch) -> None:
    """半夜跑掉的批次沒有人會即時發現，所以寧可少寄、下次再寄。"""
    scheduler = _mail_scheduler(tmp_config, mail_batch_limit=2)
    plan = _fake_plan(10)

    monkeypatch.setattr("gmail.campaign.build_plan", lambda *a, **k: plan)
    monkeypatch.setattr("gmail.campaign.send_campaign", lambda *a, **k: _FakeSendResult())

    scheduler._send_mail()

    assert sum(1 for r in plan.recipients if r.will_send) == 2
    assert plan.sendable == 2


def test_send_mail_refuses_to_run_without_a_template(tmp_config) -> None:
    config = tmp_config.model_copy(
        update={
            "scheduler": SchedulerSection.model_validate(
                {"action": "send", "mail_template": "   "}
            )
        }
    )
    with pytest.raises(ValueError, match="樣板"):
        CrawlScheduler(config)._send_mail()


def test_crawl_only_schedule_never_sends(patch_config, db_session, monkeypatch) -> None:
    config = patch_config.model_copy(
        update={
            "scheduler": SchedulerSection.model_validate(
                {"enabled": True, "action": "crawl", "sources": ["sample"],
                 "verify_after_crawl": False}
            )
        }
    )
    scheduler = CrawlScheduler(config)
    sent: list[int] = []
    monkeypatch.setattr(scheduler, "_send_mail", lambda: sent.append(1))

    scheduler._run_job()

    assert sent == []
    assert scheduler.state.run_count == 1


def test_send_only_schedule_never_crawls(tmp_config, monkeypatch) -> None:
    """只寄信的排程不該因為「沒有可爬的來源」就整個不執行。"""
    scheduler = _mail_scheduler(tmp_config, enabled=True)

    def _explode(*_a, **_k):
        raise AssertionError("只寄信的排程不應該建立爬蟲 pipeline")

    monkeypatch.setattr("crawler.pipeline.CrawlPipeline", _explode)
    monkeypatch.setattr(scheduler, "_send_mail", lambda: 0)

    scheduler._run_job()

    assert scheduler.state.run_count == 1


def test_crawl_and_send_sends_after_crawling(patch_config, db_session, monkeypatch) -> None:
    """順序有意義：crawl_and_send 要寄的是「剛剛收集到的」名單。"""
    config = patch_config.model_copy(
        update={
            "scheduler": SchedulerSection.model_validate(
                {"enabled": True, "action": "crawl_and_send", "mail_template": "t",
                 "sources": ["sample"], "verify_after_crawl": False}
            )
        }
    )
    scheduler = CrawlScheduler(config)
    order: list[str] = []

    original_pipeline = __import__("crawler.pipeline", fromlist=["CrawlPipeline"]).CrawlPipeline

    class _Traced(original_pipeline):  # type: ignore[misc, valid-type]
        def run_source(self, name, cancel_event=None):
            order.append("crawl")
            return super().run_source(name, cancel_event=cancel_event)

    monkeypatch.setattr("crawler.pipeline.CrawlPipeline", _Traced)
    monkeypatch.setattr(scheduler, "_send_mail", lambda: order.append("send"))

    scheduler._run_job()

    assert order == ["crawl", "send"]


def test_action_text_is_reported_in_chinese(tmp_config) -> None:
    scheduler = _mail_scheduler(tmp_config, action="crawl_and_send")
    assert scheduler.action_text == "爬取後寄信"
