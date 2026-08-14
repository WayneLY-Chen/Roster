#!/usr/bin/env bash
# 安裝與設定 —— macOS 使用者雙擊這一個就好。
#
# 做完三件事：建立虛擬環境、安裝套件、產生 Roster.app。之後就可以從
# 「應用程式」或 Dock 開啟，不需要再碰終端機。
#
# 可以重複執行：已經裝好的話會直接跳過，只做缺的部分。
#
# 第一次執行被擋下來的話，看同一個資料夾裡的「開不起來 請先看這個.txt」。
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

# 貓跟狗。圖放在 assets/pets.txt，不寫在這裡：那是用全形字元排出來的，
# 混在腳本裡每改一次都要重數一遍空白，抽成一個檔案三個平台共用同一份。
printf "\n"
cat "$here/assets/pets.txt" 2>/dev/null

printf "\n名單匠 Roster — 安裝程式\n"
printf "安裝位置：%s\n" "$here"

# 桌面／文件／下載被 macOS 的隱私權保護擋住的話，安裝會在半路以一句看不懂的
# 「Operation not permitted」失敗。在動任何東西之前先問清楚。
if blocked_by_macos_privacy "$here"; then
    wait_then_close
    exit 1
fi

# ---------------------------------------------------------------- Python
say "1/5  檢查 Python"

python_bin=""
for candidate in python3.14 python3.13 python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        version="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")"
        major="${version%%.*}"
        minor="${version##*.}"
        if [ "$major" -eq 3 ] && [ "$minor" -ge 12 ]; then
            python_bin="$candidate"
            break
        fi
    fi
done

if [ -z "$python_bin" ]; then
    fail "找不到 Python 3.12 或更新的版本。

macOS 內建的 Python 版本通常太舊。請先安裝：
  * 到 https://www.python.org/downloads/ 下載安裝，或
  * 已經有 Homebrew 的話執行：brew install python@3.13

裝好之後再點一次這個檔案。"
fi
printf "   使用 %s（%s）\n" "$python_bin" "$("$python_bin" --version)"

# ------------------------------------------------------------------ venv
say "2/5  建立虛擬環境"
if [ -x "$here/.venv/bin/python" ]; then
    printf "   .venv 已存在，跳過\n"
else
    "$python_bin" -m venv .venv || fail "建立虛擬環境失敗。"
    printf "   已建立 .venv\n"
fi

# ------------------------------------------------------------------ 套件
say "3/5  安裝套件（第一次會下載約 150 MB，需要幾分鐘）"
"$here/.venv/bin/python" -m pip install --upgrade pip --quiet
if ! "$here/.venv/bin/python" -m pip install -r requirements.txt; then
    fail "套件安裝失敗。上面的訊息會說明原因，最常見的是網路連線問題。"
fi

# ------------------------------------------------------------------ 瀏覽器
#
# 有一類名錄是網頁載進來之後才用 JavaScript 把資料填上去的，那種站非用真的
# 瀏覽器不可。Chromium 不是 pip 套件，放不進 requirements.txt，要另外下載。
#
# 這一步失敗不是致命的：一般的網站沒有它照樣爬得到。
say "4/5  下載內建瀏覽器（約 120 MB）"
printf "   只有「資料是 JavaScript 產生」的網站才需要它。\n"
if ! "$here/.venv/bin/python" -m playwright install chromium; then
    printf "\n   [提醒] 瀏覽器沒有下載成功。其餘功能都正常，遇到需要它的網站\n"
    printf "   程式會告訴你。之後想補裝：\n"
    printf "   .venv/bin/python -m playwright install chromium\n"
fi

# -------------------------------------------------------------- 啟動方式
say "5/5  建立啟動方式"
chmod +x "$here/macOS/啟動.command" "$here/macOS/命令列.command" \
         "$here/macOS/建立App.command" 2>/dev/null || true

# 失敗訊息一定要露出來。原本這裡是 `>/dev/null 2>&1`，結果是：Roster.app
# 沒建成的時候，畫面只寫「建立失敗」，為什麼失敗連我們自己都看不到。
app_log="$(mktemp)"
if "$here/macOS/建立App.command" >"$app_log" 2>&1; then
    printf "   已建立 Roster.app\n"
    app_made=1
else
    printf "\n\033[31m   Roster.app 建立失敗，原因如下：\033[0m\n"
    sed 's/^/     /' "$app_log"
    printf "\n   （這不影響使用，雙擊 macOS 資料夾裡的 啟動.command 一樣能開）\n"
    app_made=0
fi
rm -f "$app_log"

# ------------------------------------------------------- 解除下載隔離標記
#
# 從瀏覽器下載的 ZIP 解開之後，每個檔案都帶著 com.apple.quarantine 這個標記，
# 雙擊時 macOS 會顯示「Apple 無法驗證……是否為惡意軟體」。macOS 15 起，以前
# 那個「按右鍵 -> 打開」的繞法已經被拿掉，剩下的官方途徑是到系統設定裡對**每
# 一個檔案**分別按一次「仍要打開」。
#
# 使用者已經自己執行到這一步了，等於已經決定要信任這份程式；在這裡把標記一次
# 解除，之後的每一次啟動才不會再被擋。只影響這個資料夾，不動任何系統設定。
if command -v xattr >/dev/null 2>&1; then
    if xattr -dr com.apple.quarantine "$here" 2>/dev/null; then
        printf "   已解除「下載自網路」的隔離標記（之後雙擊不會再被擋）\n"
    fi
fi

# ------------------------------------------------------------------ 完成
printf "\n\033[32m安裝完成。\033[0m\n\n"
printf "接下來怎麼開啟：\n"
if [ "$app_made" -eq 1 ]; then
    printf "  * 把 Roster.app 拖進「應用程式」資料夾或 Dock，之後點一下就開\n"
fi
printf "  * 或直接雙擊 macOS 資料夾裡的 啟動.command\n\n"

if [ "$app_made" -eq 1 ] && [ -d "$here/Roster.app" ]; then
    printf "現在要開啟嗎？[Y/n] "
    read -r answer
    case "${answer:-Y}" in
        [Nn]*) ;;
        *) open "$here/Roster.app" ;;
    esac
fi

wait_then_close
