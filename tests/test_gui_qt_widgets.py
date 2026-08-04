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

from gui_qt.widgets import DataTable, DataTableModel, StatCard  # noqa: E402

COLUMNS = [("name", "名稱", 120), ("count", "數量", 60)]


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


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
