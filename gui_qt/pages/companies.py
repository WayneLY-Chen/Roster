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

from PySide6.QtCore import Qt, QTimer
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

from datetime import date, datetime

from core.errors import CRMError
from core.schemas import CompanyFilter, CompanyView
from core.scoring import LEAD_SCORE_ORDER
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
from gui_qt.widgets import DataTable, WideComboBox, caption

ALL = ALL_OPTION

#: 跟 Tk 版 ``gui/pages/companies.py`` 的 COLUMNS 完全一致。
# 總覽只放「掃名單時真的會看」的欄位。英文名稱、傳真、地址、主要產品這些
# 是查單一家公司時才需要的資訊，塞進來只會把每一欄擠窄、逼使用者橫向捲動
# ——它們在「編輯」開的詳細資料視窗裡都有。
COLUMNS = [
    ("id", "編號", 40),
    ("company_name", "公司名稱", 170),
    # 名單品質。放在公司名稱旁邊，因為它要回答的正是「這一列先不先看」，
    # 掃名單時視線本來就停在這兩欄之間。
    ("lead_score", "品質", 50),
    ("email", "電子信箱", 140),
    ("phone", "電話", 85),
    ("industry", "產業", 90),
    ("contact_person", "聯絡人", 80),
    # 空白代表還沒查過登記資料，不是「查到它是空的」。
    ("registration_status", "登記狀態", 80),
    ("pipeline_stage", "業務階段", 80),
    ("priority", "優先度", 65),
    ("status", "狀態", 70),
    ("tags", "標籤", 110),
    ("updated_at", "更新時間", 100),
]

#: 「排序」下拉選單。顯示文字 → ``CompanyFilter.order_by`` 的值。
#:
#: 只放實際上會用到的幾個。表頭點一下也能排，但那只排「目前這一頁載進來的
#: 資料」；這個下拉是交給資料庫排的，語意上才是整份名單的排序。
SORT_OPTIONS: list[tuple[str, str, bool]] = [
    ("名單品質（高到低）", LEAD_SCORE_ORDER, True),
    ("更新時間（新到舊）", "updated_at", True),
    ("收集時間（新到舊）", "created_at", True),
    ("公司名稱（A→Z）", "company_name", False),
    ("資本額（大到小）", "capital_amount", True),
]

