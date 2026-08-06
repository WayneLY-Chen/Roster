"""爬取頁：開始一次爬取、即時看紀錄，事後可以驗證——PySide6 版。

對照 ``gui/pages/crawler.py``（Tk 版）。爬取跟驗證都是慢、會打網路或吃 CPU
的工作，所以一律走 :class:`~gui_qt.tasks.BackgroundTask`——頁面本身只在
回呼（保證回到 UI 執行緒）裡碰 widget，遵守 ``gui_qt/tasks.py`` docstring
說的規則。

## 這頁沒有覆寫 ``on_show()``

跟公司頁、聯絡人頁不同，這頁的控制項不依賴任何「資料庫目前的狀態」去重繪
——來源清單只在使用者透過網址精靈新增/刪除自訂來源時才需要重新查詢（見
``_on_source_saved``），紀錄表格與活動紀錄只在使用者自己按下「開始爬取」
之後才會有內容。所以沿用 ``BasePage`` 的預設 ``refresh()``（什麼都不做），
不需要覆寫 ``on_show()``，效果等同 Tk 版的 ``def on_show(self) -> None: pass``。

## 爬蟲倫理規範

這裡顯示給使用者看的文字（robots.txt、請求間隔延遲、補抓信箱前的說明）
照抄 Tk 版，一個字都不精簡：只抓公開資料、一定遵守 robots.txt、絕不繞過
封鎖或反爬機制，這些規則不能因為換了介面框架就在文字上打折扣。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.errors import CRMError
from core.schemas import CrawlSummary, VerifySummary
from controllers.core import CrawlController, EnrichController, VerifyController
from controllers.source import SourceWizardController
from core.i18n import CRAWL_STATUS_LABELS, label
from gui_qt import theme
from gui_qt.pages.base import BasePage, bump_data_version
from gui_qt.source_wizard import SourceWizardDialog
from gui_qt.tasks import BackgroundTask
from gui_qt.widgets import DataTable, LabeledEntry, Section, WideComboBox, caption

#: 來源下拉選單裡「不指定單一來源」的顯示文字。
ALL_ENABLED = "（全部啟用）"


class _LabeledCombo(QWidget):
    """說明文字疊在下拉選單上面，跟 :class:`~gui_qt.widgets.LabeledEntry` 同一套排版慣例。

    ``gui_qt/widgets.py`` 目前沒有現成的「帶標題下拉選單」（Tk 版對應的
    ``gui.widgets.labeled_menu`` 也只存在 Tk 那邊），只有這一頁需要，所以
    定義在這裡，不去動共用檔案。
    """

    def __init__(
        self, label_text: str, values: list[str], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(caption(label_text))

        self.combo = WideComboBox()
        self.combo.addItems(values)
        layout.addWidget(self.combo)

    def get(self) -> str:
        return self.combo.currentText()

    def set_values(self, values: list[str]) -> None:
        """整批換掉選項，盡量保留目前選取的值（不在新清單裡才退回第一項）。"""
        current = self.combo.currentText()
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItems(values)
        self.combo.blockSignals(False)
        self.combo.setCurrentText(current if current in values else values[0])


class CrawlerPage(BasePage):
    title = "爬取"
    icon = "🕷️"

    def __init__(self, app: object) -> None:
        super().__init__(app)
        self.crawl_controller = CrawlController()
        self.verify_controller = VerifyController()
        self.enrich_controller = EnrichController()
        self.source_controller = SourceWizardController()
        self.crawl_task: BackgroundTask | None = None
        self.verify_task: BackgroundTask | None = None
        self.enrich_task: BackgroundTask | None = None

    # ------------------------------------------------------------- 建立元件

    def build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(12)

        title_label = QLabel("爬取")
        title_font = title_label.font()
        title_font.setPointSize(22)
        title_font.setBold(True)
        title_label.setFont(title_font)
        outer.addWidget(title_label)

        controls = Section("執行爬取")
        outer.addWidget(controls)

        try:
            source_names = self.crawl_controller.source_names()
        except CRMError as exc:
            self.report_error(exc)
            source_names = []

        # 這一頁只做一件事：挑一個來源、按下去。
        #
        # 起始頁／結束頁／最多幾頁以前放在這裡，但它們是「這個來源要怎麼爬」
        # 的設定，不是「這一次」的決定——而且寫在這一頁的東西自動排程根本
        # 看不到。全部搬到「＋ 自訂網址…」裡跟著來源存起來，同一個概念不要
        # 散在兩個地方，使用者才不會不確定哪個才算數。
        input_row = QHBoxLayout()
        self.source_combo = _LabeledCombo("來源", [ALL_ENABLED, *source_names])
        input_row.addWidget(self.source_combo)
        input_row.addStretch(1)
        controls.body_layout.addLayout(input_row)

        page_note = QLabel(
            "要爬幾頁、收集哪些欄位、這個來源屬於哪個產業，都在「＋ 自訂網址…」"
            "裡設定並跟著來源存起來——自動排程去爬的時候也會照著做。"
        )
        page_note.setObjectName("MutedLabel")
        page_note.setWordWrap(True)
        controls.body_layout.addWidget(page_note)

        buttons_row = QHBoxLayout()
        self.start_button = QPushButton("開始爬取")
        self.start_button.clicked.connect(self._start_crawl)
        buttons_row.addWidget(self.start_button)

        self.cancel_button = QPushButton("取消")
        self.cancel_button.setEnabled(False)
        self.cancel_button.setStyleSheet(
            f"background-color: {theme.pick(theme.DANGER)}; color: white;"
        )
        self.cancel_button.clicked.connect(self._cancel_crawl)
        buttons_row.addWidget(self.cancel_button)

        self.verify_button = QPushButton("驗證所有紀錄")
        self.verify_button.clicked.connect(self._start_verify)
        buttons_row.addWidget(self.verify_button)

        self.custom_source_button = QPushButton("＋ 自訂網址…")
        self.custom_source_button.clicked.connect(self._open_source_wizard)
        buttons_row.addWidget(self.custom_source_button)

        # 名錄類網站通常只公開電話跟連結、沒有信箱——信箱要點進公司自己的
        # 網站才看得到。沒有這個按鈕，使用者根本不會知道程式有這個能力。
        self.enrich_button = QPushButton("補抓信箱")
        self.enrich_button.clicked.connect(self._start_enrich)
        buttons_row.addWidget(self.enrich_button)

        self.manage_sources_button = QPushButton("管理自訂來源")
        self.manage_sources_button.clicked.connect(self._open_source_manager)
        buttons_row.addWidget(self.manage_sources_button)
        buttons_row.addStretch(1)
        controls.body_layout.addLayout(buttons_row)

        notice = QLabel(
            "爬取會遵守各來源的 robots.txt 規則，以及設定中的請求間隔延遲，"
            "因此完整跑一輪可能需要一些時間。"
        )
        notice.setWordWrap(True)
        notice.setStyleSheet(f"color: {theme.pick(theme.MUTED)};")
        controls.body_layout.addWidget(notice)

        log_section = Section("活動紀錄")
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(2000)
        log_section.body_layout.addWidget(self.log_box)
        outer.addWidget(log_section, 1)

        results_section = Section("結果")
        self.results_table = DataTable(
            columns=[
                ("source", "來源", 120),
                ("status", "狀態", 90),
                ("pages", "頁數", 70),
                ("found", "找到", 70),
                ("new", "新增", 60),
                ("merged", "合併", 70),
                ("dupes", "重複", 70),
                ("rejected", "拒絕", 80),
                # 網站改版之後選擇器會失效，爬取「成功」地抓到 0 筆而畫面寫著
                # 完成。這一欄就是為了讓那件事看得見。
                ("warning", "提醒", 260),
            ]
        )
        results_section.body_layout.addWidget(self.results_table)
        outer.addWidget(results_section, 1)

    # ------------------------------------------------------------- crawling

    def _start_crawl(self) -> None:
        if self.crawl_task is not None and self.crawl_task.running:
            return

        source = self.source_combo.get()
        source = None if source == ALL_ENABLED else source

        # 頁數範圍一律照來源自己的設定跑（在「＋ 自訂網址…」裡設定）。
        max_pages = from_page = to_page = None

        self._clear_log()
        self._append_log(f"開始爬取（{source or '全部啟用來源'}）...")
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.app.status_bar.start_progress()
        self.status("爬取中...", "normal")

        self.crawl_task = BackgroundTask(
            self,
            worker=self.crawl_controller.run,
            on_progress=self._on_crawl_progress,
            on_done=self._on_crawl_done,
            on_error=self._on_crawl_error,
        )
        self.crawl_task.start(source, max_pages, from_page, to_page)

    @staticmethod
    def _optional_int(text: str, label: str) -> int | None:
        """把選填的數字欄位轉成 int，空白視為未指定。"""
        stripped = (text or "").strip()
        if not stripped:
            return None
        try:
            value = int(stripped)
        except ValueError:
            raise ValueError(f"{label}必須是整數") from None
        if value < 1:
            raise ValueError(f"{label}必須大於 0")
        return value

    def _cancel_crawl(self) -> None:
        # 同一顆按鈕同時停爬取跟補抓信箱——兩者只會有一個在跑，因為彼此的
        # 開始按鈕在對方執行時都會被停用。
        if self.enrich_task is not None and self.enrich_task.running:
            self.enrich_task.cancel()
            self._append_log("取消中...")
            return
        if self.crawl_task is not None and self.crawl_task.running:
            self.crawl_task.cancel()
            self._append_log("取消中...")
            self.cancel_button.setEnabled(False)

    def _on_crawl_progress(self, payload: dict[str, Any]) -> None:
        self._append_log(
            f"[{payload.get('source')}] 第 {payload.get('page')} 頁 -- "
            f"目前已儲存 {payload.get('stored')} 筆"
        )

    def _on_crawl_done(self, summaries: list[CrawlSummary]) -> None:
        self._finish_crawl()
        self.results_table.set_rows(
            [
                {
                    "source": summary.source,
                    "status": label(summary.status, CRAWL_STATUS_LABELS),
                    "pages": summary.pages_crawled,
                    "found": summary.records_found,
                    "new": summary.records_new,
                    "merged": summary.records_updated,
                    "dupes": summary.records_duplicate,
                    "rejected": summary.records_invalid,
                    "warning": getattr(summary, "warning", None) or "",
                }
                for summary in summaries
            ]
        )
        self._append_log(f"完成 -- 已處理 {len(summaries)} 個來源。")

        # 表格只有一欄的寬度，訊息一定會被截掉；活動紀錄裡放完整的一句，
        # 而且要看得出來是哪一個來源。
        warned = [s for s in summaries if getattr(s, "warning", None)]
        for summary in warned:
            self._append_log(f"⚠ [{summary.source}] {summary.warning}")
        for summary in summaries:
            if getattr(summary, "resumed", False):
                self._append_log(
                    f"[{summary.source}] 這一次是接續上次沒跑完的地方，"
                    "已經抓過的部分沒有重跑。"
                )

        if warned:
            self.status(f"爬取完成，但有 {len(warned)} 個來源需要注意", "warning")
        else:
            self.status("爬取完成", "success")
        bump_data_version()

    def _on_crawl_error(self, exc: Exception) -> None:
        self._finish_crawl()
        self._append_log(f"錯誤：{exc}")
        self.report_error(exc)

    def _finish_crawl(self) -> None:
        self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.app.status_bar.stop_progress()

    # -------------------------------------------------------------- verify

    def _start_verify(self) -> None:
        if self.verify_task is not None and self.verify_task.running:
            return

        self._append_log("正在驗證所有紀錄...")
        self.verify_button.setEnabled(False)
        self.app.status_bar.start_progress()
        self.status("驗證中...", "normal")

        self.verify_task = BackgroundTask(
            self,
            worker=self.verify_controller.run,
            on_progress=self._on_verify_progress,
            on_done=self._on_verify_done,
            on_error=self._on_verify_error,
        )
        self.verify_task.start(True)

    def _on_verify_progress(self, payload: dict[str, Any]) -> None:
        self._append_log(f"驗證中 {payload.get('done', 0)}/{payload.get('total', 0)}...")

    def _on_verify_done(self, summary: VerifySummary) -> None:
        self._finish_verify()
        self._append_log(
            f"驗證完成 -- 已檢查 {summary.checked} 筆，有效 {summary.valid} 筆，"
            f"已更新 {summary.updated} 筆。"
        )
        self.status("驗證完成", "success")
        bump_data_version()

    def _on_verify_error(self, exc: Exception) -> None:
        self._finish_verify()
        self._append_log(f"錯誤：{exc}")
        self.report_error(exc)

    def _finish_verify(self) -> None:
        self.verify_button.setEnabled(True)
        self.app.status_bar.stop_progress()

    # ------------------------------------------------------------ 補抓信箱

    def _start_enrich(self) -> None:
        if self.enrich_task is not None and self.enrich_task.running:
            return

        try:
            pending = self.enrich_controller.missing_email_count()
        except CRMError as exc:
            self.report_error(exc)
            return

        if not pending:
            self.status("沒有「有網址、缺信箱」的公司需要補抓", "success")
            return

        # 每家公司都是各自獨立的網域、各自的 robots.txt 跟各自的禮貌延遲，
        # 所以這是「幾分鐘」等級的工作，不是幾秒鐘。開始之前就講清楚，
        # 好過讓視窗看起來像卡住了。
        minutes = max(1, round(pending * self._seconds_per_company() / 60))
        reply = QMessageBox.question(
            self,
            "補抓信箱",
            f"有 {pending} 家公司有網址但沒有信箱。\n\n"
            "程式會逐一連到它們自己的網站，找公開刊登的聯絡信箱。\n"
            "每個網站都會各自檢查 robots.txt 並遵守請求間隔，"
            f"因此大約需要 {minutes} 分鐘。\n\n"
            "要開始嗎？（過程中可以按「取消」停止）",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._append_log(f"開始補抓信箱，共 {pending} 家公司...")
        self.enrich_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.app.status_bar.start_progress()
        self.status("補抓信箱中...", "normal")

        self.enrich_task = BackgroundTask(
            self,
            worker=self.enrich_controller.run,
            on_progress=self._on_enrich_progress,
            on_done=self._on_enrich_done,
            on_error=self._on_enrich_error,
        )
        self.enrich_task.start()

    def _seconds_per_company(self) -> float:
        """粗估每家公司要花幾秒，用來給「大約要幾分鐘」的估計。

        大致是每家公司兩個請求（首頁，然後一個聯絡頁），乘上設定的禮貌延遲。
        """
        crawler = self.enrich_controller.config.crawler
        return max(1.0, crawler.delay_seconds + crawler.delay_jitter / 2) * 2

    def _on_enrich_progress(self, payload: dict[str, Any]) -> None:
        self._append_log(
            f"[{payload.get('done', 0)}/{payload.get('total', 0)}] {payload.get('name', '')}"
        )

    def _on_enrich_done(self, summary: Any) -> None:
        self._finish_enrich()
        self._append_log(
            f"補抓完成 -- 檢查 {summary.considered} 家，找到 {summary.emails_found} 個信箱，"
            f"更新 {summary.updated} 家"
            + (
                f"，{summary.skipped_robots} 家被 robots.txt 擋下"
                if summary.skipped_robots
                else ""
            )
        )
        self.status(f"補抓完成，新增 {summary.updated} 個信箱", "success")
        bump_data_version()

    def _on_enrich_error(self, exc: Exception) -> None:
        self._finish_enrich()
        self._append_log(f"錯誤：{exc}")
        self.report_error(exc)

    def _finish_enrich(self) -> None:
        self.enrich_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.app.status_bar.stop_progress()

    # ------------------------------------------------------------ custom sources

    def _open_source_wizard(self) -> None:
        dialog = SourceWizardDialog(self, self.source_controller, on_saved=self._on_source_saved)
        dialog.exec()

    def _open_source_manager(self) -> None:
        dialog = CustomSourcesDialog(
            self, self.source_controller, on_changed=self._on_source_saved
        )
        dialog.exec()

    def _on_source_saved(self, _name: str) -> None:
        """網址精靈存好（或刪掉）一個自訂來源之後，重新整理來源下拉選單。

        自訂來源存在 ``custom_sources.yaml``，會併進快取住的
        :class:`~core.config.AppConfig`——所以要先丟掉快取，來源清單才會
        反映最新狀態。
        """
        from core.config import reset_config

        reset_config()
        self.crawl_controller = CrawlController()
        try:
            source_names = self.crawl_controller.source_names()
        except CRMError as exc:
            self.report_error(exc)
            source_names = []

        self.source_combo.set_values([ALL_ENABLED, *source_names])

    # ------------------------------------------------------------------ log

    def _clear_log(self) -> None:
        self.log_box.clear()

    def _append_log(self, line: str) -> None:
        self.log_box.appendPlainText(line)


SOURCE_COLUMNS = [
    ("name", "名稱", 140),
    ("type", "類型", 100),
    ("start_url", "起始網址", 260),
    ("enabled", "啟用", 60),
]


class CustomSourcesDialog(QDialog):
    """列出所有透過網址精靈儲存的來源，可以選一個刪除。"""

    def __init__(
        self,
        parent: QWidget | None,
        controller: SourceWizardController,
        on_changed: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.on_changed = on_changed

        self.setWindowTitle("管理自訂來源")
        self.resize(640, 420)
        self.setMinimumSize(560, 360)
        self.setModal(True)

        layout = QVBoxLayout(self)
        self.table = DataTable(columns=SOURCE_COLUMNS)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)

        delete_button = QPushButton("刪除選取的來源")
        delete_button.setStyleSheet(
            f"background-color: {theme.pick(theme.DANGER)}; color: white;"
        )
        delete_button.clicked.connect(self._delete_selected)
        buttons.addWidget(delete_button)

        close_button = QPushButton("關閉")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self._refresh()

    def _refresh(self) -> None:
        sources = self.controller.custom_sources()
        self.table.set_rows(
            [
                {
                    "name": source.get("name", ""),
                    "type": source.get("type", ""),
                    "start_url": source.get("start_url", ""),
                    "enabled": source.get("enabled", True),
                }
                for source in sources
            ]
        )

    def _delete_selected(self) -> None:
        row = self.table.selected_row()
        if row is None:
            return
        name = row["name"]
        reply = QMessageBox.question(self, "刪除自訂來源", f"確定要刪除「{name}」嗎？")
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.controller.delete(name)
        self._refresh()
        if self.on_changed:
            self.on_changed(name)
