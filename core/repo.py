"""問 git「這個資料夾裡有哪些檔案」。

只有 ``python main.py security`` 在用，但**不能**放在 ``main.py`` 裡：那個檔案
在 import 的當下就會做兩件有副作用的事——把專案根目錄插進 ``sys.path``，以及
呼叫 ``sys.stdout.reconfigure(encoding="utf-8")``。後者在測試裡特別糟，因為
那時候的 ``sys.stdout`` 是 pytest 的擷取物件，重新設定它會影響**整場**測試的
輸出處理。

所以放這裡：純函式、零副作用，測試可以直接 import。

## 為什麼一律用 ``-z`` 而不是 ``text=True``

``subprocess.run(text=True)`` 是拿**系統語言**的編碼去解碼子程序的輸出，繁體
中文 Windows 那個是 cp950。而這個專案自己就有中文檔名（``docs/名單.md``、
``docs/爬取.md``），git 是以 UTF-8 吐出來的——解碼會在 subprocess 自己的讀取
執行緒裡爆掉，那個例外**攔不到**，只會讓 ``result.stdout`` 變成 ``None``。

``-z`` 另外還解決兩件事：檔名以 NUL 分隔，所以含換行的檔名不會被切錯；
而且 git 不會把非 ASCII 路徑轉成 ``\346\226\207`` 那種跳脫寫法。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

#: 等 git 回應的上限。這幾個指令都只是讀索引，正常是毫秒等級。
TIMEOUT_SECONDS = 20


def git_paths(root: Path, args: list[str]) -> list[str] | None:
    """跑一個會吐出 NUL 分隔路徑的 git 指令。

    :returns: 路徑清單；``root`` 不是 git 儲存庫（或 git 不存在）時回 ``None``，
        讓呼叫端能分辨「沒有這種檔案」與「根本查不了」。
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    text = result.stdout.decode("utf-8", errors="replace")
    return [name.strip() for name in text.split("\0") if name.strip()]


def git_tracked_files(root: Path) -> list[str] | None:
    """git 已經追蹤的檔案。"""
    return git_paths(root, ["ls-files", "-z"])


def git_untracked_unignored_files(root: Path) -> list[str] | None:
    """還沒被追蹤、而且也沒有被 ``.gitignore`` 忽略的檔案。

    這是安全檢查最該看的一群：``--others`` 是「未追蹤」，``--exclude-standard``
    把已忽略的排除掉，剩下的正好是「下一次 ``git add -A`` 會被掃進去的東西」。
    """
    return git_paths(root, ["ls-files", "--others", "--exclude-standard", "-z"])
