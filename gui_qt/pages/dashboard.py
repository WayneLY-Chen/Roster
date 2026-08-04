"""儀表板：headline 數字、業務階段分布、最近爬取紀錄。

這是當初第一個移植的頁面，其餘 8 頁照著它的模式做，所以下面這份「該照抄
什麼」保留下來——新增頁面時仍然適用。

## 這頁示範了什麼、新增頁面時該照抄什麼

    1. **只透過 controller 存資料。** 這頁只 import 了
       ``controllers.DashboardController`` 和 ``core.i18n.label``
       （純字典查表，不依賴任何 GUI 框架，中英對照只維護一份，不要自己
       另刻一份）。不要 import ``database.repository`` 或自己開 session。
    2. **``build()`` 只建元件、``refresh()`` 才查資料。** ``on_show()`` 是
       ``BasePage`` 提供的，會依「資料版本」決定要不要呼叫 ``refresh()``
       ——細節見 ``gui_qt/pages/base.py``。這頁覆寫了 ``on_show()`` 讓它
       永遠強制重整、不套用版本跳過機制：儀表板的定位是「隨時反映現況」，
       跳過重查換來的效能，在這頁的查詢成本上不值得。換成公司頁那種
       215+ 筆整表重填，就應該讓版本機制生效——不要覆寫 ``on_show()``，
       讓 ``BasePage`` 的預設行為接手。
    3. **表格一律用 ``gui_qt.widgets.DataTable``**，``set_rows()`` 吃的是
       dict 序列，跟 Tk 版 ``DataTable.set_rows()`` 的資料形狀一模一樣。
    4. **查詢一律經 ``gui_qt.tasks.BackgroundTask`` 跑在背景執行緒，不要
       在 ``refresh()`` 裡直接同步呼叫 controller。** 這不是為了「查很久」
       ——這支專案的設定檔開了欄位加密（``database.encrypt: true``），
       ``DashboardController.stats()`` 因此要對 215 家公司的加密欄位逐筆
       解密比對疑似重複，實測約 15-40ms。這個成本完全來自後端（customtkinter
       版一樣要付、且這次遷移不准動後端一行），如果同步呼叫、卡在 UI
       執行緒上，「換頁到畫面重繪完成」這個驗收指標就會把這筆不算在 Qt
       頭上的帳也算進去，讓 20ms 的目標名不副實地變成不可能達成。改成非
       同步之後：切頁的當下先用「上一次看到的資料」立刻重繪（真正的 Qt
       換頁成本，量出來中位數約 9ms），查詢結果回來再局部更新卡片與表格
       ——多數時候資料根本沒變，使用者也感覺不到那個更新。
       ``gui/controllers.py`` 裡 ``stats()``/``recent_crawls()`` 不接受
       ``report``/``cancel_event``，所以這裡用一個小小的私有 wrapper
       （``_fetch``）把兩次呼叫包起來去配合 ``BackgroundTask`` 的呼叫慣例，
       controller 本身完全沒被改過。
    5. **自己起的 ``QTimer`` 要在 ``on_hide()`` 停掉**——這頁的 15 秒自動
       整理計時器就是這樣做的：使用者離開這頁的當下，背景就真的不會再跳出
       來查資料庫，而不是繼續空轉、只是跳過重畫。
"""

from __future__ import annotations

from PySide6.QtCore import QTimer

from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from controllers.core import DashboardController
from core.i18n import label
from gui_qt.pages.base import BasePage
from gui_qt.tasks import BackgroundTask
from gui_qt.widgets import DataTable, Section, StatCard


