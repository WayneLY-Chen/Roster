"""「AI 助手」頁：從關鍵字或網址把公司抓進名單，或單純跟模型對話。

## 這一頁做四件事

**用關鍵字找網站**：打「台中 CNC 加工」→ 搜一次（**一個**請求）→ 模型替每一筆
搜尋結果貼標籤（名錄／單一公司／不相關）→ 列給使用者勾 → 勾完按下去才真的去抓。

**從網址抓資料**：貼上網址 → 程式（不是模型）去抓那一頁 → 模型讀完 → 跳出一張
預覽表格 → 使用者把不要的勾掉 → 存進名單。

**問你自己的資料庫**：打「哪些台中的公司還沒聯絡過？」→ 模型只負責把問題翻成
一組條件 → **程式**去查、算數量、寫出答案。模型連那個數字都不經手。

**聊天**：確認自己的模型設定是通的。金鑰打錯一個字、Ollama 沒在跑、模型代號
拉錯，這幾件事全都會在「抓一個網站」的當下才爆炸，而那時候畫面上同時有網路
請求、模型、比對原文、寫入資料庫好幾層，錯誤訊息會指向錯的地方。有一個「打
一句話看它回不回」的地方，這一類問題三秒就分辨得出來。

## 為什麼中間有兩道確認

**候選清單**：一個關鍵字可以展開成幾十個網站、每個網站幾十頁。那是使用者要
承擔的頻寬與時間，他得先看到清單、自己勾。模型判斷完之後**不會**自己開始抓。

**預覽表格**：模型抽完可以直接寫進資料庫——三秒的事。但一個把整排導覽選單當成
公司名稱的模型，那三秒會灌進兩百筆垃圾，而清掉它們要花的時間遠比看一眼多。

表格底下會**誠實地寫出丟掉了什麼**（哪幾個值在原始頁面上找不到、頁面文字有沒有
被截短、哪一個網站抓不到），而且點得開看細節。那份清單就是「這個模型在這個網站
上可不可信」的證據——沒有它，使用者只能憑感覺猜。

## 為什麼問答的答案不是模型寫的

「有 12 家」這種答案，使用者沒有任何辦法分辨它是查出來的還是編出來的——編出來
的數字看起來跟真的一模一樣，而他會拿它去做決定。

所以數字由程式從資料庫算出來、由程式印出來，模型只負責挑條件。畫面上一定會寫
出用了哪些條件，而符合的公司就列在下面，點兩下打得開。他要自己驗證的話，去
「公司資訊」頁篩同樣的條件應該得到同一個數字。

## 串流

回覆是一段一段浮出來的，不是等整段完成才顯示。本機模型跑一段長回覆可能要
半分鐘以上，中間完全沒有動靜的話使用者會以為當掉了——而且會去按第二次。
抽取那條路也一樣：它把「已經收到幾個字」報上來當作還活著的證明。
"""

from __future__ import annotations

from urllib.parse import urlsplit

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

from ai.query import Answer
from controllers.ai import (
    AIController,
    AIStatus,
    BatchExtractResult,
    ChatTurn,
    ExtractCancelled,
    SaveResult,
)
from core.errors import AINotConfigured, RobotsDisallowedError
from gui_qt import theme
from gui_qt.company_detail import CompanyDetailDialog
from gui_qt.pages.base import BasePage, bump_data_version
from gui_qt.tasks import BackgroundTask
from gui_qt.widgets import CHECK_KEY, DataTable, Section, WideComboBox, inline_caption

#: 預覽表格的欄位。前七欄跟 :data:`ai.extract.EXTRACT_FIELDS` 一對一。
#:
#: 七個欄位全部列出來、不挑幾個重要的顯示，是因為這張表的用途就是「讓使用者在
#: 存進去之前看到即將被存進去的東西」。藏起來的那一欄正好是抽錯的那一欄時，
#: 他要到「公司資訊」頁才會發現。
#:
#: 最後多一欄「來自」：一次抓好幾個網站時，「這一筆是哪個網站來的」是判斷
#: 「這個網站抓得對不對」唯一的線索。只抓一個網址時它每一列都一樣，那不礙事。
PREVIEW_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("company_name", "公司名稱", 210),
    ("tax_id", "統一編號", 90),
    ("phone", "電話", 120),
    ("fax", "傳真", 110),
    ("email", "電子信箱", 190),
    ("website", "網址", 160),
    ("address", "地址", 220),
    ("_host", "來自", 150),
)

