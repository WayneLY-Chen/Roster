"""每一個內容頁在小視窗下都不能把東西擠到看不見的地方。

## 這條測試在防什麼

使用者回報「匯入頁格式跑掉了」，截圖裡「來源標籤（選填）」那行說明蓋在底下
的輸入框上緣，輸入框只剩半截。

成因不是那一行寫錯了，是整頁**放不下**。``QVBoxLayout`` 遇到「要求的高度大於
拿到的高度」時不會變出捲軸，它會先把還能壓的元件壓到最小、再把壓不動的部分
直接溢出容器——溢出的那一段就跟上面的元件疊在一起，或是掉到視窗外面。而這一
頁沒有 ``QScrollArea``，掉出去就再也捲不回來。

同一個症狀在郵件頁發生過一次（「插入變數」那列疊到工具列上），修法就是包一層
``QScrollArea``；那次只修了郵件頁，匯入與爬取兩頁被留了下來。

所以這裡不是只測那兩頁，而是把規則訂在**所有內容頁**上：新加的頁面忘記包捲軸
時，這條會直接擋下來，而不是等使用者截圖回報。

## 為什麼用「最後一張卡片的底部」當判準

頁面內容超出可視範圍的時候，最先看不見的一定是最下面那一張。它的底部只要落在
頁面高度之外，就代表有東西是使用者拿不到的——捲不到、也拉不出來。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gui_qt import theme  # noqa: E402
from gui_qt.pages.companies import CompaniesPage  # noqa: E402
from gui_qt.pages.crawler import CrawlerPage  # noqa: E402
from gui_qt.pages.export_page import ExportPage  # noqa: E402
from gui_qt.pages.feedback import FeedbackPage  # noqa: E402
from gui_qt.pages.import_page import ImportPage  # noqa: E402
from gui_qt.pages.mail import MailPage  # noqa: E402
from gui_qt.pages.settings import SettingsPage  # noqa: E402

#: 一台 13 吋筆電、視窗沒有最大化時的可用高度，再保守一點。使用者回報的那台是
#: 880px 高的視窗配 macOS 的 13pt 字型（比這裡的量測字型高約兩成），所以測試用
#: 的數字要比它小才有意義。
NARROW_WINDOW = (900, 420)


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    theme.apply_theme(app, "light")
    yield app


class _FakeStatusBar:
    def start_progress(self) -> None: ...
    def stop_progress(self) -> None: ...


class _FakeApp:
    def __init__(self) -> None:
        self.status_bar = _FakeStatusBar()

    def set_status(self, message: str, tone: str = "normal") -> None: ...

    def refresh_all(self) -> None: ...


#: 側邊欄選得到的每一個內容頁。
PAGES = [
    CompaniesPage,
    CrawlerPage,
    ExportPage,
    FeedbackPage,
    ImportPage,
    MailPage,
    SettingsPage,
]


def _build(page_class, qt_app):
    page = page_class(_FakeApp())
    page.ensure_built()
    page.resize(*NARROW_WINDOW)
    page.show()
    qt_app.processEvents()
    return page


@pytest.mark.parametrize("page_class", PAGES, ids=lambda cls: cls.__name__)
def test_page_keeps_all_of_itself_reachable_in_a_short_window(page_class, qt_app, db_session):
    page = _build(page_class, qt_app)
    try:
        layout = page.layout()
        last = None
        for index in range(layout.count() - 1, -1, -1):
            widget = layout.itemAt(index).widget()
            if widget is not None:
                last = widget
                break
        assert last is not None, f"{page_class.__name__} 的版面沒有任何 widget"

        bottom = last.mapTo(page, last.rect().bottomLeft()).y()
        overflow = bottom - page.height()
        assert overflow <= 0, (
            f"{page_class.__name__} 在 {NARROW_WINDOW[1]}px 高的視窗下，"
            f"最下面的內容有 {overflow}px 落在視窗外而且捲不到——"
            f"把內容包進 QScrollArea(setWidgetResizable(True))。"
        )
    finally:
        page.hide()
        page.deleteLater()
        qt_app.processEvents()


@pytest.mark.parametrize("page_class", PAGES, ids=lambda cls: cls.__name__)
def test_page_does_not_demand_more_height_than_a_small_laptop_has(page_class, qt_app, db_session):
    """最小高度必須遠低於一個實際的視窗高度。

    上面那條測「這一次有沒有掉東西」，這條測「還剩多少餘裕」。分開的理由是
    餘裕會被字型大小吃掉：macOS 的 13pt 蘋方比這裡量到的字高約兩成，Windows
    上剛好塞得下的版面到了 macOS 就會溢出。留下的空間夠大，那個差異才吃不掉。
    """
    page = _build(page_class, qt_app)
    try:
        demanded = page.minimumSizeHint().height()
        assert demanded <= NARROW_WINDOW[1], (
            f"{page_class.__name__} 最少要 {demanded}px 高，超過 {NARROW_WINDOW[1]}px。"
            f"字型再大一點就會開始重疊——把內容包進 QScrollArea。"
        )
    finally:
        page.hide()
        page.deleteLater()
        qt_app.processEvents()


# 這裡刻意**不**加一條「每一頁都必須有 QScrollArea」。
#
# 捲軸是修法，不是規則。「公司」頁整頁幾乎就是一張表格，表格本來就縮得動，
# 它沒有捲軸也永遠不會溢出——強迫它包一層只是讓測試看起來比較整齊，實際上
# 還會多出一條「表格該撐滿還是該捲動」的矛盾。要守的是上面兩條的性質：
# 東西不能掉出去、而且要留得下字型變大的餘裕。怎麼達成交給頁面自己決定。
