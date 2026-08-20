"""「AI 助手」頁：貼一個網址讓模型讀，或單純跟它對話。

## 這一頁做兩件事

**從網址抓資料**：貼上網址 → 程式（不是模型）去抓那一頁 → 模型讀完 → 跳出一張
預覽表格 → 使用者把不要的勾掉 → 存進名單。

**聊天**：確認自己的模型設定是通的。金鑰打錯一個字、Ollama 沒在跑、模型代號
拉錯，這幾件事全都會在「抓一個網站」的當下才爆炸，而那時候畫面上同時有網路
請求、模型、比對原文、寫入資料庫好幾層，錯誤訊息會指向錯的地方。有一個「打
一句話看它回不回」的地方，這一類問題三秒就分辨得出來。

## 為什麼中間一定要有預覽這一步

模型抽完可以直接寫進資料庫——三秒的事。但一個把整排導覽選單當成公司名稱的
模型，那三秒會灌進兩百筆垃圾，而清掉它們要花的時間遠比看一眼多。

表格底下會**誠實地寫出丟掉了什麼**（哪幾個值在原始頁面上找不到、頁面文字有沒有
被截短），而且點得開看細節。那份清單就是「這個模型在這個網站上可不可信」的證據
——沒有它，使用者只能憑感覺猜。

## 串流

回覆是一段一段浮出來的，不是等整段完成才顯示。本機模型跑一段長回覆可能要
半分鐘以上，中間完全沒有動靜的話使用者會以為當掉了——而且會去按第二次。
抽取那條路也一樣：它把「已經收到幾個字」報上來當作還活著的證明。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from controllers.ai import (
    AIController,
    AIStatus,
    ChatTurn,
    ExtractCancelled,
    SaveResult,
)
from core.errors import AINotConfigured, RobotsDisallowedError
from gui_qt import theme
from gui_qt.pages.base import BasePage, bump_data_version
from gui_qt.tasks import BackgroundTask
from gui_qt.widgets import DataTable, Section, WideComboBox, inline_caption

#: 預覽表格的欄位。跟 :data:`ai.extract.EXTRACT_FIELDS` 一對一。
#:
#: 七欄全部列出來、不挑幾個重要的顯示，是因為這張表的用途就是「讓使用者在存
#: 進去之前看到即將被存進去的東西」。藏起來的那一欄正好是抽錯的那一欄時，
#: 他要到「公司資訊」頁才會發現。
PREVIEW_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("company_name", "公司名稱", 210),
    ("tax_id", "統一編號", 90),
    ("phone", "電話", 120),
    ("fax", "傳真", 110),
    ("email", "電子信箱", 190),
    ("website", "網址", 160),
    ("address", "地址", 220),
)


class AIChatPage(BasePage):
    """跟語言模型對話，以及請它讀一頁網頁。"""

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
        #: 上一次抽取的結果。表格上的每一列都對應這裡面的一筆。
        self._extracted: list = []
        self._dropped: list = []

        self.chat_task = BackgroundTask(
            self,
            self._ask,
            on_progress=self._on_chunk,
            on_done=self._on_reply,
            on_error=self._on_error,
        )
        self.extract_task = BackgroundTask(
            self,
            self._extract,
            on_progress=self._on_extract_progress,
            on_done=self._on_extracted,
            on_error=self._on_extract_error,
        )
        self.save_task = BackgroundTask(
            self,
            self._save,
            on_done=self._on_saved,
            on_error=self._on_save_error,
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
        self._build_extract(body_column)
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

    def _build_extract(self, column: QVBoxLayout) -> None:
        section = Section("從網址抓資料")

        row = QHBoxLayout()
        row.addWidget(inline_caption("網址"), 0)
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://example.com.tw/members")
        self.url_input.returnPressed.connect(self._fetch)
        row.addWidget(self.url_input, 1)
        self.fetch_button = QPushButton("抓這一頁")
        self.fetch_button.clicked.connect(self._fetch)
        row.addWidget(self.fetch_button, 0, Qt.AlignmentFlag.AlignBottom)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self._cancel_extract)
        self.cancel_button.setEnabled(False)
        row.addWidget(self.cancel_button, 0, Qt.AlignmentFlag.AlignBottom)
        section.body_layout.addLayout(row)

        hint = QLabel(
            "抓網頁的是程式不是模型：一律先查對方的 robots.txt，被擋下來就不抓。"
            "抓回來的整頁文字會送給你選的模型去讀。"
        )
        hint.setObjectName("MutedLabel")
        hint.setWordWrap(True)
        section.body_layout.addWidget(hint)

        self.extract_status = QLabel("")
        self.extract_status.setObjectName("MutedLabel")
        self.extract_status.setWordWrap(True)
        section.body_layout.addWidget(self.extract_status)

        self.preview = DataTable(PREVIEW_COLUMNS, min_rows=4, checkable=True)
        section.body_layout.addWidget(self.preview)

        # 「丟掉了什麼」用一個獨立、看得見的標籤，不是混在狀態列裡。這是這一
        # 頁唯一能讓使用者判斷「這個模型在這個網站上可不可信」的東西。
        self.dropped_label = QLabel("")
        self.dropped_label.setWordWrap(True)
        self.dropped_label.setStyleSheet(f"color: {theme.pick(theme.WARNING)};")
        self.dropped_label.hide()
        section.body_layout.addWidget(self.dropped_label)

        buttons = QHBoxLayout()
        self.save_button = QPushButton("存進名單")
        self.save_button.clicked.connect(self._save_checked)
        self.save_button.setEnabled(False)
        buttons.addWidget(self.save_button)
        self.check_all_button = QPushButton("全部勾選")
        self.check_all_button.clicked.connect(lambda: self.preview.set_all_checked(True))
        buttons.addWidget(self.check_all_button)
        self.uncheck_all_button = QPushButton("全部取消")
        self.uncheck_all_button.clicked.connect(
            lambda: self.preview.set_all_checked(False)
        )
        buttons.addWidget(self.uncheck_all_button)
        self.dropped_button = QPushButton("看被丟掉的值")
        self.dropped_button.clicked.connect(self._show_dropped)
        self.dropped_button.setEnabled(False)
        buttons.addWidget(self.dropped_button)
        buttons.addStretch(1)
        section.body_layout.addLayout(buttons)

        column.addWidget(section, 1)

    def _build_conversation(self, column: QVBoxLayout) -> None:
        section = Section("對話")
        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setPlaceholderText(
            "還沒有對話。\n\n"
            "這裡是純聊天，不會動到你的名單——要抓網頁請用上面那一欄。\n"
            "先用它確認你的模型設定是通的，例如打一句「你好」。"
        )
        self.transcript.setMinimumHeight(theme.text_box_height(8))
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
            self.fetch_button.setEnabled(False)
            self.privacy_label.hide()
            return

        self.send_button.setEnabled(True)
        self.fetch_button.setEnabled(not self.extract_task.running)
        model = self.controller.config.ai.model or "（還沒選模型）"
        self.status_label.setText(
            f"目前使用：{status.provider_label}　｜　模型：{model}"
        )
        if status.sends_data_off_device:
            self.privacy_label.setText(
                "⚠ 這個來源會把你打的內容、以及抓回來的整頁網頁文字送到你的電腦以外。"
                "不要在這裡貼客戶名單或密碼。"
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
        self.fetch_button.setEnabled(True)

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

    # --------------------------------------------------------------- 抓與抽取

    def _fetch(self) -> None:
        if self.extract_task.running:
            self.status("還在抓上一頁，等它結束", "warning")
            return
        url = self.url_input.text().strip()
        if not url:
            self.status("先貼一個網址進來", "warning")
            return

        self.preview.clear()
        self._extracted = []
        self._dropped = []
        self.dropped_label.hide()
        self.dropped_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.fetch_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.extract_status.setText("準備抓取…")
        # 網址與模型代號在這裡（畫面執行緒）先讀好再傳進去。背景執行緒一律
        # 不碰 widget，見 gui_qt/tasks.py 開頭。
        self.extract_task.start(url, self.model_combo.currentText().strip() or None)

    def _cancel_extract(self) -> None:
        self.extract_task.cancel()
        self.extract_status.setText("正在停下來…")

    def _extract(self, url: str, model: str | None, *, report, cancel_event):
        """在背景執行緒裡跑：抓頁面 → 交給模型 → 逐值對回原文。"""
        return self.controller.extract_url(
            url, model=model, report=report, cancel_event=cancel_event
        )

    def _on_extract_progress(self, message: object) -> None:
        self.extract_status.setText(str(message))

    def _on_extracted(self, result) -> None:
        self.fetch_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self._extracted = list(result.records)
        self._dropped = list(result.dropped)

        keys = {key for key, _, _ in PREVIEW_COLUMNS}
        rows = [
            {**record.model_dump(include=keys), "_index": index}
            for index, record in enumerate(self._extracted)
        ]
        self.preview.set_rows(rows)
        self.save_button.setEnabled(bool(rows))

        notes = result.notes()
        self.dropped_button.setEnabled(bool(self._dropped))
        if notes:
            self.dropped_label.setText("　".join(notes))
            self.dropped_label.show()
        else:
            self.dropped_label.hide()

        if not rows:
            self.extract_status.setText(
                "這一頁上沒有抓到任何公司。可能是頁面內容要 JavaScript 才長出來，"
                "或這一頁本來就不是名錄。"
            )
            self.status("沒有抓到公司", "warning")
        else:
            self.extract_status.setText(
                f"抓到 {len(rows)} 筆，勾掉不要的再按「存進名單」。"
            )
            self.status(f"抽出 {len(rows)} 筆，等你確認", "success")

    def _on_extract_error(self, exc: Exception) -> None:
        self.fetch_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

        if isinstance(exc, ExtractCancelled):
            # 他自己按的，不是壞了。跳錯誤視窗會讓人以為按取消把東西弄壞了。
            self.extract_status.setText("已取消，什麼都沒有存進去。")
            self.status("已取消", "warning")
            return
        if isinstance(exc, RobotsDisallowedError):
            self.extract_status.setText(
                f"這一頁被對方的 robots.txt 擋下來了，所以沒有抓：\n{exc.url}\n\n"
                "這支程式不會繞過它。換一個網址，或直接到那個網站上找對方公開的"
                "聯絡方式。"
            )
            self.status("robots.txt 不允許抓這一頁", "error")
            return
        if isinstance(exc, AINotConfigured):
            self.extract_status.setText("")
            QMessageBox.information(self, "還沒設定好 AI", str(exc))
            self.refresh()
            return

        self.extract_status.setText(f"抓不到或讀不懂這一頁：{exc}")
        self.report_error(exc)

    def _show_dropped(self) -> None:
        """列出每一個對不回原始頁面、被丟掉的值。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("被丟掉的值")
        dialog.resize(640, 420)
        layout = QVBoxLayout(dialog)

        caption = QLabel(
            "下面這些值模型有填，但在原始頁面的文字裡對不回去，所以沒有留下。\n"
            "整批都是同一種欄位的話，多半是那個模型在「整理」而不是在「抄寫」——"
            "換一個模型通常就好了。"
        )
        caption.setWordWrap(True)
        layout.addWidget(caption)

        box = QPlainTextEdit()
        box.setReadOnly(True)
        box.setPlainText("\n".join(item.describe() for item in self._dropped))
        layout.addWidget(box, 1)

        close = QPushButton("關閉")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        dialog.exec()

    # ------------------------------------------------------------------ 存檔

    def _save_checked(self) -> None:
        if self.save_task.running:
            return
        chosen = [
            self._extracted[row["_index"]]
            for row in self.preview.checked_rows()
            if "_index" in row
        ]
        if not chosen:
            self.status("一筆都沒有勾", "warning")
            return

        self.save_button.setEnabled(False)
        self.extract_status.setText(f"正在存 {len(chosen)} 筆…")
        self.save_task.start(chosen)

    def _save(self, records, *, report, cancel_event) -> SaveResult:
        return self.controller.save_records(
            records, report=report, cancel_event=cancel_event
        )

    def _on_saved(self, result: SaveResult) -> None:
        self.save_button.setEnabled(True)
        self.extract_status.setText(f"存好了：{result.describe()}。")
        self.status(f"存進名單：{result.describe()}", "success")
        # 「公司資訊」頁下次顯示時要看得到這幾筆。
        bump_data_version()

    def _on_save_error(self, exc: Exception) -> None:
        self.save_button.setEnabled(True)
        self.extract_status.setText(f"存不進去：{exc}")
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
        self.chat_task.start(self.model_combo.currentText().strip() or None)

    def _ask(self, model: str | None, *, report, cancel_event) -> str:
        """在背景執行緒裡跑。

        ``report`` 是 BackgroundTask 的進度回呼，會被排到主執行緒執行——串流
        的每一小段都經過它，所以這裡不會有任何跨執行緒直接碰 widget 的事。
        模型代號同理：在 :meth:`_send`（畫面執行緒）先讀好再傳進來。
        """
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
            self.refresh()
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
