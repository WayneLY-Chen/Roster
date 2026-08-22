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
    "ai", "app_main", "conftest", "controllers", "core", "crawler", "database",
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


# ---------------------------------------------------------------- 文件連結

#: 說明文件。README 只留安裝與導覽，細節分在 docs/ 底下——這一拆就多了一整
#: 排跨檔案的相對連結，而 Markdown 的壞連結在 GitHub 上是靜靜地變成 404。
_DOC_FILES = [ROOT / "README.md", ROOT / "CHANGELOG.md", *sorted((ROOT / "docs").glob("*.md"))]

_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _anchors(text: str) -> set[str]:
    """GitHub 會替每一個標題產生的錨點。

    規則是「轉小寫、去掉標點、空白換成連字號」，中日韓文字會原樣保留——
    所以「## 這家公司還在不在？」的錨點是 ``#這家公司還在不在``。
    """
    found = set()
    for line in text.splitlines():
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            found.add(
                re.sub(r"[^\w\u4e00-\u9fff\- ]", "", title).strip().replace(" ", "-").lower()
            )
    return found


def test_every_link_between_the_docs_resolves(tracked_files):
    """README 拆成 docs/ 之後，跨檔案的連結壞掉在網頁上是無聲的 404。

    「本機有這個檔案」不等於「連結會通」。實際發生過：``docs/`` 整個被
    ``.gitignore`` 擋著，說明文件在自己電腦上打得開，但 clone 下來的人與
    GitHub 上的頁面看到的全是 404。所以這裡問的是 git **有沒有追蹤**它，
    不是檔案系統上存不存在。
    """
    tracked = {Path(name) for name in tracked_files}
    broken: list[str] = []
    for doc in _DOC_FILES:
        text = doc.read_text(encoding="utf-8")
        for target in _LINK.findall(text):
            if target.startswith(("http://", "https://", "mailto:", "#!")):
                continue
            path_part, _, anchor = target.partition("#")
            owner = doc
            if path_part:
                resolved = (doc.parent / path_part).resolve()
                if not resolved.exists():
                    broken.append(f"{doc.name}: 檔案不存在 -> {target}")
                    continue
                relative = resolved.relative_to(ROOT)
                if relative not in tracked:
                    broken.append(
                        f"{doc.name}: 指向沒有進版控的檔案 -> {target}"
                        "（本機看得到，clone 下來的人看到 404）"
                    )
                    continue
                owner = resolved
            if anchor and anchor.lower() not in _anchors(
                owner.read_text(encoding="utf-8")
            ):
                broken.append(f"{doc.name}: 找不到錨點 -> {target}")
    assert not broken, "說明文件裡有壞掉的連結：\n" + "\n".join(f"  {b}" for b in broken)


def test_the_readme_stays_short_enough_to_read():
    """README 是專案的門面，長到要捲十次就沒有人會讀完。

    細節該放在 docs/ 底下，這裡只留安裝、上手與導覽。這條沒有什麼神聖的
    數字，它只是一個「又開始往裡面塞東西了」的提醒。
    """
    lines = len((ROOT / "README.md").read_text(encoding="utf-8").splitlines())

    assert lines <= 320, f"README 有 {lines} 行，太長了——把細節搬到 docs/ 底下"


# ---------------------------------------------------- python main.py security
#
# 這一支是 README 明文要求「上傳 git 之前跑一次」的安全檢查。它自己壞掉的話
# 沒有任何東西會發現——它壞掉的樣子就是「印出一個綠色的勾」。


#
# 這些輔助函式住在 ``core/repo.py`` 而不是 ``main.py``，就是為了讓這裡能直接
# import。``main.py`` 在 import 的當下會 ``sys.stdout.reconfigure(...)``——
# 在測試裡那個 ``sys.stdout`` 是 pytest 的擷取物件，重新設定它會影響整場
# 測試的輸出處理。


def test_the_security_check_survives_non_ascii_filenames():
    """檔名有中文時不能爆掉。

    ``subprocess.run(text=True)`` 會拿**系統語言**的編碼去解碼子程序的輸出，
    繁體中文 Windows 那個是 cp950；而這個專案自己就有 ``docs/名單.md``、
    ``docs/爬取.md``，git 是以 UTF-8 吐出來的。解碼在 subprocess 自己的讀取
    執行緒裡爆掉，攔不到，只會讓 stdout 變成 None，接著以 AttributeError
    收場——也就是說，`python main.py security` 在它最主要的使用環境上
    直接當掉。
    """
    from core.repo import git_tracked_files

    tracked = git_tracked_files(ROOT)

    assert tracked, "應該要列得出已追蹤的檔案"
    assert any(name.endswith("docs/名單.md") for name in tracked), (
        "中文檔名沒有正確解碼出來"
    )


