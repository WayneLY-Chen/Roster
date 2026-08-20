"""Tests for gui_qt/widgets.py -- mainly DataTable/DataTableModel.

These are the widgets every future page (companies, contacts, ...) will use
for its table. The important behaviours to keep correct as more pages start
relying on them:

    * ``set_rows`` takes plain dicts (same shape as the Tk ``DataTable``).
    * sorting reorders rows in place and keeps them addressable by row index.
    * selection callbacks receive a row dict, not a ``QModelIndex``.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402

from gui_qt.widgets import CHECK_KEY, DataTable, DataTableModel, StatCard  # noqa: E402

COLUMNS = [("name", "名稱", 120), ("count", "數量", 60)]


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


# ------------------------------------------------------------------ 勾選欄
#
# 「先預覽再決定要存哪幾筆」那種畫面用的。用選取（反白）表達同一件事的話，
# 使用者不小心點一下表格就會把幾十筆的選擇清空，而且完全沒有提示。


def test_a_plain_table_has_no_checkboxes(qt_app):
    """既有的頁面一個字都不必改。"""
    model = DataTableModel(COLUMNS)
    model.set_rows([{"name": "甲", "count": 1}])

    index = model.index(0, 0)
    assert model.data(index, Qt.ItemDataRole.CheckStateRole) is None
    assert not (model.flags(index) & Qt.ItemFlag.ItemIsUserCheckable)
    # 沒開勾選時「勾起來的列」就是全部——呼叫端不必分兩種寫法。
    assert len(model.checked_rows()) == 1


def test_checkable_rows_start_ticked(qt_app):
    model = DataTableModel(COLUMNS, checkable=True)
    model.set_rows([{"name": "甲", "count": 1}, {"name": "乙", "count": 2}])

    assert len(model.checked_rows()) == 2
    assert model.data(model.index(0, 0), Qt.ItemDataRole.CheckStateRole) == (
        Qt.CheckState.Checked
    )


def test_clicking_a_checkbox_actually_unticks_it(qt_app):
    """Qt 送進 setData 的是 int，不是 CheckState。

    拿它直接跟 enum 比會永遠是 False，使用者看到的是「勾了沒反應」——而那種
    錯不會有任何錯誤訊息。
    """
    model = DataTableModel(COLUMNS, checkable=True)
    model.set_rows([{"name": "甲", "count": 1}, {"name": "乙", "count": 2}])

    index = model.index(1, 0)
    assert model.setData(index, int(Qt.CheckState.Unchecked.value), Qt.ItemDataRole.CheckStateRole)

    assert [row["name"] for row in model.checked_rows()] == ["甲"]
    assert model.data(index, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Unchecked


def test_sorting_does_not_move_the_ticks_onto_other_rows(qt_app):
    """勾選狀態存在列自己的 dict 裡，就是為了這件事。

    另外存一份 list[bool] 的話，排一次序勾選就跑到別人身上——安靜地存錯資料。
    """
    model = DataTableModel(COLUMNS, checkable=True)
    model.set_rows([{"name": "甲", "count": 3}, {"name": "乙", "count": 1}])
    model.set_all_checked(False)
    model._rows[0][CHECK_KEY] = True          # 只勾「甲」

    model.sort(1, Qt.SortOrder.AscendingOrder)   # 依數量排序，兩列對調

    assert [row["name"] for row in model.checked_rows()] == ["甲"]


def test_set_all_checked_from_the_table_wrapper(qt_app):
    table = DataTable(COLUMNS, checkable=True)
    table.set_rows([{"name": "甲", "count": 1}, {"name": "乙", "count": 2}])

    table.set_all_checked(False)
    assert table.checked_rows() == []
    table.set_all_checked(True)
    assert len(table.checked_rows()) == 2


# ------------------------------------------------------------- DataTableModel


def test_model_starts_empty(qt_app):
    model = DataTableModel(COLUMNS)
    assert model.rowCount() == 0
    assert model.columnCount() == 2
    assert model.row_count() == 0


def test_set_rows_populates_model(qt_app):
    model = DataTableModel(COLUMNS)
    model.set_rows([{"name": "甲公司", "count": 3}, {"name": "乙公司", "count": 1}])

    assert model.rowCount() == 2
    assert model.row_at(0) == {"name": "甲公司", "count": 3}
    assert model.row_at(1) == {"name": "乙公司", "count": 1}


def test_data_display_role_formats_values(qt_app):
    model = DataTableModel(COLUMNS)
    model.set_rows([{"name": None, "count": 3}, {"name": "丙公司", "count": None}])

    assert model.data(model.index(0, 0), Qt.ItemDataRole.DisplayRole) == ""
    assert model.data(model.index(0, 1), Qt.ItemDataRole.DisplayRole) == "3"
    assert model.data(model.index(1, 1), Qt.ItemDataRole.DisplayRole) == ""


def test_header_data_uses_headings(qt_app):
    model = DataTableModel(COLUMNS)
    assert model.headerData(0, Qt.Orientation.Horizontal) == "名稱"
    assert model.headerData(1, Qt.Orientation.Horizontal) == "數量"


def test_sort_reorders_rows_and_toggles_direction(qt_app):
    model = DataTableModel(COLUMNS)
    model.set_rows(
        [{"name": "b", "count": 2}, {"name": "a", "count": 3}, {"name": "c", "count": 1}]
    )

    model.sort(1, Qt.SortOrder.AscendingOrder)
    assert [model.row_at(i)["count"] for i in range(3)] == [1, 2, 3]

    model.sort(1, Qt.SortOrder.DescendingOrder)
    assert [model.row_at(i)["count"] for i in range(3)] == [3, 2, 1]


def test_sort_on_empty_model_does_not_raise(qt_app):
    model = DataTableModel(COLUMNS)
    model.sort(0)  # nothing to sort; must be a no-op, not an IndexError


# ------------------------------------------------------------------- DataTable


def test_data_table_set_rows_and_row_count(qt_app):
    table = DataTable(columns=COLUMNS)
    table.set_rows([{"name": "甲", "count": 1}, {"name": "乙", "count": 2}])

    assert table.row_count() == 2

    table.clear()
    assert table.row_count() == 0


def test_data_table_selection_returns_row_dicts(qt_app):
    selected: list[dict] = []
    table = DataTable(columns=COLUMNS, on_select=selected.append)
    table.set_rows([{"name": "甲", "count": 1}, {"name": "乙", "count": 2}])

    table.view.selectRow(1)

    assert selected[-1] == {"name": "乙", "count": 2}
    assert table.selected_row() == {"name": "乙", "count": 2}


def test_data_table_activate_callback(qt_app):
    activated: list[dict] = []
    table = DataTable(columns=COLUMNS, on_activate=activated.append)
    table.set_rows([{"name": "甲", "count": 1}])

    # Same signal QTableView emits on double-click/Enter -- exercises the
    # actual wiring instead of calling the private handler directly.
    table.view.activated.emit(table.model.index(0, 0))

    assert activated == [{"name": "甲", "count": 1}]


# ---------------------------------------------------------------------- StatCard


def test_stat_card_update_values(qt_app):
    card = StatCard("公司總數", hint="初始提示")
    assert card.value_text == "-"

    card.update_values(215, "今日新增 0 筆")
    assert card.value_text == "215"
    assert card.hint_text == "今日新增 0 筆"

    card.update_values(216)  # hint omitted -> stays unchanged
    assert card.value_text == "216"
    assert card.hint_text == "今日新增 0 筆"
