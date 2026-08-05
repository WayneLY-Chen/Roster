"""Tests for gui_qt/source_wizard.py -- the "paste a URL" wizard dialog.

``SourceWizardController.analyse()``/``preview_with()``/``test_run()`` fetch a
real page over the network, which this suite must never do (see
``tests/test_crawler.py`` and the project's own crawl ethics rules). Every
test here therefore drives :class:`SourceWizardDialog` with a small in-memory
double standing in for the controller, while still exercising the real
``BackgroundTask`` (QThreadPool) plumbing -- the only thing that would be
unrealistic about calling the double directly is skipping that plumbing.

The one place a *real* controller is used is the save/delete path
(``core.config.save_custom_source``/``read_custom_sources``/
``delete_custom_source``), which only touches a YAML file redirected into
``tmp_path`` -- never the network either.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field as dc_field
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core.errors import RobotsDisallowedError  # noqa: E402
from core.schemas import RawCompany  # noqa: E402
from controllers.source import KNOWN_FIELDS  # noqa: E402
from gui_qt.pages.base import current_data_version  # noqa: E402
from gui_qt.source_wizard import SourceWizardDialog  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _no_blocking_dialogs(monkeypatch):
    """Answer every QMessageBox synchronously instead of popping a real one.

    A real ``QMessageBox.exec()`` would block the test's event loop waiting
    for a click that will never come.
    """
    calls: dict[str, list[tuple]] = {"critical": [], "information": [], "question": []}

    monkeypatch.setattr(
        QMessageBox,
        "critical",
        staticmethod(lambda *a, **k: calls["critical"].append(a)),
    )
    monkeypatch.setattr(
        QMessageBox,
        "information",
        staticmethod(lambda *a, **k: calls["information"].append(a)),
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(
            lambda *a, **k: (calls["question"].append(a), QMessageBox.StandardButton.Yes)[1]
        ),
    )
    return calls


@dataclass
class _FakeFieldGuess:
    selector: str
    attr: str = "text"
    regex: str | None = None
    hit_rate: float = 1.0
    samples: list[str] = dc_field(default_factory=list)


@dataclass
class _FakeDiscoveryResult:
    url: str = ""
    list_selector: str = "div.card"
    item_count: int = 2
    fields: dict[str, _FakeFieldGuess] = dc_field(default_factory=dict)
    preview: list[Any] = dc_field(default_factory=list)
    next_selector: str | None = None
    notes: list[str] = dc_field(default_factory=list)
    detail_link_selector: str | None = None
    page_url_template: str | None = None
    page_count: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.list_selector) and "company_name" in self.fields


class _FakeSourceController:
    """Stands in for gui.controllers_source.SourceWizardController.

    Every method matches the real signature (including the
    ``report``/``cancel_event`` keyword-only pair that
    :class:`~gui_qt.tasks.BackgroundTask` always supplies) so the dialog code
    under test never has to know it is talking to a double.
    """

    def __init__(
        self,
        analyse_result: _FakeDiscoveryResult | Exception | None = None,
        preview_rows: list[dict[str, Any]] | None = None,
        saved_name: str = "fake_source",
        test_run_summaries: list[Any] | None = None,
    ) -> None:
        self._analyse_result = analyse_result
        self._preview_rows = preview_rows if preview_rows is not None else []
        self._saved_name = saved_name
        self._test_run_summaries = test_run_summaries if test_run_summaries is not None else []
        self.saved: list[tuple[Any, str, bool]] = []
        self.built: list[dict[str, Any]] = []

    def analyse(self, url, *, report, cancel_event):
        report({"stage": "fetching", "url": url})
        if isinstance(self._analyse_result, Exception):
            raise self._analyse_result
        result = self._analyse_result
        result.url = url
        report({"stage": "done"})
        return result

    def preview_with(self, url, list_selector, field_rules):
        return self._preview_rows

    def build_source(
        self,
        url,
        name,
        list_selector,
        field_rules,
        next_selector,
        max_pages=None,
        *,
        detail_link_selector=None,
        max_details=None,
        default_industry="",
        collect_fields=None,
        page_start=1,
        page_end=None,
    ):
        built = {
            "url": url,
            "name": name,
            "list_selector": list_selector,
            "field_rules": field_rules,
            "next_selector": next_selector,
            "max_pages": max_pages,
            "detail_link_selector": detail_link_selector,
            "max_details": max_details,
            "default_industry": default_industry,
            "collect_fields": collect_fields,
            "page_start": page_start,
            "page_end": page_end,
        }
        self.built.append(built)
        return built

    def save(self, source, name, enabled=True):
        self.saved.append((source, name, enabled))
        return self._saved_name

    def test_run(self, name, *, report, cancel_event):
        return self._test_run_summaries

    def custom_sources(self):
        return []

    def delete(self, name):
        return True


def _wait_for_task(qt_app, task, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while task is not None and task.running and time.time() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)
    assert task is None or not task.running, "background task never completed"
    qt_app.processEvents()


# ------------------------------------------------------------------ layout


def test_advanced_section_starts_collapsed(qt_app):
    dialog = SourceWizardDialog(None, _FakeSourceController())
    assert dialog.advanced_section.expanded is False
    assert dialog.save_button.isEnabled() is False
    assert dialog.save_crawl_button.isEnabled() is False


def test_ethics_notice_and_field_terminology_are_preserved(qt_app):
    """The plain-language design constraint: CSS talk stays inside "進階設定"."""
    from PySide6.QtWidgets import QLabel

    dialog = SourceWizardDialog(None, _FakeSourceController())

    texts = "\n".join(label.text() for label in dialog.findChildren(QLabel))
    assert "robots.txt" in texts
    assert "只會爬取公開資料" in texts
    # The advanced button's own caption still says "選擇器" -- that word is
    # meant to live only inside the collapsed section, not in the top-level UI.
    assert "CSS 選擇器" not in dialog.summary_label.text()
    assert dialog.advanced_section.toggle_button.text().endswith("進階設定（自動偵測正確的話不用動）")


# ---------------------------------------------------------------- analysis


def test_successful_analysis_keeps_advanced_section_collapsed(qt_app):
    result = _FakeDiscoveryResult(
        list_selector="div.card",
        item_count=2,
        fields={"company_name": _FakeFieldGuess(selector="h3.name", hit_rate=1.0, samples=["甲"])},
        preview=[
            RawCompany(company_name="甲科技", source="wizard-test"),
            RawCompany(company_name="乙工業", source="wizard-test"),
        ],
    )
    controller = _FakeSourceController(analyse_result=result)
    dialog = SourceWizardDialog(None, controller)

    dialog.url_entry.setText("https://example.test/companies")
    dialog._start_analyse()
    _wait_for_task(qt_app, dialog.analyse_task)

    assert "在這一頁找到 2 筆資料" in dialog.summary_label.text()
    assert dialog.advanced_section.expanded is False
    assert dialog.fields_table.row_count() == 1
    assert dialog.preview_table.row_count() == 2
    assert dialog.save_button.isEnabled() is True
    assert dialog.save_crawl_button.isEnabled() is True
    assert dialog.name_entry.get() == "example.test"


def test_analysis_missing_company_name_expands_advanced_section(qt_app):
    result = _FakeDiscoveryResult(list_selector="div.card", item_count=3, fields={})
    controller = _FakeSourceController(analyse_result=result)
    dialog = SourceWizardDialog(None, controller)

    dialog.url_entry.setText("https://example.test/companies")
    dialog._start_analyse()
    _wait_for_task(qt_app, dialog.analyse_task)

    assert dialog.advanced_section.expanded is True
    assert dialog.save_button.isEnabled() is False
    assert "公司名稱" in dialog.save_hint_label.text()


def test_analysis_error_shows_friendly_robots_message(qt_app):
    controller = _FakeSourceController(
        analyse_result=RobotsDisallowedError("https://example.test/companies", "Roster/1.0")
    )
    dialog = SourceWizardDialog(None, controller)

    dialog.url_entry.setText("https://example.test/companies")
    dialog._start_analyse()
    _wait_for_task(qt_app, dialog.analyse_task)

    assert "分析失敗" in dialog.summary_label.text()
    assert dialog.analyse_button.isEnabled() is True
    assert dialog.analyse_progress.isVisible() is False


def test_start_analyse_without_a_url_shows_error_and_does_not_start(qt_app):
    controller = _FakeSourceController()
    dialog = SourceWizardDialog(None, controller)

    dialog._start_analyse()

    assert dialog.analyse_task is None


# -------------------------------------------------------------- field table


def test_add_and_delete_field_round_trip(qt_app):
    dialog = SourceWizardDialog(None, _FakeSourceController())

    target_label = dialog.new_field_combo.itemText(0)
    assert target_label  # KNOWN_FIELDS is non-empty
    dialog._add_field()
    assert dialog.fields_table.row_count() == 1

    dialog._delete_field()
    assert dialog.fields_table.row_count() == 0
    assert dialog.editing_label.text() == "尚未選取欄位"


def test_apply_edit_without_a_selected_field_is_a_no_op(qt_app, _no_blocking_dialogs):
    dialog = SourceWizardDialog(None, _FakeSourceController())
    dialog._apply_edit()
    assert _no_blocking_dialogs["information"]  # a guidance message, not a crash


# -------------------------------------------------------------- preview


def test_preview_updates_table_and_recomputes_hit_rate(qt_app):
    controller = _FakeSourceController(
        preview_rows=[
            {"company_name": "甲科技", "email": "a@example.com"},
            {"company_name": "乙工業", "email": ""},
        ]
    )
    dialog = SourceWizardDialog(None, controller)
    dialog.last_url = "https://example.test/companies"
    dialog.list_selector_entry.set("div.card")
    dialog.field_rules = {"company_name": {"selector": "h3", "attr": "text"}}

    dialog._start_preview()
    _wait_for_task(qt_app, dialog.preview_task)

    assert dialog.preview_table.row_count() == 2
    assert dialog.field_rules["company_name"]["hit_rate"] == 1.0


def test_preview_without_list_selector_shows_error(qt_app, _no_blocking_dialogs):
    dialog = SourceWizardDialog(None, _FakeSourceController())
    dialog.last_url = "https://example.test/companies"

    dialog._start_preview()

    assert dialog.preview_task is None
    assert _no_blocking_dialogs["critical"]


# -------------------------------------------------------------- save & crawl


def test_default_name_strips_www_and_scheme(qt_app):
    dialog = SourceWizardDialog(None, _FakeSourceController())
    assert dialog._default_name("https://www.Example.COM/companies?x=1") == "example.com"


def test_save_without_crawl_calls_on_saved_and_closes(qt_app):
    controller = _FakeSourceController(saved_name="my_source")
    saved: list[str] = []
    dialog = SourceWizardDialog(None, controller, on_saved=saved.append)

    dialog.last_url = "https://example.test/companies"
    dialog.list_selector_entry.set("div.card")
    dialog.field_rules = {"company_name": {"selector": "h3", "attr": "text"}}
    dialog.name_entry.set("my_source")

    dialog._save(also_crawl=False)

    assert saved == ["my_source"]
    assert controller.saved[0][1] == "my_source"
    assert dialog.result() == dialog.DialogCode.Accepted


def test_save_and_crawl_runs_test_crawl_and_bumps_data_version(qt_app):
    from core.schemas import CrawlSummary

    controller = _FakeSourceController(
        saved_name="my_source",
        test_run_summaries=[
            CrawlSummary(source="my_source", status="Success", records_found=3, records_new=2)
        ],
    )
    saved: list[str] = []
    dialog = SourceWizardDialog(None, controller, on_saved=saved.append)

    dialog.last_url = "https://example.test/companies"
    dialog.list_selector_entry.set("div.card")
    dialog.field_rules = {"company_name": {"selector": "h3", "attr": "text"}}
    dialog.name_entry.set("my_source")

    before = current_data_version()
    dialog._save(also_crawl=True)
    _wait_for_task(qt_app, dialog.crawl_task)

    assert saved == ["my_source"]
    assert current_data_version() == before + 1
    assert dialog.result() == dialog.DialogCode.Accepted


def test_save_without_a_url_shows_error(qt_app, _no_blocking_dialogs):
    dialog = SourceWizardDialog(None, _FakeSourceController())
    dialog._save(also_crawl=False)
    assert _no_blocking_dialogs["critical"]
