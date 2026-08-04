"""Tests for gui_qt/pages/base.py -- BasePage lifecycle and the data-version
skip mechanism.

``_data_version`` is a module-level counter, so these tests only ever assert
on *changes* relative to whatever the counter already is -- never on its
absolute value -- since other tests in the same process may have bumped it.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from gui_qt.pages.base import BasePage, bump_data_version, current_data_version  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeApp:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def set_status(self, message: str, tone: str = "normal") -> None:
        self.messages.append((message, tone))


class _RecordingPage(BasePage):
    title = "測試頁"

    def __init__(self, app) -> None:
        super().__init__(app)
        self.build_calls = 0
        self.refresh_calls = 0
        self.reveal_calls = 0
        self.hide_calls = 0

    def build(self) -> None:
        self.build_calls += 1

    def refresh(self) -> None:
        self.refresh_calls += 1

    def on_reveal(self) -> None:
        self.reveal_calls += 1

    def on_hide(self) -> None:
        self.hide_calls += 1


def test_ensure_built_runs_build_exactly_once(qt_app):
    page = _RecordingPage(_FakeApp())
    page.ensure_built()
    page.ensure_built()
    page.ensure_built()
    assert page.build_calls == 1


def test_first_on_show_always_refreshes(qt_app):
    page = _RecordingPage(_FakeApp())
    page.on_show()
    assert page.refresh_calls == 1
    assert page.reveal_calls == 0


def test_on_show_again_without_a_bump_skips_refresh(qt_app):
    page = _RecordingPage(_FakeApp())
    page.on_show()
    assert page.refresh_calls == 1

    page.on_show()  # nothing bumped the data version in between
    assert page.refresh_calls == 1  # still 1: refresh() was skipped
    assert page.reveal_calls == 1  # on_reveal() ran instead


def test_bump_data_version_forces_the_next_refresh(qt_app):
    page = _RecordingPage(_FakeApp())
    page.on_show()
    assert page.refresh_calls == 1

    bump_data_version()
    page.on_show()
    assert page.refresh_calls == 2
    assert page.reveal_calls == 0


def test_force_true_always_refreshes(qt_app):
    page = _RecordingPage(_FakeApp())
    page.on_show()
    assert page.refresh_calls == 1

    page.on_show(force=True)
    page.on_show(force=True)
    assert page.refresh_calls == 3


def test_current_data_version_is_monotonic(qt_app):
    before = current_data_version()
    bump_data_version()
    bump_data_version()
    after = current_data_version()
    assert after == before + 2


def test_status_and_report_error_delegate_to_app(qt_app):
    app = _FakeApp()
    page = _RecordingPage(app)

    page.status("hello", "success")
    assert app.messages[-1] == ("hello", "success")

    page.report_error(ValueError("bad"))
    kind, tone = app.messages[-1]
    assert "ValueError" in kind and "bad" in kind
    assert tone == "error"


def test_on_hide_default_is_a_no_op_but_override_is_called(qt_app):
    page = _RecordingPage(_FakeApp())
    page.on_hide()
    assert page.hide_calls == 1
