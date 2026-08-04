"""匯出頁：篩選公司清單、挑欄位、寫成檔案。

篩選走 :class:`~core.schemas.CompanyFilter`，跟公司頁用的是同一個條件物件，
所以匯出結果永遠跟「如果在公司頁用同樣條件搜尋會看到的名單」一致。

跟 Tk 版一樣不覆寫 ``on_show``/``refresh``：篩選條件與欄位勾選都是使用者
每次手動設定，沒有「一顯示就該重查」的資料。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.constants import RecordStatus
from core.errors import CRMError
from core.schemas import CompanyFilter
from controllers.core import ExportController
from core.i18n import ALL_OPTION, FORMAT_LABELS, field_label, label, stage_labels, to_value
from gui_qt.pages.base import BasePage
from gui_qt.tasks import BackgroundTask
from gui_qt.widgets import LabeledEntry, Section, WideComboBox, caption

#: 業務階段下拉選單裡，用來表示「不篩選階段」的那個選項。
ANY_STAGE = ALL_OPTION


class ExportPage(BasePage):
    title = "匯出"
    icon = "📤"

    def __init__(self, app: object) -> None:
        super().__init__(app)
        self.controller = ExportController()
        self.column_checks: dict[str, QCheckBox] = {}
        self.last_result: tuple[Path, int] | None = None

        self.export_task = BackgroundTask(
            self,
            self.controller.run,
            on_progress=self._on_progress,
            on_done=self._on_done,
            on_error=self._on_error,
        )

    # ------------------------------------------------------------- 建立元件

    def build(self) -> None:
        page_layout = QVBoxLayout(self)
        page_layout.setContentsMargins(0, 0, 0, 0)

        # 整頁包一層捲動區。少了它，視窗縮到支援的最小尺寸（940x600）時內容
        # 塞不下，Qt 會把子元件壓到低於它們的最小高度——實測「篩選條件」裡
        # 三個輸入框各被切掉 4px。有捲動區就是出現捲軸，而不是把東西壓扁。
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        page_layout.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)

        outer = QVBoxLayout(content)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(12)

        title_label = QLabel("匯出")
        title_font = title_label.font()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title_label.setFont(title_font)
        outer.addWidget(title_label)

        outer.addWidget(self._build_output_section())

        columns_body = QHBoxLayout()
        columns_body.setSpacing(12)
        # 「篩選條件」欄位不多，天生就比較矮：給它 AlignTop 讓它維持自然高度、
        # 貼齊頂端，不要被 QHBoxLayout 硬拉到跟旁邊一樣高（那樣只會在底部
        # 生出一大塊空白）。多出來的高度改讓「欄位」清單（會捲動的那個）吃
        # 掉，清單才有機會整個顯示、不會連第一批項目都被壓縮到裁切。
        columns_body.addWidget(self._build_filters_section(), 1, Qt.AlignmentFlag.AlignTop)
        columns_body.addWidget(self._build_columns_section(), 1)
        outer.addLayout(columns_body, 1)

        outer.addLayout(self._build_footer())

    def _build_output_section(self) -> Section:
        section = Section("格式與輸出")

        format_row = QHBoxLayout()
        format_row.addWidget(caption("格式"))
        try:
            formats = self.controller.formats()
        except CRMError as exc:
            self.report_error(exc)
            formats = []
        self.format_combo = WideComboBox()
        self.format_combo.addItems([label(f, FORMAT_LABELS) for f in formats])
        format_row.addWidget(self.format_combo)
        format_row.addStretch(1)
        section.body_layout.addLayout(format_row)

        path_row = QHBoxLayout()
        self.path_entry = QLineEdit()
        self.path_entry.setPlaceholderText("留空以自動命名至輸出資料夾")
        path_row.addWidget(self.path_entry, 1)
        browse_button = QPushButton("瀏覽...")
        browse_button.clicked.connect(self._browse)
        path_row.addWidget(browse_button, 0, Qt.AlignmentFlag.AlignBottom)
        section.body_layout.addLayout(path_row)

        return section

    def _build_filters_section(self) -> Section:
        section = Section("篩選條件")

        self.text_entry = LabeledEntry("搜尋文字")
        section.body_layout.addWidget(self.text_entry)

        self.industry_entry = LabeledEntry("產業")
        section.body_layout.addWidget(self.industry_entry)

        section.body_layout.addWidget(caption("業務階段"))
        self.stage_combo = WideComboBox()
        self.stage_combo.addItems(stage_labels(with_all=True))
        section.body_layout.addWidget(self.stage_combo)

        self.email_only_check = QCheckBox("只匯出有信箱的紀錄")
        section.body_layout.addWidget(self.email_only_check)

        self.active_only_check = QCheckBox("只匯出使用中的紀錄")
        section.body_layout.addWidget(self.active_only_check)

        self.limit_entry = LabeledEntry("筆數上限（選填）")
        section.body_layout.addWidget(self.limit_entry)

        # 不在這裡加 addStretch(1)：這張卡片用 AlignTop 貼齊頂端顯示自然高度
        # 就好（見 build() 的說明），加了反而會在 Section 內部又擠出一塊
        # 用不到的空白。
        return section

    def _build_columns_section(self) -> Section:
        section = Section("欄位")

        # 十八個欄位一個個點太累了，尤其是「只要公司名稱和信箱」這種常見情境——
        # 先全部取消再勾兩個，比取消十六個快得多。使用者要求過這個功能，
        # 移植時要保留。
        select_row = QHBoxLayout()
        select_all_button = QPushButton("全選")
        select_all_button.clicked.connect(lambda: self._set_all_columns(True))
        select_row.addWidget(select_all_button)
        clear_all_button = QPushButton("全部取消")
        clear_all_button.clicked.connect(lambda: self._set_all_columns(False))
        select_row.addWidget(clear_all_button)
        select_row.addStretch(1)
        section.body_layout.addLayout(select_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(220)
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)

        try:
            columns = self.controller.columns()
        except CRMError as exc:
            self.report_error(exc)
            columns = []
        for column in columns:
            checkbox = QCheckBox(field_label(column))
            checkbox.setChecked(True)
            container_layout.addWidget(checkbox)
            self.column_checks[column] = checkbox
        container_layout.addStretch(1)

        scroll.setWidget(container)
        section.body_layout.addWidget(scroll, 1)
        return section

    def _build_footer(self) -> QHBoxLayout:
        footer = QHBoxLayout()
        self.export_button = QPushButton("匯出")
        self.export_button.clicked.connect(self._start_export)
        footer.addWidget(self.export_button)

        self.open_folder_button = QPushButton("開啟資料夾")
        self.open_folder_button.setEnabled(False)
        self.open_folder_button.clicked.connect(self._open_folder)
        footer.addWidget(self.open_folder_button, 0, Qt.AlignmentFlag.AlignBottom)

        self.result_label = QLabel("")
        self.result_label.setObjectName("MutedLabel")
        self.result_label.setWordWrap(True)
        footer.addWidget(self.result_label, 1)
        return footer

    # ------------------------------------------------------------- 欄位勾選

    def _set_all_columns(self, selected: bool) -> None:
        for checkbox in self.column_checks.values():
            checkbox.setChecked(selected)
        self.status("已全選所有欄位" if selected else "已取消所有欄位")

    # ------------------------------------------------------------- 小工具

    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "另存匯出檔案")
        if path:
            self.path_entry.setText(path)

    def _build_filter(self) -> CompanyFilter:
        stage = self.stage_combo.currentText()
        stages = [] if stage == ANY_STAGE else [to_value(stage)]

        limit_text = self.limit_entry.get()
        limit: int | None = None
        if limit_text:
            try:
                limit = int(limit_text)
            except ValueError:
                raise CRMError("筆數上限必須是整數") from None

        return CompanyFilter(
            text=self.text_entry.get() or None,
            industry=self.industry_entry.get() or None,
            stages=stages,
            statuses=[RecordStatus.ACTIVE.value] if self.active_only_check.isChecked() else [],
            has_email=True if self.email_only_check.isChecked() else None,
            limit=limit,
        )

    def _selected_columns(self) -> list[str] | None:
        selected = [column for column, checkbox in self.column_checks.items() if checkbox.isChecked()]
        if not self.column_checks or len(selected) == len(self.column_checks):
            return None  # 沒有任何欄位被取消勾選 -- 讓 exporter 用預設順序
        return selected

    # -------------------------------------------------------------- 匯出

    def _start_export(self) -> None:
        if self.export_task.running:
            return

        try:
            criteria = self._build_filter()
        except CRMError as exc:
            self.report_error(exc)
            return

        format_display = self.format_combo.currentText()
        if not format_display:
            self.status("請先選擇匯出格式", "error")
            return
        format_name = to_value(format_display, FORMAT_LABELS)

        path_text = self.path_entry.text().strip()
        path = path_text or None
        columns = self._selected_columns()
        # 全部取消之後直接匯出會得到一個只有欄位標題、沒有任何內容的檔案。
        if columns is not None and not columns:
            self.status("請至少勾選一個欄位", "error")
            return

        self.export_button.setEnabled(False)
        self.open_folder_button.setEnabled(False)
        self.result_label.setText("")
        self.app.status_bar.start_progress()
        self.status("匯出中...", "normal")

        self.export_task.start(format_name, path, criteria, columns)

    def _on_progress(self, payload: dict[str, Any]) -> None:
        self.status(f"匯出中...（{payload.get('stage', '')}）")

    def _on_done(self, result: tuple[Path, int]) -> None:
        path, count = result
        self.last_result = result
        self.export_button.setEnabled(True)
        self.open_folder_button.setEnabled(True)
        self.app.status_bar.stop_progress()
        self.result_label.setText(f"已寫入 {count} 筆資料到 {path}")
        self.status("匯出完成", "success")

    def _on_error(self, exc: Exception) -> None:
        self.export_button.setEnabled(True)
        self.app.status_bar.stop_progress()
        self.report_error(exc)

    def _open_folder(self) -> None:
        if self.last_result is None:
            return
        folder = self.last_result[0].parent
        if hasattr(os, "startfile"):
            os.startfile(folder)  # type: ignore[attr-defined]
        else:
            self.status(f"匯出資料夾：{folder}")
