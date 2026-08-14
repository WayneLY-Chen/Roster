#!/usr/bin/env bash
# 產生 Roster.app —— 可以直接放進「應用程式」或 Dock 的 macOS 應用程式。
#
# 啟動.command 雙擊得起來，但會跟著開一個 Terminal 視窗。.app 不會，
# 而且可以設圖示、可以固定在 Dock，看起來就跟一般的 Mac 應用程式一樣。
#
# 這裡不是 PyInstaller 打包：產生的 .app 只是一層薄殼，執行時仍然用專案
# 資料夾裡的 .venv。好處是換一次程式碼不用重新打包；代價是這個 .app 不能
# 單獨搬到別台電腦（它要找得到專案資料夾）。
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
app="$here/Roster.app"
. "$here/macOS/_共用.sh"

if [ ! -x "$here/.venv/bin/python" ]; then
    echo "[錯誤] 還沒安裝。請先雙擊同一個資料夾裡的「安裝.command」。"
    exit 1
fi

rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"

# 版本號從程式本身讀，不要在這裡寫死——寫死的那一份永遠會忘記跟著改。
#
# 這裡不接 `| head -1`：`set -o pipefail` 之下，head 讀完就結束會讓 sed 收到
# SIGPIPE，整條管線的結束碼變成失敗，`set -e` 就把整個腳本中止了——而且是
# 靜悄悄地中止。改用 sed 自己的 `q` 讀到第一筆就收工，不需要第二個行程。
version="$(sed -n 's/^VERSION = "\(.*\)"/\1/p;/^VERSION = /q' "$here/core/constants.py")"
version="${version:-1.0.0}"

cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Roster</string>
    <key>CFBundleDisplayName</key><string>名單匠</string>
    <key>CFBundleIdentifier</key><string>tw.roster.app</string>
    <key>CFBundleVersion</key><string>${version}</string>
    <key>CFBundleShortVersionString</key><string>${version}</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleExecutable</key><string>Roster</string>
    <key>CFBundleIconFile</key><string>icon.icns</string>
    <!-- 沒有這個，Dock 會多出一個沒有視窗的黑框圖示 -->
    <key>LSMinimumSystemVersion</key><string>11.0</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# 啟動器不能用 `exec`，也不能讓錯誤靜靜地掉在地上。
#
# Finder 啟動 .app 的時候沒有終端機可以印東西：程式一失敗，圖示在 Dock 上跳
# 一下就消失，使用者看到的是「點了沒反應」，而真正的原因（venv 被砍了、資料夾
# 搬走了、少裝一個套件）只出現在沒有人會去看的系統紀錄裡。實際回報過。
#
# 所以這裡把輸出接進檔案，失敗時用 osascript 把最後幾行直接貼在螢幕上。
cat > "$app/Contents/MacOS/Roster" <<'LAUNCHER'
#!/usr/bin/env bash
set -uo pipefail

project="__PROJECT__"
log="$project/logs/啟動失敗.log"

# 先留腳印，再做任何事。
#
# 「點了完全沒反應」是這個 App 最難查的狀況，因為 Finder 啟動時沒有終端機
# 可以印東西。原本只有「Python 跑起來但失敗」才會留紀錄——真正難查的是**更早**
# 就掛掉的情況（bundle 沒建好、路徑沒換掉、osascript 被系統擋住），那時候
# 畫面上什麼都沒有，磁碟上也什麼都沒有，等於無從查起。
#
# 所以每一次啟動都先寫一行。下次「點了沒反應」時，這個檔案就是唯一的線索：
# 有這一行代表殼有跑到，沒有代表問題在更外層（權限、隔離標記、bundle 結構）。
mkdir -p "$project/logs" 2>/dev/null || true
echo "=== $(date '+%Y-%m-%d %H:%M:%S') 啟動 ===" >> "$log" 2>/dev/null || true

fail() {
    mkdir -p "$project/logs" 2>/dev/null || true
    {
        echo "=== $(date '+%Y-%m-%d %H:%M:%S') ==="
        echo "$1"
    } >> "$log" 2>/dev/null || true

    # AppleScript 的字串裡，反斜線與雙引號要跳脫，否則對話框根本開不起來
    # ——那會變成「連錯誤訊息都看不到」，比原本的問題更糟。
    local message
    message="$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tail -c 900)"
    if ! osascript -e "display dialog \"名單匠啟動失敗：

$message

完整訊息：logs/啟動失敗.log\" buttons {\"好\"} default button 1 with title \"名單匠\" with icon stop" \
        >/dev/null 2>&1
    then
        # 對話框開不起來（osascript 被系統政策擋掉、或沒有視窗工作階段）。
        # 那就把紀錄檔直接打開——總比「點了沒反應」好。
        open -e "$log" >/dev/null 2>&1 || open "$project/logs" >/dev/null 2>&1 || true
    fi
    exit 1
}

[ -d "$project" ] || fail "找不到專案資料夾：
$project

這個 App 只是一層外殼，它要用專案資料夾裡的環境。
資料夾搬過位置的話，請重新執行一次 macOS/建立App.command。"

# macOS 的隱私權保護（TCC）擋住桌面／文件／下載時，這個檔案讀得到名字、
# 讀不到內容，所以上面的 -d 與下面的 -x 都會過，然後 Python 在啟動途中以
# 「Operation not permitted: .../pyvenv.cfg」死掉——那個訊息使用者看不懂。
# 在這裡先問，才能講人話。
if [ -f "$project/.venv/pyvenv.cfg" ] && ! head -c 1 "$project/.venv/pyvenv.cfg" >/dev/null 2>&1; then
    fail "macOS 擋住了這個資料夾的讀取權限。

