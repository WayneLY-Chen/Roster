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
