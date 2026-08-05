"""更新資訊頁：內容必須來自 CHANGELOG.md，而且版本號要對得上。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.constants import VERSION  # noqa: E402
from gui_qt.app import PAGE_CLASSES  # noqa: E402
from gui_qt.pages.changelog import (  # noqa: E402
    ChangelogPage,
    changelog_path,
    load_changelog,
)


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


class _FakeApp:
    def __init__(self):
        self.messages = []
        self.current_page = "更新資訊"

    def set_status(self, message, tone="normal"):
        self.messages.append((message, tone))


def test_the_changelog_file_exists():
    """程式直接讀它，不見的話那一頁就是空的。"""
    assert changelog_path().is_file(), f"找不到 {changelog_path()}"


def test_the_current_version_is_documented():
    """發了一版卻沒寫更新內容，使用者看到的就是上一版的說明。"""
    assert f"## {VERSION}" in load_changelog(), f"CHANGELOG.md 裡沒有 {VERSION} 這一節"


def test_the_page_shows_the_changelog_contents(qt_app):
    page = ChangelogPage(_FakeApp())
    page.ensure_built()

    text = page.view.toPlainText()
    assert VERSION in page.findChildren(type(page.view))[0].toPlainText() or VERSION in text
    assert "更新" in text


def test_the_page_is_in_the_sidebar():
    assert ChangelogPage in PAGE_CLASSES


def test_a_missing_changelog_says_so_instead_of_showing_nothing(monkeypatch, tmp_path):
    """讀不到就講出來。一片空白會讓人以為這一版什麼都沒改。"""
    monkeypatch.setattr(
        "gui_qt.pages.changelog.changelog_path", lambda: tmp_path / "nope.md"
    )
    text = load_changelog()
    assert "找不到" in text


def test_the_changelog_is_bundled_into_the_exe():
    """打包後那一頁會不會是空的——只有真的去打包才會發現，所以在這裡擋。"""
    from pathlib import Path

    spec = (Path(__file__).resolve().parent.parent / "packaging" / "roster.spec").read_text(
        encoding="utf-8"
    )
    assert "CHANGELOG.md" in spec, "roster.spec 的 datas 沒有帶上 CHANGELOG.md"