#: 候選網站表格的欄位。
#:
#: 「為什麼」那一欄是模型的說法，不是查證過的事實——所以它擺在使用者眼前讓他
#: 自己判斷，而不是拿來自動篩掉東西。被判定成「不相關」的仍然列出來、仍然勾
#: 得動，只是預設不勾。
SITE_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("kind", "類型", 80),
    ("title", "標題", 250),
    ("url", "網址", 300),
    ("reason", "模型為什麼這樣說", 280),
)

#: 問答結果的欄位。
#:
#: 這張表就是答案的「依據」——只給一個數字的話，使用者沒有辦法判斷那是查出來的
#: 還是編出來的。列出來、而且點兩下打得開，他才驗證得了。
ANSWER_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("company_name", "公司名稱", 240),
    ("industry", "產業", 120),
    ("address", "地址", 250),
    ("email", "電子信箱", 190),
    ("phone", "電話", 120),
    ("pipeline_stage", "階段", 90),
    ("lead_score", "名單品質", 80),
)


#: 工具回來的內容在對話裡最多顯示幾個字。
#:
#: 模型看得到的是完整的那一份（上限在 :data:`ai.mcp.MAX_RESULT_CHARS`）。這裡
#: 只是顯示：一個工具回八千字的話，對話框會被那一段淹掉，使用者要往上捲很久才
#: 找得到自己問了什麼。少掉的部分會照實寫出來還有幾個字。
TOOL_PREVIEW_CHARS = 800


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
        #: 上一次「用關鍵字找網站」的候選清單。
        self._candidates: list = []
        #: 上一次問答查到的那幾家，表格的每一列都對應這裡面的一筆。
        self._answered: list = []
        #: 外部工具（MCP）列到的工具。連過一次就留著，直到使用者重新整理這一頁
        #: ——每送一則訊息就重連一次，等於每一則都要多等幾秒啟動子行程。
        self._tools: list = []
        self._tools_loaded = False
        #: 正在進行中的「可以用工具」那一輪。沒有的時候是 None。
        self._session = None
        #: 這一輪用哪個模型。送出時就讀好，之後每一步都用同一個——中途換模型
        #: 的話，前半段的對話是另一個模型產生的，而那件事看不出來。
        self._turn_model: str | None = None
        #: 使用者送出的訊息在等工具清單連完。
        self._awaiting_tools = False

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
        self.sites_task = BackgroundTask(
            self,
            self._find_sites,
            on_progress=self._on_sites_progress,
            on_done=self._on_sites_found,
            on_error=self._on_sites_error,
        )
        # 抓候選網站跟抓單一網址走的是同一條抽取流程，但結果不一樣（一個是
        # BatchExtractResult），而 BackgroundTask 的 worker 是建立時就綁死的，
        # 所以分成兩個工作而不是同一個。
        self.crawl_task = BackgroundTask(
            self,
            self._crawl_sites,
            on_progress=self._on_extract_progress,
            on_done=self._on_batch_extracted,
            on_error=self._on_extract_error,
        )
        self.save_task = BackgroundTask(
            self,
            self._save,
            on_done=self._on_saved,
            on_error=self._on_save_error,
        )
        self.ask_task = BackgroundTask(
            self,
            self._ask_database,
            on_progress=self._on_ask_progress,
            on_done=self._on_answered,
            on_error=self._on_ask_error,
        )
        # 外部工具那三個工作。分開成三個而不是一個大迴圈，是因為中間那一步
        # （問使用者要不要執行）**必須**在畫面執行緒上跳視窗，而模型與外部
        # 工具都必須在背景執行緒跑。從背景執行緒去等一個視窗的答案是死結的
        # 標準寫法，所以整件事拆成「背景一步、畫面一步」輪流推。
        self.tools_task = BackgroundTask(
            self,
            self.controller.list_tools,
            on_progress=self._on_tool_progress,
            on_done=self._on_tools_listed,
            on_error=self._on_tools_error,
        )
        self.step_task = BackgroundTask(
            self,
            self._next_step,
            on_progress=self._on_tool_progress,
            on_done=self._on_step,
            on_error=self._on_turn_error,
        )
        self.invoke_task = BackgroundTask(
            self,
            self._invoke_tool,
            on_progress=self._on_tool_progress,
            on_done=self._on_tool_output,
            on_error=self._on_turn_error,
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
        self._build_sites(body_column)
        self._build_extract(body_column)
        self._build_ask(body_column)
        self._build_tools(body_column)
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

    def _build_sites(self, column: QVBoxLayout) -> None:
        section = Section("用關鍵字找網站")

        row = QHBoxLayout()
        row.addWidget(inline_caption("關鍵字"), 0)
        self.query_input = QLineEdit()
        self.query_input.setPlaceholderText("台中 CNC 加工")
        self.query_input.returnPressed.connect(self._find)
        row.addWidget(self.query_input, 1)
        self.find_button = QPushButton("找網站")
        self.find_button.clicked.connect(self._find)
        row.addWidget(self.find_button, 0, Qt.AlignmentFlag.AlignBottom)
        self.sites_cancel_button = QPushButton("取消")
        self.sites_cancel_button.clicked.connect(self._cancel_sites)
        self.sites_cancel_button.setEnabled(False)
        row.addWidget(self.sites_cancel_button, 0, Qt.AlignmentFlag.AlignBottom)
        section.body_layout.addLayout(row)

        hint = QLabel(
            "這一步只會送出一次搜尋請求。模型判斷完之後不會自己開始抓——"
            "候選網站一個都不會被碰到，直到你勾選並按下面那顆按鈕。"
        )
        hint.setObjectName("MutedLabel")
        hint.setWordWrap(True)
        section.body_layout.addWidget(hint)

        self.sites_status = QLabel("")
        self.sites_status.setObjectName("MutedLabel")
        self.sites_status.setWordWrap(True)
        section.body_layout.addWidget(self.sites_status)

        self.sites_table = DataTable(SITE_COLUMNS, min_rows=4, checkable=True)
        section.body_layout.addWidget(self.sites_table)

        buttons = QHBoxLayout()
        self.crawl_button = QPushButton("抓勾起來的網站")
        self.crawl_button.clicked.connect(self._crawl_checked)
        self.crawl_button.setEnabled(False)
        buttons.addWidget(self.crawl_button)
        self.sites_check_all_button = QPushButton("全部勾選")
        self.sites_check_all_button.clicked.connect(
            lambda: self.sites_table.set_all_checked(True)
        )
        buttons.addWidget(self.sites_check_all_button)
        self.sites_uncheck_all_button = QPushButton("全部取消")
        self.sites_uncheck_all_button.clicked.connect(
            lambda: self.sites_table.set_all_checked(False)
        )
        buttons.addWidget(self.sites_uncheck_all_button)
        buttons.addStretch(1)
        section.body_layout.addLayout(buttons)

        column.addWidget(section, 1)

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

    def _build_ask(self, column: QVBoxLayout) -> None:
        section = Section("問你自己的資料庫")

        row = QHBoxLayout()
        row.addWidget(inline_caption("問題"), 0)
        self.question_input = QLineEdit()
        self.question_input.setPlaceholderText("哪些台中的公司還沒聯絡過？")
        self.question_input.returnPressed.connect(self._ask_question)
        row.addWidget(self.question_input, 1)
        self.ask_button = QPushButton("問")
        self.ask_button.clicked.connect(self._ask_question)
        row.addWidget(self.ask_button, 0, Qt.AlignmentFlag.AlignBottom)
        section.body_layout.addLayout(row)

        hint = QLabel(
            "只查得了資料，改不了資料——新增、修改、刪除沒有對應的工具，"
            "那些請到「公司資訊」頁做。"
        )
        hint.setObjectName("MutedLabel")
        hint.setWordWrap(True)
        section.body_layout.addWidget(hint)

        # 答案的第一句由程式寫，不是模型寫的。粗體是因為它就是答案本身。
        self.answer_label = QLabel("")
        self.answer_label.setWordWrap(True)
        answer_font = self.answer_label.font()
        answer_font.setBold(True)
        self.answer_label.setFont(answer_font)
        section.body_layout.addWidget(self.answer_label)

        # 「依據」永遠跟答案一起出現。少了它，一個數字沒有辦法被驗證。
        self.basis_label = QLabel("")
        self.basis_label.setObjectName("MutedLabel")
        self.basis_label.setWordWrap(True)
        section.body_layout.addWidget(self.basis_label)

        self.answer_table = DataTable(
            ANSWER_COLUMNS, min_rows=4, on_activate=self._open_answered
        )
        section.body_layout.addWidget(self.answer_table)

        note = QLabel("點兩下任何一列可以打開那家公司的詳細資料。")
        note.setObjectName("MutedLabel")
        section.body_layout.addWidget(note)

        column.addWidget(section, 1)

    def _build_tools(self, column: QVBoxLayout) -> None:
        """外部工具（MCP）的狀態。

        一個工具都沒接的人也看得到這一段，而且看得到怎麼接——藏起來的功能等於
        不存在。接了之後這裡就是「模型現在手上有什麼」唯一看得到的地方。
        """
        section = Section("外部工具（MCP）")

        self.tools_label = QLabel("")
        self.tools_label.setWordWrap(True)
        section.body_layout.addWidget(self.tools_label)

        row = QHBoxLayout()
        self.tools_button = QPushButton("連線並列出工具")
        self.tools_button.clicked.connect(self._reload_tools)
        row.addWidget(self.tools_button)
        hint = QLabel(
            "模型每一次要用工具，都會先跳一個視窗問你，上面寫著要執行哪一個、"
            "帶什麼參數。你不按就不會執行。"
        )
        hint.setObjectName("MutedLabel")
        hint.setWordWrap(True)
        row.addWidget(hint, 1)
        section.body_layout.addLayout(row)

        column.addWidget(section)

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
        # 工具伺服器的設定也可能剛改過。這裡只是把快取作廢，不主動去連——
        # 連線要啟動子行程，每切到這一頁就啟動一輪別人的程式太過分了。
        self._tools = []
        self._tools_loaded = False
        self.status_label.setText("檢查可用的模型…")
        self._describe_tools()
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

        # 這一頁的重新整理可能落在一輪對話進行到一半的時候（使用者切走再切
        # 回來）。無條件把送出鈕打開的話，他會送出第二則，而第一輪的工具確認
        # 視窗還在後面排隊。
        self.send_button.setEnabled(not self._busy())
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
        self.send_button.setEnabled(not self._busy())
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

    # ------------------------------------------------------------ 找網站

    def _find(self) -> None:
        if self.sites_task.running or self.crawl_task.running:
            self.status("還在忙，等它結束", "warning")
            return
        query = self.query_input.text().strip()
        if not query:
            self.status("先打一個關鍵字，例如「台中 CNC 加工」", "warning")
            return

        self.sites_table.clear()
        self._candidates = []
        self.crawl_button.setEnabled(False)
        self.find_button.setEnabled(False)
        self.sites_cancel_button.setEnabled(True)
        self.sites_status.setText("準備搜尋…")
        self.sites_task.start(query, self.model_combo.currentText().strip() or None)

    def _cancel_sites(self) -> None:
        """一顆取消管兩件事：搜尋中就停搜尋，抓取中就停抓取。

        使用者不在乎現在跑的是哪一個工作，他按的是「停」。
        """
        self.sites_task.cancel()
        self.crawl_task.cancel()
        self.sites_status.setText("正在停下來…")

    def _find_sites(self, query: str, model: str | None, *, report, cancel_event):
        """在背景執行緒裡跑：搜一次 → 交給模型貼標籤。不抓任何候選網站。"""
        return self.controller.find_sites(
            query, model=model, report=report, cancel_event=cancel_event
        )

    def _on_sites_progress(self, message: object) -> None:
        self.sites_status.setText(str(message))

    def _on_sites_found(self, result) -> None:
        self.find_button.setEnabled(True)
        self.sites_cancel_button.setEnabled(False)
        self._candidates = list(result.candidates)

        rows = [
            {
                "kind": candidate.kind_label,
                "title": candidate.title,
                "url": candidate.url,
                "reason": candidate.reason,
                "_index": index,
                # 只有模型說「名錄」或「單一公司」的預設勾起來。判斷失準的
                # 那幾筆要他自己動手勾，才不會安靜地變成真的請求。
                CHECK_KEY: candidate.worth_crawling,
            }
            for index, candidate in enumerate(self._candidates)
        ]
        self.sites_table.set_rows(rows)
        self.crawl_button.setEnabled(bool(rows))

        if not rows:
            self.sites_status.setText(
                "搜不到東西。換個關鍵字試試，或直接在下面貼一個網址。"
            )
            self.status("搜不到東西", "warning")
            return

        self.sites_status.setText(
            "　".join(result.notes()) + "　勾好之後按「抓勾起來的網站」。"
        )
        self.status(f"找到 {len(result.worth_crawling)} 個值得抓的網站", "success")

    def _on_sites_error(self, exc: Exception) -> None:
        self.find_button.setEnabled(True)
        self.sites_cancel_button.setEnabled(False)

        if isinstance(exc, ExtractCancelled):
            # 重點在後半句：候選網站一個都沒有被碰到。
            self.sites_status.setText("已取消，沒有抓任何網站。")
            self.status("已取消", "warning")
            return
        if isinstance(exc, AINotConfigured):
            self.sites_status.setText("")
            QMessageBox.information(self, "還沒設定好 AI", str(exc))
            self.refresh()
            return

        self.sites_status.setText(f"搜尋失敗：{exc}")
        self.report_error(exc)

    def _crawl_checked(self) -> None:
        if self.crawl_task.running or self.extract_task.running:
            self.status("還在抓，等它結束", "warning")
            return
        urls = [
            self._candidates[row["_index"]].url
            for row in self.sites_table.checked_rows()
            if "_index" in row
        ]
        if not urls:
            self.status("一個網站都沒有勾", "warning")
            return

        self.preview.clear()
        self._extracted = []
        self._dropped = []
        self.dropped_label.hide()
        self.dropped_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.crawl_button.setEnabled(False)
        self.find_button.setEnabled(False)
        self.sites_cancel_button.setEnabled(True)
        self.extract_status.setText(f"準備抓 {len(urls)} 個網站…")
        self.crawl_task.start(urls, self.model_combo.currentText().strip() or None)

    def _crawl_sites(self, urls, model: str | None, *, report, cancel_event):
        """在背景執行緒裡跑：勾起來的每一個網址各抓一次。"""
        return self.controller.extract_urls(
            urls, model=model, report=report, cancel_event=cancel_event
        )

    def _on_batch_extracted(self, batch: BatchExtractResult) -> None:
        self.find_button.setEnabled(True)
        self.crawl_button.setEnabled(bool(self._candidates))
        self.sites_cancel_button.setEnabled(False)
        self._show_records(
            batch.records,
            batch.dropped,
            batch.notes(),
            empty_hint=(
                "這幾個網站上都沒有抓到公司。可能是頁面內容要 JavaScript 才長"
                "出來，或它們本來就不是名錄。"
            ),
        )

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
        self._show_records(
            result.records,
            result.dropped,
            result.notes(),
            empty_hint=(
                "這一頁上沒有抓到任何公司。可能是頁面內容要 JavaScript 才長出來，"
                "或這一頁本來就不是名錄。"
            ),
        )

    def _show_records(self, records, dropped, notes, *, empty_hint: str) -> None:
        """把抽出來的東西填進預覽表格。單一網址與一次抓好幾個網站共用。

        兩條路唯一的差別是那幾句「丟掉了什麼」，其餘完全一樣——分成兩份寫的話，
        改了一邊忘記另一邊，使用者會看到兩種不一樣的行為。
        """
        self._extracted = list(records)
        self._dropped = list(dropped)

        keys = {key for key, _, _ in PREVIEW_COLUMNS}
        rows = [
            {
                **record.model_dump(include=keys),
                "_host": _host_of(record.source_url),
                "_index": index,
            }
            for index, record in enumerate(self._extracted)
        ]
        self.preview.set_rows(rows)
        self.save_button.setEnabled(bool(rows))
        self.dropped_button.setEnabled(bool(self._dropped))

        if notes:
            self.dropped_label.setText("　".join(notes))
            self.dropped_label.show()
        else:
            self.dropped_label.hide()

        if not rows:
            self.extract_status.setText(empty_hint)
            self.status("沒有抓到公司", "warning")
        else:
            self.extract_status.setText(
                f"抓到 {len(rows)} 筆，勾掉不要的再按「存進名單」。"
            )
            self.status(f"抽出 {len(rows)} 筆，等你確認", "success")

    def _on_extract_error(self, exc: Exception) -> None:
        self.fetch_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        # 這個處理函式兩條路共用（單一網址與一次抓好幾個網站），所以兩邊的
        # 按鈕都要放回去——只放回自己那一邊的話，另一邊會永遠停在「不能按」。
        self.find_button.setEnabled(True)
        self.crawl_button.setEnabled(bool(self._candidates))
        self.sites_cancel_button.setEnabled(False)

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

    # ------------------------------------------------------------ 問資料庫

    def _ask_question(self) -> None:
        if self.ask_task.running:
            self.status("上一個問題還在查，等它結束", "warning")
            return
        question = self.question_input.text().strip()
        if not question:
            self.status("先打一個問題", "warning")
            return

        self.answer_table.clear()
        self._answered = []
        self.basis_label.setText("")
        self.ask_button.setEnabled(False)
        self.answer_label.setText("正在查…")
        self.ask_task.start(question, self.model_combo.currentText().strip() or None)

    def _ask_database(self, question: str, model: str | None, *, report, cancel_event):
        """在背景執行緒裡跑：模型挑條件 → 程式去查。"""
        return self.controller.ask_database(
            question, model=model, report=report, cancel_event=cancel_event
        )

    def _on_ask_progress(self, message: object) -> None:
        self.answer_label.setText(str(message))

    def _on_answered(self, answer: Answer) -> None:
        self.ask_button.setEnabled(True)
        self._answered = list(answer.companies)
        # headline() 是程式寫的字，裡面的數字直接來自資料庫——模型沒有經手過它。
        self.answer_label.setText(answer.headline())
        self.basis_label.setText("　".join(answer.notes()))

        self.answer_table.set_rows(
            [
                {
                    "company_name": company.company_name,
                    "industry": company.industry or "",
                    "address": company.address or "",
                    "email": company.email or "",
                    "phone": company.phone or "",
                    "pipeline_stage": company.pipeline_stage,
                    "lead_score": company.lead_score,
                    "_id": company.id,
                }
                for company in self._answered
            ]
        )
        if answer.cannot:
            self.status("這個問題答不出來", "warning")
        else:
            self.status(f"查到 {answer.total} 家", "success")

    def _on_ask_error(self, exc: Exception) -> None:
        self.ask_button.setEnabled(True)
        if isinstance(exc, ExtractCancelled):
            self.answer_label.setText("已取消。")
            self.status("已取消", "warning")
            return
        if isinstance(exc, AINotConfigured):
            self.answer_label.setText("")
            QMessageBox.information(self, "還沒設定好 AI", str(exc))
            self.refresh()
            return
        self.answer_label.setText(f"查不出來：{exc}")
        self.report_error(exc)

    def _open_answered(self, row: dict) -> None:
        """點兩下打開那家公司的詳細資料。

        直接開既有的那個對話框，而不是自己再畫一份——修改資料的入口只該有一個，
        而那個入口有確認、有紀錄、有備份。
        """
        company_id = row.get("_id")
        if company_id is None:
            return
        from controllers.core import CompanyController

        dialog = CompanyDetailDialog(self, CompanyController(), int(company_id))
        dialog.exec()
        bump_data_version()

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
        if self._busy():
            self.status("上一則還在回覆中，等它結束", "warning")
            return

        self.history.append(ChatTurn("user", text))
        self.input_box.clear()
        self._append_line(f"你：{text}\n")
        self._pending = ""
        self._turn_model = self.model_combo.currentText().strip() or None
        self.send_button.setEnabled(False)

        # 沒接外部工具的人走的還是原本那條路，含串流。接了工具那條沒有辦法
        # 邊收邊顯示：整段收完之前，不知道它是在回話還是在要一個工具，而把
        # 一串 {"tool": …} 一個字一個字印出來，看起來就是程式壞了。
        if not self.controller.uses_tools():
            self._append_line("助手：")
            self.chat_task.start(self._turn_model)
            return

        if not self._tools_loaded:
            self._awaiting_tools = True
            self._append_line("（正在連你接的外部工具…）\n")
            self.tools_task.start()
            return

        self._begin_tool_turn()

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

    # -------------------------------------------------------------- 外部工具

    def _busy(self) -> bool:
        """有沒有一輪對話正在進行。工具那條路會來回好幾次，中間都不能再送。"""
        return (
            self.chat_task.running
            or self.tools_task.running
            or self.step_task.running
            or self.invoke_task.running
            or self._session is not None
        )

    def _describe_tools(self) -> None:
        """把「現在接了什麼」寫在畫面上。"""
        servers = self.controller.mcp_servers()
        enabled = [server for server in servers if server.enabled]
        if not servers:
            self.tools_label.setText(
                "還沒有接任何外部工具。到「設定」頁的「AI 的外部工具（MCP）」"
                "可以加一個——查天氣、讀本機檔案、查另一個資料庫這種這支程式"
                "自己不做的事，接一個現成的伺服器就有了。"
            )
            self.tools_button.setEnabled(False)
            return
        self.tools_button.setEnabled(True)
        names = "、".join(server.name for server in enabled) or "（全部停用中）"
        if self._tools_loaded:
            self.tools_label.setText(
                f"已連上 {len(self._tools)} 個工具，來自：{names}。"
            )
        else:
            self.tools_label.setText(
                f"設定裡有 {len(enabled)} 個啟用中的工具伺服器：{names}。"
                "第一次送出訊息時才會去連（要啟動它們的程式，要幾秒）。"
            )

    def _reload_tools(self) -> None:
        if self._busy():
            self.status("正在忙，等它結束", "warning")
            return
        self._tools_loaded = False
        self._tools = []
        self.tools_button.setEnabled(False)
        self.tools_label.setText("正在連…")
        self.tools_task.start()

    def _on_tool_progress(self, message: object) -> None:
        self.status(str(message), "normal")

    def _on_tools_listed(self, listing) -> None:
        self._tools = list(listing.tools)
        self._tools_loaded = True
        self._describe_tools()
        for name, why in listing.failures:
            # 連不上的一定要講出來。少說的話使用者看到的是「模型不用我接的
            # 工具」，而真正的原因是那支程式根本沒啟動起來。
            self._append_line(f"（連不上工具伺服器「{name}」：{why}）\n")
        self.status(listing.describe(), "warning" if listing.failures else "success")

        if not self._awaiting_tools:
            return
        self._awaiting_tools = False
        if not self._tools:
            self._append_line("（一個工具都沒有連上，這一則當一般對話送出。）\n助手：")
            self.chat_task.start(self._turn_model)
            return
        self._begin_tool_turn()

    def _on_tools_error(self, exc: Exception) -> None:
        self._tools_loaded = False
        self._describe_tools()
        self.tools_button.setEnabled(True)
        if self._awaiting_tools:
            self._awaiting_tools = False
            self._append_line(f"（連外部工具失敗：{exc}）\n助手：")
            self.chat_task.start(self._turn_model)
            return
        self.report_error(exc)

    def _begin_tool_turn(self) -> None:
        self._session = self.controller.start_tools_chat(self.history, self._tools)
        self._append_line("助手：")
        self.step_task.start(self._session, model=self._turn_model)

    def _next_step(self, session, *, model=None, report=None, cancel_event=None):
        """在背景執行緒裡問模型下一步要做什麼。**不會執行任何工具。**"""
        return self.controller.next_step(
            session, model=model, report=report, cancel_event=cancel_event
        )

    def _on_step(self, step) -> None:
        """模型回話了（這個方法跑在畫面執行緒上）。"""
        session = self._session
        if session is None:      # 使用者中途清掉對話了
            return

        if step.unknown:
            self._append_line(
                f"\n（它要了一個不存在的工具「{step.unknown}」，什麼都沒有執行。）\n"
            )
            session.record_unknown(step.unknown)
            self.step_task.start(session, model=self._turn_model)
            return

        if not step.wants_tool:
            self._finish_tool_turn(step.answer)
            return

        call = step.call
        self._append_line(f"\n▶ 它想用工具：{call.qualified}\n")
        if not self._confirm_tool(call):
            # 這裡是整個功能的那一道門。按「不要」的結果不是「稍後再問」，
            # 是這一次的呼叫從來沒有發生過。
            self._append_line("→ 你按了「不要」，沒有執行。\n")
            session.record_refusal(call)
            self.step_task.start(session, model=self._turn_model)
            return

        self._append_line("→ 你同意了，正在執行…\n")
        self.invoke_task.start(call)

    def _confirm_tool(self, call) -> bool:
        """跳出確認視窗。回 ``True`` 才會執行。

        預設按鈕刻意是「不要」：這個視窗會在使用者正在讀對話時突然跳出來，
        而習慣性地敲一下 Enter 不該等於同意執行一支外部程式。

        參數整份印出來，不是只寫工具名字——使用者唯一需要判斷的就是那些值
        （要寫哪個檔案、要送去哪裡）。
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("要執行這個工具嗎？")
        box.setText(f"模型想執行「{call.qualified}」。")
        box.setInformativeText(
            f"{call.describe()}\n\n"
            "這會在你的電腦上執行你自己接的那支程式。不確定的話按「不要」——"
            "它會改用手上的資訊回答。"
        )
        run_button = box.addButton("執行", QMessageBox.ButtonRole.AcceptRole)
        skip_button = box.addButton("不要", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(skip_button)
        box.exec()
        return box.clickedButton() is run_button

    def _invoke_tool(self, call, *, report=None, cancel_event=None):
        """在背景執行緒裡真的執行那一次呼叫。使用者已經按過同意了。"""
        output = self.controller.invoke_tool(
            call, report=report, cancel_event=cancel_event
        )
        return call, output

    def _on_tool_output(self, payload) -> None:
        call, output = payload
        session = self._session
        if session is None:
            return

        preview = output.text.strip()
        if len(preview) > TOOL_PREVIEW_CHARS:
            hidden = len(preview) - TOOL_PREVIEW_CHARS
            preview = (
                preview[:TOOL_PREVIEW_CHARS]
                + f"\n…（還有 {hidden:,} 字沒有顯示在這裡，模型看得到）"
            )
        head = "回報失敗" if output.failed else "回覆"
        tail = "（內容太長，後面被截掉了）" if output.truncated else ""
        self._append_line(f"◀ {call.qualified} {head}{tail}：\n{preview}\n\n")

        session.record_result(call, output)
        self.step_task.start(session, model=self._turn_model)

    def _finish_tool_turn(self, answer: str) -> None:
        self._session = None
        self.send_button.setEnabled(True)
        if answer:
            self.history.append(ChatTurn("assistant", answer))
            self._append_line(f"{answer}\n\n")
        else:
            self._append_line("（沒有回覆）\n\n")

    def _on_turn_error(self, exc: Exception) -> None:
        """工具那條路上出的錯。

        出錯就結束這一輪，不餵回去讓模型自己想辦法：走到這裡的失敗都是環境
        問題（伺服器沒啟動起來、逾時、金鑰不對），模型再試一次也是同一個結果，
        而每一次重試都要使用者再按一個確認視窗。
        """
        self._session = None
        self.send_button.setEnabled(True)
        self._append_line(f"\n（這一輪停在這裡：{str(exc).splitlines()[0]}）\n\n")
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


def _host_of(url: str | None) -> str:
    """網址取主機名稱，給表格的「來自」欄用。

    整個網址塞進那一格會把欄寬撐爆，而使用者要看的只是「哪個網站」。
    """
    host = urlsplit(url or "").netloc
    return host[4:] if host.startswith("www.") else host
