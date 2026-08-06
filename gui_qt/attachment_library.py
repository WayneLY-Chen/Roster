"""附件庫的管理視窗：加入、改名、寫備註、刪除。

## 為什麼要獨立成一個視窗

「郵件」頁的附件區塊只做一件事：勾選這次要帶哪幾個檔案。那是寄信流程的一
部分，應該保持精簡。而「這個檔案是什麼、什麼時候加的、寄過幾次、還要不要
留著」是另一件事——整理，不是寄信。硬塞進郵件頁只會讓那一頁變成什麼都有
一點的雜物櫃。

## 刪除為什麼要先問過排程

自動排程可以指定隨信附件（``scheduler.mail_attachments``）。刪掉排程正在用
的檔案，後果是排程在半夜三點失敗，而且沒有人會在當下看到錯誤訊息——等到
發現時已經漏寄好幾天。所以刪除前先查一次，被引用的話要額外警告。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.errors import CRMError
from controllers.mail import MailController
from gui_qt.widgets import DataTable, ErrorBanner, caption

COLUMNS = [
    ("label", "顯示名稱", 180),
    ("name", "檔名", 200),
    ("size", "大小", 80),
    ("added_at", "加入時間", 130),
    ("used", "使用狀況", 110),
    ("note", "備註", 200),
]


class AttachmentLibraryDialog(QDialog):
    """管理 ``attachments/`` 裡的檔案。"""

    def __init__(self, parent: QWidget | None, controller: MailController) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("管理附件")
        self.resize(920, 520)
        self.setMinimumSize(720, 420)
        self.setModal(True)

        outer = QVBoxLayout(self)

        intro = QLabel(
            "這裡的檔案存在專案的 attachments/ 資料夾，可以長期放著重複使用。"
            "要不要隨這次的信寄出去，是在「郵件」頁用勾選決定的。"
        )
        intro.setObjectName("MutedLabel")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        self.table = DataTable(columns=COLUMNS, min_rows=8)
        outer.addWidget(self.table, 1)

        self.summary = caption("")
        outer.addWidget(self.summary)

        buttons = QHBoxLayout()

        add_button = QPushButton("加入檔案…")
        add_button.setObjectName("PrimaryButton")
        add_button.clicked.connect(self._add)
        buttons.addWidget(add_button)

        rename_button = QPushButton("改顯示名稱")
        rename_button.clicked.connect(self._rename)
        buttons.addWidget(rename_button)

        note_button = QPushButton("編輯備註")
        note_button.clicked.connect(self._edit_note)
        buttons.addWidget(note_button)

        delete_button = QPushButton("刪除")
        delete_button.setObjectName("DangerButton")
        delete_button.clicked.connect(self._delete)
        buttons.addWidget(delete_button)

        folder_button = QPushButton("開啟資料夾")
        folder_button.clicked.connect(self._open_folder)
        buttons.addWidget(folder_button)

        buttons.addStretch(1)
        close_button = QPushButton("關閉")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button, 0, Qt.AlignmentFlag.AlignBottom)
        outer.addLayout(buttons)

        self.error_label = ErrorBanner()
        outer.addWidget(self.error_label)

        self.refresh()

    # ------------------------------------------------------------ 資料

    def refresh(self) -> None:
        try:
            items = self.controller.attachments()
        except CRMError as exc:
            self.error_label.show_error(exc)
            return

        self.table.set_rows(
            [
                {
                    "label": item.display_name,
                    "name": item.name,
                    "size": item.human_size,
                    "added_at": item.added_at.strftime("%Y-%m-%d %H:%M"),
                    "used": item.status_text,
                    "note": item.note,
                }
                for item in items
            ]
        )

        from gmail.attachments import human_size

        total = sum(item.size_bytes for item in items if item.exists)
        missing = [item for item in items if not item.exists]
        text = f"共 {len(items)} 個附件，合計 {human_size(total)}。"
        if missing:
            # 檔案被外部刪掉時保留紀錄而不是自動清掉——那一筆帶著使用者
            # 自己打的顯示名稱與備註，為了一個可能是暫時的狀況丟掉不合理。
            text += f"　其中 {len(missing)} 個檔案已經不在資料夾裡，可以直接刪除該筆紀錄。"
        self.summary.setText(text)

    def _selected_name(self) -> str | None:
        row = self.table.selected_row()
        if row is None:
            self.error_label.setText("請先選一個附件。")
            return None
        self.error_label.setText("")
        return row["name"]

    # ------------------------------------------------------------ 動作

    def _add(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "選擇要加入的檔案", "", "所有檔案 (*.*)")
        if not paths:
            return
        added = 0
        for path in paths:
            try:
                self.controller.add_attachment(path)
                added += 1
            except CRMError as exc:
                self.error_label.show_error(exc)
        if added:
            self.error_label.show_note(f"已加入 {added} 個檔案。")
        self.refresh()

    def _rename(self) -> None:
        name = self._selected_name()
        if name is None:
            return
        current = next(
            (i.label for i in self.controller.attachments() if i.name == name), ""
        )
        label, ok = QInputDialog.getText(
            self,
            "改顯示名稱",
            f"「{name}」在介面上要顯示成什麼？\n（留空就顯示原本的檔名，檔案本身不會被改名）",
            text=current,
        )
        if not ok:
            return
        try:
            self.controller.update_attachment(name, label=label)
        except CRMError as exc:
            self.error_label.show_error(exc)
            return
        self.refresh()

    def _edit_note(self) -> None:
        name = self._selected_name()
        if name is None:
            return
        current = next(
            (i.note for i in self.controller.attachments() if i.name == name), ""
        )
        note, ok = QInputDialog.getText(
            self, "編輯備註", f"「{name}」的備註：", text=current
        )
        if not ok:
            return
        try:
            self.controller.update_attachment(name, note=note)
        except CRMError as exc:
            self.error_label.show_error(exc)
            return
        self.refresh()

    def _delete(self) -> None:
        name = self._selected_name()
        if name is None:
            return

        warning = ""
        if self.controller.attachment_used_by_schedule(name):
            warning = (
                "\n\n⚠ 這個附件正被「自動排程」引用。刪掉之後，排程寄信會在"
                "執行的當下失敗——而那通常是半夜，不會有人立刻發現。\n"
                "請記得到「設定」頁的自動排程把它取消勾選。"
            )

        reply = QMessageBox.question(
            self,
            "刪除附件",
            f"確定要刪除「{name}」嗎？\n檔案會真的從硬碟移除，這個動作無法復原。{warning}",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            self.controller.remove_attachment(name)
        except CRMError as exc:
            self.error_label.show_error(exc)
            return
        self.error_label.show_note(f"已刪除 {name}。")
        self.refresh()

    def _open_folder(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.controller.attachments_dir())))
