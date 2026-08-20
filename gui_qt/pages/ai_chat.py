"""「AI 助手」頁：跟模型對話。

## 這一頁刻意還不能做的事

它只會聊天。不會抓網頁、不會改資料庫、不會碰你的名單——AI 爬取與問答查詢是
之後的版本（見 CHANGELOG）。

先出這一頁的理由很實際：**使用者需要一個地方確認自己的設定是通的**。金鑰打
錯一個字、Ollama 沒在跑、模型名稱拉錯，這幾件事全都會在「AI 幫我爬一個網站」
的當下才爆炸，而那時候畫面上同時有網路請求、解析、寫入資料庫好幾層，錯誤訊
息會指向錯的地方。有一個「打一句話看它回不回」的地方，這一類問題三秒就分辨
得出來。

## 串流

回覆是一段一段浮出來的，不是等整段完成才顯示。本機模型跑一段長回覆可能要
半分鐘以上，中間完全沒有動靜的話使用者會以為當掉了——而且會去按第二次。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from controllers.ai import AIController, AIStatus, ChatTurn
from core.errors import AINotConfigured
from gui_qt import theme
from gui_qt.pages.base import BasePage
from gui_qt.tasks import BackgroundTask
from gui_qt.widgets import Section, WideComboBox, inline_caption


class AIChatPage(BasePage):
    """跟語言模型對話。"""

    title = "AI 助手"
    icon = "🤖"

    def __init__(self, app: object) -> None:
        super().__init__(app)
        self.controller = AIController()
        #: 畫面上看得到的對話。system prompt 不在裡面——那一段由 controller
        #: 每次送出時補上，這一頁沒有辦法把它拿掉。
        self.history: list[ChatTurn] = []
        #: 串流中的回覆累積在這裡，收完才變成一則正式的 ChatTurn。
        self._pending = ""

        self.chat_task = BackgroundTask(
            self,
            self._ask,
            on_progress=self._on_chunk,
            on_done=self._on_reply,
            on_error=self._on_error,
        )
        # 「有沒有可用的模型」對 Ollama 來說要真的連一次線，最久兩秒。放在
        # 畫面執行緒上做的話，每次切到這一頁介面就凍住——實測 refresh() 花了
        # 7 秒（三個地方各探測一次）。
        self.status_task = BackgroundTask(
            self,
            self.controller.status,
            on_done=self._apply_status,
            on_error=self._on_status_error,
        )

    # ------------------------------------------------------------- 建立元件

    def build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(12)

        title_label = QLabel("AI 助手")
        title_font = title_label.font()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title_label.setFont(title_font)
        outer.addWidget(title_label)

        # 跟匯入／爬取兩頁同樣的理由：內容會長高，放不下時要多一條捲軸而不是
        # 把元件擠在一起。見 tests/test_gui_qt_page_fits.py。
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        body_column = QVBoxLayout(body)
        body_column.setContentsMargins(0, 0, 6, 0)
        body_column.setSpacing(12)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        self._build_status_row(body_column)
        self._build_conversation(body_column)
        self._build_composer(body_column)

    def _build_status_row(self, column: QVBoxLayout) -> None:
        section = Section("使用的模型")

        row = QHBoxLayout()
        row.addWidget(inline_caption("模型"), 0)
        # fill_row()：這一格要填滿整列，不是包住內容。不呼叫的話寬度會在
        # 「內容寬」與「填滿」之間跳，見 WideComboBox.fill_row 的說明。
        self.model_combo = WideComboBox().fill_row()
        self.model_combo.setEditable(True)
        row.addWidget(self.model_combo, 1)
        self.reload_button = QPushButton("重新整理")
        self.reload_button.clicked.connect(self._load_models)
        row.addWidget(self.reload_button, 0, Qt.AlignmentFlag.AlignBottom)
        section.body_layout.addLayout(row)

        self.status_label = QLabel("")
        self.status_label.setObjectName("MutedLabel")
        self.status_label.setWordWrap(True)
        section.body_layout.addWidget(self.status_label)

        # 隱私提醒是一個獨立的標籤而不是混在狀態文字裡：它只在真的會外送時
        # 出現，而且要看得見。混在一長串狀態裡等於沒有講。
        self.privacy_label = QLabel("")
        self.privacy_label.setWordWrap(True)
        self.privacy_label.setStyleSheet(f"color: {theme.pick(theme.DANGER)};")
        self.privacy_label.hide()
        section.body_layout.addWidget(self.privacy_label)

        column.addWidget(section)

    def _build_conversation(self, column: QVBoxLayout) -> None:
        section = Section("對話")
        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setPlaceholderText(
            "還沒有對話。\n\n"
            "這一版的助手只會聊天——它還不會抓網頁，也不會動到你的名單。\n"
            "先用它確認你的模型設定是通的，例如打一句「你好」。"
        )
        self.transcript.setMinimumHeight(theme.text_box_height(10))
        section.body_layout.addWidget(self.transcript)

        buttons = QHBoxLayout()
        self.clear_button = QPushButton("清除對話")
        self.clear_button.clicked.connect(self._clear)
        buttons.addWidget(self.clear_button)
        buttons.addStretch(1)
        section.body_layout.addLayout(buttons)

        column.addWidget(section, 1)

    def _build_composer(self, column: QVBoxLayout) -> None:
        section = Section("問點什麼")
        self.input_box = QPlainTextEdit()
        self.input_box.setPlaceholderText("輸入訊息，按 Ctrl+Enter 送出")
        self.input_box.setFixedHeight(theme.text_box_height(3))
        section.body_layout.addWidget(self.input_box)

        row = QHBoxLayout()
        self.send_button = QPushButton("送出")
        self.send_button.clicked.connect(self._send)
        row.addWidget(self.send_button)
        hint = QLabel("Ctrl+Enter 也可以送出。")
        hint.setObjectName("MutedLabel")
        row.addWidget(hint, 1)
        section.body_layout.addLayout(row)

        column.addWidget(section)

    # ------------------------------------------------------------- 生命週期

    def refresh(self) -> None:
        self.controller = AIController()
        # 設定可能剛改過，丟掉快取重新探測一次。
        self.controller.forget_probes()
        self.status_label.setText("檢查可用的模型…")
        if not self.status_task.running:
            self.status_task.start()

    def _apply_status(self, status: AIStatus) -> None:
        """狀態查回來了。這個方法跑在畫面執行緒上，探測本身不是。"""
        if not status.ready:
            self.status_label.setText(
                "還沒有可用的 AI 模型。到「設定」頁的「AI 模型」設定：\n"
                "本機執行（資料不出門）裝 Ollama，或填一把 OpenRouter 金鑰。"
            )
            self.send_button.setEnabled(False)
            self.privacy_label.hide()
            return

        self.send_button.setEnabled(True)
        model = self.controller.config.ai.model or "（還沒選模型）"
        self.status_label.setText(
            f"目前使用：{status.provider_label}　｜　模型：{model}"
        )
        if status.sends_data_off_device:
            self.privacy_label.setText(
                "⚠ 這個來源會把你打的內容送到你的電腦以外。不要在這裡貼客戶名單或密碼。"
            )
            self.privacy_label.show()
        else:
            self.privacy_label.hide()

        if not self.model_combo.count():
            current = self.controller.config.ai.model
            if current:
                self.model_combo.addItem(current)
                self.model_combo.setCurrentText(current)

    def _on_status_error(self, exc: Exception) -> None:
        # 探測失敗不該擋住整頁：使用者還是可以自己打模型代號送出去試。
        self.status_label.setText(f"檢查模型狀態時出錯：{exc}")
        self.send_button.setEnabled(True)

    # --------------------------------------------------------------- 模型清單

    def _load_models(self) -> None:
        self.reload_button.setEnabled(False)
        BackgroundTask(
            self,
            self.controller.models,
            on_done=self._on_models,
            on_error=self._on_models_error,
        ).start()

    def _on_models(self, models) -> None:
        self.reload_button.setEnabled(True)
        previous = self.model_combo.currentText().strip()
        self.model_combo.clear()
        self.model_combo.addItems([model.id for model in models])
        if previous:
            self.model_combo.setCurrentText(previous)
        self.status(f"找到 {len(models)} 個可用模型", "success")

    def _on_models_error(self, exc: Exception) -> None:
        self.reload_button.setEnabled(True)
        self.report_error(exc)

    # ------------------------------------------------------------------ 送出

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 的命名
        """Ctrl+Enter 送出。

        Enter 保留給換行：使用者常常要貼多行的東西（一段網址清單、一段說明），
        Enter 直接送出會讓那件事變得很痛苦。
        """
        if (
            event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self._send()
            return
        super().keyPressEvent(event)

    def _send(self) -> None:
        text = self.input_box.toPlainText().strip()
        if not text:
            return
        if self.chat_task.running:
            self.status("上一則還在回覆中，等它結束", "warning")
            return

        self.history.append(ChatTurn("user", text))
        self.input_box.clear()
        self._append_line(f"你：{text}\n")
        self._pending = ""
        self._append_line("助手：")

        self.send_button.setEnabled(False)
        self.chat_task.start()

    def _ask(self, report, cancel_event) -> str:
        """在背景執行緒裡跑。

        ``report`` 是 BackgroundTask 的進度回呼，會被排到主執行緒執行——串流
        的每一小段都經過它，所以這裡不會有任何跨執行緒直接碰 widget 的事。
        """
        model = self.model_combo.currentText().strip() or None
        return self.controller.chat(self.history, model=model, on_chunk=report)

    def _on_chunk(self, piece: object) -> None:
        text = str(piece)
        self._pending += text
        self._append_line(text)

    def _on_reply(self, reply: str) -> None:
        self.send_button.setEnabled(True)
        final = reply or self._pending
        if final:
            self.history.append(ChatTurn("assistant", final))
        self._append_line("\n\n")
        self._pending = ""

    def _on_error(self, exc: Exception) -> None:
        self.send_button.setEnabled(True)
        self._append_line("（沒有回覆）\n\n")
        self._pending = ""
        # 「還沒設定」要把人帶去設定頁，不是丟一個錯誤了事——這是使用者
        # 唯一會遇到、而且自己有辦法解決的失敗。
        if isinstance(exc, AINotConfigured):
            QMessageBox.information(self, "還沒設定好 AI", str(exc))
            self._refresh_status()
            return
        self.report_error(exc)

    def _append_line(self, text: str) -> None:
        cursor = self.transcript.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self.transcript.setTextCursor(cursor)
        self.transcript.ensureCursorVisible()

    def _clear(self) -> None:
        self.history.clear()
        self._pending = ""
        self.transcript.clear()
