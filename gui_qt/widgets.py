"""共用元件，對應 ``gui/widgets.py`` 的 Tk 元件。

給接手其餘 8 頁的人看的行為對照表：

    Tk（gui/widgets.py）        Qt（這個檔案）                用途
    --------------------------  -----------------------------  --------------------------------
    Section                     Section                        標題 + 內容區塊的卡片
    StatCard                    StatCard                       儀表板的單一數字卡
    DataTable（ttk.Treeview）   DataTable（QTableView + Model） 見下方「為什麼不用 QTableWidget」
    LabeledEntry                LabeledEntry                   說明文字疊在單行輸入框上面
    CaptionedControl            CaptionedControl               說明文字疊在任何一個控制項上面
    StatusBar                   StatusBar                       視窗最下面那條：訊息 + 不定進度條
    WrappingLabel                （不需要）                     直接用 ``QLabel.setWordWrap(True)``

``WrappingLabel`` 在 Tk 版存在，是因為 ``CTkLabel(wraplength=...)`` 的換行寬度
是建立時就寫死的猜測值，猜錯了要嘛裁字要嘛提早換行；Qt 的
``QLabel.setWordWrap(True)`` 本來就是照當下版面實際寬度換行，不需要另外補一層。

## 為什麼 DataTable 用 QTableView + QAbstractTableModel，不用 QTableWidget

``QTableWidget`` 每一格都是一個 ``QTableWidgetItem`` Python 物件；公司頁有
215+ 筆、7、8 欄，``set_rows()`` 一次呼叫就要 new 出上千個物件，且無論畫面上
看不看得到都會全部建出來——這正是 customtkinter 每個 widget 都要花 ~1ms
建置、換頁 200ms 的同一種問題，只是換了一個框架重犯。

``QAbstractTableModel`` 只保存資料本身（這裡是一份 ``list[dict]``），
``data()`` 是 view 要畫第幾格「當下」才會被呼叫一次，view 也只會替看得到
的那幾列建立顯示用的暫時物件，不會在 ``set_rows()`` 那一刻就把整張表具現化
成 Qt 物件。這就是本次遷移換頁能压到 5ms 等級的關鍵之一：不管表格有幾百筆，
填資料的成本只跟「畫面上看得到幾列」成正比，而不是跟總筆數成正比。

之後任何頁面的表格都必須走這個 ``DataTable``，不要另外建 ``QTableWidget``
或直接裸用 ``QTableView``。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from gui_qt import theme

#: Caption 字級，跟 Tk 版的 CAPTION_SIZE 一致，讓兩套介面的控制項高度看起來一樣。
CAPTION_SIZE = 12

#: DataTable 預設的單行高度（像素），跟 Tk 版 ttk.Treeview 的 rowheight=30 一致。
ROW_HEIGHT = 28


def _display(value: Any) -> str:
    """把一個儲存格的值轉成顯示字串，跟 gui/widgets.py 的 ``_display`` 一致。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


def _sort_value(value: Any) -> tuple[int, Any]:
    """排序鍵：空值排最後，數字用數值比較，其餘用小寫字串比較。"""
    if value is None or value == "":
        return (2, "")
    if isinstance(value, (int, float)):
        return (0, value)
    text = str(value)
    try:
        return (0, float(text))
    except ValueError:
        return (1, text.lower())


class Section(QFrame):
    """有標題的卡片，其他元件放進 ``body_layout`` 裡——對應 gui.widgets.Section。"""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Section")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(6)

        heading = QLabel(title)
        heading.setObjectName("SectionTitle")
        outer.addWidget(heading)

        #: 頁面把自己的內容加進這裡，不要直接加進 ``Section`` 本身的 layout。
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.body, 1)


class StatCard(QFrame):
    """單一數字加說明，用在儀表板——對應 gui.widgets.StatCard。"""

    def __init__(
        self, title: str, value: str = "-", hint: str = "", parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("StatCard")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(2)

        self._title = QLabel(title)
        self._title.setObjectName("MutedLabel")
        layout.addWidget(self._title)

        self._value = QLabel(value)
        value_font = self._value.font()
        value_font.setPointSize(20)
        value_font.setBold(True)
        self._value.setFont(value_font)
        layout.addWidget(self._value)

        self._hint = QLabel(hint)
        self._hint.setObjectName("MutedLabel")
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)

    def update_values(self, value: Any, hint: str | None = None) -> None:
        self._value.setText(str(value))
        if hint is not None:
            self._hint.setText(hint)

    @property
    def value_text(self) -> str:
        """目前顯示的數字文字。主要給測試用，頁面程式碼不需要讀回這個值。"""
        return self._value.text()

    @property
    def hint_text(self) -> str:
        """目前顯示的說明文字，理由同 :attr:`value_text`。"""
        return self._hint.text()


