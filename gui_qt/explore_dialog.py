"""「在整個網站裡找名錄」的結果視窗。

探索本身在 :mod:`crawler.explore`；這裡只負責把找到的候選列給使用者看，
並讓他挑一個帶回精靈繼續。

為什麼是獨立的視窗而不是直接把最高分的那一個填進去：評分只是猜測。
一個網站可能同時有「會員名錄」與「得獎廠商」兩份清單，哪一份才是使用者
要的，只有他自己知道。把筆數、頁數與**實際抓到的前三個名稱**擺出來，
是讓他一眼判斷的最短路徑。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gui_qt import theme

COLUMNS = ("網址", "型態", "每頁筆數", "頁數", "像公司名", "抓到的前幾筆")

#: 窄欄的欄位索引。這些照內容縮到最小，把寬度留給網址與範例。
_NARROW_COLUMNS = (1, 2, 3, 4)

#: 候選是檔案時顯示的型態。使用者要分得出「這是一個網頁」還是「這是一個
#: 掛在網站上的 PDF」——後者選了之後行為不一樣。
_KIND_LABELS = {"pdf": "PDF", "excel": "Excel", "word": "Word"}


class ExploreResultsDialog(QDialog):
    """列出探索到的名錄頁，讓使用者挑一個。"""

    def __init__(self, parent: QWidget | None, result: Any) -> None:
        super().__init__(parent)
        self.setWindowTitle("在這個網站找到的名錄")
        self.setModal(True)
        self.resize(900, theme.text_box_height(26))
        self._result = result
        self.chosen_url: str | None = None

        layout = QVBoxLayout(self)

        heading = QLabel(
            f"讀了 {result.pages_fetched} 頁，找到 {len(result.candidates)} 個看起來像"
            "廠商名錄的頁面。選一個，精靈會接著分析它。"
        )
        heading.setWordWrap(True)
        layout.addWidget(heading)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        # 網址與範例可長可短，讓它們吃掉剩下的寬度；數字欄照內容縮到最小，
        # 免得「頁數」佔掉半個視窗、網址反而被截斷。
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in _NARROW_COLUMNS:
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)

        self._fill()
        # 只接 activated：一次雙擊會同時送出 doubleClicked 與 activated，
        # 兩個都接的話這個視窗會關掉之後又自己開一次。
        self.table.activated.connect(lambda _index: self._accept_selection())

        for note in result.notes:
            label = QLabel(note)
            label.setObjectName("MutedLabel")
            label.setWordWrap(True)
            layout.addWidget(label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("分析這一個")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._accept_selection)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _fill(self) -> None:
        for candidate in self._result.candidates:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = (
                candidate.url,
                _KIND_LABELS.get(candidate.kind, "網頁"),
                str(candidate.item_count),
                str(candidate.page_count) if candidate.page_count > 1 else "1",
                f"{candidate.company_name_ratio:.0%}",
                "、".join(candidate.sample_names[:3]),
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setToolTip(text)          # 欄寬不夠時滑過去仍看得到全文
                if column in _NARROW_COLUMNS[1:]:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self.table.setItem(row, column, item)
        if self.table.rowCount():
            self.table.selectRow(0)            # 分數最高的預先選起來
        self.table.resizeRowsToContents()

    def _accept_selection(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        self.chosen_url = self._result.candidates[row].url
        self.accept()
