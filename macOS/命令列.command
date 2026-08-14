#!/usr/bin/env bash
# （貓跟狗印在下面，跟安裝與啟動用的是同一份 assets/pets.txt。）
# 名單匠的命令列 —— macOS 使用者雙擊這一個。
#
# 這裡刻意不 source .venv/bin/activate。
#
# activate 是 Python 的 venv 模組產生的，裡面寫死了建立當下的絕對路徑：
#
#     VIRTUAL_ENV="/Users/某某/某處/.venv"
#
# 只要搬動或改名任何一層資料夾，那一行就指向不存在的位置。改它沒有用，
# pip 與 venv 會重新產生。所以這裡自己設定同樣的環境變數，用的是腳本自己
# 所在的位置，搬到哪裡都能用。
set -uo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
. "$root/macOS/_共用.sh"

if blocked_by_macos_privacy "$root"; then
    wait_then_close
    exit 1
fi

if [ ! -x "$root/.venv/bin/python" ]; then
    echo "[錯誤] 還沒安裝。請先雙擊同一個資料夾裡的「安裝.command」。"
    wait_then_close
    exit 1
fi

export VIRTUAL_ENV="$root/.venv"
export PATH="$VIRTUAL_ENV/bin:$PATH"
unset PYTHONHOME

printf "\n"
cat "$root/assets/pets.txt" 2>/dev/null

cat <<'HELP'

 名單匠 —— 虛擬環境已啟用
 ---------------------------------------------------
  python main.py --help           列出所有指令
  python main.py crawl --list     列出已設定的爬取來源
  python main.py export -f excel  匯出成 Excel
  python main.py stats            資料庫摘要
  python main.py gui              開啟視窗介面

 提示：用 python -m pip、python -m pytest，不要直接用 pip / pytest。
 .venv/bin 裡的那些啟動器同樣嵌了絕對路徑，資料夾一搬就失效。

HELP

# 不用 exec：離開這個殼層之後要把終端機視窗一起收掉，exec 之後就沒有人做這件事。
"${SHELL:-/bin/bash}" || true
close_this_window