class DataTableModel(QAbstractTableModel):
    """:class:`DataTable` 背後的資料模型。列永遠是純 dict，不是 Qt 物件。

    ``columns`` 是 ``(key, heading, width)`` 的序列，跟 Tk 版 ``DataTable``
    的參數形狀完全一樣——頁面從 Tk 換成 Qt 時，呼叫 ``set_rows()`` 的方式不必改。
    """

    def __init__(self, columns: Sequence[tuple[str, str, int]], parent: Any = None) -> None:
        super().__init__(parent)
        self._keys = [key for key, _, _ in columns]
        self._headings = [heading for _, heading, _ in columns]
        self._rows: list[dict[str, Any]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._keys)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            row = self._rows[index.row()]
            return _display(row.get(self._keys[index.column()]))
        return None

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self._headings[section]
        return str(section + 1)

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        """點表頭排序時 Qt 會呼叫這個（``QTableView.setSortingEnabled(True)``）。"""
        if not self._rows:
            return
        key = self._keys[column]
        self.layoutAboutToBeChanged.emit()
        self._rows.sort(
            key=lambda row: _sort_value(row.get(key)),
            reverse=order == Qt.SortOrder.DescendingOrder,
        )
        self.layoutChanged.emit()

    # ---------------------------------------------------- 給頁面用的資料存取

    def set_rows(self, rows: Sequence[dict[str, Any]]) -> None:
        """整批換掉表格內容。``rows`` 是以欄位 key 為鍵的 dict 序列。"""
        self.beginResetModel()
        self._rows = [dict(row) for row in rows]
        self.endResetModel()

    def row_at(self, row_index: int) -> dict[str, Any]:
        return self._rows[row_index]

    def row_count(self) -> int:
        return len(self._rows)


