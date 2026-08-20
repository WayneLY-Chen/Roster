"""檢查有沒有新版本，以及在程式裡直接更新。

## 為什麼版本號是從 CHANGELOG.md 讀的

因為那一份同時回答了兩個問題：「最新版是幾號」與「它改了什麼」。第一個
``## 1.20.0 — 日期`` 標題就是版本，到下一個 ``## `` 之間就是更新說明。
去讀 ``core/constants.py`` 只拿得到號碼，還要再發一次請求拿說明。

順帶一個好處：CHANGELOG 一定會跟著版本一起更新（「更新資訊」頁直接顯示它），
所以它不會有「忘記同步」的問題。

## 檢查與更新是兩件事

**檢查**只要連得上 GitHub 就行，用 ZIP 下載的人也能知道有新版。
**更新**需要這個資料夾是 ``git clone`` 來的——沒有 ``.git`` 就沒有辦法在原地
更新，那時候會告訴使用者去下載新的 ZIP，並列出哪些檔案是他自己的。

更新的動作跟 ``Windows/更新.bat`` 那幾支腳本完全一樣（``git pull --ff-only``
＋補裝套件），差別只在這裡是從程式裡按的。改過 ``config.yaml`` 一樣會先收起來
再放回去。
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from core.config import get_config, read_user_settings, save_user_settings
from core.constants import VERSION, LogCategory
from core.logging_setup import get_logger

log = get_logger(LogCategory.GUI)

#: 專案根目錄（這個檔案在 core/ 底下）。
ROOT = Path(__file__).resolve().parent.parent

CHANGELOG_URL = (
    "https://raw.githubusercontent.com/WayneLY-Chen/Roster/main/CHANGELOG.md"
)
RELEASES_URL = "https://github.com/WayneLY-Chen/Roster"

#: 檢查用的逾時。開程式時在背景跑，連不上就算了，不值得讓使用者等。
CHECK_TIMEOUT = 8.0

#: 自動檢查的間隔。
#:
#: 一天一次。這個專案不是每天發版，而每次啟動都去敲 GitHub 一次，對使用者是
#: 沒必要的連線、對 GitHub 是沒必要的流量。手動按「檢查更新」不受這個限制。
CHECK_INTERVAL = timedelta(days=1)

#: 上次自動檢查的時間存在 user_settings.yaml 的這個位置。
_LAST_CHECK_SECTION = "app"
_LAST_CHECK_KEY = "last_update_check"

_HEADING = re.compile(r"^##\s+(\d+(?:\.\d+)*)\s*(?:—|-|–)?\s*(.*)$")


@dataclass(frozen=True, slots=True)
class UpdateCheck:
    """檢查結果。"""

    current: str
    latest: str = ""
    notes: str = ""
    #: 這個資料夾能不能在原地更新（是 git clone 來的，而且有 git）。
    can_update_in_place: bool = False
    #: 檢查失敗時的原因。有值代表「不知道」，不是「已經最新」。
    error: str = ""

    @property
    def available(self) -> bool:
        if self.error or not self.latest:
            return False
        return _parse(self.latest) > _parse(self.current)


@dataclass(frozen=True, slots=True)
class UpdateResult:
    """更新結果。"""

    ok: bool
    message: str
    #: 更新完成之後的版本號（讀本機檔案，所以一定是真的）。
    version: str = ""


def _parse(version: str) -> tuple[int, ...]:
    """``"1.20.0"`` -> ``(1, 20, 0)``。

    用整數 tuple 比大小，不要比字串——字串比較會說 "1.9.0" > "1.20.0"，
    而那正是版本號最常見的形狀。
    """
    parts = []
    for chunk in str(version or "0").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def parse_changelog(text: str) -> tuple[str, str]:
    """從 CHANGELOG 內容取出「最新版本號」與「那一版的說明」。"""
    lines = (text or "").splitlines()
    version = ""
    notes: list[str] = []
    for line in lines:
        match = _HEADING.match(line.strip())
        if match:
            if version:  # 碰到下一個版本的標題，這一版的說明結束
                break
            version = match.group(1)
            continue
        if version:
            notes.append(line)
    return version, "\n".join(notes).strip()


def has_git_checkout() -> bool:
    """這個資料夾是 git clone 來的、而且系統上有 git 嗎？"""
    if not (ROOT / ".git").exists():
        return False
    try:
        subprocess.run(
            ["git", "--version"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def check(*, report=None, cancel_event=None) -> UpdateCheck:
    """去 GitHub 問一次最新版是幾號。**會連網**，放背景執行緒跑。"""
    in_place = has_git_checkout()
    try:
        response = httpx.get(CHANGELOG_URL, timeout=CHECK_TIMEOUT)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.debug("檢查更新失敗：{}", exc)
        return UpdateCheck(
            current=VERSION,
            can_update_in_place=in_place,
            error=f"連不上 GitHub：{exc}",
        )

    latest, notes = parse_changelog(response.text)
    if not latest:
        return UpdateCheck(
            current=VERSION,
            can_update_in_place=in_place,
            error="讀不出遠端的版本號。",
        )
    return UpdateCheck(
        current=VERSION,
        latest=latest,
        notes=notes,
        can_update_in_place=in_place,
    )


# ------------------------------------------------------------ 自動檢查的節流


def due_for_check(now: datetime | None = None) -> bool:
    """現在該做一次自動檢查了嗎？

    關掉 ``app.check_for_updates`` 或距離上次不到一天，都回 False。
    """
    if not get_config().app.check_for_updates:
        return False
    now = now or datetime.now()
    stamp = read_user_settings().get(_LAST_CHECK_SECTION, {})
    raw = stamp.get(_LAST_CHECK_KEY) if isinstance(stamp, dict) else None
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    return now - last >= CHECK_INTERVAL


def remember_check(now: datetime | None = None) -> None:
    """記下「剛剛檢查過了」。"""
    now = now or datetime.now()
    try:
        save_user_settings(_LAST_CHECK_SECTION, {_LAST_CHECK_KEY: now.isoformat(timespec="seconds")})
    except Exception as exc:  # 寫不進去不該讓程式開不起來
        log.debug("記錄檢查時間失敗：{}", exc)


# ------------------------------------------------------------------ 實際更新


def _run(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def apply_update(*, report=None, cancel_event=None) -> UpdateResult:
    """在原地更新到最新版。**會連網、會動檔案**，放背景執行緒跑。

    步驟跟 ``Windows/更新.bat`` 一模一樣，只是從程式裡按的：改過的追蹤檔案先
    收起來、``git pull --ff-only``、把修改放回去、補裝新增的套件。
    """

    def say(message: str) -> None:
        log.info("更新：{}", message)
        if report:
            report(message)

    if not has_git_checkout():
        return UpdateResult(
            ok=False,
            message=(
                "這個資料夾不是用 git 下載的，沒辦法在程式裡直接更新。\n\n"
                "到 GitHub 下載新版之後，記得把 data/（資料庫）、attachments/、"
                "output/、backups/、user_settings.yaml、templates/mail/ 這幾個"
                "搬過去——那些是你的東西，新下載的裡面沒有。"
            ),
        )

    say("檢查本機有沒有改過的檔案…")
    dirty = _run(["git", "diff", "--quiet", "HEAD"]).returncode != 0
    if dirty:
        say("把你改過的設定先收起來…")
        stash = _run(["git", "stash", "push", "-m", "roster-in-app-update"])
        if stash.returncode != 0:
            return UpdateResult(
                ok=False,
                message="沒辦法把你改過的設定收起來，什麼都沒有變動。\n\n" + stash.stderr.strip(),
            )

    say("下載新版本…")
    pull = _run(["git", "pull", "--ff-only"])
    if pull.returncode != 0:
        if dirty:
            _run(["git", "stash", "pop"])
        return UpdateResult(
            ok=False,
            message=(
                "更新裝不上去。最常見的原因是這一份自己有額外的修改記錄，"
                "沒辦法直接接上去。\n\n" + (pull.stderr.strip() or pull.stdout.strip())
            ),
        )

    if dirty:
        say("把你改過的設定放回去…")
        pop = _run(["git", "stash", "pop"])
        if pop.returncode != 0:
            return UpdateResult(
                ok=False,
                message=(
                    "新版本已經裝好了，但你自己改過的設定沒辦法自動放回去"
                    "——同樣幾行兩邊都改過。\n\n"
                    "它沒有不見：開啟「命令列」執行 git stash pop 自己處理。"
                ),
            )

    say("補裝新增的套件…")
    pip = _run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "--quiet"], timeout=900)
    if pip.returncode != 0:
        return UpdateResult(
            ok=False,
            message=(
                "新版本已經下載好了，但套件安裝失敗。重新啟動之後到「命令列」"
                "執行一次：\n  python -m pip install -r requirements.txt\n\n"
                + (pip.stderr.strip()[:400])
            ),
        )

    return UpdateResult(ok=True, message="更新完成。", version=_local_version())


def _local_version() -> str:
    """更新之後讀本機檔案拿版本號。

    不能用 ``core.constants.VERSION``——那是程式啟動時就 import 進記憶體的，
    更新之後那個值還是舊的。要重新讀檔案才是真的。
    """
    try:
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    version, _ = parse_changelog(text)
    return version
