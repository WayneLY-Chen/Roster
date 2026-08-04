"""公司頁：搜尋、篩選、編輯、去重。

對應 ``gui/pages/companies.py``（Tk 版 ``CompaniesPage`` + ``DuplicatesDialog``）。

## 效能：215 筆整表重填怎麼壓進 20ms 的換頁預算

實測 ``CompanyController.search(CompanyFilter())``（無篩選、215 筆、欄位有
加密）對這支專案的資料庫要花約 12-14ms（冷啟動甚至到 40ms 起跳）——單獨看
不算慢，但已經逼近 20ms 的換頁預算本身，若在 ``refresh()`` 裡同步呼叫、卡在
UI 執行緒上，這筆查詢時間就會被算進「換頁到畫面更新」的驗收數字裡，跟
``gui_qt/pages/dashboard.py`` 檔頭說明的理由完全一樣。所以這裡查詢一律走
``gui_qt.tasks.BackgroundTask``：切頁當下先用（可能是空的，也可能是上次看過
的）資料立刻完成 Qt 本身的換頁動作，查詢結果回來後才局部更新表格。

真正讓「同一頁資料沒變就不用整批重查」成立的，是完全不覆寫 ``on_show()``、
讓 ``gui_qt/pages/base.py`` 的資料版本機制接手：使用者在公司頁跟其他頁之間
來回切換、資料庫沒有任何寫入動作發生時，``BasePage.on_show`` 會直接呼叫
``on_reveal()``（這裡沒有覆寫，什麼都不做），完全跳過 ``refresh()``、也就
跳過了這 12-14ms 的查詢——這時候整個「換頁」成本就只剩下
``QStackedWidget.setCurrentWidget()`` 本身，量出來是個位數毫秒。只有下列
情況才會真的觸發 ``refresh()``：第一次顯示這頁、或任何地方呼叫過
``bump_data_version()``（新增/刪除/編輯一家公司、合併重複、匯入、爬蟲跑完
……）。

## 搜尋框的 debounce

``search_entry`` 是即時過濾（打字就查），為了不讓「慢路徑」（有篩選文字時
``CompanyRepository.search()`` 必須整批撈到 Python 端比對加密欄位，見
``database/repository.py`` 的說明）在每個按鍵都跑一次，這裡用
``QTimer.singleShot`` 風格的單發計時器（300ms）做 debounce；下拉篩選跟按鈕
點擊則是使用者意圖明確的單次動作，不需要 debounce，選了就立刻查。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.errors import CRMError
from core.schemas import CompanyFilter, CompanyView
from controllers.core import CompanyController
from core.i18n import (
    ALL_OPTION,
    PRIORITY_LABELS,
    STAGE_LABELS,
    STATUS_LABELS,
    label,
    stage_labels,
    status_labels,
    to_value,
)
from gui_qt.company_detail import CompanyDetailDialog
from gui_qt.pages.base import BasePage, bump_data_version
from gui_qt.tasks import BackgroundTask
from gui_qt.widgets import DataTable, caption

ALL = ALL_OPTION

#: 跟 Tk 版 ``gui/pages/companies.py`` 的 COLUMNS 完全一致。
COLUMNS = [
    ("id", "編號", 40),
    ("company_name", "公司名稱", 170),
    ("email", "電子信箱", 140),
    ("phone", "電話", 85),
    ("industry", "產業", 90),
    ("pipeline_stage", "業務階段", 80),
    ("priority", "優先度", 65),
    ("status", "狀態", 70),
    ("tags", "標籤", 110),
    ("updated_at", "更新時間", 100),
]

#: 搜尋框 debounce 的間隔。
SEARCH_DEBOUNCE_MS = 300


class CompaniesPage(BasePage):
    title = "公司"
    icon = "🏢"

    def __init__(self, app: object) -> None:
        super().__init__(app)
        self.controller = CompanyController()
        self.filter_combos: dict[str, QComboBox] = {}
        self._rows: list[CompanyView] = []
        #: 上一次查詢還沒回來時，使用者又觸發了新的一次搜尋——記下最新的條件，
        #: 等目前這次查詢完成後立刻用它補查一次，而不是把使用者的輸入丟掉。
        self._pending_criteria: CompanyFilter | None = None

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self._run_search)

        self._fetch_task = BackgroundTask(
            self, self._fetch, on_done=self._apply_result, on_error=self._handle_error
        )

    # ------------------------------------------------------------- 建立元件

    def build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(8)

        header = QHBoxLayout()
        title_label = QLabel("公司")
        title_font = title_label.font()
        title_font.setPointSize(22)
        title_font.setBold(True)
        title_label.setFont(title_font)
        header.addWidget(title_label)
        header.addStretch(1)

        new_button = QPushButton("新增公司")
        new_button.setObjectName("PrimaryButton")  # 這頁最主要的動作
        new_button.clicked.connect(self._new_company)
        header.addWidget(new_button)

        edit_button = QPushButton("編輯")
        edit_button.clicked.connect(self._edit_selected)
        header.addWidget(edit_button)

        delete_button = QPushButton("刪除")
        delete_button.setObjectName("DangerButton")
        delete_button.clicked.connect(self._delete_selected)
        header.addWidget(delete_button)

        duplicates_button = QPushButton("尋找重複")
        duplicates_button.clicked.connect(self._find_duplicates)
        header.addWidget(duplicates_button)

        refresh_button = QPushButton("重新整理")
        refresh_button.clicked.connect(lambda checked=False: self.on_show(force=True))
        header.addWidget(refresh_button)

        outer.addLayout(header)

        filters = QHBoxLayout()
        filters.setSpacing(8)

        search_column = QVBoxLayout()
        search_column.addWidget(caption("關鍵字"))
        self.search_entry = QLineEdit()
        self.search_entry.setPlaceholderText("公司、信箱、電話、地址...")
        self.search_entry.textChanged.connect(self._schedule_search)
        self.search_entry.returnPressed.connect(self._run_search)
        search_column.addWidget(self.search_entry)
        filters.addLayout(search_column, 1)

        self._add_filter(filters, "industry", "產業")
        self._add_filter(filters, "pipeline_stage", "業務階段")
        self._add_filter(filters, "status", "狀態")
        self._add_filter(filters, "tags", "標籤")

        search_button = QPushButton("搜尋")
        search_button.clicked.connect(self._run_search)
        filters.addWidget(search_button)

        clear_button = QPushButton("清除")
        clear_button.clicked.connect(self._clear_filters)
        filters.addWidget(clear_button)

        outer.addLayout(filters)

        self.table = DataTable(columns=COLUMNS, on_activate=self._open_selected)
        outer.addWidget(self.table, 1)

        self.count_label = QLabel("")
        self.count_label.setObjectName("MutedLabel")
        outer.addWidget(self.count_label)

    def _add_filter(self, layout: QHBoxLayout, field: str, label_text: str) -> None:
        column = QVBoxLayout()
        column.addWidget(caption(label_text))
        combo = QComboBox()
        combo.addItem(ALL)
        combo.currentIndexChanged.connect(lambda _index: self._run_search())
        column.addWidget(combo)
        layout.addLayout(column)
        self.filter_combos[field] = combo

    # ------------------------------------------------------------- 生命週期

    def refresh(self) -> None:
        self._run_search()

    def on_hide(self) -> None:
        self._search_timer.stop()

    # --------------------------------------------------------- 篩選下拉選單

    def _apply_filter_options(self, industries: list[str], tags: list[str]) -> None:
        self._set_combo_values(self.filter_combos["industry"], [ALL, *industries])
        self._set_combo_values(self.filter_combos["pipeline_stage"], [ALL, *stage_labels()])
        self._set_combo_values(self.filter_combos["status"], [ALL, *status_labels()])
        self._set_combo_values(self.filter_combos["tags"], [ALL, *tags])

    @staticmethod
    def _set_combo_values(combo: QComboBox, values: list[str]) -> None:
        """重灌下拉選單的選項，盡量保留使用者原本選的那個。"""
        current = combo.currentText() or ALL
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(values)
        combo.setCurrentText(current if current in values else ALL)
        combo.blockSignals(False)

    def _current_filter(self) -> CompanyFilter:
        stage = self.filter_combos["pipeline_stage"].currentText()
        status = self.filter_combos["status"].currentText()
        tag = self.filter_combos["tags"].currentText()
        industry = self.filter_combos["industry"].currentText()
        return CompanyFilter(
            text=self.search_entry.text().strip() or None,
            industry=None if industry in ("", ALL) else industry,
            stages=[] if stage in ("", ALL) else [to_value(stage, STAGE_LABELS)],
            statuses=[] if status in ("", ALL) else [to_value(status, STATUS_LABELS)],
            tags=[] if tag in ("", ALL) else [tag],
        )

    def _clear_filters(self) -> None:
        self.search_entry.blockSignals(True)
        self.search_entry.clear()
        self.search_entry.blockSignals(False)
        for combo in self.filter_combos.values():
            combo.blockSignals(True)
            combo.setCurrentText(ALL)
            combo.blockSignals(False)
        self._run_search()

    # ------------------------------------------------------------- 查詢（背景執行緒）

    def _schedule_search(self, _text: str = "") -> None:
        self._search_timer.start(SEARCH_DEBOUNCE_MS)

    def _run_search(self) -> None:
        self._search_timer.stop()
        criteria = self._current_filter()
        if self._fetch_task.running:
            self._pending_criteria = criteria
            return
        self._fetch_task.start(criteria)

    def _fetch(self, criteria: CompanyFilter, *, report, cancel_event) -> tuple:
        """在背景執行緒被呼叫；千萬不能在這裡碰任何 widget。

        跟篩選下拉選單的選項（產業、標籤）一起在同一次背景呼叫裡撈回來，
        不要讓 UI 執行緒自己另外同步呼叫 ``distinct()``/``all_tags()``——
        即使那兩個查詢單獨看很便宜（<2ms），跟背景執行緒的 ``search()``
        同時對同一個資料庫連線池搶連線，實測會讓兩邊互相拖慢，兩者一起
        丟進背景執行緒才能讓 ``on_show()`` 同步的部分趨近於零成本。
        """
        rows = self.controller.search(criteria)
        industries = self.controller.distinct("industry")
        tags = self.controller.all_tags()
        return rows, industries, tags

    def _apply_result(self, result: tuple) -> None:
        rows, industries, tags = result
        self._rows = rows
        self.table.set_rows([self._to_row(view) for view in rows])
        count = len(rows)
        self.count_label.setText(f"共 {count} 家公司")
        self.status(f"已載入 {count} 家公司")
        self._apply_filter_options(industries, tags)
        self._start_pending_if_any()

    def _handle_error(self, exc: Exception) -> None:
        self.report_error(exc)
        self._start_pending_if_any()

    def _start_pending_if_any(self) -> None:
        if self._pending_criteria is None:
            return
        criteria, self._pending_criteria = self._pending_criteria, None
        self._fetch_task.start(criteria)

    @staticmethod
    def _to_row(view: CompanyView) -> dict[str, Any]:
        return {
            "id": view.id,
            "company_name": view.company_name,
            "email": view.email,
            "phone": view.phone,
            "industry": view.industry,
            "pipeline_stage": label(view.pipeline_stage, STAGE_LABELS),
            "priority": label(view.priority, PRIORITY_LABELS),
            "status": label(view.status, STATUS_LABELS),
            "tags": view.tags,
            "updated_at": view.updated_at.strftime("%Y-%m-%d %H:%M") if view.updated_at else "",
        }

    # ------------------------------------------------------------- 動作

    def _new_company(self) -> None:
        dialog = CompanyDetailDialog(self, self.controller, None, on_saved=self._run_search)
        dialog.exec()

    def _edit_selected(self) -> None:
        row = self.table.selected_row()
        if row is None:
            self.status("請先選擇一家公司", "muted")
            return
        self._open_selected(row)

    def _open_selected(self, row: dict[str, Any]) -> None:
        dialog = CompanyDetailDialog(self, self.controller, row["id"], on_saved=self._run_search)
        dialog.exec()

    def _delete_selected(self) -> None:
        row = self.table.selected_row()
        if row is None:
            self.status("請先選擇一家公司", "muted")
            return
        reply = QMessageBox.question(
            self, "刪除公司", f"確定要刪除「{row['company_name']}」嗎？此動作無法復原。"
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self.controller.delete(row["id"])
        except CRMError as exc:
            self.report_error(exc)
            return
        bump_data_version()
        self.status(f"已刪除 {row['company_name']}", "success")
        self._run_search()

    def _find_duplicates(self) -> None:
        # 同步呼叫：這是使用者主動點的一次性動作（不是換頁），實測約 10-12ms，
        # 遠低於任何會讓人感覺到卡頓的門檻，不需要為此另外走背景執行緒。
        try:
            groups = self.controller.duplicate_groups()
        except CRMError as exc:
            self.report_error(exc)
            return
        dialog = DuplicatesDialog(self, self.controller, groups, on_merged=self._run_search)
        dialog.exec()


class DuplicatesDialog(QDialog):
    """列出疑似重複的公司分組，一鍵合併。對應 Tk 版 ``DuplicatesDialog``。"""

    def __init__(
        self,
        parent: QWidget | None,
        controller: CompanyController,
        groups: list[list[CompanyView]],
        on_merged,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.groups = groups
        self.on_merged = on_merged

        self.setWindowTitle("尋找重複")
        self.resize(640, 480)
        self.setModal(True)

        outer = QVBoxLayout(self)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        outer.addWidget(self.scroll)

        self._render()

    def _render(self) -> None:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(8)

        if not self.groups:
            layout.addWidget(QLabel("沒有找到重複的公司。"))
        else:
            for group in self.groups:
                layout.addWidget(self._build_group_frame(group))
        layout.addStretch(1)

        self.scroll.setWidget(content)

    def _build_group_frame(self, group: list[CompanyView]) -> QFrame:
        frame = QFrame()
        frame.setObjectName("Section")
        frame_layout = QVBoxLayout(frame)

        names = ", ".join(f"#{c.id} {c.company_name}" for c in group)
        name_label = QLabel(names)
        name_label.setWordWrap(True)
        frame_layout.addWidget(name_label)

        row = QHBoxLayout()
        keep_combo = QComboBox()
        keep_combo.addItems([str(c.id) for c in group])
        row.addWidget(keep_combo)
        row.addStretch(1)
        merge_button = QPushButton("合併")
        merge_button.clicked.connect(
            lambda checked=False, g=group, combo=keep_combo: self._merge(g, combo)
        )
        row.addWidget(merge_button)
        frame_layout.addLayout(row)

        return frame

    def _merge(self, group: list[CompanyView], keep_combo: QComboBox) -> None:
        keep_id = int(keep_combo.currentText())
        drop_ids = [c.id for c in group if c.id != keep_id]
        try:
            self.controller.merge(keep_id, drop_ids)
        except CRMError as exc:
            QMessageBox.critical(self, "合併失敗", str(exc))
            return
        bump_data_version()
        self.groups = [g for g in self.groups if g is not group]
        self._render()
        self.on_merged()