#: 沒有值的儲存格顯示這個。
#:
#: 表格裡用短破折號而不是「無資料」三個字：一張幾百列的表，每個空格都寫著
#: 「無資料」會蓋掉真正有資料的欄位，反而更難掃。詳細視窗那邊欄位少、
#: 一個一個看，那裡才用完整的「（無資料）」。
EMPTY_CELL = "—"

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

        # 永遠可以按。選了「收集日期」就刪那一天，沒選就刪今天收集到的——
        # 「把剛剛爬壞的那批清掉」是最常見的需求，不該逼使用者先去下拉選單
        # 挑一個日期才點得下去。按鈕文字會跟著變，按下去之前就看得出會刪什麼。
        #
        # 不管哪一種都只刪「某一天」，不會有「刪除全部」這個選項——那個沒有
        # 任何安全的按錯方式。
        self.delete_day_button = QPushButton("刪除今日新增")
        self.delete_day_button.setObjectName("DangerButton")
        self.delete_day_button.clicked.connect(self._delete_selected_day)
        header.addWidget(self.delete_day_button)

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

        # 收集日期。使用者是一批一批爬的，「哪一天爬的」才是他們心裡真正的
        # 分組方式——比「第 1 到 200 筆」直覺得多，也是唯一能整批清掉某次
        # 爬壞的資料的入口。
        date_column = QVBoxLayout()
        date_column.addWidget(caption("收集日期"))
        self.date_combo = WideComboBox()
        self.date_combo.addItem(ALL)
        self.date_combo.currentIndexChanged.connect(self._on_date_changed)
        date_column.addWidget(self.date_combo)
        filters.addLayout(date_column)

        # 排序。放在最右邊、緊鄰按鈕，因為它跟左邊那些「篩掉什麼」不是同一
        # 類動作——那些決定看得到誰，這個只決定誰排前面。
        sort_column = QVBoxLayout()
        sort_column.addWidget(caption("排序"))
        self.sort_combo = WideComboBox()
        self.sort_combo.addItems([text for text, _, _ in SORT_OPTIONS])
        self.sort_combo.setToolTip(
            "「名單品質」會把有信箱、有電話、資本額大、資料新的排在前面。"
        )
        self.sort_combo.currentIndexChanged.connect(lambda _index: self._run_search())
        sort_column.addWidget(self.sort_combo)
        filters.addLayout(sort_column)

        # 靠下對齊。這一列的其他項目都是「說明文字在上、控制項在下」的直向
        # 堆疊，按鈕預設會對齊整個堆疊的垂直中心，看起來就浮在說明文字那一
        # 排、跟輸入框與下拉沒有對齊。
        search_button = QPushButton("搜尋")
        search_button.clicked.connect(self._run_search)
        filters.addWidget(search_button, 0, Qt.AlignmentFlag.AlignBottom)

        # 名稱一定要寫「篩選」。原本只叫「清除」，使用者以為那顆會把下面的
        # 名單全部清掉——一個看起來會刪資料的按鈕，實際上只是把搜尋條件歸零。
        clear_button = QPushButton("清除篩選")
        clear_button.setToolTip("把上面的搜尋條件全部歸零，不會刪除任何資料")
        clear_button.clicked.connect(self._clear_filters)
        filters.addWidget(clear_button, 0, Qt.AlignmentFlag.AlignBottom)

        outer.addLayout(filters)

        self.table = DataTable(columns=COLUMNS, on_activate=self._open_selected)
        outer.addWidget(self.table, 1)

        self.count_label = QLabel("")
        self.count_label.setObjectName("MutedLabel")
        outer.addWidget(self.count_label)

    def _add_filter(self, layout: QHBoxLayout, field: str, label_text: str) -> None:
        column = QVBoxLayout()
        column.addWidget(caption(label_text))
        combo = WideComboBox()
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
        day = self.selected_date()
        _, order_by, descending = SORT_OPTIONS[max(0, self.sort_combo.currentIndex())]
        return CompanyFilter(
            order_by=order_by,
            descending=descending,
            text=self.search_entry.text().strip() or None,
            industry=None if industry in ("", ALL) else industry,
            stages=[] if stage in ("", ALL) else [to_value(stage, STAGE_LABELS)],
            statuses=[] if status in ("", ALL) else [to_value(status, STATUS_LABELS)],
            tags=[] if tag in ("", ALL) else [tag],
            created_after=(
                datetime.combine(day, datetime.min.time()) if day else None
            ),
            # 到當天 23:59:59.999999 為止。用「隔天零點」會把隔天零點整
            # 建立的那一筆也算進來。
            created_before=(
                datetime.combine(day, datetime.max.time()) if day else None
            ),
        )

    def _clear_filters(self) -> None:
        self.search_entry.blockSignals(True)
        self.search_entry.clear()
        self.search_entry.blockSignals(False)
        for combo in self.filter_combos.values():
            combo.blockSignals(True)
            combo.setCurrentText(ALL)
            combo.blockSignals(False)
        self.date_combo.blockSignals(True)
        self.date_combo.setCurrentText(ALL)
        self.date_combo.blockSignals(False)
        self._update_delete_day_button()
        self._run_search()

    # ----------------------------------------------------------- 收集日期

    def selected_date(self):
        """目前選到的收集日期；選「全部」時回 ``None``。

        選項資料存的是 ISO 字串而不是 ``date`` 物件。``QComboBox.findData()``
        是拿 QVariant 比對的，兩個「值相等但不是同一個」的 Python 物件在那裡
        比不出相等——重建選單時就會找不到原本選的那一項而退回「全部」，
        看起來就是「選了會被彈回來」。字串沒有這個問題。
        """
        raw = self.date_combo.currentData()
        return date.fromisoformat(raw) if raw else None

    def _on_date_changed(self, _index: int) -> None:
        self._update_delete_day_button()
        self._run_search()

    def target_delete_date(self):
        """「刪除」按鈕會動到哪一天：選了日期就是那天，沒選就是今天。"""
        return self.selected_date() or date.today()

    def _update_delete_day_button(self) -> None:
        day = self.selected_date()
        if day is None:
            self.delete_day_button.setText("刪除今日新增")
            self.delete_day_button.setToolTip(
                "刪除今天收集到的所有公司。要刪別天的，先在下面的「收集日期」選那一天。"
            )
        else:
            self.delete_day_button.setText(f"刪除 {day:%m-%d} 全部")
            self.delete_day_button.setToolTip(f"刪除 {day:%Y-%m-%d} 收集到的所有公司")

    def _apply_date_options(self, dates: list) -> None:
        """重填日期選單，保留目前選的那一天。"""
        previous = self.date_combo.currentData()      # ISO 字串或 None
        self.date_combo.blockSignals(True)
        self.date_combo.clear()
        self.date_combo.addItem(ALL, None)
        for day, count in dates:
            self.date_combo.addItem(f"{day:%Y-%m-%d}（{count} 家）", day.isoformat())
        if previous:
            index = self.date_combo.findData(previous)
            # 剛把那一天整批刪掉的話，該選項就不存在了，退回「全部」。
            self.date_combo.setCurrentIndex(index if index >= 0 else 0)
        self.date_combo.blockSignals(False)
        self._update_delete_day_button()

    def _delete_selected_day(self) -> None:
        day = self.target_delete_date()

        # 家數要問資料庫，不能用畫面上的列數——使用者可能同時下了關鍵字或
        # 產業篩選，表格上看到的只是那一天的一部分，但刪除是刪整天。
        try:
            count = self.controller.count(
                CompanyFilter(
                    created_after=datetime.combine(day, datetime.min.time()),
                    created_before=datetime.combine(day, datetime.max.time()),
                )
            )
        except CRMError as exc:
            self.report_error(exc)
            return

        if not count:
            self.status(f"{day:%Y-%m-%d} 沒有收集到任何公司", "error")
            return

        reply = QMessageBox.question(
            self,
            "刪除整天的公司",
            f"確定要刪除 {day:%Y-%m-%d} 收集到的全部公司嗎？\n"
            f"這一天共 {count} 家，連同底下的聯絡人、活動記錄與附件都會一起刪除。\n\n"
            "這個動作無法復原。需要保險的話，先到「設定」頁建立一份備份。",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            removed = self.controller.delete_by_date(day)
        except CRMError as exc:
            self.report_error(exc)
            return

        self.status(f"已刪除 {day:%Y-%m-%d} 的 {removed} 家公司", "success")
        self.on_show(force=True)

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
        dates = self.controller.crawl_dates()
        return rows, industries, tags, dates

    def _apply_result(self, result: tuple) -> None:
        rows, industries, tags, dates = result
        self._rows = rows
        self.table.set_rows([self._to_row(view) for view in rows])
        count = len(rows)
        self.count_label.setText(f"共 {count} 家公司")
        self.status(f"已載入 {count} 家公司")
        self._apply_filter_options(industries, tags)
        self._apply_date_options(dates)
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
        def shown(value: Any) -> Any:
            """空的就顯示破折號，不要留白。

            留白讓人分不出「這一家沒有這項資料」與「程式沒抓／沒載入」。
            """
            return value if (value or "").strip() else EMPTY_CELL

        return {
            "id": view.id,
            "company_name": view.company_name,
            "lead_score": view.lead_score,
            "email": shown(view.email),
            "phone": shown(view.phone),
            "industry": shown(view.industry),
            "contact_person": shown(view.contact_person),
            "registration_status": shown(view.registration_status),
            "pipeline_stage": label(view.pipeline_stage, STAGE_LABELS),
            "priority": label(view.priority, PRIORITY_LABELS),
            "status": label(view.status, STATUS_LABELS),
            "tags": view.tags or EMPTY_CELL,
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
        keep_combo = WideComboBox()
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
