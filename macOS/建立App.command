#!/usr/bin/env bash
# 產生 Roster.app —— 可以直接放進「應用程式」或 Dock 的 macOS 應用程式。
#
# start.command 雙擊得起來，但會跟著開一個 Terminal 視窗。.app 不會，
# 而且可以設圖示、可以固定在 Dock，看起來就跟一般的 Mac 應用程式一樣。
#
# 這裡不是 PyInstaller 打包：產生的 .app 只是一層薄殼，執行時仍然用專案
# 資料夾裡的 .venv。好處是換一次程式碼不用重新打包；代價是這個 .app 不能
# 單獨搬到別台電腦（它要找得到專案資料夾）。
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
app="$here/Roster.app"

if [ ! -x "$here/.venv/bin/python" ]; then
    echo "[錯誤] 還沒安裝。請先雙擊同一個資料夾裡的「安裝.command」。"
    exit 1
fi

rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"

cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Roster</string>
    <key>CFBundleDisplayName</key><string>名單匠</string>
    <key>CFBundleIdentifier</key><string>tw.roster.app</string>
    <key>CFBundleVersion</key><string>1.3.0</string>
    <key>CFBundleShortVersionString</key><string>1.3.0</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleExecutable</key><string>Roster</string>
    <key>CFBundleIconFile</key><string>icon.icns</string>
    <!-- 沒有這個，Dock 會多出一個沒有視窗的黑框圖示 -->
    <key>LSMinimumSystemVersion</key><string>11.0</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# 專案位置寫進啟動器裡。.app 通常會被搬到「應用程式」資料夾，那時候就沒辦法
# 再用「自己所在的位置」推算專案在哪了。
cat > "$app/Contents/MacOS/Roster" <<LAUNCHER
#!/usr/bin/env bash
set -euo pipefail
cd "$here"
exec "$here/.venv/bin/python" main.py gui
LAUNCHER
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

echo "已建立 $app"
echo
echo "把它拖進「應用程式」資料夾或 Dock 就可以用了。"
echo "第一次開啟如果被 Gatekeeper 擋下來，按右鍵 -> 打開。"
