"""更新資訊頁：這一版改了什麼。

內容直接讀專案根目錄的 ``CHANGELOG.md``，不在程式碼裡另外抄一份。抄一份的
下場是兩邊遲早不一致，而使用者看到的那一份就是會過期的那一份。

同一個檔案也是 GitHub 上讀得到的更新紀錄，所以維護一次就好。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QHBoxLayout, QLabel, QTextBrowser, QVBoxLayout

from core.config import RESOURCE_ROOT
from core.constants import DISPLAY_NAME, PROJECT_NAME, VERSION
from gui_qt.pages.base import BasePage

CHANGELOG_FILENAME = "CHANGELOG.md"


def changelog_path() -> Path:
    """打包成 exe 之後 CHANGELOG 會在解壓出來的資源目錄裡，不是執行檔旁邊。"""
    return RESOURCE_ROOT / CHANGELOG_FILENAME


def load_changelog() -> str:
    """讀更新紀錄。讀不到就說讀不到，不要顯示一片空白。"""
    path = changelog_path()
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return (
            f"# 更新紀錄\n\n找不到 {CHANGELOG_FILENAME}。\n\n"
            "完整的更新紀錄可以在專案的 GitHub 頁面上看到。"
        )


class ChangelogPage(BasePage):
    title = "更新資訊"
    icon = "🆕"

    def build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(12)

        header = QHBoxLayout()
        title_label = QLabel("更新資訊")
        title_font = title_label.font()
        title_font.setPointSize(22)
        title_font.setBold(True)
        title_label.setFont(title_font)
        header.addWidget(title_label)
        header.addStretch(1)

        version_label = QLabel(f"{DISPLAY_NAME} {PROJECT_NAME} v{VERSION}")
        version_label.setObjectName("MutedLabel")
        header.addWidget(version_label)
        outer.addLayout(header)

        # QTextBrowser 而不是 QLabel：內容是 Markdown，而且會長到需要捲動。
        # setMarkdown() 是 Qt 6 內建的，不需要額外的套件。
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        self.view.setMarkdown(load_changelog())
        outer.addWidget(self.view, 1)
