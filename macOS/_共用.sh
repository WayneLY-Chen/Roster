#!/usr/bin/env bash
# 這個檔案不用點。它是同一個資料夾裡那幾個 .command 共用的一小段程式。
#
# 用途只有一個：跑完之後把終端機視窗關掉。
#
# 終端機的預設值是「當 Shell 結束時：不要關閉視窗」，所以每一個 .command 跑完
# 都會留下一個寫著「[程序完成]」的空視窗要人自己關。改那個預設值會影響使用者
# 所有的終端機視窗，不該由我們動手；正確的做法是這個腳本自己收拾自己。

# 關掉「執行這個腳本的那一個」視窗。
#
# 比對的是 tty（每個視窗或分頁各自唯一），不是「最前面的視窗」——後者會在使用
# 者剛好切到別的終端機視窗時關錯人家的東西。
close_this_window() {
    # 只在 macOS 內建的終端機裡做。iTerm、VS Code 內嵌終端機等等各有各的規矩，
    # 認不出來就什麼都不做，維持原本的行為。
    [ "${TERM_PROGRAM:-}" = "Apple_Terminal" ] || return 0
    command -v osascript >/dev/null 2>&1 || return 0

    # 被另一個腳本呼叫時（安裝.command 會呼叫建立App.command，輸出接到檔案或
    # 管線裡），這個視窗不是我們的，關掉等於把上層的安裝畫面一起收走。
    [ -t 1 ] || return 0

    local tty_path
    tty_path="$(tty 2>/dev/null)" || return 0
    case "$tty_path" in /dev/*) ;; *) return 0 ;; esac

    # 背景執行 + 一秒延遲。要等這個腳本自己先結束、視窗不再處於「執行中」，
    # 終端機才會直接關掉；否則它會跳出「確定要中止執行中的程序嗎」再問一次，
    # 等於沒省到事。腳本結束後這個子行程會被系統接管，照樣跑完。
    (
        sleep 1
        osascript >/dev/null 2>&1 <<APPLESCRIPT
tell application "Terminal"
    repeat with w in windows
        repeat with t in tabs of w
            try
                if tty of t is "$tty_path" then
                    close w saving no
                    return
                end if
            end try
        end repeat
    end repeat
end tell
APPLESCRIPT
    ) >/dev/null 2>&1 &
}

# 「按 Enter 關閉」＋真的把它關掉。
wait_then_close() {
    printf "\n%s" "${1:-按 Enter 關閉這個視窗。}"
    read -r _ || true
    close_this_window
}
