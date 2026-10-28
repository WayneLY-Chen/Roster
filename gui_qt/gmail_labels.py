"""郵件頁的「Gmail 標籤」區塊：讓 Gmail 標籤跟名單匠的分類互通。

三件事，都是「預覽 → 勾選 → 套用」：

* 分類現有公司：Gmail 標籤裡的往來對象是名單裡的公司，就貼上同名標籤
* 從標籤挖新公司：讀那個標籤的信件簽名檔，存進名單時貼上同名標籤
* 寫回 Gmail：把名單匠的分類與狀態貼到 Gmail（只新增標籤，放在「名單匠/」底下）

後端是 :class:`controllers.labels.GmailLabelController`，網頁版用的也是它。
只有一個背景工作：同時只跑一件跟 Gmail 有關的事，不會兩條連線搶著寫。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from controllers.labels import GmailLabelController
from core.config import AppConfig
from gui_qt.pages.base import bump_data_version
from gui_qt.tasks import BackgroundTask
from gui_qt.widgets import CHECK_KEY, DataTable, Section

CLASSIFY_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("company_name", "公司名稱", 220),
    ("address", "對上的地址", 220),
    ("state", "狀態", 140),
)
HARVEST_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("company_name", "公司名稱", 220),
    ("person", "聯絡人", 100),
    ("email", "信箱", 200),
    ("action", "會做什麼", 180),
)
SYNC_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("label", "Gmail 標籤", 220),
    ("companies", "公司", 80),
    ("messages", "信件", 80),
)


class GmailLabelSection(Section):
    """``page`` 是郵件頁，借用它的狀態列與錯誤處理。"""

    def __init__(self, page: Any, config: AppConfig) -> None:
        super().__init__("Gmail 標籤")
        self.page = page
        self.config = config
        self._then: Callable[[Any], None] | None = None
        self._classify_scan = None
        self._harvest_scan = None
        self._harvest_label = ""
        self.task = BackgroundTask(
            self, _call, on_progress=self._on_progress, on_done=self._on_done, on_error=self._on_error
        )
        self._build()

    # ------------------------------------------------------------- 版面

    def _build(self) -> None:
        intro = QLabel(
            "讓 Gmail 的標籤跟名單匠的分類互通。每一步都是先預覽、你看過再按下去才會寫入。"
            "寫回 Gmail 時<b>只會新增標籤</b>：不刪信、不搬信、不改已讀，"
            "全部放在「名單匠/」底下，不要了在 Gmail 裡把那一層刪掉就乾淨。"
        )
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setObjectName("MutedLabel")
        intro.setWordWrap(True)
        self.body_layout.addWidget(intro)

        row = QHBoxLayout()
        self.load_button = QPushButton("讀取我的 Gmail 標籤")
        self.load_button.clicked.connect(self._load_labels)
        row.addWidget(self.load_button)
        self.label_combo = QComboBox()
        self.label_combo.setMinimumWidth(180)
        self.label_combo.setPlaceholderText("先讀取標籤")
        self.label_combo.setEnabled(False)
        row.addWidget(self.label_combo)
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(10, 2000)
        self.limit_spin.setSingleStep(50)
        self.limit_spin.setPrefix("最多看 ")
        self.limit_spin.setSuffix(" 封")
        self.limit_spin.setValue(200)
        row.addWidget(self.limit_spin)
        row.addStretch(1)
        self.body_layout.addLayout(row)

        self.status_label = QLabel("")
        self.status_label.setObjectName("MutedLabel")
        self.status_label.setWordWrap(True)
        self.body_layout.addWidget(self.status_label)

        tabs = QTabWidget()
        tabs.addTab(self._build_classify_tab(), "分類現有公司")
        tabs.addTab(self._build_harvest_tab(), "從標籤挖新公司")
        tabs.addTab(self._build_sync_tab(), "寫回 Gmail")
        self.body_layout.addWidget(tabs)

        self.label_combo.currentTextChanged.connect(self._refresh_buttons)
        self._refresh_buttons()

    def _tab(self, note: str, *controls: QWidget) -> tuple[QWidget, QVBoxLayout]:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        caption = QLabel(note)
        caption.setObjectName("MutedLabel")
        caption.setWordWrap(True)
        layout.addWidget(caption)
        row = QHBoxLayout()
        for control in controls:
            row.addWidget(control)
        row.addStretch(1)
        layout.addLayout(row)
        return tab, layout

    def _build_classify_tab(self) -> QWidget:
        self.classify_scan_button = QPushButton("預覽")
        self.classify_scan_button.clicked.connect(self._scan_classify)
        self.classify_apply_button = QPushButton("貼上標籤")
        self.classify_apply_button.clicked.connect(self._apply_classify)
        tab, layout = self._tab(
            "看選好的標籤裡的信，寄件者或收件者是名單裡的公司，就替它貼上同名的標籤。原本的標籤都會留著。",
            self.classify_scan_button, self.classify_apply_button,
        )
        self.classify_table = DataTable(CLASSIFY_COLUMNS, min_rows=3, checkable=True)
        layout.addWidget(self.classify_table)
        return tab

    def _build_harvest_tab(self) -> QWidget:
        self.harvest_scan_button = QPushButton("預覽")
        self.harvest_scan_button.clicked.connect(self._scan_harvest)
        self.harvest_apply_button = QPushButton("存進名單並貼上標籤")
        self.harvest_apply_button.clicked.connect(self._apply_harvest)
        tab, layout = self._tab(
            "從選好的標籤裡的信件簽名檔挖出公司與聯絡人，存進名單時自動貼上同名的標籤。",
            self.harvest_scan_button, self.harvest_apply_button,
        )
        self.harvest_table = DataTable(HARVEST_COLUMNS, min_rows=3, checkable=True)
        layout.addWidget(self.harvest_table)
        return tab

    def _build_sync_tab(self) -> QWidget:
        self.sync_tags_check = QCheckBox("依公司分類（例如「名單匠/客戶」）")
        self.sync_tags_check.setChecked(True)
        self.sync_status_check = QCheckBox("依狀態（已回覆、退信、不再聯絡）")
        self.sync_status_check.setChecked(True)
        self.sync_scan_button = QPushButton("預覽")
        self.sync_scan_button.clicked.connect(lambda: self._sync(apply=False))
        self.sync_apply_button = QPushButton("貼到 Gmail")
        self.sync_apply_button.clicked.connect(self._confirm_sync)
        tab, layout = self._tab(
            "替跟每家公司往來過的信貼上 Gmail 標籤。這一段不用先選上面的標籤。",
            self.sync_tags_check, self.sync_status_check,
        )
        row = QHBoxLayout()
        row.addWidget(self.sync_scan_button)
        row.addWidget(self.sync_apply_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.sync_table = DataTable(SYNC_COLUMNS, min_rows=3)
        layout.addWidget(self.sync_table)
        return tab

    # ------------------------------------------------------------- 背景工作

    def _run(self, message: str, work: Callable[..., Any], then: Callable[[Any], None]) -> None:
        if self.task.running:
            return
        self._then = then
        self.status_label.setText(message)
        self.task.start(work)
        self._refresh_buttons()

    def _controller(self) -> GmailLabelController:
        # 每次都建新的：使用者剛在設定頁改完 Gmail 密碼時，不能拿舊的去登入。
        return GmailLabelController(self.config)

    def _on_progress(self, message: object) -> None:
        self.status_label.setText(str(message))

    def _on_done(self, result: Any) -> None:
        then, self._then = self._then, None
        if then is not None:
            then(result)
        self._refresh_buttons()

    def _on_error(self, exc: Exception) -> None:
        self._then = None
        self.status_label.setText(str(exc).splitlines()[0])
        self.page.report_error(exc)
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        busy = self.task.running
        has_label = bool(self.label_combo.currentText())
        self.load_button.setEnabled(not busy)
        self.classify_scan_button.setEnabled(not busy and has_label)
        self.harvest_scan_button.setEnabled(not busy and has_label)
        self.classify_apply_button.setEnabled(not busy and self._classify_scan is not None)
        self.harvest_apply_button.setEnabled(not busy and self._harvest_scan is not None)
        self.sync_scan_button.setEnabled(not busy)
        self.sync_apply_button.setEnabled(not busy and self.sync_table.model.rowCount() > 0)

    def _say(self, text: str, tone: str = "normal") -> None:
        self.status_label.setText(text)
        self.page.status(text, tone)

    # ------------------------------------------------------------- 標籤清單

    def _load_labels(self) -> None:
        self._run("正在連你的 Gmail…", lambda **kw: self._controller().labels(**kw), self._on_labels)

    def _on_labels(self, names: list[str]) -> None:
        self.label_combo.clear()
        self.label_combo.addItems(names)
        self.label_combo.setEnabled(bool(names))
        self._say(f"讀到 {len(names)} 個標籤。" if names else "你的 Gmail 還沒有自己建的標籤。")

    # ------------------------------------------------------------- 分類現有公司

    def _scan_classify(self) -> None:
        label, limit = self.label_combo.currentText(), self.limit_spin.value()
        self._classify_scan = None
        self.classify_table.clear()
        self._run(
            f"正在看「{label}」…",
            lambda **kw: self._controller().scan_classify(label, limit, **kw),
            self._on_classify_scan,
        )

    def _on_classify_scan(self, scan) -> None:
        self._classify_scan = scan
        self.classify_table.set_rows(
            {
                "company_name": match.company_name,
                "address": match.address,
                "state": "已經有這個標籤" if match.already else "會貼上",
                CHECK_KEY: not match.already,
                "_index": index,
            }
            for index, match in enumerate(scan.matches)
        )
        self._say(scan.describe())

    def _apply_classify(self) -> None:
        scan = self._classify_scan
        chosen = [scan.matches[row["_index"]] for row in self.classify_table.checked_rows()]
        if not chosen:
            self._say("一筆都沒有勾", "warning")
            return
        self._run(
            "正在貼上標籤…",
            lambda **kw: self._controller().apply_classify(chosen, **kw),
            self._on_classify_applied,
        )

    def _on_classify_applied(self, added: int) -> None:
        self._classify_scan = None
        self.classify_table.clear()
        bump_data_version()
        self._say(f"{added} 家公司貼上了標籤。", "success")

    # ------------------------------------------------------------- 從標籤挖新公司

    def _scan_harvest(self) -> None:
        label, limit = self.label_combo.currentText(), self.limit_spin.value()
        self._harvest_scan = None
        self.harvest_table.clear()
        self._run(
            f"正在讀「{label}」的信件簽名檔…",
            lambda **kw: self._controller().scan_harvest(label, limit, **kw),
            lambda scan: self._on_harvest_scan(scan, label),
        )

    def _on_harvest_scan(self, scan, label: str) -> None:
        self._harvest_scan, self._harvest_label = scan, label
        self.harvest_table.set_rows(
            {
                "company_name": candidate.company_name,
                "person": candidate.person or "",
                "email": candidate.email or "",
                "action": candidate.action,
                CHECK_KEY: candidate.storable,
                "_index": index,
            }
            for index, candidate in enumerate(scan.candidates)
        )
        self._say(scan.describe())

    def _apply_harvest(self) -> None:
        scan, label = self._harvest_scan, self._harvest_label
        chosen = [scan.candidates[row["_index"]] for row in self.harvest_table.checked_rows()]
        chosen = [candidate for candidate in chosen if candidate.storable]
        if not chosen:
            self._say("一筆都沒有勾", "warning")
            return
        self._run(
            "正在存進名單…",
            lambda **kw: self._controller().apply_harvest(chosen, label, **kw),
            self._on_harvest_applied,
        )

    def _on_harvest_applied(self, summary) -> None:
        self._harvest_scan = None
        self.harvest_table.clear()
        bump_data_version()
        self._say(summary.describe(), "success")

    # ------------------------------------------------------------- 寫回 Gmail

    def _sync(self, apply: bool) -> None:
        tags, status = self.sync_tags_check.isChecked(), self.sync_status_check.isChecked()
        if not (tags or status):
            self._say("至少勾一種要貼的標籤。", "warning")
            return
        if not apply:
            self.sync_table.clear()
        self._run(
            "正在貼到 Gmail…" if apply else "正在找往來的信…",
            lambda **kw: self._controller().sync(tags, status, apply, **kw),
            self._on_synced,
        )

    def _confirm_sync(self) -> None:
        answer = QMessageBox.question(
            self,
            "要貼到 Gmail 嗎？",
            "預覽表裡的標籤會貼到你的 Gmail。\n\n只會新增標籤，不會刪信、搬信或改已讀。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._sync(apply=True)

    def _on_synced(self, result) -> None:
        if result.applied:
            self.sync_table.clear()
        else:
            self.sync_table.set_rows(
                {"label": c.label, "companies": c.companies, "messages": c.messages}
                for c in result.counts
            )
        self._say(result.describe(), "success" if result.applied else "normal")


def _call(work: Callable[..., Any], *, report: Any = None, cancel_event: Any = None) -> Any:
    """BackgroundTask 的 worker：執行 ``_run`` 交進來的那件工作。"""
    return work(report=report, cancel_event=cancel_event)