def test_the_security_check_sees_files_that_are_neither_tracked_nor_ignored(tmp_path):
    """「還沒追蹤、但也沒被忽略」是風險最高的狀態，一定要看得到。

    原本的檢查只看「已經被追蹤的檔案」和三個寫死的路徑，所以一份放在專案
    根目錄的名單檔兩邊都不會被提到——然後 ``git add -A`` 一下就進了公開的
    repo。實際發生過：一份 2699 家公司的聯絡資料躺在根目錄，而這支指令
    印的是「檢查通過，可以安全上傳 git」。
    """
    from core.repo import git_untracked_unignored_files

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("ignored.csv\n", encoding="utf-8")
    (tmp_path / "會員名單.xlsx").write_bytes(b"x")
    (tmp_path / "ignored.csv").write_text("x", encoding="utf-8")

    loose = git_untracked_unignored_files(tmp_path)

    assert loose is not None
    assert "會員名單.xlsx" in loose
    assert "ignored.csv" not in loose, ".gitignore 蓋到的不該再報一次"


def test_the_git_helpers_report_none_outside_a_repository(tmp_path):
    """不是 git 儲存庫時回 ``None``，讓呼叫端能分辨「沒有」與「查不到」。"""
    from core.repo import git_tracked_files, git_untracked_unignored_files

    assert git_tracked_files(tmp_path) is None
    assert git_untracked_unignored_files(tmp_path) is None


#: 每一支「雙擊之後會開一個終端機視窗」的腳本。每一支都該印貓跟狗。
#:
#: 判準是「使用者會不會看到一個黑底視窗」，不是「它算不算啟動器」——診斷與
#: 建立 App 那兩支也會開視窗，少了圖案就是不一致，而不一致沒有理由。
LAUNCHERS = (
    "Windows/安裝.bat", "Windows/啟動.bat", "Windows/命令列.bat", "Windows/更新.bat",
    "macOS/安裝.command", "macOS/啟動.command", "macOS/命令列.command", "macOS/更新.command",
    "macOS/建立App.command", "macOS/診斷.command",
    "Linux/install.sh", "Linux/start.sh", "Linux/console.sh", "Linux/update.sh",
)


@pytest.mark.parametrize("name", LAUNCHERS)
def test_every_launcher_prints_the_mascots(name):
    """每一支腳本都要印 assets/pets.txt。

    這一條是使用者回報來的：Windows 開起來沒有貓跟狗，mac 有。當時的真相是
    「三個平台都只有安裝程式有」，但那個不一致本身就是 bug——同一個程式在
    不同作業系統上給人不同的第一印象，而且沒有任何理由。

    圖只存在 assets/pets.txt 一份。每個地方各自貼一份的話，改一次要記得改
    十幾個地方，而漏掉的那幾個沒有人會發現。
    """
    script = ROOT / name
    assert script.exists(), f"{name} 不見了"
    text = script.read_text(encoding="utf-8", errors="replace")
    # 直接 type/cat 那個檔案，或者呼叫 macOS/_共用.sh 裡的 print_pets——兩種
    # 都算，重點是「印得出來」而不是用哪一種寫法。
    assert "pets.txt" in text or "print_pets" in text, f"{name} 沒有印貓跟狗"


def test_the_mascots_live_in_exactly_one_file():
    """圖本身不准被複製到腳本裡。

    ``pets.txt`` 用全形字元排版，混進腳本裡每改一次都要重數空白；而且 .bat
    檔是刻意只用 ASCII 的（cmd.exe 遇到非 ASCII 會把後面整個檔案解析壞）。
    """
    art = (ROOT / "assets" / "pets.txt").read_text(encoding="utf-8")
    signature = art.strip().splitlines()[0].strip()
    assert signature, "pets.txt 是空的"
    for name in LAUNCHERS:
        text = (ROOT / name).read_text(encoding="utf-8", errors="replace")
        assert signature not in text, f"{name} 把圖複製進去了，應該 type/cat 那個檔案"


