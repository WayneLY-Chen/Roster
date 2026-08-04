"""應用程式外殼：側邊導覽 + 頁面容器 + 狀態列。

前身是 customtkinter 版的 ``gui/app.py``（已刪除），設計哲學完全沿用：

    * 側邊欄 9 個項目，全部都是真正的頁面——移植已經完成，沒有佔位頁。
    * **頁面延遲建立**：啟動時把全部 9 個頁面物件建出來、放進
      ``QStackedWidget``（這一步很便宜——一個空的 ``QWidget`` 幾乎不花時間，
      跟 customtkinter 「每個 widget 都要畫一次圓角矩形、耗時 ~1ms」完全不
      同量級），但只有第一次真的被點到的頁面才會呼叫 ``ensure_built()``
      去長出裡面真正的元件。
    * **換頁只是 ``QStackedWidget.setCurrentWidget()``**，對照 Tk 版的
      ``tkraise()``——兩者的共同點是都不會重新計算/重建整個頁面的版面，
      這正是換頁延遲能壓到 20ms 以內的關鍵之一。
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QGridLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core import legal
from core.config import AppConfig, get_config
from core.constants import DISPLAY_NAME, PROJECT_NAME, VERSION, LogCategory
from gui_qt.assets import app_icon
from core.errors import CRMError
from core.logging_setup import get_logger, setup_logging
from core.scheduler import CrawlScheduler
from gui_qt import theme
from gui_qt.pages.base import BasePage, bump_data_version
from gui_qt.pages.companies import CompaniesPage
from gui_qt.pages.contacts import ContactsPage
from gui_qt.pages.crawler import CrawlerPage
from gui_qt.pages.dashboard import DashboardPage
from gui_qt.pages.export_page import ExportPage
from gui_qt.pages.import_page import ImportPage
from gui_qt.pages.logs import LogsPage
from gui_qt.pages.mail import MailPage
from gui_qt.pages.settings import SettingsPage
from gui_qt.widgets import StatusBar

log = get_logger(LogCategory.GUI)

#: 側邊欄的排列直接跟著這個 tuple 走，順序沿用 Tk 版：
#:
#:     頁面（gui_qt/pages/*.py）  title   icon
#:     -------------------------  ------  ----
#:     dashboard.DashboardPage    儀表板  📊
#:     companies.CompaniesPage    公司    🏢
#:     contacts.ContactsPage      聯絡人  👤
#:     crawler.CrawlerPage        爬取    🕷️
#:     import_page.ImportPage     匯入    📥
#:     export_page.ExportPage     匯出    📤
#:     mail.MailPage              郵件    ✉
#:     logs.LogsPage              日誌    📜
#:     settings.SettingsPage      設定    ⚙️
#:
#: title 是頁面的識別字串（``show_page()``、``self.pages``、``nav_buttons``
#: 都拿它當 key），改字等於改 key，要一起改。
PAGE_CLASSES: tuple[type[BasePage], ...] = (
    DashboardPage,
    CompaniesPage,
    ContactsPage,
    CrawlerPage,
    ImportPage,
    ExportPage,
    MailPage,
    LogsPage,
    SettingsPage,
)

MIN_WIDTH = 1100
MIN_HEIGHT = 700
SIDEBAR_WIDTH = 210


class _SchedulerBridge(QObject):
    """把 :class:`~core.scheduler.CrawlScheduler` 背景執行緒的完成回呼轉成
    UI 執行緒可以安全處理的 signal。

    ``CrawlScheduler`` 的排程執行緒（見 ``core/scheduler.py``）是一般的
    ``threading.Thread``，跟 Tk 版一樣直接呼叫 ``on_finished``；Qt 版不能讓
    這個回呼直接碰 widget，所以這裡只做「emit 一個 signal」這件事——這個
    橋接物件本身建立於 UI 執行緒（``MainWindow.__init__`` 呼叫的當下），
    Qt 偵測到 emit 時目前執行緒跟 receiver（``MainWindow``，同樣住在 UI
    執行緒）所在執行緒不同，會自動把已連結的 slot 呼叫排進 UI 執行緒的
    事件迴圈，槽函式因此永遠在 UI 執行緒被呼叫——跟 ``gui_qt/tasks.py`` 的
    ``BackgroundTask`` 是同一套機制（見該檔案 docstring）。
    """

    finished = Signal(object)


class MainWindow(QMainWindow):
    """應用程式視窗。"""

    def __init__(self, config: AppConfig | None = None) -> None:
        super().__init__()
        self.config_data = config or get_config()

        # 必須在建立任何頁面元件之前設定：字型跟主題套用的是 QApplication
        # 層級的預設值/QSS，之後建立的每個 widget 都會自動繼承。
        qt_app = QApplication.instance()
        theme.configure_fonts(qt_app)
        theme.apply_theme(qt_app, self.config_data.app.theme)

        self.setWindowTitle(f"{self.config_data.app.name} v{VERSION}")
        self.resize(MIN_WIDTH, MIN_HEIGHT)
        self.setMinimumSize(940, 600)

        self.pages: dict[str, BasePage] = {}
        self.nav_buttons: dict[str, QPushButton] = {}
        self.current_page: str | None = None
        self.scheduler: CrawlScheduler | None = None

        central = QWidget()
        self.setCentralWidget(central)
        grid = QGridLayout(central)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(0)

        self._build_sidebar(grid)
        self._build_container(grid)
        self._build_status_bar(grid)
        self._build_pages()

        grid.setRowStretch(0, 1)
        grid.setColumnStretch(1, 1)

        self.show_page(DashboardPage.title)
        self._start_scheduler()

    # ------------------------------------------------------------- 排程

    def _start_scheduler(self) -> None:
        """比照 Tk 版 ``CRMApp._start_scheduler``：設定檔開了排程才啟動。

        ``CrawlScheduler`` 的完成回呼是從排程執行緒（見
        ``core/scheduler.py``）呼叫的，不能直接在這裡碰 widget，所以先經過
        :class:`_SchedulerBridge` 轉成 signal，實際更新畫面的
        :meth:`_on_scheduled_run` 一定會在 UI 執行緒被呼叫。
        """
        if not self.config_data.scheduler.enabled:
            return

        self._scheduler_bridge = _SchedulerBridge(self)
        self._scheduler_bridge.finished.connect(self._on_scheduled_run)

        self.scheduler = CrawlScheduler(
            self.config_data, on_finished=self._scheduler_bridge.finished.emit
        )
        if self.scheduler.start():
            self.set_status(self.scheduler.status_text, "muted")

    def _on_scheduled_run(self, summaries) -> None:
        """排程跑完一輪後，在 UI 執行緒更新狀態列並讓目前頁面知道要重查。

        排程的爬取會真的寫入資料庫，所以跟任何頁面自己觸發的爬取一樣，
        成功後要呼叫一次 :func:`bump_data_version`。
        """
        bump_data_version()
        total_new = sum(summary.records_new for summary in summaries)
        self.set_status(f"排程爬取完成，新增 {total_new} 筆資料", "success")
        self.refresh_current()

    # ------------------------------------------------------------- 版面

    def _build_sidebar(self, grid: QGridLayout) -> None:
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(SIDEBAR_WIDTH)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(18, 24, 18, 18)
        layout.setSpacing(3)

        title_label = QLabel(DISPLAY_NAME)
        title_font = title_label.font()
        title_font.setPointSize(14)
        title_font.setBold(True)
        title_label.setFont(title_font)
        title_label.setWordWrap(True)
        layout.addWidget(title_label)

        subtitle = QLabel(PROJECT_NAME)
        subtitle.setObjectName("MutedLabel")
        layout.addWidget(subtitle)
        layout.addSpacing(16)

        # QButtonGroup 讓 9 顆導覽按鈕互斥勾選；視覺上選取狀態則交給 QSS 的
        # ``:checked`` 偽狀態處理（見 gui_qt/theme.py 的 stylesheet()）。
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        for page_class in PAGE_CLASSES:
            button = QPushButton(page_class.title)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setFixedHeight(theme.nav_button_height())
            button.clicked.connect(
                lambda checked=False, name=page_class.title: self.show_page(name)
            )
            self._nav_group.addButton(button)
            layout.addWidget(button)
            self.nav_buttons[page_class.title] = button

        layout.addStretch(1)

        footer = QLabel(f"v{VERSION}\n{legal.SHORT_NOTICE}")
        footer.setObjectName("MutedLabel")
        footer.setWordWrap(True)
        layout.addWidget(footer)

        grid.addWidget(sidebar, 0, 0, 2, 1)

    def _build_container(self, grid: QGridLayout) -> None:
        self.stack = QStackedWidget()
        grid.addWidget(self.stack, 0, 1)

    def _build_status_bar(self, grid: QGridLayout) -> None:
        self.status_bar = StatusBar()
        grid.addWidget(self.status_bar, 1, 1)

    def _build_pages(self) -> None:
        """把每個頁面物件都建出來、放進 stack。

        跟 Tk 版 ``_build_pages`` 一樣，這裡只呼叫 ``page_class(self)``
        （便宜的 ``__init__``），真正長出元件要等 ``ensure_built()`` 才會做。
        """
        for page_class in PAGE_CLASSES:
            page = page_class(self)
            self.stack.addWidget(page)
            self.pages[page_class.title] = page

    # -------------------------------------------------------- navigation

    def show_page(self, name: str) -> None:
        """切到某一頁，需要的話先建立它、再重新整理它。"""
        page = self.pages.get(name)
        if page is None:
            log.warning("no such page: {}", name)
            return

        if name == self.current_page:
            page.on_show()          # 使用者刻意再點一次同一頁：單純重整
            return

        old_page = self.pages.get(self.current_page) if self.current_page else None
        if old_page is not None:
            old_page.on_hide()

        self._style_nav_button(self.current_page, selected=False)
        self._style_nav_button(name, selected=True)

        self.set_status("")  # 換頁前先清掉舊頁面留下的訊息，避免顯示過期內容
        page.ensure_built()
        self.stack.setCurrentWidget(page)
        self.current_page = name

        try:
            page.on_show()
        except CRMError as exc:
            self.set_status(str(exc), "error")
        except Exception as exc:  # noqa: BLE001 - 頁面壞掉不能連累整個視窗
            log.exception("page {} failed to refresh", name)
            self.set_status(f"{type(exc).__name__}: {exc}", "error")

    def _style_nav_button(self, name: str | None, selected: bool) -> None:
        button = self.nav_buttons.get(name) if name else None
        if button is None:
            return
        button.setChecked(selected)

    def set_status(self, message: str, tone: str = "normal") -> None:
        self.status_bar.set_message(message, tone)

    def refresh_current(self) -> None:
        if self.current_page:
            self.show_page(self.current_page)

    # ------------------------------------------------------------ closing

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 的覆寫方法命名
        running = [
            name
            for name, page in self.pages.items()
            if getattr(getattr(page, "task", None), "running", False)
        ]
        if running:
            reply = QMessageBox.question(
                self,
                "Work in progress",
                f"{', '.join(running)} is still running. Quit anyway?",
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

        if self.scheduler is not None:
            self.scheduler.stop()

        log.info("GUI closing (Qt)")
        event.accept()


def run_gui_qt(config: AppConfig | None = None) -> None:
    """啟動桌面應用程式。"""
    config = config or get_config()
    config.ensure_directories()
    setup_logging(config)

    from core.tls import install_os_trust_store

    install_os_trust_store()

    from database.session import init_db

    init_db()

    # 一定要在任何背景工作開始之前、而且在主執行緒做。控制器用的是延遲
    # import，而 PySide6 的 import 掛勾在執行緒池借出來的執行緒上會讓整個
    # 行程 Fatal Python error: Aborted——理由與實測見 core/preload.py。
    from core.preload import preload

    preload()

    qt_app = QApplication.instance() or QApplication(sys.argv)
    qt_app.setApplicationName(PROJECT_NAME)
    qt_app.setApplicationDisplayName(DISPLAY_NAME)

    icon = app_icon()
    if not icon.isNull():
        qt_app.setWindowIcon(icon)
    window = MainWindow(config)
    window.show()
    qt_app.exec()
