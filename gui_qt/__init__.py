"""PySide6 介面層（實驗性）。

這是 ``gui/``（customtkinter）之外的第二套介面，兩者並存、互不相依：

    * 後端（``core``/``crawler``/``database``/``gmail``/``exporter``/``verifier``）
      完全不知道自己被哪一套介面呼叫，一行都沒有為了這裡而改。
    * 存取資料一律經過 ``gui.controllers``（唯一允許 import 的 ``gui/`` 模組，
      因為它本來就是不依賴 Tk 的乾淨 MVC 控制器層）；也可以 import
      ``gui.i18n``（純字典查表，同樣沒有 Tk 相依），中英對照只維護一份。
    * 除了 ``gui.controllers`` 和 ``gui.i18n`` 之外，不 import ``gui/`` 底下
      任何其他模組——那些都是 Tk 元件，混進來只會製造耦合。

換頁延遲是這次遷移唯一要驗收的數字：customtkinter 每個 widget 都要花約 1ms
畫一次圓角矩形（郵件頁 222 個 widget、換頁 205ms 是量出來的），Qt 用
model/view 與 QStackedWidget，同樣的頁面換頁量到中位數 5ms 等級。目標是
20ms 以內；本套件的每一個設計決定（延遲建頁、``QAbstractTableModel`` 而非
``QTableWidget``、背景工作一律走 signal 不碰 widget）都是為了不讓這個數字
劣化下去。

模組地圖：

    gui_qt/theme.py         -- 字型與 QSS（亮／暗主題）
    gui_qt/widgets.py       -- 共用元件（Section、DataTable、StatCard……）
    gui_qt/tasks.py         -- 背景工作（QThread），介面沿用
                               ``report=callable, cancel_event=threading.Event``
    gui_qt/pages/base.py    -- 頁面生命週期基底類別 + 「資料版本」機制
    gui_qt/pages/dashboard.py -- 示範頁：儀表板（唯一完整移植的頁面）
    gui_qt/pages/placeholder.py -- 尚未移植頁面的佔位頁
    gui_qt/app.py           -- 視窗外殼：側邊導覽 + QStackedWidget + 狀態列
"""

from __future__ import annotations