def test_the_mascots_use_crlf():
    """**這一條是回歸測試。**

    使用者回報 Windows 開起來看不到貓跟狗，mac 看得到。腳本兩邊都有印，圖也
    兩邊都讀得到——差別在換行字元：這個檔案以前是 LF-only。

    **Windows 主控台的 LF 只把游標往下移一行，不會回到第 0 欄。** 所以第二行
    會從第一行結束的地方開始畫，第三行再往右一段……九行疊完是一團往右下延伸、
    還會折行的東西，完全看不出是兩隻動物。macOS 與 Linux 的終端機把 LF 當成
    換行加歸位，同一個檔案在那邊一直是好的。

    ``.gitattributes`` 用 ``-text`` 把它釘成 CRLF：不是 ``eol=crlf``，因為那個
    只改「簽出時怎麼轉換」，blob 沒變的話已經 clone 過的人 pull 完檔案不會被
    重寫，bug 還在。
    """
    art = (ROOT / "assets" / "pets.txt").read_bytes()
    lone_lf = art.count(b"\n") - art.count(b"\r\n")
    assert art.count(b"\r\n") > 0, (
        "assets/pets.txt 不是 CRLF——Windows 上會印成一團往右下延伸的東西"
    )
    assert lone_lf == 0, f"assets/pets.txt 裡還有 {lone_lf} 個單獨的 LF"

    # 而且 git 不能再把它正規化回 LF，否則下一次 clone 又壞掉。
    attr = subprocess.run(
        ["git", "check-attr", "text", "--", "assets/pets.txt"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "text: unset" in attr, (
        ".gitattributes 要用 -text 把 assets/pets.txt 釘住，"
        f"現在是：{attr.strip()}"
    )


def test_no_launcher_restores_the_codepage_after_drawing():
    """**這一條是回歸測試，而且是被使用者的截圖抓到的。**

    每一支 .bat 為了讓 ``type`` 讀得懂 UTF-8 的圖，會先 ``chcp 65001``。以前
    它們印完圖之後還會把字碼頁切回原本的值——**而每一次 chcp 都會把主控台的
    畫面清掉**，所以那一行的實際效果是「把剛剛印出來的貓跟狗擦掉」。

    症狀是使用者看到「完全沒有貓跟狗」，連空行都少一行。而它在任何用管線接
    輸出的測試裡都看不出來——管線沒有畫面可以被清，所以位元組全都在。

    切回去本來也沒有必要：字碼頁只活在這一個視窗裡，視窗關掉就沒了。
    """
    offenders = []
    for name in LAUNCHERS:
        if not name.endswith((".bat", ".cmd")):
            continue
        text = (ROOT / name).read_text(encoding="utf-8", errors="replace")
        # 只看真的會執行的那幾行，註解裡提到 chcp 是在解釋為什麼不要用它。
        commands = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().upper().startswith("REM")
        ]
        switches = [line for line in commands if line.lower().startswith("chcp")]
        if len(switches) > 1 or any("OLDCP" in line for line in commands):
            offenders.append(f"{name}（{switches}）")
    assert not offenders, (
        f"這幾支又在印完圖之後切回字碼頁了，會把圖擦掉：{offenders}"
    )


def test_batch_files_contain_no_non_ascii_bytes(tracked_files):
    """.bat 的內容只能是 ASCII。

    每一支 .bat 的開頭都寫著這條規則，但沒有任何東西在檢查它——直到有人在
    訊息裡打了一個中文字為止。cmd.exe 讀到非 ASCII 位元組時不會報錯，它會把
    **那一行之後的整個檔案**解析壞，症狀是腳本安靜地跑掉一半就結束，或是跳出
    一句跟內容毫無關係的「命令語法不正確」。

    檔名可以是中文（Finder 與檔案總管都正常顯示），受限的只有內容。
    """
    offenders: list[str] = []
    for name in tracked_files:
        if not name.endswith((".bat", ".cmd")):
            continue
        data = (ROOT / name).read_bytes()
        for number, line in enumerate(data.splitlines(), start=1):
            if any(byte > 127 for byte in line):
                offenders.append(f"{name}:{number}")
                break
    assert not offenders, (
        ".bat 只能用 ASCII，中文請放進程式本身：\n  " + "\n  ".join(offenders)
    )