class DataTable(QWidget):
    """可排序、可捲動的表格，包著 :class:`DataTableModel`。

    ``on_select``/``on_activate`` 收到的是列的 dict（跟 Tk 版一樣），不是
    Qt 的 ``QModelIndex``——頁面程式碼原本怎麼處理一列資料，搬過來不用改。
    """

    def __init__(
        self,
        columns: Sequence[tuple[str, str, int]],
        on_select: Callable[[dict[str, Any]], None] | None = None,
        on_activate: Callable[[dict[str, Any]], None] | None = None,
        selectmode: str = "browse",
        min_rows: int = 3,
        parent: QWidget | None = None,
    ) -> None:
        """``selectmode``："browse"（單選，預設）或 "extended"（可複選）。

        ``min_rows`` 只設表格的最小高度，不設上限——跟 Tk 版
        ``ttk.Treeview(height=min_rows)`` 的用意一樣：表格仍然會撐滿外層
        ``Section`` 給它的空間，這裡只是避免視窗太矮時被壓縮到看不見幾列。
        """
        super().__init__(parent)
        self.on_select = on_select
        self.on_activate = on_activate

        self.model = DataTableModel(columns)
        self.view = QTableView(self)
        self.view.setModel(self.model)
        self.view.setAlternatingRowColors(True)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
            if selectmode == "extended"
            else QAbstractItemView.SelectionMode.SingleSelection
        )
        self.view.setSortingEnabled(True)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.verticalHeader().setVisible(False)
        self.view.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.view.horizontalHeader().setStretchLastSection(True)
        self.view.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        self.view.setMinimumHeight(min_rows * ROW_HEIGHT + self.view.horizontalHeader().height())

        for index, (_, _, width) in enumerate(columns):
            self.view.setColumnWidth(index, width)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)

        self.view.selectionModel().currentRowChanged.connect(self._handle_select)
        # 只接 activated，不要同時接 doubleClicked。
        #
        # 一次雙擊會**同時**送出 doubleClicked 與 activated（實測過），兩個都
        # 接的話 on_activate 會被呼叫兩次——使用者看到的是「對話框關掉之後
        # 又自己開一次」，而且每一個用雙擊開啟的視窗都會這樣。
        #
        # 留 activated 而不是 doubleClicked：它同時涵蓋雙擊與鍵盤 Enter，
        # 不用鍵盤的人跟用鍵盤的人都能開得起來。
        self.view.activated.connect(self._handle_activate)

    # ---------------------------------------------------------------- 資料

    def set_rows(self, rows: Sequence[dict[str, Any]]) -> None:
        self.model.set_rows(rows)

    def selected_rows(self) -> list[dict[str, Any]]:
        rows_by_index = {
            index.row(): self.model.row_at(index.row())
            for index in self.view.selectionModel().selectedRows()
        }
        return [rows_by_index[key] for key in sorted(rows_by_index)]

    def selected_row(self) -> dict[str, Any] | None:
        rows = self.selected_rows()
        return rows[0] if rows else None

    def row_count(self) -> int:
        return self.model.row_count()

    def clear(self) -> None:
        self.set_rows([])

    # ------------------------------------------------------------- 事件

    def _handle_select(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if self.on_select and current.isValid():
            self.on_select(self.model.row_at(current.row()))

    def _handle_activate(self, index: QModelIndex) -> None:
        if self.on_activate and index.isValid():
            self.on_activate(self.model.row_at(index.row()))


def caption(text: str = "") -> QLabel:
    """一個控制項的說明文字標籤，用共用的 caption 字級。"""
    label = QLabel(text)
    label.setObjectName("MutedLabel")
    font = label.font()
    font.setPointSize(CAPTION_SIZE)
    label.setFont(font)
    return label


class WideComboBox(QComboBox):
    """收合時看得到完整的選項文字，展開時的清單也不會被切掉。

    兩個都是 Qt 的預設行為造成的，而且成因不同：

    **收合狀態**：預設的 ``AdjustToContentsOnFirstShow`` 只在第一次顯示時
    量一次寬度。這支程式的篩選下拉框建立時只有「全部」一個選項，真正的
    產業／標籤是稍後由背景查詢填進去的——寬度早就照「全部」兩個字定死了，
    於是「新名單」被截成「新名」。改成 ``AdjustToContents``，內容一變就
    重算。

    **展開狀態**：Qt 讓彈出清單跟下拉框一樣寬。下拉框本來就窄的話，
    「電子零組件」會顯示成「電...件」，使用者根本看不出自己在選什麼。

    兩邊都設上限，免得一個特別長的產業名把整列版面撐爆。
    """

    #: 收合狀態的寬度上限，以「0」的字寬為單位。
    #:
    #: 不能寫死像素。macOS 的系統介面字比 Windows 大一號，同樣的 240px 在
    #: Windows 上綽綽有餘，到了 Mac 就會少掉最後一兩個字。
    MAX_WIDTH_DIGITS = 24
    #: 彈出清單的寬度上限，同樣以字寬為單位。
    MAX_POPUP_WIDTH_DIGITS = 42

    #: 下拉箭頭、外框與左右內距要預留的空間，一樣以字寬為單位。
    #:
    #: Qt 自己算的 sizeHint 在 macOS 上會少幾個 px——原生樣式的箭頭區比它
    #: 預期的寬，而全形括號「（）」又比 Qt 的字寬估計值再寬一些。少個兩三 px
    #: 的結果，就是「（全部啟用）」顯示成「（全部啟用」。
    _CHROME_DIGITS = 5

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # 內容變了就重新量寬度，不是只量第一次。
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)

    def _digit(self) -> int:
        return max(QFontMetrics(self.font()).horizontalAdvance("0"), 1)

    def max_width(self) -> int:
        return self._digit() * self.MAX_WIDTH_DIGITS

    def _widest_item(self) -> int:
        metrics = QFontMetrics(self.font())
        return max(
            (metrics.horizontalAdvance(self.itemText(i)) for i in range(self.count())),
            default=0,
        )

    def _needed_width(self) -> int:
        chrome = self._digit() * self._CHROME_DIGITS
        return min(self._widest_item() + chrome, self.max_width())

    def sizeHint(self):  # noqa: N802 - Qt 的覆寫方法命名
        hint = super().sizeHint()
        hint.setWidth(max(hint.width(), self._needed_width()))
        return hint

    def minimumSizeHint(self):  # noqa: N802 - Qt 的覆寫方法命名
        # 版面在空間不足時會縮到 minimumSizeHint。預設值只夠顯示幾個字，
        # 於是文字被切掉——而使用者看不出自己選的是什麼。寧可讓這一列擠一點。
        hint = super().minimumSizeHint()
        hint.setWidth(max(hint.width(), self._needed_width()))
        return hint

    def showPopup(self) -> None:  # noqa: N802 - Qt 的覆寫方法命名
        # 加上捲軸與左右內距的餘裕；不足下拉框本身寬度時就沿用下拉框的寬度。
        limit = self._digit() * self.MAX_POPUP_WIDTH_DIGITS
        needed = min(self._widest_item() + self._digit() * 6, limit)
        self.view().setMinimumWidth(max(needed, self.width()))
        super().showPopup()


