"""把「別人 clone 下來能不能跑」變成測試，而不是靠人記得。

這裡每一條都對應一個真的發生過的問題：

* ``requirements.txt`` 曾經同時漏掉 PySide6 與 cryptography。前者讓程式一開
  就 ImportError；後者更糟——``core/crypto.py`` 會安靜地丟
  ``EncryptionUnavailable``，程式照常啟動，只是 ``database.encrypt`` 形同虛設。
* ``.sh`` 帶 CRLF 在 macOS 上會噴 ``bad interpreter: ...^M``，訊息完全看不出
  真正原因。
* 絕對路徑寫進追蹤檔案，等於只有作者那台機器跑得起來。
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _git(*args: str) -> str:
    """跑 git，拿不到就讓呼叫端 skip。"""
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout


@pytest.fixture(scope="module")
def tracked_files() -> list[str]:
    try:
        output = _git("ls-files")
    except (OSError, subprocess.CalledProcessError):  # 沒有 git，或不是 repo
        pytest.skip("需要可用的 git 與 git repo")
    return [line for line in output.splitlines() if line.strip()]


# --------------------------------------------------------------- 絕對路徑

#: 會讓專案綁死在某一台機器上的寫法。用 raw string 避免跳脫字元誤判。
_ABSOLUTE_PATH = re.compile(
    r"""(?x)
    [A-Za-z]:[\\/]{1,2}Users     # C:\Users\... 或 C:/Users/...
  | /Users/[A-Za-z0-9._-]+/      # /Users/someone/
  | /home/[a-z][a-z0-9._-]*/     # /home/someone/
    """
)

#: 這些檔案本來就會提到路徑寫法，是說明不是設定。
_PATH_DOC_ALLOWLIST = {
    ".gitattributes",
    "README.md",
    "console.bat",
    "console.sh",
    "tests/test_repo_hygiene.py",
}


def test_no_tracked_file_contains_an_absolute_path(tracked_files):
    """絕對路徑一旦進 repo，別人 clone 下來就跑不起來。"""
    offenders: list[str] = []
    for name in tracked_files:
        if name in _PATH_DOC_ALLOWLIST:
            continue
        path = ROOT / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # 二進位檔
        for number, line in enumerate(text.splitlines(), start=1):
            if _ABSOLUTE_PATH.search(line):
                offenders.append(f"{name}:{number}: {line.strip()[:100]}")
    assert not offenders, "追蹤檔案裡有絕對路徑：\n" + "\n".join(offenders)


# ------------------------------------------------------- shell script 可攜性


def test_shell_scripts_are_executable_and_use_lf(tracked_files):
    """Mac 使用者要能直接執行，而且不能被 CRLF 卡住。

    ``.command`` 一起檢查：macOS 的 Finder 不會執行 ``.sh``（會用文字編輯器
    打開），``.command`` 才是它認得的「雙擊執行」副檔名——所以那個檔案更
    需要權限與換行都正確，它就是使用者實際會點的那一個。
    """
    scripts = [
        name for name in tracked_files if name.endswith((".sh", ".command"))
    ]
    assert scripts, "應該要有給 macOS/Linux 用的啟動腳本"
    assert any(name.endswith(".command") for name in scripts), (
        "macOS 的 Finder 不執行 .sh，需要一個 .command 才能雙擊啟動"
    )

    for name in scripts:
        mode = _git("ls-files", "-s", "--", name).split()[0]
        assert mode == "100755", f"{name} 在 git 裡不是可執行檔（{mode}）"

        blob = subprocess.run(
            ["git", "show", f":{name}"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
        assert b"\r\n" not in blob, f"{name} 在 repo 裡是 CRLF，macOS 上會無法執行"


# --------------------------------------------------------------- 相依宣告

#: import 名稱與 PyPI 套件名不一致的對照。
_IMPORT_TO_PACKAGE = {
    "yaml": "pyyaml",
    "dotenv": "python-dotenv",
    "bs4": "beautifulsoup4",
    "dns": "dnspython",
    "PIL": "pillow",
    "docx": "python-docx",
    "pptx": "python-pptx",
}

#: 專案自己的頂層套件，不是外部相依。
_LOCAL_PACKAGES = {
    "app_main", "conftest", "controllers", "core", "crawler", "database",
    "exporter", "gmail", "gui_qt", "main", "tests", "verifier",
}

_SKIP_DIRS = {".venv", ".git", "build", "dist", "docs", "__pycache__", ".pytest_cache"}


def _normalise(name: str) -> str:
    return _IMPORT_TO_PACKAGE.get(name, name).lower().replace("-", "_")


def _declared_packages() -> set[str]:
    names: set[str] = set()
    for filename in ("requirements.txt", "requirements-dev.txt"):
        for raw in (ROOT / filename).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", "-")):
                continue
            # 砍掉版本條件與 extras，只留套件名。
            line = re.split(r"[<>=!~;\[]", line)[0]
            names.add(_normalise(line.strip()))
    return names


def _imported_packages() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in ROOT.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            for name in names:
                if name in sys.stdlib_module_names or name in _LOCAL_PACKAGES:
                    continue
                found.setdefault(name, set()).add(str(path.relative_to(ROOT)))
    return found


def test_every_imported_package_is_declared_in_requirements():
    """漏宣告一個相依，別人照 README 裝完就是壞的。

    cryptography 這個特別要命：缺了它程式不會當，只是加密安靜地失效。
    """
    declared = _declared_packages()
    missing = {
        name: sorted(files)
        for name, files in _imported_packages().items()
        if _normalise(name) not in declared
    }
    assert not missing, "有 import 但 requirements 沒宣告：\n" + "\n".join(
        f"  {name} <- {', '.join(files[:5])}" for name, files in sorted(missing.items())
    )
