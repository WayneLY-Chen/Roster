"""版面尺寸必須跟著字型走，不能寫死像素。

這支測試的存在理由是兩個真實回報：

1. 使用者在匯出、郵件、設定、網址精靈四個畫面反覆回報「按鈕沒對齊」。
   根因是同一列的控制項高度不一致，靠 AlignBottom/AlignTop 都補不起來
   ——高度不同的話，不管靠哪邊都會有另一邊對不齊。
2. macOS 上介面跑位。根因是尺寸全是照 Windows 的 10pt 微軟正黑體寫死的，
   而 macOS 用 13pt 蘋方，行高高了 5px，寫死的框就裝不下。

沒有 macOS 可以測，但「跑位」的根因是可以在這裡驗的：把字級換成 macOS
的 13pt 重算一次，控制項必須仍然等高。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtGui import QFont, QFontMetrics  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

from gui_qt import theme  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    theme.configure_fonts(app)
    yield app


@pytest.fixture
def at_point_size(qt_app):
    """把介面切到指定字級，離開時還原，避免影響其他測試。"""
    original = qt_app.font()

    def _apply(point_size: int) -> None:
        font = QFont(theme.ui_family())
        font.setPointSize(point_size)
        qt_app.setFont(font)
        theme._measure(font)
        qt_app.setStyleSheet(theme.stylesheet("light"))

    yield _apply

    qt_app.setFont(original)
    theme._measure(original)


#: 10 = Windows 的設定，13 = macOS 的系統介面字級。
@pytest.mark.parametrize("point_size", [10, 13])
def test_row_controls_are_the_same_height_at_any_font_size(qt_app, at_point_size, point_size):
    """同一列的四種控制項必須等高，否則怎麼對齊都會有一邊不齊。"""
    at_point_size(point_size)

    host = QWidget()
    host.move(-3000, -3000)      # 移出畫面，測試不該在使用者桌面閃視窗
    row = QHBoxLayout(host)

    spin = QSpinBox()
    theme.match_control_height(spin)
    widgets = {
        "QLineEdit": QLineEdit(),
        "QPushButton": QPushButton("儲存上限"),
        "QComboBox": QComboBox(),
        "QSpinBox": spin,
    }
    for widget in widgets.values():
        row.addWidget(widget)

    host.show()
    qt_app.processEvents()
    try:
        heights = {name: widget.height() for name, widget in widgets.items()}
        bottoms = {widget.y() + widget.height() for widget in widgets.values()}
    finally:
        host.close()
        host.deleteLater()
        qt_app.processEvents()

    assert len(set(heights.values())) == 1, f"{point_size}pt 下控制項高度不一致：{heights}"
    assert len(bottoms) == 1, f"{point_size}pt 下控制項底邊沒對齊：{bottoms}"


def test_derived_sizes_grow_with_the_font(qt_app, at_point_size):
    """字級變大時尺寸要跟著變大——寫死的話就會維持不變，文字被切掉。"""
    at_point_size(10)
    small = (
        theme.line_height(),
        theme.control_content_height(),
        theme.text_box_height(4),
        theme.input_width(7),
    )
    at_point_size(16)
    large = (
        theme.line_height(),
        theme.control_content_height(),
        theme.text_box_height(4),
        theme.input_width(7),
    )

    for name, before, after in zip(
        ("行高", "控制項內容高", "四行文字框", "七位數輸入框"), small, large
    ):
        assert after > before, f"{name} 沒有跟著字級變大：{before} -> {after}"


def test_windows_metrics_are_unchanged(qt_app, at_point_size):
    """改成推導之後，Windows 上算出來的仍是原本寫死的那些數字。

    先前的對齊是在 Windows 上一格一格調出來的，這裡確保那份成果沒被
    「改成跨平台」這件事弄壞。
    """
    at_point_size(10)
    assert theme.control_content_height() == 22   # 原本 QSS 寫死的 min-height
    assert theme.control_height() == 32           # 22 + 內距 8 + 邊框 2


def test_dropdown_popup_is_wide_enough_to_read_the_options(qt_app, at_point_size):
    """篩選列的下拉框很窄，Qt 預設讓彈出清單跟著一樣寬。

    後果是「包裝材料」「電子零組件」被截成「包...料」「電...件」——使用者
    根本看不出自己在選什麼。下拉框本身要維持窄的（版面需要），只有展開時
    的清單才變寬。
    """
    from gui_qt.widgets import WideComboBox

    at_point_size(10)
    combo = WideComboBox()
    combo.addItems(["全部", "包裝材料", "電子零組件", "精密機械與量測儀器"])
    combo.setFixedWidth(90)          # 模擬篩選列裡的窄下拉框
    combo.show()
    qt_app.processEvents()
    try:
        combo.showPopup()
        qt_app.processEvents()
        popup_width = combo.view().minimumWidth()
    finally:
        combo.hidePopup()
        combo.close()
        combo.deleteLater()
        qt_app.processEvents()

    assert popup_width > combo.width(), "彈出清單沒有比下拉框寬，選項還是會被截斷"
    limit = QFontMetrics(combo.font()).horizontalAdvance("0") * (
        WideComboBox.MAX_POPUP_WIDTH_DIGITS
    )
    assert popup_width <= limit, "不能寬到蓋掉整個畫面"


def test_a_combo_never_reports_a_width_that_would_cut_its_text(qt_app, at_point_size):
    """收合狀態要放得下最長的選項。

    實測在 macOS 上「（全部啟用）」被顯示成「（全部啟用」——原生樣式的箭頭區
    比 Qt 的 sizeHint 預期的寬，全形括號又比字寬估計值再寬一些，加起來差的那
    幾個 px 就吃掉了最後一個字。版面把寬度壓到 minimumSizeHint 時同樣要夠。
    """
    from gui_qt.widgets import WideComboBox

    at_point_size(13)                # macOS 的字級
    combo = WideComboBox()
    combo.addItems(["（全部啟用）", "台北市進出口商業同業公會"])
    try:
        metrics = QFontMetrics(combo.font())
        widest = max(metrics.horizontalAdvance(combo.itemText(i)) for i in range(2))
        for hint in (combo.sizeHint(), combo.minimumSizeHint()):
            assert hint.width() >= min(widest, combo.max_width()), (
                "寬度不足以顯示最長的選項，文字會被切掉"
            )
    finally:
        combo.deleteLater()
        qt_app.processEvents()


def test_inline_caption_matches_the_control_height(qt_app, at_point_size):
    """「單次最多 [50] 封」這種一列裡，說明文字要跟輸入框同高才對得齊。

    一列如果有 LabeledEntry（說明在上、輸入框在下，兩行高），普通 QLabel 會
    被擺在那一列的垂直中央，而旁邊的輸入框靠下——文字就浮在半空中。
    """
    from gui_qt.widgets import caption, inline_caption

    at_point_size(10)
    plain = caption("單次最多")
    sized = inline_caption("單次最多")

    assert sized.height() == theme.control_height()
    # 普通 caption 沒有被固定高度，這正是兩者的差別。
    assert plain.height() != sized.height() or plain.maximumHeight() > sized.height()


def test_platform_font_preference_puts_the_native_family_first():
    """Mac 上如果裝了 Office，微軟正黑體會出現在字型清單裡。

    真的被選到的話，Mac 會用一個 Windows 字型算版面，字寬與行高都跟系統
    原生不同——所以各平台一定要把自己的原生字型排在最前面。
    """
    families = theme.preferred_families()
    assert families, "字型偏好清單不能是空的"

    mac_index = families.index("PingFang TC")
    windows_index = families.index("Microsoft JhengHei UI")

    import sys

    if sys.platform == "darwin":
        assert mac_index < windows_index
    elif sys.platform == "win32":
        assert windows_index < mac_index


# --------------------------------------------------- 整個介面的文字寬度


#: 量測本身的誤差容許值。QLabel 沒有內距、QPushButton 的內距由樣式表決定，
#: 這裡的估算不可能跟 Qt 的排版完全一致，差幾個 px 不算問題。
_WIDTH_TOLERANCE = 6

_BUTTON_PADDING = 26
_COMBO_CHROME = 36


def _text_and_padding(widget) -> tuple[str, int] | None:
    """這個元件要顯示的最長文字，以及文字以外要留的空間。"""
    from PySide6.QtWidgets import QCheckBox, QComboBox, QLabel, QPushButton, QRadioButton

    if isinstance(widget, QComboBox):
        texts = [widget.itemText(i) for i in range(widget.count())]
        texts = texts or [widget.currentText()]
        metrics = QFontMetrics(widget.font())
        return max(texts, key=metrics.horizontalAdvance, default=""), _COMBO_CHROME
    if isinstance(widget, (QPushButton, QCheckBox, QRadioButton)):
        return widget.text(), _BUTTON_PADDING
    if isinstance(widget, QLabel) and not widget.wordWrap():
        return widget.text(), 0
    return None


def _widgets_whose_text_does_not_fit(app) -> list[str]:
    offenders: list[str] = []
    for widget in app.allWidgets():
        if not widget.isVisible() or widget.width() <= 1:
            continue
        found = _text_and_padding(widget)
        if found is None:
            continue
        text, padding = found
        # 富文字（超連結、粗體）量不準，而且它們一律有換行，跳過。
        if not text or "<" in text:
            continue
        needed = QFontMetrics(widget.font()).horizontalAdvance(text) + padding
        if needed > widget.width() + _WIDTH_TOLERANCE:
            offenders.append(
                f"{type(widget).__name__}「{text[:40]}」需要 {needed}px，只有 {widget.width()}px"
            )
    return offenders


#: 10 = Windows 的設定，13 = macOS 的系統介面字級，16 = 放大顯示的使用者。
@pytest.mark.parametrize("point_size", [10, 13, 16])
def test_no_text_in_the_whole_window_is_cut_off(qt_app, point_size, monkeypatch):
    """整個介面掃一遍，確認沒有任何元件的文字放不下自己的框。

    這是重複發生過好幾次的一類問題，成因都一樣：版面尺寸寫死成像素。
    macOS 的系統介面字比 Windows 大一號，Windows 上剛好的寬度到了 Mac 就會
    少掉最後一兩個字——「（全部啟用）」變成「（全部啟用」。逐個元件檢查比
    等使用者截圖回報快得多。
    """
    from core.config import get_config
    from gui_qt.app import MainWindow

    original_font = qt_app.font()

    # MainWindow 建立時會把字型設回平台預設；換成我們要模擬的字級，
    # 這正是那個函式在 macOS 上會做的事。
    def fake_configure(app) -> str:
        font = QFont(theme.ui_family())
        font.setPointSize(point_size)
        app.setFont(font)
        theme._measure(font)
        return font.family()

    monkeypatch.setattr(theme, "configure_fonts", fake_configure)

    window = MainWindow(get_config())
    try:
        window.resize(1280, 860)
        window.show()
        qt_app.processEvents()
        for button in window.nav_buttons.values():
            button.click()          # 每一頁都要建出來才量得到
            qt_app.processEvents()

        offenders = _widgets_whose_text_does_not_fit(qt_app)
        assert not offenders, "這些元件的文字會被截斷：\n" + "\n".join(offenders)
    finally:
        window.close()
        window.deleteLater()
        qt_app.setFont(original_font)
        theme._measure(original_font)
        qt_app.processEvents()


@pytest.mark.parametrize("point_size", [10, 13, 16])
def test_no_text_in_the_wizard_dialogs_is_cut_off(qt_app, point_size, monkeypatch):
    """精靈與「找名錄」是獨立的對話框，不在主視窗底下，上面那支掃不到。

    新的控制項（逐項查詢、全選、全部取消）都住在這兩個視窗裡。
    """
    from gui_qt.explore_dialog import ExploreResultsDialog
    from gui_qt.source_wizard import SourceWizardDialog

    class _Candidate:
        url = "https://example.org.tw/members"
        item_count = 20
        page_count = 3
        sample_names = ["甲有限公司", "乙股份有限公司"]
        company_name_ratio = 0.9
        kind = ""

    class _ExploreResult:
        start_url = "https://example.org.tw/"
        candidates = [_Candidate(), _Candidate()]
        pages_fetched = 12
        notes = ["已達 30 頁的讀取上限就停下來了。"]

    class _Controller:
        def crawl_delay(self) -> float:
            return 1.0

        def custom_sources(self) -> list:
            return []

    original_font = qt_app.font()
    font = QFont(theme.ui_family())
    font.setPointSize(point_size)
    qt_app.setFont(font)
    theme._measure(font)

    dialogs = []
    try:
        explore = ExploreResultsDialog(None, _ExploreResult())
        explore.resize(940, 640)
        explore.show()
        dialogs.append(explore)

        wizard = SourceWizardDialog(None, controller=_Controller())
        wizard.resize(980, 900)
        wizard.show()
        wizard.advanced_section.set_expanded(True)
        dialogs.append(wizard)
        qt_app.processEvents()

        offenders = _widgets_whose_text_does_not_fit(qt_app)
        assert not offenders, "這些元件的文字會被截斷：\n" + "\n".join(offenders)
    finally:
        for dialog in dialogs:
            dialog.close()
            dialog.deleteLater()
        qt_app.setFont(original_font)
        theme._measure(original_font)
        qt_app.processEvents()
