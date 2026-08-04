"""聯絡人頁：橫跨所有公司、可搜尋的聯絡人列表。

對應 ``gui/pages/contacts.py``（Tk 版 ``ContactsPage``）。真實資料庫目前只有
4 位聯絡人，資料量遠低於公司頁的 215 筆，但 ``ContactRepository.search()``
一樣要對加密欄位（姓名、信箱、電話、手機）逐筆解密再比對、排序——這件事
SQL 做不到（見 ``database/repository.py`` 開頭的說明），一定是 Python 端整批
處理。行數雖小、機制跟公司頁一致，所以這裡沿用同一套模式：

    * 查詢一律經 ``gui_qt.tasks.BackgroundTask`` 背景執行，不在 ``refresh()``
      同步呼叫 controller——理由與 ``gui_qt/pages/companies.py``、
      ``gui_qt/pages/dashboard.py`` 檔頭說明的一致。
    * 不覆寫 ``on_show()``，讓 ``gui_qt/pages/base.py`` 的資料版本機制決定
      要不要真的重新查詢。
    * 搜尋框即時過濾，用 ``QTimer`` 做 debounce，避免每個按鍵都觸發一次查詢。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from core.errors import CRMError
from core.schemas import ContactView
from controllers.core import CompanyController, ContactController
from gui_qt.company_detail import CompanyDetailDialog
from gui_qt.pages.base import BasePage, bump_data_version
from gui_qt.tasks import BackgroundTask
from gui_qt.widgets import DataTable

#: 跟 Tk 版 ``gui/pages/contacts.py`` 的 COLUMNS 完全一致。
COLUMNS = [
    ("id", "編號", 50),
    ("name", "姓名", 160),
    ("title", "職稱", 130),
    ("company_name", "公司", 200),
    ("email", "電子信箱", 180),
    ("phone", "電話", 110),
    ("mobile", "手機", 110),
    ("is_primary", "主要聯絡人", 70),
]

SEARCH_LIMIT = 500
SEARCH_DEBOUNCE_MS = 300

#: ``_pending_text``/``_has_pending`` 需要區分「沒有排隊中的查詢」跟「排隊
#: 中的查詢條件剛好是空字串（=不篩選）」，兩者都可能是 ``None``/``""``，用
#: 一個獨立的 sentinel 比多帶一個布林旗標更不容易寫錯。
_NO_PENDING = object()


class ContactsPage(BasePage):
    title = "聯絡人"
    icon = "👤"

    def __init__(self, app: object) -> None:
        super().__init__(app)
        self.controller = ContactController()
        self.company_controller = CompanyController()
        self._rows: dict[int, ContactView] = {}
        self._pending_text: Any = _NO_PENDING

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
        title_label = QLabel("聯絡人")
        title_font = title_label.font()
        title_font.setPointSize(22)
        title_font.setBold(True)
        title_label.setFont(title_font)
        header.addWidget(title_label)
        header.addStretch(1)

        open_company_button = QPushButton("開啟公司")
        open_company_button.setObjectName("PrimaryButton")  # 這頁最主要的動作
        open_company_button.clicked.connect(self._open_company)
        header.addWidget(open_company_button)

        delete_button = QPushButton("刪除")
        delete_button.setObjectName("DangerButton")
        delete_button.clicked.connect(self._delete_selected)
        header.addWidget(delete_button)

        refresh_button = QPushButton("重新整理")
        refresh_button.clicked.connect(lambda checked=False: self.on_show(force=True))
        header.addWidget(refresh_button)

        outer.addLayout(header)

        search_bar = QHBoxLayout()
        self.search_entry = QLineEdit()
        self.search_entry.setPlaceholderText("搜尋聯絡人...")
        self.search_entry.textChanged.connect(self._schedule_search)
        self.search_entry.returnPressed.connect(self._run_search)
        search_bar.addWidget(self.search_entry, 1)

        search_button = QPushButton("搜尋")
        search_button.clicked.connect(self._run_search)
        search_bar.addWidget(search_button)

        outer.addLayout(search_bar)

        self.table = DataTable(columns=COLUMNS, on_activate=self._activate_row)
        outer.addWidget(self.table, 1)

        self.count_label = QLabel("")
        self.count_label.setObjectName("MutedLabel")
        outer.addWidget(self.count_label)

    # ------------------------------------------------------------- 生命週期

    def refresh(self) -> None:
        self._run_search()

    def on_hide(self) -> None:
        self._search_timer.stop()

    # ------------------------------------------------------------- 查詢（背景執行緒）

    def _schedule_search(self, _text: str = "") -> None:
        self._search_timer.start(SEARCH_DEBOUNCE_MS)

    def _run_search(self) -> None:
        self._search_timer.stop()
        text = self.search_entry.text().strip() or None
        if self._fetch_task.running:
            self._pending_text = text
            return
        self._fetch_task.start(text)

    def _fetch(self, text: str | None, *, report, cancel_event) -> list[ContactView]:
        """在背景執行緒被呼叫；千萬不能在這裡碰任何 widget。"""
        return self.controller.search(text, SEARCH_LIMIT)

    def _apply_result(self, contacts: list[ContactView]) -> None:
        self._rows = {contact.id: contact for contact in contacts}
        self.table.set_rows([self._to_row(contact) for contact in contacts])
        count = len(contacts)
        self.count_label.setText(f"共 {count} 位聯絡人")
        self.status(f"已載入 {count} 位聯絡人")
        self._start_pending_if_any()

    def _handle_error(self, exc: Exception) -> None:
        self.report_error(exc)
        self._start_pending_if_any()

    def _start_pending_if_any(self) -> None:
        if self._pending_text is _NO_PENDING:
            return
        text, self._pending_text = self._pending_text, _NO_PENDING
        self._fetch_task.start(text)

    @staticmethod
    def _to_row(contact: ContactView) -> dict[str, Any]:
        return {
            "id": contact.id,
            "name": contact.name,
            "title": contact.title,
            "company_name": contact.company_name,
            "email": contact.email,
            "phone": contact.phone,
            "mobile": contact.mobile,
            "is_primary": contact.is_primary,
        }

    # ------------------------------------------------------------- 動作

    def _activate_row(self, row: dict[str, Any]) -> None:
        self._open_company_dialog(row["id"])

    def _open_company(self) -> None:
        row = self.table.selected_row()
        if row is None:
            self.status("請先選擇一位聯絡人", "muted")
            return
        self._open_company_dialog(row["id"])

    def _open_company_dialog(self, contact_id: int) -> None:
        contact = self._rows.get(contact_id)
        if contact is None:
            return
        dialog = CompanyDetailDialog(
            self, self.company_controller, contact.company_id, on_saved=self._run_search
        )
        dialog.exec()

    def _delete_selected(self) -> None:
        row = self.table.selected_row()
        if row is None:
            self.status("請先選擇一位聯絡人", "muted")
            return
        reply = QMessageBox.question(self, "刪除聯絡人", f"確定要刪除「{row['name']}」嗎？")
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self.controller.delete(row["id"])
        except CRMError as exc:
            self.report_error(exc)
            return
        bump_data_version()
        self.status(f"已刪除 {row['name']}", "success")
        self._run_search()
