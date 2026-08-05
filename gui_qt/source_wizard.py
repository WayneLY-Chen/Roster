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
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.errors import CrawlError, CRMError, RobotsDisallowedError
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
        self.setModal(True)

        # 分析成功後才會有值；``field_rules`` 把欄位代碼對應到
        # {"selector", "attr", "regex", "hit_rate", "samples"}——多出來的
        # 顯示用欄位在儲存前會被 _plain_rules() 濾掉。
        self.last_url: str = ""
        self.field_rules: dict[str, dict[str, Any]] = {}
        self.preview_rows: list[dict[str, Any]] = []
        self._editing_field: str | None = None

        self.analyse_task: BackgroundTask | None = None
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
        section.body_layout.addLayout(row)

        self.analyse_progress = QProgressBar()
        self.analyse_progress.setRange(0, 0)  # 不定進度：分析中沒有百分比可言
        self.analyse_progress.hide()
        section.body_layout.addWidget(self.analyse_progress)

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

        self.analyse_button.setEnabled(False)
        self.analyse_progress.show()
        self.summary_label.setText("分析中，請稍候...此步驟會實際連線到目標網站。")
        self.notes_label.setText("")

        self.analyse_task = BackgroundTask(
            self,
            worker=self.controller.analyse,
            on_done=self._on_analyse_done,
            on_error=self._on_analyse_error,
        )
        self.analyse_task.start(url)

    def _stop_analyse_progress(self) -> None:
        self.analyse_progress.hide()
        self.analyse_button.setEnabled(True)

    def _on_analyse_done(self, result: Any) -> None:
        self.analyse_task = None
        self._stop_analyse_progress()

        self.last_url = result.url
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
            self.save_hint_label.setText("")
        elif not has_list:
            self.save_hint_label.setText("請先貼上網址並按「分析網頁」。")
        else:
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

        max_pages_text = self.max_pages_entry.get()
        max_pages: int | None = None
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
                page_start=page_start,
                page_end=page_end,
            )
            saved_name = self.controller.save(source, name, enabled=True)
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
