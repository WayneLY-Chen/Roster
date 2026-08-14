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
. "$root/macOS/_共用.sh"

# 這一關要排在「還沒安裝」前面。被 TCC 擋住時 .venv 其實是好好的，只是讀不到
# 內容——先問「安裝了沒」會得到一句「請先執行安裝」，而使用者照做之後安裝也會
# 因為同一個原因失敗，然後就卡在那裡了。
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

# 貓跟狗。跟安裝程式印的是同一份圖（assets/pets.txt），三個平台、每一支
# 啟動腳本都印同一份——分開放的話改一次要記得改六個地方。
printf "\n"
cat "$root/assets/pets.txt" 2>/dev/null

echo
echo "啟動名單匠…"
echo "（關掉應用程式視窗即可結束）"
echo

# 這裡不用 exec。exec 會把這個腳本換成 Python，程式關掉之後就沒有人能把終端機
# 視窗收掉，畫面上會留一個寫著「[程序完成]」的空視窗。改成一般呼叫，等程式結束
# 再自己關窗。
# `|| status=$?` 這個寫法是必要的：`set -e` 之下直接呼叫，程式一回傳非零就整個
# 腳本被中止，下面那段解釋錯誤的訊息根本不會印。
status=0
"$root/.venv/bin/python" main.py gui || status=$?

if [ "$status" -ne 0 ]; then
    printf "\n\033[31m程式結束時回報了錯誤（代碼 %s）。上面的訊息是原因。\033[0m\n" "$status"
    wait_then_close
    exit "$status"
fi

close_this_window
