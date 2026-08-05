#!/usr/bin/env bash
# 啟動名單匠 —— macOS 使用者雙擊這一個。
#
# 為什麼是 .command 而不是 .sh：macOS 的 Finder 不會執行 .sh，它只會用文字
# 編輯器把檔案打開。.command 才是 Finder 認得的「在終端機執行」副檔名。
#
# 被「Apple 無法驗證是否為惡意軟體」擋下來的話，先跑一次同一個資料夾裡的
# 「安裝.command」——它會解除整個資料夾的下載隔離標記。真的開不了的話，
# 同一個資料夾裡有「開不起來 請先看這個.txt」。
#
# 這個檔案是自足的，不去呼叫 Linux/ 資料夾裡的腳本——每個資料夾對應一個
# 作業系統，互相引用只會讓「我該用哪一個」變得更難回答。
set -euo pipefail

# 專案根目錄在上一層。
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

if [ ! -x "$root/.venv/bin/python" ]; then
    cat <<EOF

[錯誤] 還沒安裝。

請先雙擊同一個資料夾裡的「安裝.command」。

EOF
    printf "按 Enter 關閉這個視窗。"
    read -r _
    exit 1
fi

echo "啟動名單匠…"
echo "（關掉應用程式視窗即可結束）"
echo

exec "$root/.venv/bin/python" main.py gui
