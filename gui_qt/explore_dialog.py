"""「在整個網站裡找名錄」的結果視窗。

探索本身在 :mod:`crawler.explore`；這裡只負責把找到的候選列給使用者看，
並讓他挑要哪幾個。

為什麼是獨立的視窗而不是直接把最高分的那一個填進去：評分只是猜測。
一個網站可能同時有「會員名錄」與「得獎廠商」兩份清單，哪一份才是使用者
要的，只有他自己知道。把筆數、頁數與**實際抓到的前三個名稱**擺出來，
是讓他一眼判斷的最短路徑。

可以複選：一個網站常常有好幾份名錄（各縣市分會、各產業分類），一次只能挑
一個等於同一套流程要重跑五遍。勾起來的會一次全部加成來源。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gui_qt import theme

COLUMNS = ("要加入", "網址", "型態", "每頁筆數", "頁數", "像公司名", "抓到的前幾筆")

#: 勾選欄。
_CHECK_COLUMN = 0

#: 窄欄的欄位索引。這些照內容縮到最小，把寬度留給網址與範例。
_NARROW_COLUMNS = (0, 2, 3, 4, 5)

#: 靠右對齊的數字欄。
_NUMERIC_COLUMNS = (3, 4, 5)

#: 候選是檔案時顯示的型態。使用者要分得出「這是一個網頁」還是「這是一個
#: 掛在網站上的 PDF」——後者選了之後行為不一樣。
_KIND_LABELS = {"pdf": "PDF", "excel": "Excel", "word": "Word", "powerpoint": "PowerPoint"}


class ExploreResultsDialog(QDialog):
    """列出探索到的名錄頁，讓使用者挑一個或多個。"""

    def __init__(self, parent: QWidget | None, result: Any) -> None:
        super().__init__(parent)
        self.setWindowTitle("在這個網站找到的名錄")
        self.setModal(True)
        self.resize(940, theme.text_box_height(28))
        self._result = result
        self.chosen_urls: list[str] = []

        layout = QVBoxLayout(self)

        heading = QLabel(
            f"讀了 {result.pages_fetched} 頁，找到 {len(result.candidates)} 個看起來像"
            "廠商名錄的頁面。勾起來的會一次全部加成來源；只勾一個的話，"
            "精靈會接著分析它讓你微調。"
        )
        heading.setWordWrap(True)
        layout.addWidget(heading)

        layout.addLayout(self._build_select_bar())

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        # 網址與範例可長可短，讓它們吃掉剩下的寬度；數字欄照內容縮到最小，
        # 免得「頁數」佔掉半個視窗、網址反而被截斷。
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in _NARROW_COLUMNS:
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)

        self._fill()
        # 只接 activated：一次雙擊會同時送出 doubleClicked 與 activated，
        # 兩個都接的話這個視窗會關掉之後又自己開一次。
        self.table.activated.connect(self._on_activated)
        self.table.itemChanged.connect(lambda _item: self._sync_counts())

        for note in result.notes:
            label = QLabel(note)
            label.setObjectName("MutedLabel")
            label.setWordWrap(True)
            layout.addWidget(label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self._accept_selection)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._sync_counts()

    # ------------------------------------------------------------ 版面

    def _build_select_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()

        self.select_all_button = QPushButton("全選")
        self.select_all_button.clicked.connect(lambda: self._set_all(True))
        bar.addWidget(self.select_all_button)

        self.select_none_button = QPushButton("全部取消")
        self.select_none_button.clicked.connect(lambda: self._set_all(False))
        bar.addWidget(self.select_none_button)

        self.count_label = QLabel("")
        self.count_label.setObjectName("MutedLabel")
        bar.addWidget(self.count_label)

        bar.addStretch(1)
        return bar

    def _fill(self) -> None:
        for index, candidate in enumerate(self._result.candidates):
            row = self.table.rowCount()
            self.table.insertRow(row)

            check = QTableWidgetItem()
            check.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            # 只把最像的那一個先勾起來。全部預勾等於幫使用者做了他沒說要做的
            # 決定——按下確定就會多出十個來源。
            check.setCheckState(
                Qt.CheckState.Checked if index == 0 else Qt.CheckState.Unchecked
            )
            self.table.setItem(row, _CHECK_COLUMN, check)

            values = (
                candidate.url,
                _KIND_LABELS.get(candidate.kind, "網頁"),
                str(candidate.item_count),
                str(candidate.page_count) if candidate.page_count > 1 else "1",
                f"{candidate.company_name_ratio:.0%}",
                "、".join(candidate.sample_names[:3]),
            )
            for offset, text in enumerate(values):
                column = offset + 1
                item = QTableWidgetItem(text)
                item.setToolTip(text)          # 欄寬不夠時滑過去仍看得到全文
                if column in _NUMERIC_COLUMNS:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self.table.setItem(row, column, item)
        if self.table.rowCount():
            self.table.selectRow(0)            # 分數最高的預先選起來
        self.table.resizeRowsToContents()

    # ------------------------------------------------------------ 勾選

    def _set_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self.table.rowCount()):
            item = self.table.item(row, _CHECK_COLUMN)
            if item is not None:
                item.setCheckState(state)
        self._sync_counts()

    def _checked_rows(self) -> list[int]:
        rows = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, _CHECK_COLUMN)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                rows.append(row)
        return rows

    def _sync_counts(self) -> None:
        """按鈕文字要說出「按下去會發生什麼」，數量是其中最重要的一半。"""
        total = self.table.rowCount()
        chosen = len(self._checked_rows())
        self.count_label.setText(f"已勾選 {chosen} / {total} 個")

        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if chosen > 1:
            ok_button.setText(f"加入這 {chosen} 個")
        else:
            ok_button.setText("分析這一個")
        ok_button.setEnabled(chosen > 0)

    # ------------------------------------------------------------ 送出

    def _on_activated(self, index: Any) -> None:
        """雙擊某一列 ＝「我只要這一個」。"""
        row = index.row() if hasattr(index, "row") else self.table.currentRow()
        if row < 0:
            return
        self._set_all(False)
        item = self.table.item(row, _CHECK_COLUMN)
        if item is not None:
            item.setCheckState(Qt.CheckState.Checked)
        self._accept_selection()

    def _accept_selection(self) -> None:
        rows = self._checked_rows()
        if not rows:
            return
        self.chosen_urls = [self._result.candidates[row].url for row in rows]
        self.accept()

    @property
    def chosen_url(self) -> str | None:
        """第一個選到的網址。只挑一個時呼叫端不必去管清單。"""
        return self.chosen_urls[0] if self.chosen_urls else None
