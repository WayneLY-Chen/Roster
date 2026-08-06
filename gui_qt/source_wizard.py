"""網址精靈：貼上網址、看自動偵測結果、視需要微調、儲存或試爬——PySide6 版。

對照 ``gui/pages/source_wizard.py``（Tk 版）。``crawler.discover.discover()``
的猜測永遠只是「起點」，不是黑盒子：每一個它選到的 CSS 選擇器都攤在一張表格
裡讓使用者看得到、改得動，而且在使用者選擇「儲存」之前不會寫入任何東西。

## 最重要的設計約束：一般使用者不需要懂 CSS 選擇器

這支專案的使用者明確抱怨過：「一般使用者哪知道什麼清單選擇器、下一頁連結
選擇器、CSS 選擇器、取值方式」。所以整個對話框分成兩層：

    * 上層（"1. 貼上網址"、"2. 分析結果"、"3. 預覽抓到的資料"、"4. 儲存"）
      全部是白話文——「找到 N 筆資料」「抓得到的欄位」「沒問題就按儲存」，
      完全不提「選擇器」三個字。
    * 技術性的 CSS 選擇器欄位全部收在最下面的 :class:`CollapsibleSection`
      裡，預設收合，只有自動偵測結果不理想（抓不到公司名稱、或整頁都沒偵測
      到清單）時才會自動展開——見 :meth:`SourceWizardDialog._on_analyse_done`
      最後幾行。

``gui_qt/widgets.py`` 目前沒有可摺疊的區塊（``Section`` 一律展開），所以
:class:`CollapsibleSection` 定義在這個檔案裡，不去動共用檔案。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.errors import CrawlError, CRMError, RobotsDisallowedError
from crawler.discover import DISCOVERY_STEPS
from crawler.documents import DOCUMENT_KINDS
from crawler.pipeline import COLLECTABLE_FIELDS
from controllers.source import KNOWN_FIELDS, PREVIEW_FIELDS, SourceWizardController
from core.i18n import field_label
from gui_qt import theme
from gui_qt.pages.base import bump_data_version
from gui_qt.tasks import BackgroundTask
from gui_qt.widgets import (
    DataTable,
    LabeledEntry,
    Section,
    WideComboBox,
    caption,
    inline_caption,
)

FIELD_COLUMNS = [
    ("field", "欄位", 110),
    ("samples", "抓到的內容", 300),
    ("hit_rate", "抓到比例", 80),
    ("selector", "取值位置（CSS 選擇器）", 220),
    ("attr", "取什麼", 90),
]

PREVIEW_COLUMNS = [
    ("company_name", "公司名稱", 160),
    ("email", "信箱", 160),
    ("phone", "電話", 110),
    ("website", "網站", 160),
    ("address", "地址", 200),
    ("industry", "產業", 100),
]

#: 格式代碼 -> 給人看的短名稱（「PDF 檔」而不是「PDF 檔（.pdf）」）。
DOCUMENT_LABELS = {kind.key: kind.label.split("（")[0] for kind in DOCUMENT_KINDS}

#: 中文標籤 -> 欄位代碼，是 ``field_label`` 的反查表。
_FIELD_CODE_BY_LABEL: dict[str, str] = {field_label(code): code for code in KNOWN_FIELDS}


def _friendly_error(exc: Exception) -> str:
    """把攔到的例外翻成非工程師也看得懂、也能照著做的訊息。"""
    if isinstance(exc, RobotsDisallowedError):
        return "這個網站的 robots.txt 規則不允許爬取此網址，因此無法分析或預覽。"
    if isinstance(exc, CrawlError):
        return f"無法完成：{exc}"
    if isinstance(exc, CRMError):
        return str(exc)
    return f"發生未預期的錯誤：{type(exc).__name__}: {exc}"


class CollapsibleSection(QFrame):
    """可摺疊的卡片，標題列可以點擊展開/收合，預設收合。

    對應 ``gui.widgets.CollapsibleSection``：把「大多數人永遠不需要碰」的
    控制項摺起來，不是拿掉——自動偵測猜錯時，這裡的 CSS 選擇器仍然要
    找得到，只是預設不會嚇到只想貼一個網址的人。

    複用 :class:`gui_qt.widgets.Section` 同一個 QSS 選擇器
    （``QFrame#Section``）取得一致的卡片外觀，不需要另外修改共用的
    ``gui_qt/theme.py``。
    """

    def __init__(
        self,
        title: str,
        hint: str = "",
        expanded: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Section")
        self._title = title
        self._expanded = expanded

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(6)

        self.toggle_button = QPushButton()
        button_font = self.toggle_button.font()
        button_font.setBold(True)
        button_font.setPointSize(13)
        self.toggle_button.setFont(button_font)
        self.toggle_button.setFlat(True)
        self.toggle_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_button.setStyleSheet("text-align: left; border: none; background: transparent;")
        self.toggle_button.clicked.connect(self.toggle_expanded)
        outer.addWidget(self.toggle_button)

        self.hint_label = QLabel(hint)
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet(f"color: {theme.pick(theme.MUTED)};")
        if hint:
            outer.addWidget(self.hint_label)
        else:
            self.hint_label.hide()

        #: 頁面把技術性控制項加進這裡，跟 gui_qt.widgets.Section 的
        #: ``body``/``body_layout`` 用法一致。
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.body, 1)

        self._sync()

    def toggle_expanded(self) -> None:
        self.set_expanded(not self._expanded)

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self._sync()

    @property
    def expanded(self) -> bool:
        return self._expanded

    def _sync(self) -> None:
        arrow = "▾" if self._expanded else "▸"
        self.toggle_button.setText(f"{arrow} {self._title}")
        self.body.setVisible(self._expanded)


class SourceWizardDialog(QDialog):
    """貼上網址、看自動偵測結果、視需要微調、儲存或試爬。"""

    def __init__(
        self,
        parent: QWidget | None,
        controller: SourceWizardController,
        on_saved: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.on_saved = on_saved

        self.setWindowTitle("自訂網址精靈")
        self.resize(1000, 760)
        self.setMinimumSize(920, 640)

        # 不擋住主程式，而且可以縮到旁邊去。
        #
        # 分析一個站要一兩分鐘（開瀏覽器、試查、往下點一層），期間整張表單是
        # 反灰的。原本它是強制回應視窗，等於那一兩分鐘裡整個程式都不能動——
        # 使用者連去「公司」頁看一眼上一批爬到什麼都不行，只能盯著它。
        # 現在只有這個視窗自己在等，主視窗照常可以看。
        self.setModal(False)
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
        )

        # 分析成功後才會有值；``field_rules`` 把欄位代碼對應到
        # {"selector", "attr", "regex", "hit_rate", "samples"}——多出來的
        # 顯示用欄位在儲存前會被 _plain_rules() 濾掉。
        self.last_url: str = ""
        self.field_rules: dict[str, dict[str, Any]] = {}
        self.preview_rows: list[dict[str, Any]] = []
        self._editing_field: str | None = None

        self._suggested_actions: list[dict] = []

        self.analyse_task: BackgroundTask | None = None
        self.explore_task: BackgroundTask | None = None
        self.batch_task: BackgroundTask | None = None
        #: 分析時判定「這個網站要開瀏覽器才看得到資料」的話會填進來。
        self._detected_engine: str | None = None
        #: 分析時偵測到的查詢選單（要先選條件才有資料的那種）。
        self._query_form: dict = {}
        #: 正在編輯的是哪一個已存來源；``None`` 代表這是新增。
        #: 改了名字的話，儲存時要順手把舊的那一份收掉，否則會多出一個孤兒。
        self._editing_source: str | None = None
        self.preview_task: BackgroundTask | None = None
        self.crawl_task: BackgroundTask | None = None

        self._build()

    # ------------------------------------------------------------- layout

    def _build(self) -> None:
        outer = QVBoxLayout(self)

        notice = QLabel(
            "只會爬取公開資料，並遵守目標網站的 robots.txt 規則與設定中的請求延遲。"
        )
        notice.setWordWrap(True)
        notice.setStyleSheet(f"color: {theme.pick(theme.MUTED)};")
        outer.addWidget(notice)

        # 六個堆疊區塊塞不進任何合理的對話框高度，內容用捲動的，而不是被裁掉。
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll, 1)

        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setSpacing(12)
        scroll.setWidget(container)

        self._build_url_section(container_layout)
        self._build_summary_section(container_layout)
        self._build_preview_section(container_layout)
        self._build_bottom_section(container_layout)
        # 以下只有在自動偵測猜錯時才需要：折疊起來，才不會讓整個對話框
        # 一開始看起來就需要專業知識。
        self._build_advanced_section(container_layout)
        container_layout.addStretch(1)

    def _build_url_section(self, parent_layout: QVBoxLayout) -> None:
        section = Section("1. 貼上網址")
        row = QHBoxLayout()

        self.url_entry = QLineEdit()
        self.url_entry.setPlaceholderText("貼上要爬取的網址，例如 https://example.com/companies")
        row.addWidget(self.url_entry, 1)

        self.analyse_button = QPushButton("分析網頁")
        self.analyse_button.clicked.connect(self._start_analyse)
        row.addWidget(self.analyse_button)

        # 不知道名錄在哪一頁的時候用這個。名錄常常藏在「關於我們 → 組織 →
        # 會員專區」底下三層，從首頁完全看不出來。
        self.explore_button = QPushButton("找名錄…")
        self.explore_button.setToolTip(
            "不確定名錄在哪一頁時，貼上這個網站的任一個網址，\n"
            "程式會在站內找出看起來像廠商名錄的頁面。"
        )
        self.explore_button.clicked.connect(self._start_explore)
        row.addWidget(self.explore_button)
        section.body_layout.addLayout(row)

        hint = QLabel(
            "知道名錄網址就按「分析網頁」；只知道網站、不知道名錄在哪一頁，"
            "按「找名錄…」讓程式去站內找。"
        )
        hint.setObjectName("MutedLabel")
        hint.setWordWrap(True)
        section.body_layout.addWidget(hint)

        # 分析一個 JavaScript 名錄要一兩分鐘（讀頁面、開瀏覽器、試查、往下
        # 點一層）。不定進度條唯一回答得了的問題是「有沒有當掉」——跑了 5 秒
        # 跟跑了 90 秒看起來一模一樣。改成走固定的幾個階段，而且把「現在在
        # 做什麼」寫在旁邊。
        self.analyse_progress = QProgressBar()
        self.analyse_progress.setRange(0, 0)
        self.analyse_progress.hide()
        section.body_layout.addWidget(self.analyse_progress)

        self.analyse_step_label = QLabel("")
        self.analyse_step_label.setObjectName("MutedLabel")
        self.analyse_step_label.setWordWrap(True)
        self.analyse_step_label.hide()
        section.body_layout.addWidget(self.analyse_step_label)

        parent_layout.addWidget(section)

    def _build_summary_section(self, parent_layout: QVBoxLayout) -> None:
        section = Section("2. 分析結果與收集設定")

        self.summary_label = QLabel("尚未分析。請先貼上網址並按「分析網頁」。")
        self.summary_label.setWordWrap(True)
        section.body_layout.addWidget(self.summary_label)

        self.notes_label = QLabel("")
        self.notes_label.setWordWrap(True)
        self.notes_label.setStyleSheet(f"color: {theme.pick(theme.MUTED)};")
        section.body_layout.addWidget(self.notes_label)

        self._build_collect_controls(section.body_layout)
        self.summary_section = section
        parent_layout.addWidget(section)

    def _build_collect_controls(self, body_layout: QVBoxLayout) -> None:
        """要爬幾頁、要收集哪些欄位。

        放在第 2 步而不是「進階設定」裡：這些是「你想要什麼資料」的決定，
        每個人都要做一次。進階設定裡的是 CSS 選擇器——那才是「自動偵測猜錯
        時才需要碰」的東西，兩者不是同一種。

        這些設定會跟著來源存起來，所以自動排程去爬的時候也照著做。
        """
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet(f"color: {theme.pick(theme.BORDER)};")
        body_layout.addWidget(divider)

        pages_row = QHBoxLayout()
        self.page_start_entry = LabeledEntry("從第幾頁開始", value="1")
        pages_row.addWidget(self.page_start_entry)
        self.page_end_entry = LabeledEntry("爬到第幾頁為止（留空＝不限）")
        pages_row.addWidget(self.page_end_entry)
        self.max_pages_entry = LabeledEntry("最多爬幾頁", value="3")
        pages_row.addWidget(self.max_pages_entry)
        # 名錄網站幾乎都是一個分類一頁，分類本身就是產業——但它寫在麵包屑或
        # 頁面標題裡，逐列抓的欄位規則抓不到它，產業欄就永遠是空的。
        self.default_industry_entry = LabeledEntry("這個來源的產業（頁面沒寫時才套用）")
        pages_row.addWidget(self.default_industry_entry)
        body_layout.addLayout(pages_row)

        collect_header = QHBoxLayout()
        collect_header.addWidget(caption("要收集哪些欄位（公司名稱一定會收集）"))
        collect_header.addStretch(1)
        all_button = QPushButton("全選")
        all_button.clicked.connect(lambda: self._set_all_collect(True))
        collect_header.addWidget(all_button)
        none_button = QPushButton("全不選")
        none_button.clicked.connect(lambda: self._set_all_collect(False))
        collect_header.addWidget(none_button)
        body_layout.addLayout(collect_header)

        checks_row = QHBoxLayout()
        self.collect_checks: dict[str, QCheckBox] = {}
        for field, label_text in COLLECTABLE_FIELDS:
            check = QCheckBox(label_text)
            check.setChecked(True)
            self.collect_checks[field] = check
            checks_row.addWidget(check)
        checks_row.addStretch(1)
        body_layout.addLayout(checks_row)

        collect_note = QLabel(
            "沒勾的欄位在寫入資料庫前會被清空。這些設定跟著這個來源存起來，"
            "所以自動排程去爬的時候也會照著做。"
        )
        collect_note.setWordWrap(True)
        collect_note.setStyleSheet(f"color: {theme.pick(theme.MUTED)};")
        body_layout.addWidget(collect_note)

        # 分析完才打開。順序上一定是「先分析、才知道有哪些欄位可以選、
        # 這個名錄有幾頁」——分析之前就讓它們可以按，看起來像是要先填好
        # 才能按分析，剛好把因果顛倒過來。
        self._collect_widgets = [
            self.page_start_entry,
            self.page_end_entry,
            self.max_pages_entry,
            self.default_industry_entry,
            all_button,
            none_button,
            *self.collect_checks.values(),
        ]
        self._set_collect_enabled(False)

        # 檔案格式的勾選**不**跟著分析開關。它跟「這一頁有哪些欄位」無關，
        # 而且「找名錄…」是在分析之前按的，那時候也要能決定要不要找 PDF。
        self._build_document_controls(body_layout)

    def _build_document_controls(self, body_layout: QVBoxLayout) -> None:
        """要不要順便讀頁面上連出去的 PDF／Excel／Word。

        跟「要收集哪些欄位」放在一起：兩者都是「你想要什麼資料」的決定，
        使用者在同一個地方一次勾完。

        **預設全部不勾。** 讀別人的檔案跟讀網頁不是同一件事：檔案通常大得多，
        而且使用者未必想要那些內容——這要他自己決定，不能預設幫他決定。
        """
        body_layout.addWidget(caption("要不要順便讀頁面上的檔案（預設不讀）"))

        row = QHBoxLayout()
        self.document_checks: dict[str, QCheckBox] = {}
        for kind in DOCUMENT_KINDS:
            check = QCheckBox(kind.label)
            check.setChecked(False)
            # 分析之前一律不能勾：要先知道這一頁到底有沒有這種檔案。
            check.setEnabled(False)
            self.document_checks[kind.key] = check
            row.addWidget(check)
        row.addStretch(1)
        body_layout.addLayout(row)

        self.document_note = QLabel("分析網頁之後，這一頁真的有的檔案格式才會亮起來。")
        self.document_note.setWordWrap(True)
        self.document_note.setStyleSheet(f"color: {theme.pick(theme.MUTED)};")
        body_layout.addWidget(self.document_note)

        note = QLabel(
            "不少公協會沒有把會員名冊做成網頁，而是掛一個 PDF 或 Excel，"
            "常常還要先點進某個子頁面才看得到那個連結。勾了之後，爬取時遇到"
            "這種連結就會跟進去把裡面的名單讀出來。\n"
            "讀出來的內容不像廠商名冊時（章程、會議記錄、年報那一類）會整份略過，"
            "不會把雜訊收進資料庫。"
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.pick(theme.MUTED)};")
        body_layout.addWidget(note)

        # 「不點就看不到」的按鈕。跟上面的檔案格式擺在一起：都是「這個名錄的
        # 資料還藏在哪裡」的決定。分析時偵測到才會啟用。
        self.click_check = QCheckBox("先點開頁面上的按鈕（顯示電話／載入更多）")
        self.click_check.setChecked(False)
        self.click_check.setEnabled(False)
        body_layout.addWidget(self.click_check)

        self.click_note = QLabel(
            "分析時如果發現「顯示電話」「載入更多」之類的按鈕，這一項才會啟用。"
            "那些資料不在原始網頁裡，要按下去才會出現——需要用瀏覽器引擎，"
            "比一般爬取慢很多。"
        )
        self.click_note.setWordWrap(True)
        self.click_note.setStyleSheet(f"color: {theme.pick(theme.MUTED)};")
        body_layout.addWidget(self.click_note)

        self._build_query_loop_controls(body_layout)

    def _build_query_loop_controls(self, body_layout: QVBoxLayout) -> None:
        """要先選一個條件才有資料的名錄：逐項查詢。

        跟頁數上限是同一種東西——「總共要跑幾趟」——所以放在一起，欄位也用
        同樣的樣子：偵測到幾個會講出來，實際要查幾個由使用者決定。
        """
        self.query_loop_check = QCheckBox("這一頁要先查詢才有資料，幫我一項一項查")
        self.query_loop_check.setChecked(False)
        self.query_loop_check.setEnabled(False)
        self.query_loop_check.toggled.connect(self._sync_query_loop_enabled)
        body_layout.addWidget(self.query_loop_check)

        # 說明放在勾選框正下方，欄位放在說明下面。反過來的話，使用者先看到的
        # 是一個看不懂的欄位，解釋它的那段字還在更下面。
        self.query_loop_note = QLabel(
            "有一類名錄不給完整清單，只給一個查詢框：選一個分類、或打一個關鍵字，"
            "按下查詢才會出現廠商。分析時偵測到這種查詢框，這一項才會啟用。"
        )
        self.query_loop_note.setWordWrap(True)
        self.query_loop_note.setStyleSheet(f"color: {theme.pick(theme.MUTED)};")
        body_layout.addWidget(self.query_loop_note)

        # 起訖跟頁碼那一排是同一個意思，所以用同一種樣子：97 個分類跑完要
        # 好幾個小時，使用者本來就會分次跑。沒有起點的話，想爬第 7 個分類的
        # 唯一辦法是從第 1 個重跑到第 7 個。
        # 要查哪一段。兩種指法擇一，選了一種另一種就反灰——兩邊都填得下去的話
        # 「到底哪一個算數」就變成一個沒有人答得出來的問題，而答錯的代價是跑掉
        # 一整個下午。
        mode_row = QHBoxLayout()
        mode_row.addWidget(caption("要查哪一段"))
        self.query_by_number = QRadioButton("用序號")
        self.query_by_number.setChecked(True)
        self.query_by_number.toggled.connect(self._sync_query_range_mode)
        mode_row.addWidget(self.query_by_number)
        self.query_by_text = QRadioButton("用選項上的文字")
        mode_row.addWidget(self.query_by_text)
        mode_row.addStretch(1)
        body_layout.addLayout(mode_row)

        number_row = QHBoxLayout()
        self.query_start_entry = LabeledEntry("從第幾個開始", "1")
        number_row.addWidget(self.query_start_entry)
        self.query_end_entry = LabeledEntry("到第幾個為止（留空＝往後數）", "")
        number_row.addWidget(self.query_end_entry)
        # 預設查 3 個：先少量試跑，確定抓對了再放大。97 個條件一次跑下去，
        # 錯了就是白等半小時，而且是對別人的網站送出幾百次請求。
        self.query_count_entry = LabeledEntry("往後查幾個", "3")
        number_row.addWidget(self.query_count_entry)
        body_layout.addLayout(number_row)

        text_row = QHBoxLayout()
        self.query_start_text_entry = LabeledEntry(
            "從哪一個開始", "", "例如：03 魚類"
        )
        text_row.addWidget(self.query_start_text_entry)
        self.query_end_text_entry = LabeledEntry(
            "到哪一個為止", "", "例如：10 動植物油脂"
        )
        text_row.addWidget(self.query_end_text_entry)
        body_layout.addLayout(text_row)

        # 有些網站的選單要再點好幾層才看得到廠商，打關鍵字反而一步到位。
        # 填了這一格就改走關鍵字，上面的「先查前幾個」就不算數了。
        self.query_values_entry = LabeledEntry(
            "如果是打關鍵字查詢的，填要查哪些字（用、分隔）", "",
            "例如：貿易、科技、實業",
        )
        body_layout.addWidget(self.query_values_entry)

        # 沒勾之前整組藏起來。停用的欄位還是看得到，使用者會停下來問「這是什麼、
        # 給誰看的」——那正是它不該在那裡的證據。
        self._query_inputs = (
            self.query_by_number,
            self.query_by_text,
            self.query_start_entry,
            self.query_end_entry,
            self.query_count_entry,
            self.query_start_text_entry,
            self.query_end_text_entry,
            self.query_values_entry,
        )
        self._sync_query_range_mode()
        self._sync_query_loop_enabled(False)

    def _sync_query_range_mode(self, _checked: bool = False) -> None:
        """序號與文字擇一。選了哪一種，另一種就反灰。

        兩邊都填得下去的話，「到底哪一個算數」會變成一個沒有人答得出來的
        問題，而答錯的代價是對別人的網站跑掉一整個下午。
        """
        by_number = self.query_by_number.isChecked()
        for widget in (
            self.query_start_entry, self.query_end_entry, self.query_count_entry
        ):
            widget.setEnabled(by_number)
        for widget in (self.query_start_text_entry, self.query_end_text_entry):
            widget.setEnabled(not by_number)

    def _sync_query_loop_enabled(self, checked: bool) -> None:
        on = bool(checked) and self.query_loop_check.isEnabled()
        for widget in self._query_inputs:
            widget.setVisible(on)

        # 逐項查詢的來源沒有「頁」可以翻——它的一趟就是查一次，而要查幾次是
        # 上面那一格說了算。兩個都在講「總共跑幾趟」的欄位同時擺在畫面上，
        # 使用者只會填其中一個，另一個維持預設值然後默默地贏——實際發生過：
        # 選單那格填了 97，「最多爬幾頁」還停在 3，結果查 3 個就結束。
        for widget in (self.page_start_entry, self.page_end_entry, self.max_pages_entry):
            widget.setVisible(not on)

    def _apply_query_form(self, form: dict | None) -> None:
        """依照分析結果決定「逐項查詢」勾不勾得動，並講出偵測到幾個條件。"""
        self._query_form = dict(form or {})
        available = bool(self._query_form)
        self.query_loop_check.setEnabled(available)
        if not available:
            self.query_loop_check.setChecked(False)
            self._sync_query_loop_enabled(False)
            self.query_loop_note.setText(
                "這一頁沒有「要先選條件或打關鍵字才有資料」的查詢框。"
            )
            return

        count = int(self._query_form.get("option_count", 0) or 0)
        sample = "、".join(str(s) for s in (self._query_form.get("sample") or [])[:3])
        # 分析實際驗證過走得通的是哪一條路。這一頁上有下拉選單，不代表那條路
        # 查得出廠商——ieatpe 的選單查出來是商品分類，要再點一層才是廠商，而
        # 那一層點不通的時候只剩關鍵字這條路。
        route = str(self._query_form.get("verified_route") or "")
        drilled = bool(self._query_form.get("drill"))

        if count and route != "text":
            text = (
                f"這一頁有一個 {count} 個選項的查詢選單"
                + (f"（例如：{sample}）" if sample else "")
                + f"。勾起來就會一個選項查一次，總共可以查 {count} 個。"
            )
            if drilled:
                text += "這個網站查出來還要再點一層才是廠商，爬取時會自動做，會慢很多。"
            if self._query_form.get("text_input_selector"):
                text += "這一頁也可以打關鍵字查，勾起來之後填字就改走那一條。"
        elif count and route == "text":
            # 選單在，但驗證過查不出廠商。照實說，不要給一個按了會白跑的選項。
            text = (
                f"這一頁雖然有一個 {count} 個選項的選單，但實際試查的結果不是廠商"
                "（中間那一層也點不出廠商），所以那條路不能用。"
                "只有打關鍵字這條走得通——勾起來之後填要查哪些字，一個字查一次。"
            )
        else:
            text = "這一頁是打關鍵字查詢的。勾起來之後填要查哪些字，一個字查一次。"
        self.query_loop_note.setText(text)

    def _selected_query_loop(self) -> dict | None:
        """使用者勾了「逐項查詢」時，要存進來源的設定。"""
        if not (self.query_loop_check.isChecked() and self._query_form):
            return None

        values = [
            part.strip()
            for part in re.split(r"[、,，;；\s]+", self.query_values_entry.get())
            if part.strip()
        ]

        # 中間還要再點一層的話，分析已經確認過點下去真的會出現廠商，那是這個
        # 網站的性質，不是使用者的選擇——跟著走，不另外問。
        drill = self._query_form.get("drill") or None

        if values:
            # 自己指定了要查的字，就用打字的那一個查詢框（如果有的話）。
            # 把關鍵字塞進下拉選單是沒有意義的——選單只吃它自己的選項。
            input_selector = str(
                self._query_form.get("text_input_selector")
                or self._query_form.get("input_selector")
                or ""
            )
            submit_selector = str(
                self._query_form.get("text_submit_selector")
                or self._query_form.get("submit_selector")
                or ""
            )
            return {
                "input_selector": input_selector,
                "submit_selector": submit_selector,
                "values": values,
                "max_queries": len(values),
                "drill": drill,
            }

        detected = int(self._query_form.get("option_count", 0) or 0)
        if not detected:
            return None            # 關鍵字查詢框但沒填字，沒有東西可以查
        if str(self._query_form.get("verified_route") or "") == "text":
            # 選單在，但分析實際試過查不出廠商。沒填關鍵字就寧可什麼都不存，
            # 也不要存一條已知會抓回一堆分類代號的設定。
            return None

        def _int(text: str, fallback: int) -> int:
            try:
                return int(text.strip())
            except ValueError:
                return fallback

        if self.query_by_number.isChecked():
            start_at = max(1, min(_int(self.query_start_entry.get(), 1), detected))
            start_value = end_value = ""
            # 填了終點就以終點為準（「從第 7 個爬到第 9 個」是使用者真正的說法），
            # 沒填才看「往後查幾個」那一格。
            end_text = self.query_end_entry.get().strip()
            if end_text:
                end_at = max(start_at, min(_int(end_text, detected), detected))
                max_queries = end_at - start_at + 1
            else:
                max_queries = _int(self.query_count_entry.get(), 3)
            max_queries = max(1, min(max_queries, detected - start_at + 1))
        else:
            # 文字要等真的把頁面打開、讀到選單才對得起來，所以原樣存下去，
            # 由 crawler/fetcher.py 在查詢當下比對。對不到會退回序號並在
            # 日誌裡說明，不會安靜地爬成別的範圍。
            start_at = 1
            start_value = self.query_start_text_entry.get().strip()
            end_value = self.query_end_text_entry.get().strip()
            max_queries = detected if end_value else _int(self.query_count_entry.get(), 3)
            max_queries = max(1, min(max_queries, detected))

        return {
            "input_selector": str(self._query_form.get("input_selector") or ""),
            "submit_selector": str(self._query_form.get("submit_selector") or ""),
            "start_at": start_at,
            "start_value": start_value,
            "end_value": end_value,
            "max_queries": max_queries,
            "drill": drill,
        }

    def _apply_document_links(self, found: dict) -> None:
        """依照分析結果決定哪些格式勾得動，並把找到的數量講出來。

        沒找到的格式維持停用。勾了不會發生任何事的選項比沒有那個選項更糟——
        使用者會以為自己已經打開了，然後納悶名冊怎麼沒抓進來。
        """
        for key, check in self.document_checks.items():
            available = key in found
            check.setEnabled(available)
            if not available:
                check.setChecked(False)

        if not found:
            self.document_note.setText(
                "這一頁沒有連出去的 PDF／Excel／Word／PowerPoint 檔。"
            )
            return

        listed = "、".join(
            f"{count} 個 {DOCUMENT_LABELS.get(key, key)}" for key, count in found.items()
        )
        self.document_note.setText(f"這一頁連出去 {listed}，要一起讀就勾起來。")

    def _selected_document_kinds(self) -> list[str]:
        return [key for key, check in self.document_checks.items() if check.isChecked()]

    def _selected_page_actions(self) -> list[dict]:
        """使用者勾了「先點開按鈕」時，分析階段偵測到的那些動作。"""
        if not self.click_check.isChecked():
            return []
        return list(self._suggested_actions)

    def _set_collect_enabled(self, enabled: bool) -> None:
        for widget in self._collect_widgets:
            widget.setEnabled(enabled)

    def _build_preview_section(self, parent_layout: QVBoxLayout) -> None:
        section = Section("3. 預覽抓到的資料")

        header = QHBoxLayout()
        self.preview_button = QPushButton("重新預覽")
        self.preview_button.clicked.connect(self._start_preview)
        header.addWidget(self.preview_button)
        header.addStretch(1)
        section.body_layout.addLayout(header)

        self.preview_table = DataTable(columns=PREVIEW_COLUMNS)
        section.body_layout.addWidget(self.preview_table)

        self.preview_section = section
        parent_layout.addWidget(section)

    def _build_bottom_section(self, parent_layout: QVBoxLayout) -> None:
        section = Section("4. 儲存")

        row = QHBoxLayout()
        self.name_entry = LabeledEntry("來源名稱")
        row.addWidget(self.name_entry, 1)

        # 「最多爬幾頁」搬到第 2 步跟其他收集設定放在一起了——同一個概念散在
        # 兩個地方，使用者會不確定哪個才算數。

        # 靠下對齊：這一列前面的是「說明文字在上、輸入框在下」的直向堆疊，
        # 按鈕預設會對齊整個堆疊的垂直中心，看起來浮在說明文字那一排。
        bottom = Qt.AlignmentFlag.AlignBottom

        self.save_button = QPushButton("儲存來源")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(lambda: self._save(also_crawl=False))
        row.addWidget(self.save_button, 0, bottom)

        self.save_crawl_button = QPushButton("儲存並立即爬取")
        self.save_crawl_button.setEnabled(False)
        self.save_crawl_button.clicked.connect(lambda: self._save(also_crawl=True))
        row.addWidget(self.save_crawl_button, 0, bottom)

        close_button = QPushButton("關閉")
        close_button.clicked.connect(self.reject)
        row.addWidget(close_button, 0, bottom)
        row.addStretch(1)
        section.body_layout.addLayout(row)

        self.save_hint_label = QLabel("請先分析網址。")
        self.save_hint_label.setStyleSheet(f"color: {theme.pick(theme.DANGER)};")
        section.body_layout.addWidget(self.save_hint_label)

        parent_layout.addWidget(section)

    def _set_all_collect(self, checked: bool) -> None:
        for check in self.collect_checks.values():
            check.setChecked(checked)

    def _selected_collect_fields(self) -> list[str]:
        """勾選的欄位；全部勾選時回空清單，代表「不做任何過濾」。"""
        chosen = [f for f, check in self.collect_checks.items() if check.isChecked()]
        return [] if len(chosen) == len(self.collect_checks) else chosen

    def _build_advanced_section(self, parent_layout: QVBoxLayout) -> None:
        """技術性的 CSS 選擇器控制項，全部收在這裡，預設收合。

        "清單選擇器" "下一頁連結選擇器" "取值方式" 這些詞，對只想貼一個網址
        的使用者沒有意義；只有自動偵測結果看起來不對時
        （:meth:`_on_analyse_done` 最後會判斷），才自動展開提醒使用者
        可能需要手動指定。
        """
        section = CollapsibleSection(
            "進階設定（自動偵測正確的話不用動）",
            hint="只有在抓到的資料不對時才需要打開。這裡設定程式要從網頁的哪個位置取值。",
        )
        self.advanced_section = section
        body_layout = section.body_layout

        selector_row = QHBoxLayout()
        self.list_selector_entry = LabeledEntry("每一筆資料的位置（CSS 選擇器）")
        self.list_selector_entry.entry.textChanged.connect(
            lambda _text: self._update_save_state()
        )
        selector_row.addWidget(self.list_selector_entry)

        self.next_selector_entry = LabeledEntry("「下一頁」連結的位置（留空＝只爬這一頁）")
        selector_row.addWidget(self.next_selector_entry)
        body_layout.addLayout(selector_row)

        detail_row = QHBoxLayout()
        self.detail_link_entry = LabeledEntry("每家公司「詳細頁」連結的位置（留空＝不進入）")
        detail_row.addWidget(self.detail_link_entry)

        # 這個上限就是為什麼一個 202 家的名錄可能只回來 98 個電話：一旦達到
        # 上限，爬取就不會再進入詳細頁。一定要讓使用者看得到，否則資料被
        # 截斷看起來會像是程式壞掉。
        self.max_details_entry = LabeledEntry("最多進入幾個詳細頁", value="100")
        detail_row.addWidget(self.max_details_entry)
        body_layout.addLayout(detail_row)

        detail_note = QLabel(
            "很多名錄的清單頁只有公司名稱，信箱與電話要點進去才看得到。"
            "「最多進入幾個詳細頁」設得比公司總數小的話，後面的公司就不會有這些資料。"
        )
        detail_note.setWordWrap(True)
        detail_note.setStyleSheet(f"color: {theme.pick(theme.MUTED)};")
        body_layout.addWidget(detail_note)

        fields_title = QLabel("偵測到的欄位")
        title_font = fields_title.font()
        title_font.setBold(True)
        title_font.setPointSize(13)
        fields_title.setFont(title_font)
        body_layout.addWidget(fields_title)

        self.fields_table = DataTable(columns=FIELD_COLUMNS, on_select=self._on_field_selected)
        body_layout.addWidget(self.fields_table)

        self.editing_label = QLabel("尚未選取欄位")
        self.editing_label.setStyleSheet(f"color: {theme.pick(theme.MUTED)};")
        body_layout.addWidget(self.editing_label)

        edit_row = QHBoxLayout()
        self.selector_entry = LabeledEntry("取值位置（CSS 選擇器）")
        edit_row.addWidget(self.selector_entry, 1)

        self.attr_entry = LabeledEntry("要取什麼（text＝文字、href＝連結）")
        edit_row.addWidget(self.attr_entry, 1)

        # 靠下對齊。旁邊兩個是 LabeledEntry（說明在上、輸入框在下，兩行高），
        # 按鈕預設會對齊整個堆疊的垂直中心，看起來就浮在說明文字那一排。
        apply_button = QPushButton("套用")
        apply_button.clicked.connect(self._apply_edit)
        edit_row.addWidget(apply_button, 0, Qt.AlignmentFlag.AlignBottom)
        body_layout.addLayout(edit_row)

        new_field_row = QHBoxLayout()
        new_field_caption = inline_caption("新增欄位：")
        new_field_row.addWidget(new_field_caption, 0, Qt.AlignmentFlag.AlignBottom)

        self.new_field_combo = WideComboBox()
        self.new_field_combo.addItems([field_label(code) for code in KNOWN_FIELDS])
        new_field_row.addWidget(self.new_field_combo)

        add_button = QPushButton("新增欄位")
        add_button.clicked.connect(self._add_field)
        new_field_row.addWidget(add_button)

        delete_button = QPushButton("刪除欄位")
        delete_button.setStyleSheet(
            f"background-color: {theme.pick(theme.DANGER)}; color: white;"
        )
        delete_button.clicked.connect(self._delete_field)
        new_field_row.addWidget(delete_button)
        new_field_row.addStretch(1)
        body_layout.addLayout(new_field_row)

        parent_layout.addWidget(section)

    # ------------------------------------------------------------ analysis

    def _start_analyse(self) -> None:
        if self.analyse_task is not None and self.analyse_task.running:
            return
        url = self.url_entry.text().strip()
        if not url:
            QMessageBox.critical(self, "自訂網址精靈", "請先貼上要分析的網址。")
            return

        self._set_url_locked(True)
        self.analyse_button.setEnabled(False)
        self.analyse_progress.setRange(0, len(DISCOVERY_STEPS))
        self.analyse_progress.setValue(0)
        self.analyse_progress.setFormat("%v / %m")
        self.analyse_progress.show()
        self.analyse_step_label.setText("準備中…")
        self.analyse_step_label.show()
        self.summary_label.setText(
            "分析中，請稍候…此步驟會實際連線到目標網站。\n"
            "如果這個網站的資料是網頁開起來之後才產生的，程式會自動再開一次"
            "內建瀏覽器重看，那時候會多花半分鐘左右。"
        )
        self.notes_label.setText("")

        self.analyse_task = BackgroundTask(
            self,
            worker=self.controller.analyse,
            on_progress=self._on_analyse_step,
            on_done=self._on_analyse_done,
            on_error=self._on_analyse_error,
        )
        self.analyse_task.start(url)

    # ------------------------------------------------------ 站內尋找名錄

    def _start_explore(self) -> None:
        """在整個網站裡找名錄頁。"""
        from crawler.explore import DEFAULT_PAGE_BUDGET

        if self.explore_task is not None and self.explore_task.running:
            return
        url = self.url_entry.text().strip()
        if not url:
            QMessageBox.critical(
                self, "自訂網址精靈", "請先貼上這個網站的任一個網址（首頁就可以）。"
            )
            return

        budget = DEFAULT_PAGE_BUDGET
        # 先講清楚會做什麼、要等多久再開始。這個動作會對別人的網站送出
        # 幾十次請求，不該按下去才發現。
        seconds = int(budget * (self.controller.crawl_delay() + 0.5))
        reply = QMessageBox.question(
            self,
            "在整個網站裡找名錄",
            f"程式會從這個網址出發，在同一個網站裡最多讀 {budget} 頁，"
            f"找出看起來像廠商名錄的頁面。\n\n"
            f"預估需要 {seconds // 60} 分 {seconds % 60} 秒"
            "（每次請求之間有禮貌延遲，這是對方網站的規矩）。\n\n"
            "要開始嗎？",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._set_url_locked(True)
        self.explore_button.setEnabled(False)
        self.analyse_button.setEnabled(False)
        self.analyse_progress.setRange(0, budget)
        self.analyse_progress.setValue(0)
        self.analyse_progress.show()
        self.summary_label.setText("正在站內尋找名錄…")
        self.notes_label.setText("")

        self.explore_task = BackgroundTask(
            self,
            worker=self.controller.explore,
            on_done=self._on_explore_done,
            on_error=self._on_explore_error,
            on_progress=self._on_explore_progress,
        )
        self.explore_task.start(url, document_kinds=self._selected_document_kinds())

    def _on_explore_progress(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("stage") == "exploring":
            self.analyse_progress.setValue(int(payload.get("done", 0)))
            self.summary_label.setText(
                f"正在站內尋找名錄…已讀 {payload.get('done', 0)}／"
                f"{payload.get('total', 0)} 頁"
            )

    def _stop_explore_progress(self) -> None:
        self.analyse_progress.hide()
        self.analyse_progress.setRange(0, 0)   # 還原成分析用的不定進度
        self._set_url_locked(False)
        self.explore_button.setEnabled(True)
        self.analyse_button.setEnabled(True)

    def _on_explore_done(self, result: Any) -> None:
        self.explore_task = None
        self._stop_explore_progress()

        if not result.candidates:
            self.summary_label.setText("這個網站裡沒有找到看起來像廠商名錄的頁面。")
            self.notes_label.setText("\n".join(result.notes))
            return

        from gui_qt.explore_dialog import ExploreResultsDialog

        dialog = ExploreResultsDialog(self, result)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.chosen_urls:
            self.summary_label.setText(
                f"找到 {len(result.candidates)} 個名錄頁，尚未選擇。"
            )
            return

        if len(dialog.chosen_urls) > 1:
            self._start_batch_add(dialog.chosen_urls)
            return

        # 只挑一個時走原本的分析流程——後面的每一步都不必知道網址是怎麼來的。
        self.url_entry.setText(dialog.chosen_urls[0])
        self._start_analyse()

    # ------------------------------------------------------ 一次加入好幾個

    def _start_batch_add(self, urls: list[str]) -> None:
        """把勾起來的名錄頁一次全部加成來源。

        一個一個手動微調是對的做法，但十份名錄要重跑十遍精靈就不是了。
        這裡用偵測出來的設定直接存，之後仍然可以在來源清單裡個別修改。
        """
        seconds = int(len(urls) * (self.controller.crawl_delay() + 1.0))
        reply = QMessageBox.question(
            self,
            "一次加入這幾個名錄",
            f"程式會分析這 {len(urls)} 個頁面，並用偵測到的設定各存成一個來源。\n\n"
            f"預估需要 {seconds // 60} 分 {seconds % 60} 秒。\n\n"
            "抓不到公司名稱的會跳過，不會存進來。要開始嗎？",
        )
        if reply != QMessageBox.StandardButton.Yes:
            self.summary_label.setText(f"找到 {len(urls)} 個名錄頁，尚未加入。")
            return

        self._set_url_locked(True)
        self.explore_button.setEnabled(False)
        self.analyse_button.setEnabled(False)
        self.analyse_progress.setRange(0, len(urls))
        self.analyse_progress.setValue(0)
        self.analyse_progress.show()
        self.summary_label.setText("正在逐一分析並加入…")
        self.notes_label.setText("")

        self.batch_task = BackgroundTask(
            self,
            worker=self.controller.add_many,
            on_done=self._on_batch_done,
            on_error=self._on_batch_error,
            on_progress=self._on_batch_progress,
        )
        self.batch_task.start(urls)

    def _on_batch_progress(self, payload: Any) -> None:
        if not isinstance(payload, dict) or payload.get("stage") != "adding":
            return
        done = int(payload.get("done", 0))
        total = int(payload.get("total", 0))
        self.analyse_progress.setValue(done)
        self.summary_label.setText(f"正在逐一分析並加入…{done}／{total}")

    def _on_batch_done(self, outcome: Any) -> None:
        self.batch_task = None
        self._stop_explore_progress()

        added = list(outcome.get("added", []))
        skipped = list(outcome.get("skipped", []))

        for name in added:
            if self.on_saved:
                self.on_saved(name)

        lines = [f"已加入 {len(added)} 個來源。"]
        if added:
            lines.append("　" + "、".join(added))
        if skipped:
            lines.append(f"跳過 {len(skipped)} 個：")
            lines.extend(f"　{url} －{reason}" for url, reason in skipped)

        QMessageBox.information(self, "一次加入這幾個名錄", "\n".join(lines))
        if added:
            self.accept()
        else:
            self.summary_label.setText("沒有任何一個頁面加得進來。")
            self.notes_label.setText("\n".join(lines[1:]))

    def _on_batch_error(self, exc: Exception) -> None:
        self.batch_task = None
        self._stop_explore_progress()
        self.summary_label.setText("加入失敗。")
        QMessageBox.critical(self, "一次加入這幾個名錄", _friendly_error(exc))

    def _on_explore_error(self, exc: Exception) -> None:
        self.explore_task = None
        self._stop_explore_progress()
        self._on_analyse_error(exc)

    def _set_url_locked(self, locked: bool) -> None:
        """分析或站內尋找進行中時，整張表單都不給動。

        不只是網址。下面每一格的內容都是「上一次分析的結果」——分析跑完會把
        它們整批覆寫掉，所以在跑的時候改任何一格，改的東西幾秒後就消失了，
        而使用者不會知道自己剛剛做的事被丟掉。反灰是唯一講得清楚的方式。

        「儲存」在這段時間也一定要停用：那一份設定是新舊混在一起的，存下去
        會得到一個看起來正常、實際上爬不到東西的來源。
        """
        self.url_entry.setReadOnly(locked)
        self.url_entry.setEnabled(not locked)
        for section in (
            getattr(self, "summary_section", None),
            getattr(self, "preview_section", None),
            getattr(self, "advanced_section", None),
        ):
            if section is not None:
                section.setEnabled(not locked)
        if locked:
            for name in ("save_button", "save_crawl_button"):
                button = getattr(self, name, None)
                if button is not None:
                    button.setEnabled(False)
        else:
            # 解鎖時由「有沒有抓到公司名稱」決定儲存能不能按，不是無條件開啟。
            self._update_save_state()

    def _on_analyse_step(self, payload: Any) -> None:
        """分析跑到哪一步了。

        分析要一兩分鐘，而中間有一半的時間是在等別人的網站回應。沒有這一行
        字的話，使用者只看得到一條橫槓，分不出「還在跑」跟「卡住了」。
        """
        if not isinstance(payload, dict) or payload.get("stage") != "step":
            return
        step = int(payload.get("step") or 0)
        total = int(payload.get("total") or len(DISCOVERY_STEPS))
        self.analyse_progress.setRange(0, total)
        self.analyse_progress.setValue(step)
        self.analyse_step_label.setText(
            f"第 {step} / {total} 步：{payload.get('label', '')}"
        )

    def _stop_analyse_progress(self) -> None:
        self.analyse_progress.hide()
        self.analyse_step_label.hide()
        self._set_url_locked(False)
        self.analyse_button.setEnabled(True)
        self.explore_button.setEnabled(True)

    def _on_analyse_done(self, result: Any) -> None:
        self.analyse_task = None
        self._stop_analyse_progress()

        self.last_url = result.url
        self._set_collect_enabled(True)

        # 「不點就看不到」的按鈕：偵測到才讓使用者勾得動，沒偵測到就維持停用
        # ——給一個按了不會有任何作用的勾選框，比不給更糟。
        self._apply_document_links(dict(getattr(result, "document_links", {}) or {}))
        # 分析時發現「要開瀏覽器才看得到」的話，這個來源存下去也要記住這件事，
        # 否則存完第一次爬取又會是空的。
        self._detected_engine = getattr(result, "engine", None)
        self._apply_query_form(getattr(result, "query_form", None))
        self._suggested_actions = list(getattr(result, "suggested_actions", []))
        self.click_check.setEnabled(bool(self._suggested_actions))
        if not self._suggested_actions:
            self.click_check.setChecked(False)
        self.list_selector_entry.set(result.list_selector)
        self.next_selector_entry.set(result.next_selector or "")
        self.detail_link_entry.set(result.detail_link_selector or "")
        if result.detail_link_selector and result.item_count:
            # 上限預設抓這一頁實際找到的筆數，超過 100 家的名錄才不會被
            # 悄悄截斷。
            self.max_details_entry.set(str(max(100, result.item_count)))

        # 頁數上限比照辦理。這個欄位原本固定預設 3，而使用者沒有辦法知道
        # 該調到多少——一個 24 頁的名錄就這樣只被爬了 3 頁，看起來像是程式
        # 不會自動翻頁。偵測得出總頁數時就直接填進去。
        if result.page_count > 1:
            self.max_pages_entry.set(str(result.page_count))
            self.max_details_entry.set(
                str(max(100, result.page_count * result.item_count))
            )

        self.field_rules = {
            code: {
                "selector": guess.selector,
                "attr": guess.attr,
                "regex": guess.regex,
                "hit_rate": guess.hit_rate,
                "samples": list(guess.samples),
            }
            for code, guess in result.fields.items()
        }
        self.preview_rows = [record.model_dump() for record in result.preview]
        self._editing_field = None
        self.selector_entry.set("")
        self.attr_entry.set("")
        self.editing_label.setText("尚未選取欄位")

        # 白話文優先：貼網址的人想知道「有沒有成功、接下來要做什麼」，
        # 不是「選到了哪個 CSS 選擇器」——選擇器仍然只在「進階設定」一鍵之遙。
        field_names = "、".join(field_label(code) for code in self.field_rules) or "（無）"
        pages = (
            "有「下一頁」，可以連續抓好幾頁"
            if result.next_selector
            else "沒有找到「下一頁」，只會抓這一頁"
        )
        if result.item_count:
            headline = f"✓ 在這一頁找到 {result.item_count} 筆資料"
        else:
            headline = "✗ 這一頁找不到成列的資料，可能不是清單頁"
        self.summary_label.setText(
            f"{headline}\n"
            f"抓得到的欄位：{field_names}\n"
            f"分頁：{pages}\n"
            f"下方可以先看看抓到的內容對不對，沒問題就按「儲存並立即爬取」。"
        )
        if result.notes:
            self.notes_label.setText("\n".join(f"• {note}" for note in result.notes))
            colour = theme.DANGER if not result.ok else theme.MUTED
            self.notes_label.setStyleSheet(f"color: {theme.pick(colour)};")
        else:
            self.notes_label.setText("沒有其他提醒。")
            self.notes_label.setStyleSheet(f"color: {theme.pick(theme.MUTED)};")

        if not self.name_entry.get():
            self.name_entry.set(self._default_name(result.url))

        self._refresh_fields_table()
        self._refresh_preview_table()

        # 只有偵測沒抓到公司名稱、或整頁都找不到清單時，才展開技術性控制項。
        if not result.item_count or "company_name" not in self.field_rules:
            self.advanced_section.set_expanded(True)

    def _on_analyse_error(self, exc: Exception) -> None:
        self.analyse_task = None
        self._stop_analyse_progress()
        self.summary_label.setText("分析失敗，尚未取得任何結果。")
        QMessageBox.critical(self, "分析失敗", _friendly_error(exc))

    # --------------------------------------------------------- field table

    # ------------------------------------------------------ 編輯已存的來源

    def load_source(self, entry: dict[str, Any]) -> None:
        """把一個已存來源填回每一格，讓使用者直接改。

        不重新分析。分析是「去問那個網站長什麼樣」，而使用者要改的是自己當初
        的決定（要爬哪一段、要收集哪些欄位、選擇器指到哪）——那些東西存在
        ``custom_sources.yaml`` 裡，不必為了看它們再去打擾別人的網站一次。
        改完想重驗的話，「分析網頁」那顆按鈕仍然在。
        """
        self._editing_source = str(entry.get("name") or "") or None
        self.setWindowTitle(f"編輯來源：{self._editing_source or ''}")

        url = str(entry.get("start_url") or "")
        self.url_entry.setText(url)
        self.last_url = url
        self.name_entry.set(str(entry.get("name") or ""))
        self._detected_engine = entry.get("engine") or None

        self.list_selector_entry.set(str(entry.get("list_selector") or ""))
        pagination = entry.get("pagination") or {}
        self.next_selector_entry.set(str(pagination.get("next_selector") or ""))

        self.page_start_entry.set(str(entry.get("page_start") or 1))
        self.page_end_entry.set(
            "" if entry.get("page_end") is None else str(entry.get("page_end"))
        )
        self.max_pages_entry.set(
            "" if entry.get("max_pages") is None else str(entry.get("max_pages"))
        )
        self.default_industry_entry.set(str(entry.get("default_industry") or ""))

        detail_link = entry.get("detail_link") or {}
        self.detail_link_entry.set(str(detail_link.get("selector") or ""))
        self.max_details_entry.set(str(entry.get("max_details") or ""))

        # 空的 collect_fields 代表「全部收集」，不是「一個都不收集」。
        collect = entry.get("collect_fields") or []
        for field, check in self.collect_checks.items():
            check.setChecked(not collect or field in collect)
        self._set_collect_enabled(True)

        kinds = entry.get("document_kinds") or []
        for kind, check in self.document_checks.items():
            check.setEnabled(True)
            check.setChecked(kind in kinds)

        actions = entry.get("page_actions") or []
        self._suggested_actions = list(actions)
        self.click_check.setEnabled(bool(actions))
        self.click_check.setChecked(bool(actions))

        self._load_query_loop(entry.get("query_loop") or None)

        self.field_rules = {
            code: dict(rule) for code, rule in (entry.get("fields") or {}).items()
        }
        self._refresh_fields_table()
        self._refresh_preview_table()
        self._update_save_state()

        self.summary_label.setText(
            f"正在編輯已儲存的來源「{self._editing_source}」。"
            "下面每一格都是當初存下來的設定，改完按「儲存來源」就會覆蓋它。"
        )
        self.notes_label.setText(
            "沒有重新連線到那個網站——要重新偵測的話按上面的「分析網頁」，"
            "但那會把下面所有的欄位覆寫成新的偵測結果。"
        )
        # 選擇器是使用者最可能來改的東西，直接打開，不要讓他去找。
        self.advanced_section.set_expanded(True)

    def _load_query_loop(self, loop: dict[str, Any] | None) -> None:
        """回填「逐項查詢」那一區。

        ``_query_form`` 平常是分析填的，這裡沒有分析可用，所以從存下來的設定
        反推一份出來——不然勾選框會是停用的，使用者看得到自己存的設定卻改不動。
        ``option_count`` 用 max_queries 當下限，真正有幾個選項要開頁面才知道。
        """
        if not loop:
            self._apply_query_form(None)
            return

        self._query_form = {
            "input_selector": loop.get("input_selector") or "",
            "submit_selector": loop.get("submit_selector") or "",
            "option_count": max(1, int(loop.get("max_queries") or 1)),
            "drill": loop.get("drill") or None,
            "verified_route": "select" if not loop.get("values") else "text",
        }
        self.query_loop_check.setEnabled(True)
        self.query_loop_check.setChecked(True)
        self._sync_query_loop_enabled(True)

        self.query_values_entry.set("、".join(loop.get("values") or []))
        start_value = str(loop.get("start_value") or "")
        end_value = str(loop.get("end_value") or "")
        if start_value or end_value:
            self.query_by_text.setChecked(True)
            self.query_start_text_entry.set(start_value)
            self.query_end_text_entry.set(end_value)
        else:
            self.query_by_number.setChecked(True)
            self.query_start_entry.set(str(loop.get("start_at") or 1))
            self.query_count_entry.set(str(loop.get("max_queries") or 3))
        self._sync_query_range_mode()

    def _refresh_fields_table(self) -> None:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for code in KNOWN_FIELDS:
            if code in self.field_rules:
                rows.append(self._field_row(code))
                seen.add(code)
        for code in self.field_rules:
            if code not in seen:
                rows.append(self._field_row(code))
        self.fields_table.set_rows(rows)
        self._update_save_state()

    def _field_row(self, code: str) -> dict[str, Any]:
        info = self.field_rules[code]
        hit_rate = info.get("hit_rate")
        samples = info.get("samples") or []
        return {
            "field": field_label(code),
            "selector": info.get("selector", ""),
            "attr": info.get("attr", "text"),
            "hit_rate": f"{hit_rate:.0%}" if isinstance(hit_rate, (int, float)) else "-",
            "samples": ", ".join(str(sample) for sample in samples[:3]),
            "field_code": code,
        }

    def _on_field_selected(self, row: dict[str, Any]) -> None:
        code = row.get("field_code")
        if not code:
            return
        self._editing_field = code
        self.selector_entry.set(row.get("selector", ""))
        self.attr_entry.set(row.get("attr", "text"))
        self.editing_label.setText(f"目前編輯欄位：{row.get('field', code)}")

    def _apply_edit(self) -> None:
        if not self._editing_field:
            QMessageBox.information(
                self, "自訂網址精靈", "請先在上方表格選取一列欄位，或先用下方「新增欄位」。"
            )
            return
        selector = self.selector_entry.get()
        if not selector:
            QMessageBox.critical(self, "自訂網址精靈", "CSS 選擇器不能是空的。")
            return
        info = self.field_rules.setdefault(self._editing_field, {})
        info["selector"] = selector
        info["attr"] = self.attr_entry.get() or "text"
        self._refresh_fields_table()

    def _add_field(self) -> None:
        code = _FIELD_CODE_BY_LABEL.get(self.new_field_combo.currentText())
        if code is None:
            return
        if code not in self.field_rules:
            self.field_rules[code] = {
                "selector": "",
                "attr": "text",
                "regex": None,
                "hit_rate": None,
                "samples": [],
            }
        self._editing_field = code
        self.selector_entry.set(self.field_rules[code].get("selector", ""))
        self.attr_entry.set(self.field_rules[code].get("attr", "text"))
        self.editing_label.setText(f"目前編輯欄位：{field_label(code)}")
        self._refresh_fields_table()

    def _delete_field(self) -> None:
        code = self._editing_field
        if code is None:
            row = self.fields_table.selected_row()
            code = row.get("field_code") if row else None
        if code is None:
            QMessageBox.information(self, "自訂網址精靈", "請先在表格中選取要刪除的欄位。")
            return
        self.field_rules.pop(code, None)
        self._editing_field = None
        self.selector_entry.set("")
        self.attr_entry.set("")
        self.editing_label.setText("尚未選取欄位")
        self._refresh_fields_table()

    # -------------------------------------------------------------- preview

    def _plain_rules(self) -> dict[str, dict[str, Any]]:
        """去掉顯示用欄位、準備交給 controller 的欄位規則。"""
        return {
            code: {
                "selector": info.get("selector", ""),
                "attr": info.get("attr", "text"),
                "regex": info.get("regex"),
            }
            for code, info in self.field_rules.items()
            if (info.get("selector") or "").strip()
        }

    def _refresh_preview_table(self) -> None:
        self.preview_table.set_rows(
            [
                {key: (row.get(key) or "") for key in PREVIEW_FIELDS}
                for row in self.preview_rows
            ]
        )

    def _recompute_hit_rates(self) -> None:
        total = len(self.preview_rows)
        if not total:
            return
        for code, info in self.field_rules.items():
            values = [
                str(row[code]).strip()
                for row in self.preview_rows
                if row.get(code) and str(row[code]).strip()
            ]
            info["hit_rate"] = len(values) / total
            info["samples"] = values[:3]

    def _start_preview(self) -> None:
        if self.preview_task is not None and self.preview_task.running:
            return
        url = self.last_url or self.url_entry.text().strip()
        list_selector = self.list_selector_entry.get()
        if not url or not list_selector:
            QMessageBox.critical(self, "自訂網址精靈", "請先分析網址，並確認清單選擇器不是空的。")
            return

        def worker(
            url_: str,
            selector_: str,
            rules_: dict[str, dict[str, Any]],
            *,
            report: Callable[[Any], None],
            cancel_event,
        ) -> list[dict[str, Any]]:
            return self.controller.preview_with(url_, selector_, rules_)

        self.preview_button.setEnabled(False)
        self.preview_task = BackgroundTask(
            self, worker=worker, on_done=self._on_preview_done, on_error=self._on_preview_error
        )
        self.preview_task.start(url, list_selector, self._plain_rules())

    def _on_preview_done(self, rows: list[dict[str, Any]]) -> None:
        self.preview_task = None
        self.preview_button.setEnabled(True)
        self.preview_rows = rows
        self._refresh_preview_table()
        self._recompute_hit_rates()
        self._refresh_fields_table()

    def _on_preview_error(self, exc: Exception) -> None:
        self.preview_task = None
        self.preview_button.setEnabled(True)
        QMessageBox.critical(self, "重新預覽失敗", _friendly_error(exc))

    # -------------------------------------------------------- save & crawl

    def _default_name(self, url: str | None = None) -> str:
        host = urlsplit(url or self.last_url or self.url_entry.text()).netloc.lower()
        return host.removeprefix("www.") or "custom_source"

    def _update_save_state(self) -> None:
        has_name = bool((self.field_rules.get("company_name") or {}).get("selector"))
        has_list = bool(self.list_selector_entry.get().strip())
        ok = has_name and has_list
        self.save_button.setEnabled(ok)
        self.save_crawl_button.setEnabled(ok)

        if ok:
            # 分析與勾選的先後關係要講清楚。使用者會停在這裡問「我勾完之後
            # 是不是要再按一次分析網頁？」——不用，而且再按一次會把勾選連同
            # 分析結果一起重來。這句話就是為了不讓人白跑一次分析。
            self.save_hint_label.setText(
                "上面的勾選（要收集哪些欄位、要不要逐項查詢、要不要讀檔案）"
                "會在按「儲存」的當下一起存進去，不用再按一次「分析網頁」。"
            )
            self.save_hint_label.setStyleSheet(f"color: {theme.pick(theme.MUTED)};")
        elif not has_list:
            # 這兩句是「還不能存」的原因，要用警示色，跟上面那句說明不同。
            self.save_hint_label.setStyleSheet(f"color: {theme.pick(theme.DANGER)};")
            self.save_hint_label.setText("請先貼上網址並按「分析網頁」。")
        else:
            self.save_hint_label.setStyleSheet(f"color: {theme.pick(theme.DANGER)};")
            self.save_hint_label.setText(
                "沒有抓到「公司名稱」，這樣的資料存了也用不了。"
                "請打開下方「進階設定」手動指定它的位置。"
            )

    def _save(self, also_crawl: bool) -> None:
        url = self.last_url or self.url_entry.text().strip()
        if not url:
            QMessageBox.critical(self, "自訂網址精靈", "請先分析網址。")
            return

        name = self.name_entry.get() or self._default_name(url)
        list_selector = self.list_selector_entry.get()
        next_selector = self.next_selector_entry.get().strip() or None

        query_loop = self._selected_query_loop()

        # 逐項查詢的來源不存頁數上限。存了的話設定檔裡會躺著一個看起來像在
        # 管事、實際上不管事的數字（跑幾趟是 query_loop.max_queries 說了算），
        # 打開 custom_sources.yaml 的人只會被它誤導。
        max_pages: int | None = None
        max_pages_text = "" if query_loop else self.max_pages_entry.get()
        if max_pages_text:
            try:
                max_pages = int(max_pages_text)
            except ValueError:
                QMessageBox.critical(self, "自訂網址精靈", "頁數上限必須是整數。")
                return

        max_details_text = self.max_details_entry.get().strip()
        max_details: int | None = None
        if max_details_text:
            try:
                max_details = int(max_details_text)
            except ValueError:
                QMessageBox.critical(self, "自訂網址精靈", "「最多進入幾個詳細頁」必須是整數。")
                return

        try:
            page_start = int(self.page_start_entry.get().strip() or "1")
            page_end_text = self.page_end_entry.get().strip()
            page_end = int(page_end_text) if page_end_text else None
        except ValueError:
            QMessageBox.critical(self, "自訂網址精靈", "頁碼必須是整數。")
            return
        if page_end is not None and page_end < page_start:
            QMessageBox.critical(self, "自訂網址精靈", "結束頁不能小於起始頁。")
            return

        try:
            source = self.controller.build_source(
                url,
                name,
                list_selector,
                self._plain_rules(),
                next_selector,
                max_pages,
                detail_link_selector=self.detail_link_entry.get().strip() or None,
                max_details=max_details,
                default_industry=self.default_industry_entry.get(),
                collect_fields=self._selected_collect_fields(),
                document_kinds=self._selected_document_kinds(),
                page_actions=self._selected_page_actions(),
                page_start=page_start,
                page_end=page_end,
                engine=self._detected_engine,
                query_loop=query_loop,
            )
            saved_name = self.controller.save(source, name, enabled=True)
            # 編輯時把名字改掉的話，舊的那一份還躺在設定檔裡（儲存是照名字
            # 覆蓋的）。不收掉的話畫面上會多出一個沒有人想要的孤兒來源，
            # 而且排程還會照樣去爬它。
            if self._editing_source and self._editing_source != saved_name:
                self.controller.delete(self._editing_source)
            self._editing_source = saved_name
        except CRMError as exc:
            QMessageBox.critical(self, "儲存失敗", _friendly_error(exc))
            return

        if self.on_saved:
            self.on_saved(saved_name)

        if also_crawl:
            self._run_test_crawl(saved_name)
        else:
            QMessageBox.information(self, "自訂網址精靈", f"已儲存來源「{saved_name}」。")
            self.accept()

    def _run_test_crawl(self, name: str) -> None:
        self.save_button.setEnabled(False)
        self.save_crawl_button.setEnabled(False)
        self.crawl_task = BackgroundTask(
            self,
            worker=self.controller.test_run,
            on_done=lambda summaries: self._on_test_crawl_done(name, summaries),
            on_error=self._on_test_crawl_error,
        )
        self.crawl_task.start(name)

    def _on_test_crawl_done(self, name: str, summaries: Any) -> None:
        self.crawl_task = None
        # 試爬會真的寫入資料庫（公司/聯絡人），讓其他頁面知道要重新查詢。
        bump_data_version()
        summary = summaries[0] if summaries else None
        if summary is not None:
            QMessageBox.information(
                self,
                "爬取完成",
                f"已儲存來源「{name}」，並試爬 1 頁：\n"
                f"找到 {summary.records_found} 筆，新增 {summary.records_new} 筆。",
            )
        else:
            QMessageBox.information(self, "爬取完成", f"已儲存來源「{name}」，試爬未回傳結果。")
        self.accept()

    def _on_test_crawl_error(self, exc: Exception) -> None:
        self.crawl_task = None
        self.save_button.setEnabled(True)
        self.save_crawl_button.setEnabled(True)
        QMessageBox.critical(
            self, "試爬失敗", f"來源已儲存，但試爬時發生錯誤：{_friendly_error(exc)}"
        )
