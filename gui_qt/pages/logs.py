"""日誌頁：在應用程式裡直接看各分類的日誌檔案（tail）。

``LogController.tail()`` 是把整個日誌檔案讀進記憶體再取最後 N 行——實測在
真實環境下（日誌檔案長期累積）這個同步呼叫可以吃掉 40ms 以上，遠超過換頁
20ms 的驗收目標。所以這頁跟儀表板/設定頁一樣，讀檔案這件事丟給
:class:`~gui_qt.tasks.BackgroundTask` 在背景執行緒做，切頁本身只負責觸發、
不等它；結果回來才更新文字框。唯一要小心的是自動重新整理的計時器，離開這頁
或關掉開關時要確實停掉，不要留著空轉。

## 分段按鈕怎麼避免「被拉寬後多一格空白」

Tk 版用 ``CTkSegmentedButton``：使用者反映「錯誤的右側還多了一條垂直的線」
——那其實是分段按鈕在填滿可用寬度時，內部把可用寬度除以分段數，四捨五入
之後多出來的餘數被畫成一個看起來像空按鈕的縫隙。

這裡刻意**不**用任何「一個 widget 內部自己管理多個分段」的元件，而是一排
獨立的 ``QPushButton(checkable=True)`` 丟進 ``QButtonGroup`` 做互斥選取，
每個按鈕用 ``QHBoxLayout.addWidget(button)``（預設 stretch 是 0）—— 按鈕永遠
維持自己文字所需要的寬度，不會被拉寬，自然也不存在「拉寬後除不盡多一格」
這個問題的成因。
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from core.errors import CRMError
from controllers.core import LogController
from core.i18n import LOG_LABELS, label
from gui_qt import theme
from gui_qt.pages.base import BasePage
from gui_qt.tasks import BackgroundTask
from gui_qt.widgets import Section, caption

AUTO_REFRESH_MS = 3000
DEFAULT_LINES = 400


class LogsPage(BasePage):
    title = "日誌"
    icon = "📜"

    def __init__(self, app: object) -> None:
        super().__init__(app)
        self.controller = LogController()
        self._category_buttons: dict[str, QPushButton] = {}

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._auto_tick)

        # 讀檔案這件事丟到背景執行緒——見檔案開頭的說明，同步讀大檔案會讓
        # 換頁延遲遠超過 20ms 的目標。
        self._refresh_task = BackgroundTask(
            self, self._fetch, on_done=self._apply_refresh, on_error=self._handle_refresh_error
        )

    # ------------------------------------------------------------- 建立元件

    def build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(12)

        title_label = QLabel("日誌")
        title_font = title_label.font()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title_label.setFont(title_font)
        outer.addWidget(title_label)

        outer.addLayout(self._build_controls())

        log_section = Section("日誌內容")
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        mono_font = QFont(theme.mono_family())
        mono_font.setPointSize(10)
        self.log_box.setFont(mono_font)
        log_section.body_layout.addWidget(self.log_box)
        outer.addWidget(log_section, 1)

        # 按鈕先建立、預設選第一個分類，最後才接上 toggled——避免
        # setChecked(True) 在 log_box 都還沒建出來之前就觸發一次重整。
        for button in self._category_buttons.values():
            button.toggled.connect(
                lambda checked, btn=button: self._on_category_toggled(btn, checked)
            )

    def _build_controls(self) -> QHBoxLayout:
        controls = QHBoxLayout()
        controls.setSpacing(8)

        try:
            categories = self.controller.categories()
        except CRMError as exc:
            self.report_error(exc)
            categories = []

        group = QButtonGroup(self)
        group.setExclusive(True)
        self._category_group = group
        for index, category in enumerate(categories):
            button = QPushButton(label(category, LOG_LABELS))
            button.setCheckable(True)
            button.setProperty("logCategory", category)
            if index == 0:
                button.setChecked(True)
            group.addButton(button)
            controls.addWidget(button)
            self._category_buttons[category] = button

        controls.addSpacing(12)
        controls.addWidget(caption("行數"))
        self.lines_entry = QLineEdit(str(DEFAULT_LINES))
        self.lines_entry.setFixedWidth(70)
        controls.addWidget(self.lines_entry)

        refresh_button = QPushButton("重新整理")
        refresh_button.clicked.connect(lambda checked=False: self.on_show(force=True))
        controls.addWidget(refresh_button)

        clear_button = QPushButton("清除日誌")
        clear_button.clicked.connect(self._clear_log)
        controls.addWidget(clear_button)

        controls.addStretch(1)

        self.auto_check = QCheckBox("自動重新整理")
        self.auto_check.toggled.connect(self._on_auto_toggle)
        controls.addWidget(self.auto_check)

        return controls

    # ------------------------------------------------------------- 生命週期

    def on_show(self, force: bool = False) -> None:
        # 日誌內容跟「資料版本」（資料庫寫入）無關，每次顯示都該真的重讀檔案。
        super().on_show(force=True)

    def on_hide(self) -> None:
        self._timer.stop()

    def refresh(self) -> None:
        category = self._current_category()
        if not category:
            return
        if self._refresh_task.running:
            return  # 上一次讀取還沒回來，不要疊加第二次
        self._refresh_task.start(category, self._current_lines())

    # ------------------------------------------------------------- 讀取（背景執行緒）

    def _fetch(self, category: str, lines: int, *, report, cancel_event) -> str:
        """在背景執行緒被呼叫；千萬不能在這裡碰任何 widget。"""
        return self.controller.tail(category, lines)

    def _apply_refresh(self, content: str) -> None:
        if getattr(self.app, "current_page", None) != self.title:
            return  # 讀檔案的期間使用者已經切走了，不要更新看不到的頁面
        self.log_box.setPlainText(content)
        cursor = self.log_box.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.log_box.setTextCursor(cursor)

    def _handle_refresh_error(self, exc: Exception) -> None:
        self.report_error(exc)

    # ------------------------------------------------------------- 小工具

    def _current_category(self) -> str | None:
        for category, button in self._category_buttons.items():
            if button.isChecked():
                return category
        return None

    def _current_lines(self) -> int:
        text = self.lines_entry.text().strip()
        try:
            return max(1, int(text))
        except ValueError:
            return DEFAULT_LINES

    def _on_category_toggled(self, button: QPushButton, checked: bool) -> None:
        if checked:
            self.refresh()

    def _clear_log(self) -> None:
        category = self._current_category()
        if not category:
            return
        category_display = label(category, LOG_LABELS)
        reply = QMessageBox.question(
            self,
            "清除日誌",
            f"確定要永久清除「{category_display}」日誌檔案嗎？",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self.controller.clear(category)
        except CRMError as exc:
            self.report_error(exc)
            return
        self.refresh()
        self.status(f"已清除「{category_display}」日誌")

    # --------------------------------------------------------- 自動重新整理

    def _on_auto_toggle(self, checked: bool) -> None:
        if checked:
            self.refresh()
            self._timer.start(AUTO_REFRESH_MS)
        else:
            self._timer.stop()

    def _auto_tick(self) -> None:
        # 跟儀表板的計時器一樣多一層保險：離開這頁時 on_hide() 已經停過一次，
        # 這裡再確認一次目前真的還顯示在這頁。
        if getattr(self.app, "current_page", None) != self.title:
            return
        self.refresh()
