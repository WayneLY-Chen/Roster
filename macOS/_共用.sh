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

# macOS 的隱私權限（TCC）把整個資料夾擋住了嗎？
#
# 症狀是 Python 連自己都還沒啟動完就死掉，而且訊息完全看不出是權限問題：
#
#     Fatal Python error: init_import_site: Failed to import the site module
#     PermissionError: [Errno 1] Operation not permitted:
#         '/Users/某某/Desktop/Roster/.venv/pyvenv.cfg'
#
# 「Operation not permitted」在一個**明明存在、權限位元也正常**的檔案上，
# 就是 macOS 的 TCC。桌面、文件、下載這三個資料夾從 macOS Catalina 起受保護：
# 終端機（或 Roster.app）沒有被授權讀取桌面的話，讀得到檔案名字、卻讀不到
# 內容——所以 `test -x` 會過，真正開檔的時候才失敗。這也是為什麼一般的
# 「還沒安裝」檢查攔不住它。
#
# 回傳 1 代表被擋住了，而且已經把該怎麼辦印出來。
blocked_by_macos_privacy() {
    local root="$1"
    local probe=""

    # 找一個「一定存在」的檔案來試讀。
    #
    # 依序試是為了讓安裝之前也測得出來：那時候還沒有 .venv，但 main.py 一定
    # 在。少了這一段，被擋住的使用者會先看到安裝跑到一半失敗，訊息一樣看不懂。
    local candidate
    for candidate in "$root/.venv/pyvenv.cfg" "$root/main.py" "$root/README.md"; do
        if [ -f "$candidate" ]; then
            probe="$candidate"
            break
        fi
    done
    # 一個都找不到＝資料夾本身有別的問題，不是權限，交給呼叫端原本的檢查。
    [ -n "$probe" ] || return 1
    # 讀得到就沒事。用真的讀一個位元組，不是 test -r：TCC 擋的是開檔，
    # 而 access(2) 在某些版本上仍然回答「可以」。
    head -c 1 "$probe" >/dev/null 2>&1 && return 1

    printf "\n\033[31m[錯誤] macOS 擋住了這個資料夾的讀取權限。\033[0m\n\n"
    cat <<EOF
不是程式壞掉，也不是檔案毀損——是 macOS 的隱私權保護。桌面、文件、下載
這三個資料夾受系統保護，終端機沒有被授權的話讀不到裡面的內容。

兩個解法，選一個就好：

  1) 授權給終端機（Roster 可以留在桌面）

     系統設定 → 隱私權與安全性 → 檔案與資料夾
       → 找到「終端機」→ 打開「桌面資料夾」

     清單裡沒有「終端機」的話，改用：
     系統設定 → 隱私權與安全性 → 完整取得磁碟權限 → ＋ → 選「終端機」

     授權之後**要把終端機整個結束再重開**（⌘Q），設定才會生效。

     用 Roster.app 啟動的話，要授權的是「Roster」而不是「終端機」。

  2) 把 Roster 移出桌面（不必動任何系統設定）

     在「訪達」把整個 Roster 資料夾拖到你的「使用者主目錄」——
     側邊欄上有你名字的那一個（不是桌面、不是文件）。
     主目錄的最上層不受這個保護，移過去就直接能用。

     移動之後如果原本有 Roster.app，請重新執行一次「建立App.command」。
EOF
    return 0
}

# 「按 Enter 關閉」＋真的把它關掉。
wait_then_close() {
    printf "\n%s" "${1:-按 Enter 關閉這個視窗。}"
    read -r _ || true
    close_this_window
}
