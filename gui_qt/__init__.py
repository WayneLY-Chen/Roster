"""PySide6 介面層——本專案唯一的介面。

原本還有一套 ``gui/``（customtkinter），9 頁全部移植完成後就整包刪掉了。
本套件各處的註解仍會拿「Tk 版」當對照，說明某個設計決定當初為什麼要那樣做；
那些 ``gui/...`` 路徑指的是**已經不存在的舊實作**，是設計理由的出處，不是
還能打開來看的檔案。

分層規則（刪掉 Tk 版之後依然成立）：

    * 後端（``core``/``crawler``/``database``/``gmail``/``exporter``/``verifier``）
      完全不知道自己被介面怎麼呼叫，一行都沒有為了這裡而改。
    * 存取資料一律經過 ``controllers/``——不依賴任何 GUI 框架的 MVC 控制器層，
      原本住在 ``gui/controllers.py``，Tk 版要刪掉時搬成獨立套件。
    * 中英對照走 ``core.i18n``（純字典查表），只維護一份。
    * 頁面不 import ``database.repository``、不自己開 session。

換頁延遲是這次遷移唯一要驗收的數字：customtkinter 每個 widget 都要花約 1ms
畫一次圓角矩形（郵件頁 222 個 widget、換頁 205ms 是量出來的），Qt 用
model/view 與 QStackedWidget，同樣的頁面換頁量到中位數 5ms 等級。目標是
20ms 以內，實測 9 頁落在 3.3-7.6ms；本套件的每一個設計決定（延遲建頁、
``QAbstractTableModel`` 而非 ``QTableWidget``、背景工作一律走 signal 不碰
widget）都是為了不讓這個數字劣化下去。

模組地圖：

    gui_qt/theme.py         -- 字型與 QSS（亮／暗主題）
    gui_qt/widgets.py       -- 共用元件（Section、DataTable、StatCard……）
    gui_qt/assets.py        -- 應用程式圖示的尋找邏輯（含打包後的 _MEIPASS）
    gui_qt/tasks.py         -- 背景工作（QThreadPool），介面沿用
                               ``report=callable, cancel_event=threading.Event``
    gui_qt/company_detail.py -- 公司詳細資料對話框
    gui_qt/composer.py      -- 信件內文的格式化編輯器與放大編輯視窗
    gui_qt/source_wizard.py -- 新增爬取來源的精靈
    gui_qt/pages/base.py    -- 頁面生命週期基底類別 + 「資料版本」機制
    gui_qt/pages/*.py       -- 9 個頁面，順序見 ``gui_qt/app.py`` 的 PAGE_CLASSES
    gui_qt/app.py           -- 視窗外殼：側邊導覽 + QStackedWidget + 狀態列
"""

from __future__ import annotations