def inline_caption(text: str = "") -> QLabel:
    """跟旁邊的輸入框「同一列」的說明文字，例如「單次最多 [50] 封」。

    跟 :func:`caption` 的差別只有高度：這個標籤會被撐成跟 QLineEdit／
    QSpinBox 一樣高，文字在裡面垂直置中。

    為什麼需要：一列裡如果有 LabeledEntry（說明在上、輸入框在下，兩行高），
    那一列的高度就是兩行。普通的 QLabel 會被擺在那一列的**垂直中央**，
    而旁邊的輸入框靠下——文字看起來就浮在半空中，跟輸入框沒有對齊。
    給標籤一個跟控制項等高的框，再讓整個框靠下對齊，兩邊的文字就會落在
    同一條基線上。
    """
    from gui_qt import theme

    label = caption(text)
    label.setFixedHeight(theme.control_height())
    label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    return label


class CaptionedControl(QWidget):
    """說明文字疊在任何一個控制項上面，兩者當成一個 widget 使用。

    對應 gui.widgets.CaptionedControl：``attach()`` 之後把要疊的控制項放進來。
    """

    def __init__(self, label_text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(caption(label_text))
        self.control: QWidget | None = None

    def attach(self, control: QWidget) -> None:
        self.control = control
        self.layout().addWidget(control)


class LabeledEntry(QWidget):
    """說明文字疊在單行輸入框上面，這支專案每個表單都用這個排版。"""

    def __init__(
        self,
        label_text: str,
        value: str = "",
        placeholder: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(caption(label_text))

        self.entry = QLineEdit(value)
        self.entry.setPlaceholderText(placeholder)
        layout.addWidget(self.entry)

    def get(self) -> str:
        return self.entry.text().strip()

    def set(self, value: str | None) -> None:
        self.entry.setText(value or "")


class StatusBar(QWidget):
    """視窗最下面那條：左邊一則訊息，右邊一個可隱藏的不定進度條。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("StatusBar")
        self.setFixedHeight(theme.status_bar_height())

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 4)

        self.message = QLabel("Ready")
        layout.addWidget(self.message, 1)

        self.progress = QProgressBar()
        self.progress.setFixedWidth(180)
        self.progress.setRange(0, 0)  # 不定進度：跟 Tk 版 mode="indeterminate" 一樣
        self.progress.hide()
        layout.addWidget(self.progress)

    def set_message(self, text: str, tone: str = "normal") -> None:
        """``tone``："normal" | "error" | "warning" | "success" | "muted"。"""
        colours = {
            "error": theme.DANGER,
            "warning": theme.WARNING,
            "success": theme.SUCCESS,
            "muted": theme.MUTED,
        }
        colour = colours.get(tone)
        self.message.setText(text)
        self.message.setStyleSheet(f"color: {theme.pick(colour)};" if colour else "")

    def start_progress(self, total: int | None = None) -> None:
        """顯示進度條。給了 ``total`` 就是真的進度，沒給就是「還在跑」。

        不定進度條只能回答「有沒有當掉」。使用者盯著一趟要跑一個多小時的爬取
        時，真正想知道的是「還要多久」——來回跑的那條橫槓每一秒看起來都一樣，
        跑了 5 分鐘跟跑了 50 分鐘沒有任何差別。知道總共幾趟的時候就要講出來。
        """
        if total and total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(0)
            self.progress.setFormat("%v / %m")
            self.progress.setTextVisible(True)
        else:
            self.progress.setRange(0, 0)
            self.progress.setTextVisible(False)
        self.progress.show()

    def advance_progress(self, done: int, total: int | None = None) -> None:
        """把進度條推到第 ``done`` 格。總數變了（分析途中才數得出來）就一起換。"""
        if total and total > 0 and self.progress.maximum() != total:
            self.progress.setRange(0, total)
            self.progress.setFormat("%v / %m")
            self.progress.setTextVisible(True)
        if self.progress.maximum() > 0:
            self.progress.setValue(min(done, self.progress.maximum()))

    def stop_progress(self) -> None:
        self.progress.hide()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
