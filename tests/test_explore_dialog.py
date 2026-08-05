"""「在這個網站找到的名錄」結果視窗。

一個網站常常有好幾份名錄（各縣市分會、各產業分類），一次只能挑一個等於同一套
流程要重跑五遍。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402

from gui_qt.explore_dialog import _CHECK_COLUMN, ExploreResultsDialog  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@dataclass
class _Candidate:
    url: str
    item_count: int = 20
    page_count: int = 3
    sample_names: list[str] = field(default_factory=lambda: ["甲有限公司"])
    company_name_ratio: float = 0.9
    kind: str = ""


@dataclass
class _Result:
    start_url: str = "https://a.test/"
    candidates: list[_Candidate] = field(default_factory=list)
    pages_fetched: int = 12
    notes: list[str] = field(default_factory=list)


@pytest.fixture
def dialog(qt_app):
    result = _Result(
        candidates=[
            _Candidate("https://a.test/members"),
            _Candidate("https://a.test/suppliers"),
            _Candidate("https://a.test/factories"),
        ]
    )
    dialog = ExploreResultsDialog(None, result)
    yield dialog
    dialog.deleteLater()


def _checked(dialog) -> list[int]:
    return [
        row
        for row in range(dialog.table.rowCount())
        if dialog.table.item(row, _CHECK_COLUMN).checkState() == Qt.CheckState.Checked
    ]


def test_only_the_best_candidate_starts_ticked(dialog):
    """全部預勾等於幫使用者做了他沒說要做的決定——按下確定就會多出十個來源。"""
    assert _checked(dialog) == [0]


def test_select_all_ticks_every_row(dialog):
    dialog.select_all_button.click()

    assert _checked(dialog) == [0, 1, 2]


def test_select_none_clears_everything(dialog):
    dialog.select_all_button.click()
    dialog.select_none_button.click()

    assert _checked(dialog) == []


def test_the_count_is_shown_so_the_button_says_what_it_will_do(dialog):
    dialog.select_all_button.click()

    assert "3 / 3" in dialog.count_label.text()
    assert "3" in dialog.buttons.buttons()[0].text()


def test_nothing_ticked_means_nothing_to_confirm(dialog):
    dialog.select_none_button.click()

    ok = dialog.buttons.buttons()[0]
    assert not ok.isEnabled()


def test_accepting_returns_every_ticked_url(dialog):
    dialog.select_all_button.click()
    dialog._accept_selection()

    assert dialog.chosen_urls == [
        "https://a.test/members",
        "https://a.test/suppliers",
        "https://a.test/factories",
    ]


def test_chosen_url_is_the_first_one_so_callers_need_not_care(dialog):
    dialog._accept_selection()

    assert dialog.chosen_url == "https://a.test/members"


def test_double_clicking_a_row_means_only_that_one(dialog):
    """雙擊某一列 ＝「我只要這一個」，不管前面勾了什麼。"""
    dialog.select_all_button.click()

    class _Index:
        def row(self):
            return 2

    dialog._on_activated(_Index())

    assert dialog.chosen_urls == ["https://a.test/factories"]
