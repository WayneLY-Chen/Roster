"""每個 Qt 頁面的生命週期基底類別。

沿用 ``gui/pages/base.py``（Tk 版 ``BasePage``）的兩個核心概念：

    ensure_built() -- 元件只在第一次顯示前建立一次，開啟視窗不用先跑完
                       九頁份的查詢才看得到畫面。
    on_show()      -- 每次頁面被切到最前面時呼叫，重新整理資料。

在這之上多一個 Tk 版沒有的東西：**資料版本機制**。

## 為什麼需要「資料版本」

Tk 版每次點側邊欄按鈕，``on_show()`` 都無條件整份重查資料庫、整表重填。
這支專案的驗收目標是換頁 20ms 以內；儀表板的查詢很便宜（單一 stats 查詢，
~30ms 內），所以它可以繼續「每次都重查」而不必在意這個機制。但後續要移植
的公司頁有 215+ 筆、聯絡人頁同樣是整表——如果每次點一下側邊欄都要整批
``search()`` 再 ``set_rows()`` 一次，查詢本身的時間就可能吃光 20ms 的預算，
跟換頁動作本身完全無關的成本卻要算在換頁延遲裡，並不合理。

做法：一個跨頁面共用的整數版本號 ``_data_version``。任何「會寫入資料庫」
的動作（新增/刪除/編輯一筆公司、匯入、爬蟲或驗證跑完……）成功之後，呼叫一次
:func:`bump_data_version`。頁面的 :meth:`BasePage.on_show` 會比較「這次的
版本號」跟「自己上次重整時記下的版本號」：

    * 版本沒變 -> 資料庫從上次看過之後沒有任何地方寫入過，直接跳過
      :meth:`BasePage.refresh`，改呼叫 :meth:`BasePage.on_reveal`
      （預設什麼都不做，頁面可以覆寫成「重啟自己的計時器」之類的輕量動作）。
    * 版本變了，或是第一次顯示，或呼叫端要求 ``force=True``
      -> 呼叫 :meth:`BasePage.refresh` 真正重新查詢。

這是一個「可以省則省」的選項，不是必須遵守的規則：完全不覆寫
``data_version`` 相關的東西、永遠讓版本比對失敗，效果就跟 Tk 版一樣，
每次都整份重查，不會因為忘記接這個機制而讓資料錯誤或過期。

## 給後續頁面的規則

    1. 把「建立元件」寫在 :meth:`build`，「查資料、重填畫面」寫在
       :meth:`refresh`——不要覆寫 :meth:`on_show` 本身（除非像儀表板一樣，
       有正當理由要略過版本機制，見 ``gui_qt/pages/dashboard.py`` 的說明）。
    2. 任何一個會修改資料庫的動作（新增、刪除、更新、匯入、爬蟲/驗證完成）
       做完之後，呼叫一次 ``bump_data_version()``。忘記呼叫不會讓程式壞掉，
       只會讓「其他頁面」多顯示一次舊資料，直到下次真的有理由重查為止；
       但養成呼叫的習慣，其他頁面才能安全地略過重查。
    3. 頁面自己起的 ``QTimer``（例如儀表板 15 秒一次的自動整理）要在
       :meth:`on_hide` 裡停掉，不要讓它在使用者離開這頁之後還在背景空轉。
"""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from core.constants import LogCategory
from core.data_version import bump as core_bump
from core.data_version import current as core_current
from core.logging_setup import get_logger

log = get_logger(LogCategory.GUI)


def bump_data_version() -> None:
    """通知所有頁面「資料庫被寫入過了」，下次顯示需要重新查詢。

    在任何成功的新增/刪除/編輯/匯入/爬蟲完成……之後呼叫一次。

    計數器本身放在 :mod:`core.data_version`，因為控制器層在寫入之後也會
    自己加一次。這裡曾經有一個獨立的計數器，結果是兩邊各數各的：不經過
    頁面的寫入（排程爬取、CLI 匯入）只會加到控制器那一個，畫面永遠不知道
    該重查。共用同一個計數器才不會有這種漏洞。

    一次寫入因此可能加超過一次（控制器一次、頁面一次）。這無所謂——頁面
    只比對「跟我上次算繪的版本是不是同一個」，加幾次沒有意義。測試也應該
    斷言「版本變大了」而不是「剛好加一」。
    """
    core_bump()


def current_data_version() -> int:
    """目前的資料版本號，供 :class:`BasePage` 內部比對使用。"""
    return core_current()


class BasePage(QWidget):
    """一個可導覽到的頁面。

    ``app`` 是 :class:`~gui_qt.app.MainWindow`，用來存取狀態列與跨頁導覽，
    跟 Tk 版 ``BasePage.app`` 的用途完全一樣。
    """

    #: 顯示在側邊欄與視窗標題。
    title: str = "Page"
    #: 側邊欄圖示（純文字/emoji，不依賴任何圖示資源檔）。
    icon: str = ""

    def __init__(self, app: object) -> None:
        super().__init__()
        self.app = app
        self._built = False
        self._seen_version = -1

    # -- 子類別覆寫這些 -----------------------------------------------------

    def build(self) -> None:
        """建立元件。只在第一次顯示前被呼叫一次。"""

    def refresh(self) -> None:
        """真正查資料、重繪畫面的地方。由 :meth:`on_show` 依版本判斷是否呼叫。"""

    def on_reveal(self) -> None:
        """版本沒變、跳過 :meth:`refresh` 時呼叫。預設什麼都不做。"""

    def on_hide(self) -> None:
        """頁面即將被換下去、換到別頁之前呼叫。預設什麼都不做。

        用來停掉這頁自己起的 ``QTimer`` 之類背景動作。
        """

    # -- 生命週期，通常不需要覆寫 --------------------------------------------

    def ensure_built(self) -> None:
        if not self._built:
            self.build()
            self._built = True

    def on_show(self, force: bool = False) -> None:
        """每次頁面被切到最前面時呼叫。

        ``force=True``：無論資料版本有沒有變，都真的重新查詢一次——用在
        頁面自己的「重新整理」按鈕，使用者按下去就是要一個保證最新的畫面。
        """
        if not force and self._seen_version == current_data_version():
            self.on_reveal()
            return
        self.refresh()
        self._seen_version = current_data_version()

    # -- 給頁面用的小工具 ----------------------------------------------------

    def status(self, message: str, tone: str = "normal") -> None:
        self.app.set_status(message, tone)

    def report_error(self, exc: Exception) -> None:
        log.error("{}: {}", type(exc).__name__, exc)
        self.app.set_status(f"{type(exc).__name__}: {exc}", "error")
