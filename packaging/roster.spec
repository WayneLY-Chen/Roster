# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包設定：名單匠 Roster。

用法：
    .venv\\Scripts\\python.exe -m PyInstaller roster.spec --noconfirm

產出 dist/Roster/Roster.exe（單一資料夾，不是單一檔案）。單檔模式每次啟動
都要把幾百 MB 解壓到暫存資料夾，開啟明顯變慢，而且防毒軟體對「會自我解壓
的執行檔」特別敏感；桌面工具用資料夾模式比較實在。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# 這份 spec 住在 packaging/ 底下，所以專案根目錄是它的上一層。
ROOT = Path(SPECPATH).parent

# 這些是使用者的資料與設定，不能打包進去——程式第一次啟動會自己建立，
# 打包進去等於把開發機的資料庫發給每個使用者。
datas = [
    (str(ROOT / "assets"), "assets"),
    (str(ROOT / "config.yaml"), "."),
    (str(ROOT / "templates"), "templates"),
    # 「更新資訊」頁直接讀這個檔案。漏掉的話打包後那一頁會是空的，而且
    # 只有真的去打包、真的去點那一頁才會發現。
    (str(ROOT / "CHANGELOG.md"), "."),
]

# 控制器用延遲 import，PyInstaller 的靜態分析看不到那些字串裡的模組名；
# core/preload.py 列出的每一個都必須明確帶進來，否則打包後第一次爬取、
# 第一次匯入就會 ModuleNotFoundError。
hiddenimports = [
    "crawler.pipeline",
    "crawler.enrich",
    "crawler.discover",
    "crawler.fetcher",
    "crawler.parser",
    "verifier.service",
    "verifier.dedupe",
    "verifier.normalize",
    "exporter.service",
    "exporter.base",
    "exporter.importer",
    "exporter.sample_template",
    "gmail.sender",
    "database.encryption",
    "database.backup",
    "core.credentials",
    "core.crypto",
    "core.preload",
]
# keyring 用 entry point 找後端，靜態分析同樣看不到。少了它，加密金鑰
# 讀不出來，整個資料庫變成打不開。
hiddenimports += collect_submodules("keyring")

excludes = [
    # 舊介面已經移除，但 tkinter 還在標準函式庫裡；明確排除可以省下數十 MB。
    "tkinter",
    "customtkinter",
    "matplotlib",
    "pytest",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.Qt3DCore",
    "PySide6.QtMultimedia",
    "PySide6.QtCharts",
]

a = Analysis(
    # app_main.py 而不是 main.py：後者是 Typer 命令列程式，沒給子指令時
    # 只會印出說明——配上 console=False 就等於「點兩下毫無反應」。
    [str(Path(SPECPATH) / "app_main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Roster",
    debug=False,
    strip=False,
    upx=False,
    # console=False：這是桌面程式，開起來不該跟著一個黑色主控台視窗。
    # 需要看命令列輸出時用 命令列.bat 直接跑 main.py。
    console=False,
    icon=str(ROOT / "assets" / "icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Roster",
)
