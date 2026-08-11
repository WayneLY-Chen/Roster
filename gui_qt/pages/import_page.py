"""匯入頁：預覽試算表欄位對應後，才真正把資料寫入資料庫。

對照 Tk 版 ``gui/pages/import_page.py``。欄位對應是用猜的（見
``ImportController.preview``）——來源標頭千奇百怪，猜錯了不會怎樣，但使用者
必須先看到「猜出來的結果」再決定要不要按下去，``ImportController.run`` 才會
真的動到資料庫。

這頁不覆寫 ``on_show``/``refresh``：整頁完全是使用者手動操作驅動（選檔案、
按匯入），沒有任何「頁面一顯示就該查的資料」，讓 :class:`~gui_qt.pages.base.BasePage`
的預設（什麼都不做）生效即可，跟 Tk 版 ``on_show()`` 是空函式的用意一樣。

## 匯入後自動補齊

勾了「匯入後自動補齊資料」的話，匯入完成會**接著**跑一次
:mod:`crawler.complete`——只針對這一次匯入進來的公司，不會動到資料庫裡
既有的資料。這是兩個分開的背景工作，不是一個：匯入是純檔案操作、幾秒就完，
補齊要連網、可能好幾分鐘。合成一個的話進度條會停在「匯入中」不動好幾分鐘，
而且中途按取消會分不清楚取消掉的是哪一段。分開之後，匯入的結果在補齊還在
跑的時候就已經寫進資料庫、也已經顯示在畫面上了。

那個勾選框的狀態會存回 ``user_settings.yaml``（``completion.auto_after_import``），
所以下次打開程式還記得。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from core.config import get_config, save_user_setting
from core.errors import CRMError, ExportError
from core.i18n import field_label
from exporter.sample_template import sample_filename, write_sample
from controllers.core import CompletionController, ImportController
from gui_qt.pages.base import BasePage, bump_data_version
from gui_qt.tasks import BackgroundTask
from gui_qt.widgets import DataTable, LabeledEntry, Section

#: Qt 的檔案篩選字串語法跟 Tk 的 filetypes 不同（用分號分隔、副檔名要包在括號裡），
#: 內容對照 Tk 版 ``FILETYPES``。
FILE_FILTER = (
    "試算表 (*.csv *.xlsx *.xls *.json);;"
    "CSV 檔案 (*.csv);;"
    "Excel 檔案 (*.xlsx *.xls);;"
    "JSON 檔案 (*.json);;"
    "所有檔案 (*.*)"
)

#: 「下載範例檔」存檔對話框的篩選器——只提供匯入器/範例產生器都支援的兩種格式。
SAMPLE_FILE_FILTER = "Excel 活頁簿 (*.xlsx);;CSV 檔案 (*.csv)"

#: 對應表格裡，欄位沒有對應到任何 CRM 欄位時顯示的字。
IGNORED = "（略過）"


class ImportPage(BasePage):
    title = "匯入"
    icon = "📥"

    def __init__(self, app: object) -> None:
        super().__init__(app)
        self.controller = ImportController()
        self.completion_controller = CompletionController()
        self.selected_path: Path | None = None
        self.preview: dict[str, Any] | None = None
        #: 這一次自動補齊有幾家因為超過上限沒被處理。見 ``_maybe_start_completion``。
        self._completion_trimmed = 0

        # 跟 dashboard 的 _fetch_task 一樣：只建立一次，重複使用，
        # 不要每次按「匯入」都 new 一個新的 BackgroundTask。
        self.import_task = BackgroundTask(
            self,
            self.controller.run,
            on_progress=self._on_progress,
            on_done=self._on_done,
            on_error=self._on_error,
        )
        self.completion_task = BackgroundTask(
            self,
            self.completion_controller.run,
            on_progress=self._on_completion_progress,
            on_done=self._on_completion_done,
            on_error=self._on_completion_error,
        )

    # ------------------------------------------------------------- 建立元件

    def build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(12)

        title_label = QLabel("匯入")
        title_font = title_label.font()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title_label.setFont(title_font)
        outer.addWidget(title_label)

        choose_section = Section("選擇檔案")
        outer.addWidget(choose_section)

        pick_row = QHBoxLayout()
        self.choose_button = QPushButton("選擇檔案...")
        self.choose_button.clicked.connect(self._choose_file)
        pick_row.addWidget(self.choose_button)

        self.download_sample_button = QPushButton("下載範例檔")
        self.download_sample_button.clicked.connect(self._download_sample)
        pick_row.addWidget(self.download_sample_button)

        # QLabel.setWordWrap(True)：路徑再長，也是往下換行而不是被裁掉。
        self.path_label = QLabel("尚未選擇檔案")
        self.path_label.setObjectName("MutedLabel")
        self.path_label.setWordWrap(True)
        pick_row.addWidget(self.path_label, 1)
        choose_section.body_layout.addLayout(pick_row)

        self.label_entry = LabeledEntry("來源標籤（選填）")
        choose_section.body_layout.addWidget(self.label_entry)

        settings = get_config().completion
        self.auto_complete_check = QCheckBox("匯入後自動補齊資料")
        self.auto_complete_check.setChecked(settings.auto_after_import)
        self.auto_complete_check.setToolTip(
            "匯入完成後，對這一批公司查經濟部商業司補統一編號與登記資料、"
            "搜尋找出官網，再到官網上抓公開的信箱、電話、傳真與聯絡人。\n"
            "只補空欄位，不會覆蓋檔案裡本來就有的值。"
        )
        self.auto_complete_check.toggled.connect(self._save_auto_complete)
        choose_section.body_layout.addWidget(self.auto_complete_check)

        self.auto_complete_note = QLabel(
            "補齊會連到網路，一家公司最多四次請求，因此比匯入本身慢很多"
            f"（每次匯入最多自動補 {settings.auto_after_import_limit} 家，"
            "其餘請到「爬取」頁按「補齊公司資料」）。"
        )
        self.auto_complete_note.setObjectName("MutedLabel")
        self.auto_complete_note.setWordWrap(True)
        choose_section.body_layout.addWidget(self.auto_complete_note)

        self.import_button = QPushButton("匯入")
        self.import_button.setEnabled(False)
        self.import_button.clicked.connect(self._start_import)
        choose_section.body_layout.addWidget(self.import_button)

        self.import_status = QLabel("")
        self.import_status.setObjectName("MutedLabel")
        choose_section.body_layout.addWidget(self.import_status)

        mapping_section = Section("欄位對應預覽")
        self.mapping_table = DataTable(
            columns=[
                ("column", "試算表欄位", 200),
                ("field", "對應欄位", 160),
                ("sample", "範例值", 260),
            ]
        )
        mapping_section.body_layout.addWidget(self.mapping_table)
        self.total_rows_label = QLabel("")
        self.total_rows_label.setObjectName("MutedLabel")
        mapping_section.body_layout.addWidget(self.total_rows_label)
        outer.addWidget(mapping_section, 1)

        summary_section = Section("上次匯入結果")
        self.summary_label = QLabel("尚未執行匯入。")
        self.summary_label.setWordWrap(True)
        summary_section.body_layout.addWidget(self.summary_label)
        outer.addWidget(summary_section)

    # ------------------------------------------------------------- 範例檔

    def _download_sample(self) -> None:
        """存一份可以直接照著填的匯入範例檔（欄位跟匯入器認得的欄位一致）。

        純檔案寫入、很快，不需要走 :class:`~gui_qt.tasks.BackgroundTask`。
        """
        path, _ = QFileDialog.getSaveFileName(
            self, "下載匯入範例檔", sample_filename(".xlsx"), SAMPLE_FILE_FILTER
        )
        if not path:
            return
        try:
            written = write_sample(path)
        except ExportError as exc:
            self.report_error(exc)
            return
        if written.suffix.lower() in {".xlsx", ".xls"}:
            guidance = "檔案裡的「填寫說明」分頁有逐欄說明，示範資料填完後請刪掉。"
        else:
            guidance = "第一列是標題請勿更動，三列示範資料填完後請刪掉。"
        self.import_status.setText(f"範例檔已存到 {written}。{guidance}")
        self.status(f"範例檔已存到 {written}", "success")

    # ------------------------------------------------------------- 選擇檔案

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "選擇要匯入的檔案", "", FILE_FILTER)
        if not path:
            return
        self.selected_path = Path(path)
        self.path_label.setText(str(self.selected_path))
        self._load_preview()

    def _load_preview(self) -> None:
        if self.selected_path is None:
            return
        try:
            self.preview = self.controller.preview(self.selected_path)
        except CRMError as exc:
            self.preview = None
            self.import_button.setEnabled(False)
            self.report_error(exc)
            return

        columns = self.preview["columns"]
        mapping = self.preview["mapping"]
        rows = self.preview["rows"]
        first_row = rows[0] if rows else []

        table_rows = []
        for index, column in enumerate(columns):
            field = mapping.get(column)
            sample = first_row[index] if index < len(first_row) else ""
            table_rows.append({"column": column, "field": field or IGNORED, "sample": sample})
        self.mapping_table.set_rows(table_rows)
        self.total_rows_label.setText(f"檔案中偵測到 {self.preview['total_rows']} 筆資料。")
        self.import_button.setEnabled(True)
        self.status(f"已載入 {self.selected_path.name} 的預覽")

    # -------------------------------------------------------------- 匯入

    def _start_import(self) -> None:
        if self.selected_path is None or self.import_task.running:
            return

        label = self.label_entry.get() or None
        self.import_button.setEnabled(False)
        self.choose_button.setEnabled(False)
        self.import_status.setText("讀取檔案中...")
        self.app.status_bar.start_progress()
        self.status("匯入中...", "normal")

        self.import_task.start(self.selected_path, label)

    def _on_progress(self, payload: dict[str, Any]) -> None:
        self.import_status.setText(f"{payload.get('stage', '')}...")

    def _on_done(self, summary: Any) -> None:
        self._finish_import()
        # 真的寫入資料庫了：通知其他頁面（公司/聯絡人/儀表板）下次顯示要重查。
        bump_data_version()
        unmapped = "、".join(summary.unmapped_columns) if summary.unmapped_columns else "無"
        self.summary_label.setText(
            f"檔案：{summary.file}\n"
            f"已讀取筆數：{summary.rows_read}\n"
            f"新增：{summary.records_new}    合併：{summary.records_merged}\n"
            f"重複：{summary.records_duplicate}    無效：{summary.records_invalid}\n"
            f"沒有固定欄位可對應：{unmapped}\n"
            "（這些會以原本的欄位名稱保留，在公司的「詳細資料」裡看得到）"
        )
        self.import_status.setText("匯入完成。")
        self.status("匯入完成", "success")
        self._maybe_start_completion(summary)

    def _on_error(self, exc: Exception) -> None:
        self._finish_import()
        self.import_status.setText("匯入失敗。")
        self.report_error(exc)

    def _finish_import(self) -> None:
        self.import_button.setEnabled(True)
        self.choose_button.setEnabled(True)
        self.app.status_bar.stop_progress()

    # --------------------------------------------------------- 匯入後補齊

    def _save_auto_complete(self, checked: bool) -> None:
        """把勾選狀態記到 user_settings.yaml，下次開程式還記得。

        存不起來不該讓使用者以為自己沒勾到——這一次的選擇仍然算數（真正
        決定要不要跑的是勾選框當下的狀態，不是設定檔），所以只提示，
        不把勾勾取消掉。
        """
        try:
            save_user_setting("completion", "auto_after_import", checked)
        except CRMError as exc:
            self.status(f"這次仍會照做，但設定存不起來：{exc}", "warning")

    def _maybe_start_completion(self, summary: Any) -> None:
        if not self.auto_complete_check.isChecked():
            return
        company_ids = list(getattr(summary, "company_ids", []) or [])
        if not company_ids:
            return
        if self.completion_task.running:
            # 上一批還在補。硬插進去會有兩條執行緒同時寫同一個資料庫，
            # 而且進度顯示會互相蓋掉。照實說一聲就好。
            self.import_status.setText(
                "匯入完成。上一批的自動補齊還在跑，這一批請到「爬取」頁"
                "按「補齊公司資料」。"
            )
            return

        limit = get_config().completion.auto_after_import_limit
        targets = company_ids[:limit]
        # 記下來，等補齊跑完再講一次。開跑時那一句會被完成訊息蓋掉，使用者
        # 只會在畫面上看到它閃一下——「有 N 家沒被處理」是他必須知道、而且
        # 之後要自己去補的事，不能只出現在一個會消失的位置。
        self._completion_trimmed = len(company_ids) - len(targets)

        self.import_button.setEnabled(False)
        self.choose_button.setEnabled(False)
        self.app.status_bar.start_progress()
        self.import_status.setText(
            f"匯入完成，開始自動補齊 {len(targets)} 家的資料..."
            + (
                f"（超過上限，另外 {self._completion_trimmed} 家未處理）"
                if self._completion_trimmed
                else ""
            )
        )
        self.status("自動補齊中...", "normal")
        self.completion_task.start(company_ids=targets)

    def _on_completion_progress(self, payload: dict[str, Any]) -> None:
        self.import_status.setText(
            f"補齊中 [{payload.get('done', 0)}/{payload.get('total', 0)}] "
            f"{payload.get('name', '')}"
        )

    def _on_completion_done(self, summary: Any) -> None:
        self._finish_completion()
        bump_data_version()

        detail = "、".join(
            f"{field_label(name)} {count}"
            for name, count in sorted(summary.filled.items())
        )
        lines = [
            f"自動補齊完成：處理 {summary.considered} 家，更新 {summary.updated} 家，"
            f"補上 {summary.fields_filled} 個欄位。"
        ]
        if detail:
            lines.append(detail)
        if self._completion_trimmed:
            lines.append(
                f"另外 {self._completion_trimmed} 家超過單次自動補齊的上限沒有處理，"
                "請到「爬取」頁按「補齊公司資料」。"
            )
        if summary.rejected_unconfirmed:
            lines.append(
                f"{summary.rejected_unconfirmed} 家搜尋到的網頁沒有提到該公司，"
                "沒有採用——寧可留白也不要存錯的網址。"
            )
        if summary.search_stopped:
            lines.append(f"搜尋中途停止：{summary.search_stopped}")

        self.summary_label.setText(self.summary_label.text() + "\n\n" + "\n".join(lines))
        self.import_status.setText(lines[0])
        self.status(
            f"補齊完成，更新 {summary.updated} 家",
            "warning" if summary.search_stopped else "success",
        )

    def _on_completion_error(self, exc: Exception) -> None:
        self._finish_completion()
        # 匯入本身已經成功了，這一段失敗不該讓使用者以為資料沒進去。
        self.import_status.setText("匯入已完成，但自動補齊失敗。")
        self.report_error(exc)

    def _finish_completion(self) -> None:
        self.import_button.setEnabled(True)
        self.choose_button.setEnabled(True)
        self.app.status_bar.stop_progress()
