"""公司明細對話框：檢視/編輯單一公司，含聯絡人、活動記錄、附件。

對應 ``gui/pages/company_detail.py``（Tk 版 ``CompanyDetailDialog``）。這是一個
獨立的 ``QDialog``，不受 ``gui_qt/pages/base.py`` 的 ``build()``/``refresh()``
生命週期管理——跟 Tk 版 ``CTkToplevel`` 一樣，每次「新增公司」或「編輯」都
重新建立一個實例、用完即丟，元件在 ``__init__`` 裡一次建好、立刻載入資料，
不需要延遲建立。

只 import ``gui.controllers.CompanyController`` 與 ``gui.i18n``，不 import
``gui/`` 底下其他任何模組。任何成功寫入資料庫的動作（儲存公司、刪除、新增
聯絡人／活動記錄／附件、刪除聯絡人／附件）之後都會呼叫
``gui_qt.pages.base.bump_data_version()``，讓公司頁與聯絡人頁下次顯示時
知道資料庫被動過、需要重新查詢。

這個對話框不在「換頁 20ms」的驗收範圍內（不是側邊欄的一頁，是使用者主動
點開的視窗），所以裡面的 controller 呼叫都是同步的，沒有走
``gui_qt.tasks.BackgroundTask``——單一公司的明細/新增/刪除都是幾毫秒等級的
操作，跟公司頁「215 筆整表查詢」那種才需要背景執行緒的情況不是同一類。
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.constants import ActivityType, PipelineStage, Priority, RecordStatus
from core.errors import CRMError
from controllers.core import CompanyController
from core.i18n import (
    ACTIVITY_LABELS,
    PRIORITY_LABELS,
    STAGE_LABELS,
    STATUS_LABELS,
    activity_labels,
    label,
    priority_labels,
    stage_labels,
    status_labels,
    to_value,
)
from core.legal import OPEN_DATA_ATTRIBUTION
from core.scoring import HOW_TO_FILL, MAX_POINTS, explain, lead_score
from gui_qt import theme
from gui_qt.pages.base import bump_data_version
from gui_qt.widgets import (
    CaptionedControl,
    DataTable,
    ErrorBanner,
    LabeledEntry,
    WideComboBox,
    caption,
)

CONTACT_COLUMNS = [
    ("name", "姓名", 140),
    ("title", "職稱", 110),
    ("email", "電子信箱", 170),
    ("phone", "電話", 100),
    ("mobile", "手機", 100),
    ("is_primary", "主要聯絡人", 70),
]

ACTIVITY_COLUMNS = [
    ("occurred_at", "時間", 130),
    ("type", "類型", 100),
    ("subject", "主旨", 200),
    ("body", "備註", 220),
]

#: 空欄位顯示這個，不要留白。留白看起來像壞掉或還沒載入完。
NO_DATA = "（無資料）"

ATTACHMENT_COLUMNS = [
    ("filename", "檔名", 220),
    ("size_bytes", "大小（位元組）", 100),
    ("uploaded_at", "上傳時間", 140),
]

class CompanyDetailDialog(QDialog):
    """新增或編輯一家公司，以及它的聯絡人、活動記錄與附件。"""

    def __init__(
        self,
        parent: QWidget | None,
        controller: CompanyController,
        company_id: int | None,
        on_saved: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.company_id = company_id
        self.on_saved = on_saved

        # 詳細資料頁現在有 12 列（多了英文名稱、傳真、主要產品），照舊的
        # 640 高度會把每一列壓到文字被切掉一半。高度跟著字型度量走，
        # macOS 的字比較大也裝得下。
        self.resize(820, theme.text_box_height(34))
        self.setMinimumSize(700, 600)
        self.setModal(True)

        self._contacts: list[Any] = []
        self._activities: list[Any] = []
        self._attachments: list[dict[str, Any]] = []

        outer = QVBoxLayout(self)

        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)

        self.details_tab = QWidget()
        self.contacts_tab = QWidget()
        self.activity_tab = QWidget()
        self.attachments_tab = QWidget()
        self.tabs.addTab(self.details_tab, "詳細資料")
        self.tabs.addTab(self.contacts_tab, "聯絡人")
        self.tabs.addTab(self.activity_tab, "活動記錄")
        self.tabs.addTab(self.attachments_tab, "附件")

        self.error_label = ErrorBanner()
        outer.addWidget(self.error_label)

        self._build_details_tab()
        self._build_contacts_tab()
        self._build_activity_tab()
        self._build_attachments_tab()

        self._load()

    # -- loading --------------------------------------------------------

    def _load(self) -> None:
        if self.company_id is None:
            self.setWindowTitle("新增公司")
            self._set_related_enabled(False)
            return

        try:
            data = self.controller.detail(self.company_id)
        except CRMError as exc:
            self._show_error(exc)
            return

        if data is None:
            self.error_label.set_text("找不到這一家公司，可能已經被刪掉了。")
            return

        company = data["company"]
        self.setWindowTitle(f"#{company.id} - {company.company_name}")
        self.name_entry.set(company.company_name)
        self.tax_id_entry.set(company.tax_id)
        self.email_entry.set(company.email)
        self.phone_entry.set(company.phone)
        self.website_entry.set(company.website)
        self.address_entry.set(company.address)
        self.industry_entry.set(company.industry)
        self.english_name_entry.set(company.english_name)
        self.fax_entry.set(company.fax)
        self.products_entry.set(company.products)
        self.contact_person_entry.set(company.contact_person)
        self.stage_combo.setCurrentText(label(company.pipeline_stage, STAGE_LABELS))
        self.priority_combo.setCurrentText(label(company.priority, PRIORITY_LABELS))
        self.status_combo.setCurrentText(label(company.status, STATUS_LABELS))
        self.follow_up_entry.set(
            company.follow_up_date.strftime("%Y-%m-%d") if company.follow_up_date else ""
        )
        self.tags_entry.set(", ".join(company.tags))
        self._set_extra_fields(company.extra_fields)
        self._refresh_quality(company)
        self.remark_box.setPlainText(company.remark or "")

        self._contacts = list(data["contacts"])
        self._activities = list(data["activities"])
        self._attachments = list(data["attachments"])
        self._refresh_contacts_table()
        self._refresh_activity_table()
        self._refresh_attachments_table()
        self._set_related_enabled(True)

    def _set_related_enabled(self, enabled: bool) -> None:
        for widget in (
            self.add_contact_button,
            self.delete_contact_button,
            self.add_activity_button,
            self.attach_button,
            self.open_attachment_button,
            self.save_attachment_button,
            self.delete_attachment_button,
        ):
            widget.setEnabled(enabled)
        self.hint_label.setVisible(not enabled)

    def _show_error(self, exc: Exception) -> None:
        """把例外顯示出來——翻成中文，而且使用者關得掉。

        以前這裡是 ``f"{type(exc).__name__}: {exc}"``，資料庫的例外會把整句
        SQL 連同所有參數一起印出來，其中包含加密後的欄位。使用者看到的是
        一段英文加一串密文，看不懂、不知道要做什麼，截圖一貼就外流了。
        """
        self.error_label.show_error(exc)

    # -- details tab ------------------------------------------------------

    def _build_details_tab(self) -> None:
        # 這一頁會長高：固定欄位之外還有「其他欄位」，而那一區有幾列是名錄
        # 決定的，不是我們能事先算出來的。放進捲動區之後，欄位再多也只是往下
        # 捲，不會把每一列壓到文字被切掉。
        scroller = QScrollArea(self.details_tab)
        scroller.setWidgetResizable(True)
        scroller.setFrameShape(QScrollArea.Shape.NoFrame)
        page = QWidget()
        scroller.setWidget(page)
        QVBoxLayout(self.details_tab).addWidget(scroller)

        grid = QGridLayout(page)

        # 空欄位一律顯示「無資料」的淡色提示，而不是留白。留白看起來像
        # 「這個欄位壞了」或「還沒載入完」；寫著無資料才看得出是「爬到的
        # 頁面上就沒有這一項」。提示文字不是內容，存檔時存進去的仍是空值。
        def _entry(label_text: str, placeholder: str = NO_DATA) -> LabeledEntry:
            return LabeledEntry(label_text, placeholder=placeholder)

        self.name_entry = _entry("公司名稱", "必填")
        grid.addWidget(self.name_entry, 0, 0)
        self.english_name_entry = _entry("英文名稱")
        grid.addWidget(self.english_name_entry, 0, 1)

        self.tax_id_entry = _entry("統一編號")
        grid.addWidget(self.tax_id_entry, 1, 0)
        self.industry_entry = _entry("產業")
        grid.addWidget(self.industry_entry, 1, 1)

        self.email_entry = _entry("電子信箱")
        grid.addWidget(self.email_entry, 2, 0)
        self.phone_entry = _entry("電話")
        grid.addWidget(self.phone_entry, 2, 1)

        self.website_entry = _entry("公司網址")
        grid.addWidget(self.website_entry, 3, 0)
        self.fax_entry = _entry("傳真")
        grid.addWidget(self.fax_entry, 3, 1)

        self.address_entry = _entry("公司地址")
        grid.addWidget(self.address_entry, 4, 0)
        self.contact_person_entry = _entry("聯絡人")
        grid.addWidget(self.contact_person_entry, 4, 1)

        # 產品描述常常是一整段（「界面活性劑及化學品進口貿易。」），
        # 給它整列的寬度。
        self.products_entry = _entry("主要產品／代理品項")
        grid.addWidget(self.products_entry, 5, 0, 1, 2)

        self.stage_combo = WideComboBox()
        self.stage_combo.addItems(stage_labels())
        self.stage_combo.setCurrentText(label(PipelineStage.NEW.value, STAGE_LABELS))
        stage_control = CaptionedControl("業務階段")
        stage_control.attach(self.stage_combo)
        grid.addWidget(stage_control, 6, 0)

        self.priority_combo = WideComboBox()
        self.priority_combo.addItems(priority_labels())
        self.priority_combo.setCurrentText(label(Priority.MEDIUM.value, PRIORITY_LABELS))
        priority_control = CaptionedControl("優先度")
        priority_control.attach(self.priority_combo)
        grid.addWidget(priority_control, 6, 1)

        self.status_combo = WideComboBox()
        self.status_combo.addItems(status_labels())
        self.status_combo.setCurrentText(label(RecordStatus.ACTIVE.value, STATUS_LABELS))
        status_control = CaptionedControl("狀態")
        status_control.attach(self.status_combo)
        grid.addWidget(status_control, 7, 0)

        self.follow_up_entry = _entry("追蹤日期（YYYY-MM-DD）", "YYYY-MM-DD")
        grid.addWidget(self.follow_up_entry, 7, 1)

        self.tags_entry = _entry("標籤（以逗號分隔）")
        grid.addWidget(self.tags_entry, 8, 0, 1, 2)

        self._build_extra_fields(grid, row=9)
        self._build_quality(grid, row=13)

        grid.addWidget(caption("備註"), 15, 0, 1, 2)
        self.remark_box = QTextEdit()
        self.remark_box.setPlaceholderText(NO_DATA)
        self.remark_box.setFixedHeight(theme.text_box_height(6))
        grid.addWidget(self.remark_box, 16, 0, 1, 2)

        save_button = QPushButton("儲存")
        save_button.setObjectName("PrimaryButton")  # 這個對話框最主要的動作
        save_button.clicked.connect(self._save)
        grid.addWidget(save_button, 17, 1)

    # -- 名單品質 ---------------------------------------------------------

    def _build_quality(self, grid: QGridLayout, row: int) -> None:
        """這一筆的名單品質分數是怎麼來的，以及公司登記資料的來源標示。

        分數在公司列表上只是一個數字，看不出為什麼是那個數字。把配分攤開來
        寫，使用者才知道「要提高它，下一步該補什麼」。
        """
        grid.addWidget(caption("名單品質"), row, 0, 1, 2)

        # 這一區要先回答「這個數字是幹嘛的」。只丟一個 10/100 出來，使用者
        # 看到的是一個沒有用途的分數——實際被問過。
        purpose = QLabel(
            "這是排序用的。「公司」頁右邊的「排序」選「名單品質」，"
            "分數高的排前面，先聯絡真的聯絡得上的那些。"
        )
        purpose.setObjectName("MutedLabel")
        purpose.setWordWrap(True)
        grid.addWidget(purpose, row + 1, 0, 1, 2)

        self.quality_label = QLabel(NO_DATA)
        self.quality_label.setWordWrap(True)
        grid.addWidget(self.quality_label, row + 2, 0, 1, 2)

    def _refresh_quality(self, company) -> None:
        score = lead_score(company)
        items = explain(company)
        earned = [(name, points) for name, points in items if points > 0]
        # 一票否決（已解散、標記不再聯絡）是負分，要單獨講，不能混進「還缺」。
        vetoed = [name for name, points in items if points < 0]

        lines = [f"<b>{score} / 100</b>"]
        if earned:
            lines.append("目前得分：" + "、".join(f"{n} {p}" for n, p in earned))

        if vetoed:
            lines.append(f"⚠ {vetoed[0]}——這一筆被壓到最低，不會排在前面。")
        else:
            # 缺什麼、補了會加幾分。只列已得分的話，使用者看得到分數卻不知道
            # 怎麼往上拉；列出缺口才是可以動手的資訊。
            got = {name for name, _ in earned}
            missing = [
                f"{name} +{MAX_POINTS[name]}"
                + (f"（{HOW_TO_FILL[name]}）" if name in HOW_TO_FILL else "")
                for name in MAX_POINTS
                if name not in got
            ]
            if missing:
                lines.append("還缺：" + "、".join(missing))

        if company.registration_status:
            detail = f"登記狀態：{company.registration_status}"
            if company.capital_amount:
                detail += f"　資本額 {company.capital_amount:,} 元"
            if company.registration_checked_at:
                detail += f"　查詢時間 {company.registration_checked_at:%Y-%m-%d}"
            lines.append(detail)
            # 顯名標示是授權條款的強制義務，不是說明文字——有登記資料就要有
            # 這一行。見 core.legal。
            lines.append(OPEN_DATA_ATTRIBUTION)
        elif company.tax_id:
            lines.append("還沒查過公司登記資料。到「爬取」頁按「補公司登記資料」。")

        self.quality_label.setText("<br>".join(lines))

    # -- 其他欄位 ---------------------------------------------------------

    def _build_extra_fields(self, grid: QGridLayout, row: int) -> None:
        """名錄自己才有的欄位，一列一項，名稱與內容都可以改。

        每個名錄列的東西都不一樣——旅行公會有「會員代表」「入會年月日」，
        化工公會有「代理廠商及代銷產品」。上面那些固定欄位裝不下它們，而丟掉
        等於使用者在網頁上看得到、在這裡卻找不到。
        """
        grid.addWidget(caption("其他欄位"), row, 0, 1, 2)

        hint = QLabel("這一區是名錄上有、但上面沒有對應欄位的資料，照原本的名稱保留。")
        hint.setObjectName("MutedLabel")
        hint.setWordWrap(True)
        grid.addWidget(hint, row + 1, 0, 1, 2)

        self.extra_table = QTableWidget(0, 2)
        self.extra_table.setHorizontalHeaderLabels(["欄位名稱", "內容"])
        header = self.extra_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.extra_table.verticalHeader().setVisible(False)
        self.extra_table.setFixedHeight(theme.text_box_height(6))
        grid.addWidget(self.extra_table, row + 2, 0, 1, 2)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        add_button = QPushButton("新增欄位")
        add_button.clicked.connect(lambda: self._append_extra_row("", ""))
        buttons.addWidget(add_button)
        remove_button = QPushButton("刪除這一列")
        remove_button.setObjectName("DangerButton")
        remove_button.clicked.connect(self._remove_extra_row)
        buttons.addWidget(remove_button)
        grid.addLayout(buttons, row + 3, 0, 1, 2)

    def _append_extra_row(self, name: str, value: str) -> None:
        row = self.extra_table.rowCount()
        self.extra_table.insertRow(row)
        self.extra_table.setItem(row, 0, QTableWidgetItem(name))
        self.extra_table.setItem(row, 1, QTableWidgetItem(value))

    def _remove_extra_row(self) -> None:
        row = self.extra_table.currentRow()
        if row >= 0:
            self.extra_table.removeRow(row)

    def _set_extra_fields(self, values: dict[str, str]) -> None:
        self.extra_table.setRowCount(0)
        for name, value in values.items():
            self._append_extra_row(name, value)

    def _collect_extra_fields(self) -> dict[str, str]:
        """讀回表格內容。沒有名稱的列直接丟掉——那是使用者按了新增卻沒填。"""
        collected: dict[str, str] = {}
        for row in range(self.extra_table.rowCount()):
            name_item = self.extra_table.item(row, 0)
            value_item = self.extra_table.item(row, 1)
            name = name_item.text().strip() if name_item else ""
            if not name:
                continue
            collected[name] = value_item.text().strip() if value_item else ""
        return collected

    def _save(self) -> None:
        self.error_label.setText("")

        follow_up_text = self.follow_up_entry.get()
        follow_up_date = None
        if follow_up_text:
            try:
                follow_up_date = datetime.strptime(follow_up_text, "%Y-%m-%d").date()
            except ValueError:
                self.error_label.setText("追蹤日期格式必須為 YYYY-MM-DD。")
                return

        tags = [tag.strip() for tag in self.tags_entry.get().split(",") if tag.strip()]

        fields: dict[str, Any] = {
            "company_name": self.name_entry.get(),
            "tax_id": self.tax_id_entry.get() or None,
            "email": self.email_entry.get() or None,
            "phone": self.phone_entry.get() or None,
            "website": self.website_entry.get() or None,
            "address": self.address_entry.get() or None,
            "industry": self.industry_entry.get() or None,
            "english_name": self.english_name_entry.get() or None,
            "fax": self.fax_entry.get() or None,
            "products": self.products_entry.get() or None,
            "contact_person": self.contact_person_entry.get() or None,
            "extra_fields": self._collect_extra_fields(),
            "pipeline_stage": to_value(self.stage_combo.currentText(), STAGE_LABELS),
            "priority": to_value(self.priority_combo.currentText(), PRIORITY_LABELS),
            "status": to_value(self.status_combo.currentText(), STATUS_LABELS),
            "follow_up_date": follow_up_date,
            "remark": self.remark_box.toPlainText().strip() or None,
        }

        try:
            if self.company_id is None:
                view = self.controller.create(**fields)
                self.company_id = view.id
            else:
                view = self.controller.update(self.company_id, **fields)
            self.controller.set_tags(self.company_id, tags)
        except CRMError as exc:
            self._show_error(exc)
            return

        bump_data_version()
        self.setWindowTitle(f"#{view.id} - {view.company_name}")
        if self.on_saved:
            self.on_saved()
        self._load()

    # -- contacts tab -----------------------------------------------------

    def _build_contacts_tab(self) -> None:
        layout = QVBoxLayout(self.contacts_tab)

        self.contacts_table = DataTable(columns=CONTACT_COLUMNS)
        layout.addWidget(self.contacts_table, 1)

        form = QGridLayout()
        self.contact_name_entry = LabeledEntry("姓名")
        form.addWidget(self.contact_name_entry, 0, 0)
        self.contact_title_entry = LabeledEntry("職稱")
        form.addWidget(self.contact_title_entry, 0, 1)
        self.contact_email_entry = LabeledEntry("電子信箱")
        form.addWidget(self.contact_email_entry, 0, 2)
        self.contact_phone_entry = LabeledEntry("電話")
        form.addWidget(self.contact_phone_entry, 0, 3)
        self.contact_mobile_entry = LabeledEntry("手機")
        form.addWidget(self.contact_mobile_entry, 0, 4)

        self.contact_primary_check = QCheckBox("主要聯絡人")
        form.addWidget(self.contact_primary_check, 1, 0)

        buttons = QHBoxLayout()
        self.add_contact_button = QPushButton("新增聯絡人")
        self.add_contact_button.clicked.connect(self._add_contact)
        buttons.addWidget(self.add_contact_button)
        self.delete_contact_button = QPushButton("刪除")
        self.delete_contact_button.setObjectName("DangerButton")
        self.delete_contact_button.clicked.connect(self._delete_contact)
        buttons.addWidget(self.delete_contact_button)
        form.addLayout(buttons, 1, 3, 1, 2)

        layout.addLayout(form)

        self.hint_label = QLabel("請先儲存公司資料，才能新增相關紀錄。")
        self.hint_label.setObjectName("MutedLabel")
        layout.addWidget(self.hint_label)
        self.hint_label.setVisible(False)

    def _refresh_contacts_table(self) -> None:
        self.contacts_table.set_rows(
            [
                {
                    "id": c.id,
                    "name": c.name,
                    "title": c.title,
                    "email": c.email,
                    "phone": c.phone,
                    "mobile": c.mobile,
                    "is_primary": c.is_primary,
                }
                for c in self._contacts
            ]
        )

    def _add_contact(self) -> None:
        if self.company_id is None:
            return
        name = self.contact_name_entry.get()
        if not name:
            self.error_label.setText("請輸入聯絡人姓名。")
            return
        try:
            self.controller.add_contact(
                self.company_id,
                name=name,
                title=self.contact_title_entry.get() or None,
                email=self.contact_email_entry.get() or None,
                phone=self.contact_phone_entry.get() or None,
                mobile=self.contact_mobile_entry.get() or None,
                is_primary=self.contact_primary_check.isChecked(),
            )
        except CRMError as exc:
            self._show_error(exc)
            return

        bump_data_version()
        self.contact_name_entry.set("")
        self.contact_title_entry.set("")
        self.contact_email_entry.set("")
        self.contact_phone_entry.set("")
        self.contact_mobile_entry.set("")
        self.contact_primary_check.setChecked(False)
        self._load()

    def _delete_contact(self) -> None:
        row = self.contacts_table.selected_row()
        if row is None:
            return
        reply = QMessageBox.question(self, "刪除聯絡人", f"確定要刪除「{row['name']}」嗎？")
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self.controller.delete_contact(row["id"])
        except CRMError as exc:
            self._show_error(exc)
            return
        bump_data_version()
        self._load()

    # -- activity tab -----------------------------------------------------

    def _build_activity_tab(self) -> None:
        layout = QVBoxLayout(self.activity_tab)

        self.activity_table = DataTable(columns=ACTIVITY_COLUMNS)
        layout.addWidget(self.activity_table, 1)

        form = QGridLayout()

        self.activity_type_combo = WideComboBox()
        self.activity_type_combo.addItems(activity_labels())
        self.activity_type_combo.setCurrentText(label(ActivityType.NOTE.value, ACTIVITY_LABELS))
        type_control = CaptionedControl("類型")
        type_control.attach(self.activity_type_combo)
        form.addWidget(type_control, 0, 0)

        self.activity_subject_entry = LabeledEntry("主旨")
        form.addWidget(self.activity_subject_entry, 0, 1)

        # 按鈕要跟旁邊的輸入框對齊。
        #
        # 「類型」與「主旨」都是兩行高（說明文字在上、控制項在下），而裸的
        # QPushButton 只有一行——放進同一列會被擺在那一列的**垂直中央**，
        # 看起來浮在輸入框上面一截。給它一個空的說明列佔掉上面那一行，
        # 兩邊的控制項就落在同一條線上。
        self.add_activity_button = QPushButton("新增")
        self.add_activity_button.clicked.connect(self._add_activity)
        add_control = CaptionedControl("")
        add_control.attach(self.add_activity_button)
        form.addWidget(add_control, 0, 2)

        form.addWidget(caption("備註"), 1, 0, 1, 3)
        self.activity_body_box = QTextEdit()
        self.activity_body_box.setFixedHeight(theme.text_box_height(4))
        form.addWidget(self.activity_body_box, 2, 0, 1, 3)

        layout.addLayout(form)

    def _refresh_activity_table(self) -> None:
        self.activity_table.set_rows(
            [
                {
                    "occurred_at": (
                        a.occurred_at.strftime("%Y-%m-%d %H:%M") if a.occurred_at else ""
                    ),
                    "type": label(a.type, ACTIVITY_LABELS),
                    "subject": a.subject,
                    "body": a.body,
                }
                for a in self._activities
            ]
        )

    def _add_activity(self) -> None:
        if self.company_id is None:
            return
        subject = self.activity_subject_entry.get()
        body = self.activity_body_box.toPlainText().strip()
        try:
            self.controller.add_activity(
                self.company_id,
                to_value(self.activity_type_combo.currentText(), ACTIVITY_LABELS),
                subject,
                body,
            )
        except CRMError as exc:
            self._show_error(exc)
            return

        bump_data_version()
        self.activity_subject_entry.set("")
        self.activity_body_box.clear()
        self._load()

    # -- attachments tab ----------------------------------------------------

    def _build_attachments_tab(self) -> None:
        layout = QVBoxLayout(self.attachments_tab)

        self.attachments_table = DataTable(columns=ATTACHMENT_COLUMNS)
        layout.addWidget(self.attachments_table, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        # 附件加進來之後要拿得回去。只有「附加」跟「刪除」的話，檔案等於進了
        # 一個看得到、打不開的地方——使用者要看內容只能自己去翻資料夾，而
        # 那個資料夾在哪裡程式也不會告訴他。
        self.open_attachment_button = QPushButton("開啟")
        self.open_attachment_button.setToolTip("用系統預設的程式打開這個附件")
        self.open_attachment_button.clicked.connect(self._open_attachment)
        buttons.addWidget(self.open_attachment_button)
        self.save_attachment_button = QPushButton("另存為...")
        self.save_attachment_button.setToolTip("把這個附件複製到你選的位置")
        self.save_attachment_button.clicked.connect(self._save_attachment_as)
        buttons.addWidget(self.save_attachment_button)
        self.attach_button = QPushButton("附加檔案...")
        self.attach_button.clicked.connect(self._attach_file)
        buttons.addWidget(self.attach_button)
        self.delete_attachment_button = QPushButton("刪除")
        self.delete_attachment_button.setObjectName("DangerButton")
        self.delete_attachment_button.clicked.connect(self._delete_attachment)
        buttons.addWidget(self.delete_attachment_button)
        layout.addLayout(buttons)

    def _refresh_attachments_table(self) -> None:
        self.attachments_table.set_rows(
            [
                {
                    "filename": a["filename"],
                    "size_bytes": a["size_bytes"],
                    "uploaded_at": (
                        a["uploaded_at"].strftime("%Y-%m-%d %H:%M") if a["uploaded_at"] else ""
                    ),
                    "id": a["id"],
                }
                for a in self._attachments
            ]
        )

    def _attach_file(self) -> None:
        if self.company_id is None:
            return
        path, _selected_filter = QFileDialog.getOpenFileName(self, "選擇要附加的檔案")
        if not path:
            return
        try:
            self.controller.add_attachment(self.company_id, path)
        except (CRMError, OSError) as exc:
            self._show_error(exc)
            return
        bump_data_version()
        self._load()

    def _selected_attachment_file(self) -> Path | None:
        """選取那一筆附件在磁碟上的檔案；沒選、或檔案不在了就回 ``None``。

        訊息裡只講檔名，不講完整路徑——那是使用者自己電腦上的位置，畫面上
        不需要出現，截圖出去也不該出現。
        """
        row = self.attachments_table.selected_row()
        if row is None:
            self.error_label.set_text("請先在上面選一個附件。")
            return None
        found = next(
            (a for a in self._attachments if a["id"] == row["id"]), None
        )
        path = Path(found["path"]) if found else None
        if path is None or not path.exists():
            self.error_label.set_text(
                f"找不到「{row['filename']}」這個檔案，它可能已經被移走或刪掉了。"
            )
            return None
        self.error_label.clear()
        return path

    def _open_attachment(self) -> None:
        path = self._selected_attachment_file()
        if path is None:
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self.error_label.set_text(
                f"這台電腦上沒有可以打開「{path.name}」的程式。"
                "可以先用「另存為...」存到別的地方再開。"
            )

    def _save_attachment_as(self) -> None:
        source = self._selected_attachment_file()
        if source is None:
            return
        target, _selected_filter = QFileDialog.getSaveFileName(
            self, "另存附件", source.name
        )
        if not target:
            return
        try:
            shutil.copy2(source, target)
        except OSError as exc:
            self._show_error(exc)
            return
        self.error_label.show_note(f"已存出「{Path(target).name}」。")

    def _delete_attachment(self) -> None:
        row = self.attachments_table.selected_row()
        if row is None:
            return
        reply = QMessageBox.question(self, "刪除附件", f"確定要刪除「{row['filename']}」嗎？")
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self.controller.delete_attachment(row["id"])
        except CRMError as exc:
            self._show_error(exc)
            return
        bump_data_version()
        self._load()
