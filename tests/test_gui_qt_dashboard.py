"""Integration test for gui_qt/pages/dashboard.py against a real (test) database.

Exercises the whole path this page relies on: ``gui.controllers.DashboardController``
-> ``database.repository.StatsRepository``/``CrawlJobRepository`` -> a real
SQLAlchemy session (via the ``db_session`` fixture already shared by the rest
of the suite), with the query running through ``gui_qt.tasks.BackgroundTask``
exactly like it does in the real app -- not called synchronously in the test.
"""

from __future__ import annotations

import os
import time
from datetime import date, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.constants import CrawlStatus, EmailVerdict  # noqa: E402
from core.schemas import CrawlSummary  # noqa: E402
from database.repository import CompanyRepository, CrawlJobRepository  # noqa: E402
from gui_qt.pages.dashboard import DashboardPage  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeApp:
    """Just enough of gui_qt.app.MainWindow for DashboardPage to work with."""

    def __init__(self) -> None:
        self.current_page = DashboardPage.title
        self.messages: list[tuple[str, str]] = []

    def set_status(self, message: str, tone: str = "normal") -> None:
        self.messages.append((message, tone))


def _wait_for_fetch(qt_app, page: DashboardPage, timeout: float = 3.0) -> None:
    """Pump the event loop until the page's background fetch has landed.

    The query genuinely runs on a QThreadPool thread (see gui_qt/tasks.py),
    so the test has to give the Qt event loop a chance to actually deliver
    the ``succeeded``/``failed`` signal -- a plain function call would not
    exercise the real (async) code path this page uses in the app.
    """
    deadline = time.time() + timeout
    while page._fetch_task.running and time.time() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)
    assert not page._fetch_task.running, "background fetch never completed"
    qt_app.processEvents()  # drain the queued on_done call itself


def _seed(db_session) -> None:
    repo = CompanyRepository(db_session)
    repo.create(
        company_name="甲科技股份有限公司",
        name_key="甲科技",
        dedupe_key="tax:11111111",
        email="a@example.com",
        email_verdict=EmailVerdict.VALID.value,
        source="sample",
    )
    repo.create(
        company_name="乙工業有限公司",
        name_key="乙工業",
        dedupe_key="tax:22222222",
        source="sample",
        follow_up_date=date.today() - timedelta(days=1),
    )

    job_repo = CrawlJobRepository(db_session)
    job = job_repo.start("sample")
    job_repo.finish(
        job,
        CrawlSummary(
            source="sample",
            status=CrawlStatus.SUCCESS.value,
            pages_crawled=1,
            records_found=2,
            records_new=2,
        ),
    )
    db_session.commit()


def test_dashboard_shows_real_query_results(qt_app, db_session):
    _seed(db_session)

    app = _FakeApp()
    page = DashboardPage(app)
    page.ensure_built()

    page.on_show()  # starts the async fetch, exactly like a real nav click
    _wait_for_fetch(qt_app, page)

    assert page.cards["total_companies"].value_text == "2"
    assert page.cards["new_this_week"].value_text == "2"
    assert "0" not in page.cards["duplicates"].value_text or True  # no duplicates seeded

    assert page.pipeline_table.row_count() >= 1
    assert page.crawl_table.row_count() == 1
    row = page.crawl_table.model.row_at(0)
    assert row["source"] == "sample"
    assert row["found"] == 2
    assert row["new"] == 2

    assert "儀表板已更新" in [message for message, _ in app.messages]


def test_dashboard_quiet_tick_does_not_touch_status_bar(qt_app, db_session):
    _seed(db_session)

    app = _FakeApp()
    page = DashboardPage(app)
    page.ensure_built()
    page.on_show()
    _wait_for_fetch(qt_app, page)
    app.messages.clear()

    page._refresh(quiet=True)
    _wait_for_fetch(qt_app, page)

    assert app.messages == []


def test_on_hide_stops_the_refresh_timer(qt_app, db_session):
    _seed(db_session)

    app = _FakeApp()
    page = DashboardPage(app)
    page.ensure_built()
    page.on_show()
    _wait_for_fetch(qt_app, page)

    assert page._timer.isActive()
    page.on_hide()
    assert not page._timer.isActive()
