"""Tests for core/updates.py.

沒有一條會連到真的網路或真的動到工作目錄——``httpx`` 用 MockTransport，
``subprocess.run`` 用假的。
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta

import httpx
import pytest

from core.updates import (
    CHECK_INTERVAL,
    UpdateCheck,
    _parse,
    apply_update,
    check,
    due_for_check,
    parse_changelog,
)

SAMPLE = """# 更新紀錄

## 1.21.0 — 2026-08-21

程式裡就可以更新了。

### 新增

- 更新通知

## 1.20.0 — 2026-08-20

AI 的第一步。
"""


# --------------------------------------------------------------- 版本比大小


@pytest.mark.parametrize(
    ("older", "newer"),
    [
        ("1.19.1", "1.20.0"),
        ("1.9.0", "1.20.0"),   # 字串比較會說反話，這一組就是為了釘住它
        ("1.20.0", "1.20.1"),
        ("0.9", "1.0"),
        ("1.2", "1.2.1"),
    ],
)
def test_version_ordering(older, newer):
    """版本號要當數字比，不能當字串比。

    ``"1.9.0" > "1.20.0"`` 在字串比較下是 True——而那正是版本號最常見的形狀。
    照字串比的話，使用者從 1.9 升到 1.20 之後，程式會一直告訴他有「新版」1.9。
    """
    assert _parse(older) < _parse(newer)


def test_same_version_is_not_an_update():
    assert not UpdateCheck(current="1.20.0", latest="1.20.0").available


def test_older_remote_is_not_an_update():
    """開發機上跑著還沒推出去的版本時，不要叫他「更新」回舊版。"""
    assert not UpdateCheck(current="1.21.0", latest="1.20.0").available


def test_an_error_is_not_the_same_as_up_to_date():
    """連不上時 available 要是 False，但那是「不知道」不是「已經最新」。

    兩者混在一起的話，網路壞掉會被顯示成「已經是最新版」——使用者就再也不會
    去檢查了。
    """
    result = UpdateCheck(current="1.0.0", latest="", error="連不上")
    assert not result.available
    assert result.error


# ----------------------------------------------------------- CHANGELOG 解析


def test_parse_changelog_takes_the_first_heading_only():
    version, notes = parse_changelog(SAMPLE)
    assert version == "1.21.0"
    assert "更新通知" in notes
    # 只要這一版的說明，不要把下一版的也吃進來。
    assert "AI 的第一步" not in notes


def test_parse_changelog_survives_junk():
    assert parse_changelog("") == ("", "")
    assert parse_changelog("沒有標題的一段字") == ("", "")


# ------------------------------------------------------------------- 檢查


def _mock_http(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    def fake_get(url, **kwargs):
        with httpx.Client(transport=transport) as client:
            return client.get(url, **kwargs)

    monkeypatch.setattr(httpx, "get", fake_get)


def test_check_reads_the_version_from_github(monkeypatch):
    _mock_http(monkeypatch, lambda request: httpx.Response(200, text=SAMPLE))
    monkeypatch.setattr("core.updates.has_git_checkout", lambda: True)
    monkeypatch.setattr("core.updates.VERSION", "1.20.0")

    result = check()
    assert result.latest == "1.21.0"
    assert result.can_update_in_place is True
    assert not result.error


def test_check_reports_network_failure_without_raising(monkeypatch):
    """開程式時在背景跑，網路壞掉不可以讓它丟例外。"""

    def boom(request):
        raise httpx.ConnectError("no network", request=request)

    _mock_http(monkeypatch, boom)
    monkeypatch.setattr("core.updates.has_git_checkout", lambda: False)

    result = check()
    assert result.error
    assert not result.available
    assert result.can_update_in_place is False


# --------------------------------------------------------------- 檢查節流


def test_due_for_check_respects_the_setting(monkeypatch, tmp_config):
    monkeypatch.setattr("core.updates.read_user_settings", dict)

    class _Off:
        class app:
            check_for_updates = False

    monkeypatch.setattr("core.updates.get_config", lambda: _Off)
    assert due_for_check() is False


def test_due_for_check_is_true_when_never_checked(monkeypatch, tmp_config):
    monkeypatch.setattr("core.updates.read_user_settings", dict)
    assert due_for_check() is True


def test_due_for_check_waits_a_day(monkeypatch, tmp_config):
    now = datetime(2026, 8, 20, 12, 0, 0)
    just_now = (now - timedelta(minutes=5)).isoformat()
    monkeypatch.setattr(
        "core.updates.read_user_settings", lambda: {"app": {"last_update_check": just_now}}
    )
    assert due_for_check(now) is False

    long_ago = (now - CHECK_INTERVAL - timedelta(minutes=1)).isoformat()
    monkeypatch.setattr(
        "core.updates.read_user_settings", lambda: {"app": {"last_update_check": long_ago}}
    )
    assert due_for_check(now) is True


def test_a_corrupt_timestamp_does_not_block_checking(monkeypatch, tmp_config):
    monkeypatch.setattr(
        "core.updates.read_user_settings", lambda: {"app": {"last_update_check": "垃圾"}}
    )
    assert due_for_check() is True


# ------------------------------------------------------------------- 更新


def test_update_refuses_without_a_git_checkout(monkeypatch):
    """ZIP 下載的人不能在原地更新，訊息要列出哪些檔案是他自己的。"""
    monkeypatch.setattr("core.updates.has_git_checkout", lambda: False)
    result = apply_update()
    assert not result.ok
    assert "data/" in result.message
    assert "user_settings.yaml" in result.message


class _FakeRun:
    """記下跑過哪些 git 指令，並照腳本回傳結果。"""

    def __init__(self, script: dict[str, int]):
        self.script = script
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        key = " ".join(args[:3])
        code = self.script.get(key, 0)
        return subprocess.CompletedProcess(args, code, stdout="", stderr="boom" if code else "")

    def ran(self, prefix: str) -> bool:
        return any(" ".join(call).startswith(prefix) for call in self.calls)


def test_update_stashes_local_edits_and_puts_them_back(monkeypatch):
    """改過 config.yaml 不可以擋住更新，也不可以被蓋掉。"""
    monkeypatch.setattr("core.updates.has_git_checkout", lambda: True)
    monkeypatch.setattr("core.updates._local_version", lambda: "1.21.0")
    fake = _FakeRun({"git diff --quiet": 1})  # 1 = 有本機修改
    monkeypatch.setattr("core.updates._run", fake)

    result = apply_update()
    assert result.ok, result.message
    assert fake.ran("git stash push")
    assert fake.ran("git pull --ff-only")
    assert fake.ran("git stash pop")


def test_update_with_a_clean_tree_does_not_stash(monkeypatch):
    monkeypatch.setattr("core.updates.has_git_checkout", lambda: True)
    monkeypatch.setattr("core.updates._local_version", lambda: "1.21.0")
    fake = _FakeRun({})  # 0 = 沒有本機修改
    monkeypatch.setattr("core.updates._run", fake)

    assert apply_update().ok
    assert not fake.ran("git stash")


def test_a_failed_pull_restores_the_stash(monkeypatch):
    """更新失敗時，使用者收起來的修改一定要放回去。

    不放回去的話，他的設定看起來就是憑空消失了——而且他不知道 git stash
    這個東西存在。
    """
    monkeypatch.setattr("core.updates.has_git_checkout", lambda: True)
    fake = _FakeRun({"git diff --quiet": 1, "git pull --ff-only": 1})
    monkeypatch.setattr("core.updates._run", fake)

    result = apply_update()
    assert not result.ok
    assert fake.ran("git stash pop")


def test_update_reports_the_version_from_disk_not_memory(monkeypatch):
    """更新後的版本號要重新讀檔案。

    ``core.constants.VERSION`` 是程式啟動時就 import 進記憶體的，更新之後
    那個值還是舊的——拿它回報會告訴使用者「已更新到 1.20.0」，而他剛裝的是
    1.21.0。
    """
    monkeypatch.setattr("core.updates.has_git_checkout", lambda: True)
    monkeypatch.setattr("core.updates._run", _FakeRun({}))
    monkeypatch.setattr("core.updates._local_version", lambda: "9.9.9")

    assert apply_update().version == "9.9.9"
