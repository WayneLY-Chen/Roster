"""找出隨程式一起帶著的資源檔（目前只有應用程式圖示）。

打包成 exe 之後，資源不在原始碼旁邊，而是被解壓到 PyInstaller 的暫存資料夾
（``sys._MEIPASS``）。兩種情況都要能找到，所以路徑要用查的，不能寫死。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon

from core.constants import LogCategory
from core.logging_setup import get_logger

log = get_logger(LogCategory.GUI)


def assets_dir() -> Path:
    """資源資料夾。打包後是 PyInstaller 的解壓目錄，開發時是專案根目錄。"""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled) / "assets"
    return Path(__file__).resolve().parent.parent / "assets"


def app_icon() -> QIcon:
    """視窗與工作列用的圖示。

    優先用 ``.ico``：它裡面包了 16 到 256 的各種尺寸，Windows 會依情境自己
    挑合適的那張；只給一張大 PNG 的話，縮到工作列大小會糊掉。找不到就回傳
    空的 QIcon，呼叫端自己判斷——沒有圖示不該讓程式開不起來。
    """
    for name in ("icon.ico", "icon.png"):
        path = assets_dir() / name
        if path.exists():
            return QIcon(str(path))
    log.warning("找不到應用程式圖示（找過 {}）", assets_dir())
    return QIcon()
