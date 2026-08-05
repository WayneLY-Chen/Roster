#!/usr/bin/env bash
# 安裝與設定 —— macOS 使用者雙擊這一個就好。
#
# 做完三件事：建立虛擬環境、安裝套件、產生 Roster.app。之後就可以從
# 「應用程式」或 Dock 開啟，不需要再碰終端機。
#
# 可以重複執行：已經裝好的話會直接跳過，只做缺的部分。
#
# 第一次執行如果被 Gatekeeper 擋下來，按右鍵 ->「打開」。
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"

say() { printf "\n\033[1m%s\033[0m\n" "$1"; }
fail() {
    printf "\n\033[31m[錯誤] %s\033[0m\n\n" "$1"
    printf "按 Enter 關閉這個視窗。"
    read -r _
    exit 1
}

printf "\n名單匠 Roster — 安裝程式\n"
printf "安裝位置：%s\n" "$here"

# ---------------------------------------------------------------- Python
say "1/4  檢查 Python"

python_bin=""
for candidate in python3.13 python3.12 python3; do
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
say "2/4  建立虛擬環境"
if [ -x "$here/.venv/bin/python" ]; then
    printf "   .venv 已存在，跳過\n"
else
    "$python_bin" -m venv .venv || fail "建立虛擬環境失敗。"
    printf "   已建立 .venv\n"
fi

# ------------------------------------------------------------------ 套件
say "3/4  安裝套件（第一次會下載約 150 MB，需要幾分鐘）"
"$here/.venv/bin/python" -m pip install --upgrade pip --quiet
if ! "$here/.venv/bin/python" -m pip install -r requirements.txt; then
    fail "套件安裝失敗。上面的訊息會說明原因，最常見的是網路連線問題。"
fi

# -------------------------------------------------------------- 啟動方式
say "4/4  建立啟動方式"
chmod +x "$here/start.sh" "$here/console.sh" "$here/start.command" \
         "$here/make_macos_app.sh" 2>/dev/null || true

if "$here/make_macos_app.sh" >/dev/null 2>&1; then
    printf "   已建立 Roster.app\n"
    app_made=1
else
    printf "   Roster.app 建立失敗，用 start.command 也可以\n"
    app_made=0
fi

# ------------------------------------------------------------------ 完成
printf "\n\033[32m安裝完成。\033[0m\n\n"
printf "接下來怎麼開啟：\n"
if [ "$app_made" -eq 1 ]; then
    printf "  * 把 Roster.app 拖進「應用程式」資料夾或 Dock，之後點一下就開\n"
fi
printf "  * 或直接雙擊 start.command\n\n"
printf "第一次開啟如果出現「來自未識別的開發者」，按右鍵 ->「打開」。\n"
printf "那是 macOS 對所有未簽章程式的預設行為。\n\n"

if [ "$app_made" -eq 1 ] && [ -d "$here/Roster.app" ]; then
    printf "現在要開啟嗎？[Y/n] "
    read -r answer
    case "${answer:-Y}" in
        [Nn]*) ;;
        *) open "$here/Roster.app" ;;
    esac
fi

printf "\n按 Enter 關閉這個視窗。"
read -r _