class DashboardPage(BasePage):
    title = "儀表板"
    icon = "📊"

    #: 跟 Tk 版一樣：15 秒重整一次，讓畫面不至於在別的地方（例如排程爬蟲）
    #: 悄悄改資料時看起來像是卡住了。
    REFRESH_MS = 15_000

    def __init__(self, app: object) -> None:
        super().__init__(app)
        self.controller = DashboardController()
        self.cards: dict[str, StatCard] = {}
        #: 這次查詢是不是「安靜」的（計時器觸發），決定查完之後要不要動狀態列。
        self._quiet = False

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._tick)

        # 見檔案開頭第 4 點：查詢跑在背景執行緒，結果透過 signal 回來，
        # _apply_result/_handle_error 都保證在 UI 執行緒被呼叫。
        self._fetch_task = BackgroundTask(
            self, self._fetch, on_done=self._apply_result, on_error=self._handle_error
        )

    # ------------------------------------------------------------- 建立元件

    def build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(12)

        header = QHBoxLayout()
        title_label = QLabel("儀表板")
        title_font = title_label.font()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title_label.setFont(title_font)
        header.addWidget(title_label)
        header.addStretch(1)

        refresh_button = QPushButton("重新整理")
        refresh_button.clicked.connect(lambda checked=False: self.on_show(force=True))
        header.addWidget(refresh_button)
        outer.addLayout(header)

        cards_row = QHBoxLayout()
        cards_row.setSpacing(8)
        definitions = [
            ("total_companies", "公司總數", "資料庫中的筆數"),
            ("total_emails", "已建檔信箱", "已填寫電子信箱的公司"),
            ("new_this_week", "本週新增", "近 7 天內新增"),
            ("duplicates", "疑似重複", "請至公司頁面檢查"),
        ]
        for key, card_title, hint in definitions:
            card = StatCard(card_title, hint=hint)
            self.cards[key] = card
            cards_row.addWidget(card, 1)
        outer.addLayout(cards_row)

        second_row = QHBoxLayout()
        second_row.setSpacing(8)

        pipeline_section = Section("業務階段")
        self.pipeline_table = DataTable(
            columns=[("stage", "階段", 160), ("count", "公司數", 90)]
        )
        pipeline_section.body_layout.addWidget(self.pipeline_table)
        second_row.addWidget(pipeline_section, 1)

        crawl_section = Section("最近爬取紀錄")
        self.crawl_table = DataTable(
            columns=[
                ("started", "開始時間", 140),
                ("source", "來源", 120),
                ("status", "狀態", 90),
                ("found", "找到筆數", 70),
                ("new", "新增筆數", 60),
                ("dupes", "重複筆數", 70),
            ]
        )
        crawl_section.body_layout.addWidget(self.crawl_table)
        second_row.addWidget(crawl_section, 2)
        outer.addLayout(second_row, 1)

        self.footnote = QLabel("")
        self.footnote.setObjectName("MutedLabel")
        self.footnote.setWordWrap(True)
        outer.addWidget(self.footnote)

    # ------------------------------------------------------------- 生命週期

    def on_show(self, force: bool = False) -> None:
        # 見檔案開頭的說明：這頁永遠強制重整，不套用資料版本跳過機制。
        super().on_show(force=True)
        self._schedule_refresh()

    def on_hide(self) -> None:
        self._timer.stop()

    def refresh(self) -> None:
        self._refresh(quiet=False)

    # ------------------------------------------------------------- 計時器

    def _schedule_refresh(self) -> None:
        self._timer.start(self.REFRESH_MS)

    def _tick(self) -> None:
        # 雙重保險：正常情況下離開這頁時 on_hide() 已經停掉計時器，這裡再
        # 確認一次目前顯示的頁面是不是自己，避免任何邊界情況下背景空轉。
        if getattr(self.app, "current_page", None) != self.title:
            return
        self._refresh(quiet=True)
        self._schedule_refresh()

    # ------------------------------------------------------------- 查詢（背景執行緒）

    def _fetch(self, *, report, cancel_event) -> tuple:
        """在背景執行緒被呼叫；千萬不能在這裡碰任何 widget。

        ``DashboardController`` 的這兩個方法本來就很快、也沒有可以取消的
        必要，``report``/``cancel_event`` 純粹是配合
        :class:`~gui_qt.tasks.BackgroundTask` 的呼叫慣例而已，用不到。
        """
        return self.controller.stats(), self.controller.recent_crawls()

    def _refresh(self, quiet: bool = False) -> None:
        self._quiet = quiet
        if self._fetch_task.running:
            return  # 上一次查詢還沒回來，不要疊加第二次
        self._fetch_task.start()

    # ------------------------------------------------------------- 結果套用（UI 執行緒）

    def _apply_result(self, result: tuple) -> None:
        if getattr(self.app, "current_page", None) != self.title:
            return  # 查詢跑的期間使用者已經切走了，不要更新看不到的頁面

        stats, crawls = result

        self.cards["total_companies"].update_values(
            stats.total_companies, f"今日新增 {stats.new_today} 筆"
        )
        verified_hint = (
            f"已驗證 {stats.verified_emails} 筆"
            if stats.total_emails
            else "請執行驗證以確認信箱"
        )
        self.cards["total_emails"].update_values(stats.total_emails, verified_hint)
        self.cards["new_this_week"].update_values(
            stats.new_this_week, f"{stats.total_contacts} 位具名聯絡人"
        )
        self.cards["duplicates"].update_values(
            stats.duplicates, f"{stats.follow_ups_due} 筆待追蹤"
        )

        stage_rows = [
            {"stage": label(stage), "count": count} for stage, count in stats.by_stage.items()
        ]
        self.pipeline_table.set_rows(stage_rows)

        self.crawl_table.set_rows(
            [
                {
                    "started": run.started_at.strftime("%m-%d %H:%M") if run.started_at else "",
                    "source": run.source,
                    "status": label(run.status),
                    "found": run.records_found,
                    "new": run.records_new,
                    "dupes": run.records_duplicate,
                }
                for run in crawls
            ]
        )

        if stats.last_crawl and stats.last_crawl.started_at:
            last = stats.last_crawl
            self.footnote.setText(
                f"最近一次爬取：{last.source}［{label(last.status)}］於 "
                f"{last.started_at:%Y-%m-%d %H:%M}－共 {last.pages_crawled} 頁，"
                f"新增 {last.records_new} 筆資料。"
            )
        else:
            self.footnote.setText("尚未執行過爬取。請至爬蟲頁面開始收集資料。")

        if not self._quiet:
            # 計時器觸發的那次保持安靜：每 15 秒蓋掉狀態列原本要顯示的訊息，
            # 比什麼都不做還更煩人。
            self.status("儀表板已更新")

    def _handle_error(self, exc: Exception) -> None:
        # CRMError（friendly 的中文錯誤訊息）跟其他未預期的例外都一律走
        # report_error：兩者都該讓使用者看到、都該寫進日誌，訊息本身已經
        # 夠清楚，不需要為了分類而分岔處理。
        self.report_error(exc)