不是程式壞掉——桌面、文件、下載這三個資料夾受系統保護，
Roster 沒有被授權的話讀不到裡面的內容。

解法一：系統設定 → 隱私權與安全性 → 檔案與資料夾 → Roster
　　　　→ 打開「桌面資料夾」，然後重新開啟 Roster。
　　　　清單裡沒有 Roster 的話，改用「完整取得磁碟權限」→ ＋ → 選 Roster。

解法二：把整個 Roster 資料夾從桌面移到你的「使用者主目錄」
　　　　（訪達側邊欄上有你名字的那一個），再執行一次
　　　　macOS/建立App.command。"
fi

[ -x "$project/.venv/bin/python" ] || fail "找不到 Python 環境：
$project/.venv/bin/python

請先雙擊 macOS/安裝.command。"

cd "$project" || fail "無法進入專案資料夾：$project"

mkdir -p "$project/logs" 2>/dev/null || true
output="$("$project/.venv/bin/python" main.py gui 2>&1)"
status=$?
[ $status -eq 0 ] || fail "程式結束時回報了錯誤（代碼 $status）：

$(printf '%s' "$output" | tail -n 12)"
LAUNCHER

# 專案位置寫進啟動器裡。.app 通常會被搬到「應用程式」資料夾，那時候就沒辦法
# 再用「自己所在的位置」推算專案在哪了。
#
# 用 sed 換掉佔位字串，而不是把 heredoc 開成可展開的：上面那段 shell 腳本裡
# 有一堆 `$1`、`$status`、`$(...)`，可展開的 heredoc 會在**產生檔案的當下**
# 就把它們全部吃掉，寫出來的啟動器會是一堆空字串。
python3 - "$app/Contents/MacOS/Roster" "$here" <<'PYEOF' 2>/dev/null || \
    sed -i '' "s|__PROJECT__|$here|" "$app/Contents/MacOS/Roster"
import sys
path, project = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as handle:
    text = handle.read()
with open(path, "w", encoding="utf-8") as handle:
    handle.write(text.replace("__PROJECT__", project))
PYEOF
chmod +x "$app/Contents/MacOS/Roster"

# 圖示：有 .icns 就用，沒有的話讓 macOS 用預設的。
if [ -f "$here/assets/icon.icns" ]; then
    cp "$here/assets/icon.icns" "$app/Contents/Resources/icon.icns"
elif [ -f "$here/assets/icon.png" ] && command -v sips >/dev/null 2>&1; then
    iconset="$(mktemp -d)/icon.iconset"
    mkdir -p "$iconset"
    for size in 16 32 64 128 256 512; do
        sips -z $size $size "$here/assets/icon.png" \
            --out "$iconset/icon_${size}x${size}.png" >/dev/null 2>&1 || true
    done
    iconutil -c icns "$iconset" -o "$app/Contents/Resources/icon.icns" 2>/dev/null || true
fi

# 產生完要自己確認一次。少了這一關，只要中間任何一步默默失敗，使用者拿到的
# 就是一個點不開的空殼，而畫面上卻寫著「已建立」。
if [ ! -x "$app/Contents/MacOS/Roster" ]; then
    echo "[錯誤] Roster.app 沒有建立成功（找不到 $app/Contents/MacOS/Roster）。"
    exit 1
fi

# 專案路徑真的換進去了嗎？
#
# 上面那一步有兩條路（python3，失敗才退回 sed），而兩條都失敗的話會留下一個
# 寫著 __PROJECT__ 的啟動器——檔案在、也可以執行，所以上面那一關過得了，
# 但雙擊時它會去找一個叫「__PROJECT__」的資料夾，然後失敗。使用者看到的是
# 「App 建好了，可是點不開」，而這正是最難自己查出來的那一種。
if grep -q "__PROJECT__" "$app/Contents/MacOS/Roster"; then
    echo "[錯誤] 專案路徑沒有寫進 Roster.app 的啟動器裡。"
    echo "       這樣產生出來的 App 點下去不會有反應。"
    echo "       請把這幾行整段複製給作者（macOS/診斷.command 的輸出也一起）。"
    exit 1
fi

# 這裡的大括號不能省。bash 判斷「變數名到哪裡結束」是逐位元組問 isalnum()，
# 在某些 macOS 的 bash／語系組合下，全形字（「（」＝EF BC 88）的第一個位元組
# 會被當成合法的變數名字元——`$app（` 於是被讀成一個叫 `app<EF>` 的變數，配上
# `set -u` 就直接中止，錯誤訊息是看不懂的 `app?: unbound variable`。實際回報過。
# Windows 這邊的 bash 5.2 不會重現，所以只靠自己測是測不出來的：
# 只要變數後面接的是中文字，一律寫成 ${app}。
echo "已建立 ${app}（版本 ${version}）"
echo
echo "把它拖進「應用程式」資料夾或 Dock 就可以用了。"

# 使用者自己雙擊這個檔案時才停下來等（被安裝.command 呼叫時輸出是接到檔案的，
# close_this_window 會自己判斷出來、什麼都不做）。
if [ -t 1 ]; then
    wait_then_close
fi
# 這個 .app 是在這台機器上產生的，不是從網路下載的，所以不帶隔離標記，
# 也就不會出現「Apple 無法驗證是否為惡意軟體」那個視窗。
