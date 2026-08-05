#!/usr/bin/env bash
# 出問題時跑這一個，把畫面上的輸出整段複製給我。
#
# 刻意不用 `set -e`：這個腳本的用途就是「即使有東西壞掉也要跑完」，
# 中途停掉的話最想看的那幾行反而印不出來。
#
# 只讀取狀態，不修改任何東西。輸出裡不含使用者帳號名稱以外的個人資料。
set -uo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

line() { printf "\n\033[1m--- %s ---\033[0m\n" "$1"; }

printf "名單匠 Roster — 診斷\n"

line "系統"
sw_vers 2>/dev/null || echo "(取不到 macOS 版本)"
echo "CPU：$(uname -m)"

line "Python"
for candidate in python3.14 python3.13 python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        printf "%-10s %s\n" "$candidate" "$("$candidate" --version 2>&1)"
    fi
done

line "虛擬環境"
if [ -x "$root/.venv/bin/python" ]; then
    echo "有 .venv：$("$root/.venv/bin/python" --version 2>&1)"
    "$root/.venv/bin/python" -c "import PySide6, bs4, httpx; print('主要套件都在')" 2>&1 | tail -3
else
    echo "沒有 .venv —— 還沒安裝，或安裝沒跑完"
fi

line "啟動檔的執行權限"
# 從瀏覽器下載的 ZIP 解開之後會掉掉執行權限，那會讓雙擊直接失敗。
ls -l "$root/macOS/" | sed 's/^/  /'

line "下載隔離標記"
if xattr -lr "$root/macOS" 2>/dev/null | grep -q com.apple.quarantine; then
    echo "還有隔離標記 —— 這會讓雙擊被 Gatekeeper 擋下"
    echo "解法：xattr -dr com.apple.quarantine \"$root\""
else
    echo "沒有隔離標記（正常）"
fi

line "Roster.app"
if [ -d "$root/Roster.app" ]; then
    echo "存在"
    ls -l "$root/Roster.app/Contents/MacOS/" 2>&1 | sed 's/^/  /'
else
    echo "不存在 —— 底下是重新建立一次的完整輸出："
    "$root/macOS/建立App.command" 2>&1 | sed 's/^/  /'
fi

line "直接啟動看看"
if [ -x "$root/.venv/bin/python" ]; then
    # 只問版本，不真的開視窗——開了就要人手動關，診斷腳本不該這樣。
    "$root/.venv/bin/python" -c "
from core.constants import VERSION, DISPLAY_NAME
print(f'{DISPLAY_NAME} {VERSION} 載入成功')
" 2>&1 | tail -20
else
    echo "跳過（沒有 .venv）"
fi

printf "\n\033[1m以上整段複製給我。\033[0m\n"
printf "\n按 Enter 關閉這個視窗。"
read -r _
