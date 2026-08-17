#!/usr/bin/env bash
# 更新到最新版 —— 雙擊這一個就好，不用再 clone 一次。
#
# 你的東西一個都不會動。資料庫（data/）、附件、匯出的檔案、備份、信件範本、
# user_settings.yaml 全都不在 git 的管轄範圍內，更新碰不到它們。
#
# config.yaml 是有進 git 的，所以你改過的話會擋住更新——這裡的做法是先把你的
# 修改收起來，更新完再放回去，而不是覆蓋掉。
#
# 這裡刻意沒有「更新資料庫」那一步：程式每次啟動都會自己補上新欄位
# （database/session.py 的 init_db），下次開啟就有了。
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"
. "$here/macOS/_共用.sh"

say() { printf "\n\033[1m%s\033[0m\n" "$1"; }
fail() {
    printf "\n\033[31m[錯誤] %s\033[0m\n\n" "$1"
    wait_then_close
    exit 1
}
version_now() { sed -n 's/^VERSION = "\(.*\)"/\1/p' "$here/core/constants.py" | head -n 1; }

printf "\n"
cat "$here/assets/pets.txt" 2>/dev/null

printf "\n名單匠 Roster — 更新\n"
printf "資料夾：%s\n" "$here"

# 桌面／文件／下載被 macOS 的隱私權保護擋住的話，下面每一步都會以看不懂的
# 「Operation not permitted」失敗。跟安裝一樣，動手之前先問清楚。
if blocked_by_macos_privacy "$here"; then
    wait_then_close
    exit 1
fi

# ------------------------------------------------------------ 更得動嗎？
#
# 下載 ZIP 解開的話只有檔案、沒有 .git，等於沒有「這份東西是從哪裡來的」這個
# 記錄，也就無從更新起。與其讓第一個 git 指令用它自己的話報錯，不如講清楚。
if [ ! -d "$here/.git" ]; then
    fail "這個資料夾不是用 git 下載的，所以沒有地方可以更新。
（你下載的是 ZIP，裡面沒有 .git 這個資料夾。）

兩條路，選一條：

  1) 用 git 重新下載一次，以後雙擊這個檔案就能更新：

       git clone https://github.com/WayneLY-Chen/Roster.git

     下載完把舊資料夾裡的 data/ 複製過去。

  2) 或者下載新的 ZIP 覆蓋這個資料夾——但下面這些是你的東西、
     新下載的裡面沒有，要先搬出來、之後再放回去：

       data/               資料庫
       attachments/        掛在公司底下的檔案
       output/             匯出的名單
       backups/            備份
       user_settings.yaml  你的設定
       templates/mail/     你的信件範本"
fi

command -v git >/dev/null 2>&1 || fail "找不到 git，這個檔案沒辦法下載新版本。

安裝方式（擇一）：
  * 在終端機執行 xcode-select --install
  * 或到 https://git-scm.com/download/mac 下載

裝好之後再點一次這個檔案。"

old_version="$(version_now)"
printf "目前版本：%s\n" "${old_version:-未知}"

# ---------------------------------------------------------------- 1/3
say "1/3  查看有沒有新版本"
git fetch --quiet || fail "連不上 GitHub。檢查一下網路再試一次，什麼都沒有被改動。"

remote_head="$(git rev-parse '@{u}' 2>/dev/null || true)"

[ -n "$remote_head" ] || fail "這一份沒有對應到 GitHub 上的任何分支，沒有地方可以更新。
什麼都沒有被改動。

懂 git 的話，設定上游分支：
  git branch --set-upstream-to=origin/main"

# 用「是不是祖先」而不是「兩個編號一不一樣」：自己另外改過東西的人是**超前**
# GitHub 而不是落後，本來就沒有東西要抓。只比對編號的話會把他送進更新流程。
if git merge-base --is-ancestor "$remote_head" HEAD; then
    printf "\n\033[32m已經是最新版了（%s），不用更新。\033[0m\n" "${old_version:-未知}"
    wait_then_close
    exit 0
fi

# 改過有進 git 的檔案（幾乎一定是 config.yaml）的話，更新會被擋下來。
# 先收起來，更新完再放回去。
stashed=0
if ! git diff --quiet HEAD; then
    printf "   你改過設定檔（多半是 config.yaml），先收起來，更新完再放回去。\n"
    git stash push --quiet -m "roster-update" \
        || fail "沒辦法把你的修改收起來，什麼都沒有被改動。原因見上面的訊息。"
    stashed=1
fi

# ---------------------------------------------------------------- 2/3
say "2/3  下載新版本"
if ! git pull --ff-only --quiet; then
    printf "\n\033[31m[錯誤] 更新裝不上去。原因見上面的訊息——最常見的是這一份\n"
    printf "自己有額外的修改記錄，沒辦法直接接上去。\033[0m\n"
    [ "$stashed" -eq 1 ] && printf "\n你收起來的修改還在，用 git stash pop 取回。\n"
    wait_then_close
    exit 1
fi

if [ "$stashed" -eq 1 ] && ! git stash pop --quiet; then
    printf "\n新版本已經裝好了，但你自己的修改沒辦法自動放回去——同樣幾行\n"
    printf "兩邊都改過。\n\n"
    printf "它沒有不見。要看是什麼：  git stash show -p\n"
    printf "要放回去自己處理：      git stash pop\n"
    wait_then_close
    exit 1
fi

# ---------------------------------------------------------------- 3/3
say "3/3  補裝新增的套件"
if [ ! -x "$here/.venv/bin/python" ]; then
    printf "\n新版本已經下載好了，但這裡還沒有 .venv。\n"
    printf "請雙擊同一個資料夾裡的「安裝.command」把它裝完。\n"
    wait_then_close
    exit 1
fi
# 刻意不加 --upgrade。requirements.txt 寫的是「最低版本」，不加的話只會補上
# 缺的套件、原本能跑的維持原樣；加了則是每一個套件都跳到最新版，那是比
# 「更新 Roster」大得多的改動。
"$here/.venv/bin/python" -m pip install -r requirements.txt --quiet \
    || fail "新版本已經下載好了，但套件安裝失敗。原因見上面的訊息，最常見的是
網路問題。再點一次這個檔案即可。"

# ---------------------------------------------------------------- 完成
new_version="$(version_now)"
printf "\n\033[32m更新完成：%s  →  %s\033[0m\n\n" "${old_version:-未知}" "${new_version:-未知}"
printf "你的公司資料、附件、設定都沒有被動到。\n"
printf "這一版改了什麼，見 CHANGELOG.md。\n\n"

# Roster.app 只是一層薄殼，執行的是這個資料夾裡的 .venv，所以更新完不用
# 重新產生。只有 macOS/建立App.command 本身改過才需要，那種時候重跑一次
# 「安裝.command」就好。
if [ -d "$here/Roster.app" ]; then
    printf "Roster.app 不用重新產生，它跑的就是這個資料夾。\n\n"
fi

printf "現在要開啟嗎？[Y/n] "
read -r answer
case "${answer:-Y}" in
    [Nn]*) ;;
    *)
        if [ -d "$here/Roster.app" ]; then
            open "$here/Roster.app"
        else
            "$here/.venv/bin/python" "$here/main.py" gui &
        fi
        ;;
esac

wait_then_close
