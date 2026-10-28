#!/usr/bin/env bash
# 用瀏覽器開名單匠 —— macOS 使用者雙擊這一個。
#
# 跟「啟動.command」用同一份資料庫，只是介面換成瀏覽器。網頁只有這台電腦
# 連得到，見 web/app.py 的說明。
#
# 這個終端機視窗就是網頁伺服器：開著網頁就能用，關掉它網頁就停了。
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
. "$root/macOS/_共用.sh"

# 跟「啟動.command」同樣的理由，這一關要排在「還沒安裝」前面。
if blocked_by_macos_privacy "$root"; then
    wait_then_close
    exit 1
fi

if [ ! -x "$root/.venv/bin/python" ]; then
    cat <<EOF

[錯誤] 還沒安裝。

請先雙擊同一個資料夾裡的「安裝.command」。

EOF
    wait_then_close
    exit 1
fi

print_pets "$root"

status=0
"$root/.venv/bin/python" main.py web || status=$?

if [ "$status" -ne 0 ]; then
    printf "\n\033[31m網頁版結束時回報了錯誤（代碼 %s）。上面的訊息是原因。\033[0m\n" "$status"
    wait_then_close
    exit "$status"
fi

close_this_window
