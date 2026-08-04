"""在主執行緒把重量級模組先 import 起來，背景執行緒才不會踩到 PySide6 的 import 掛勾。

## 為什麼需要這個

控制器層刻意用延遲 import：`CrawlController.run()` 進到函式裡才
`from crawler.pipeline import crawl`，讓不爬蟲的人不用付 httpx / lxml 的
啟動成本。這在 Tk 版沒問題。

換成 PySide6 之後就會出事，而且是整個行程被中止、連例外都接不到的那種：

    Thread (pooled):
      inspect.getsourcefile
      shibokensupport/feature.py:158  _mod_uses_pyside
      shibokensupport/signature/loader.py:71  feature_imported
      httpx/_models.py:11  <module>
      <frozen importlib._bootstrap>

PySide6 會裝一個 import 掛勾（`feature_imported`），每有模組被 import 就
執行一次，而它會去做 `inspect.getsource`。從 `QThreadPool` 借出來的執行緒
觸發這條路徑時，行程直接 `Fatal Python error: Aborted`。

症狀是間歇的——同一個模組只要先前已經被 import 過就不會再走掛勾，所以有時
候正常、有時候整個測試套件無聲消失。實際重現過：修好前連跑 12 輪、輪輪都
中；先前查了兩個錯的方向（資料庫連線被 dispose、loguru 的 enqueue 佇列）
都不是原因，兩者的壓力測試各跑幾百輪都沒事，正是因為那些腳本一開始就把模組
import 完了。

## 做法

啟動時在主執行緒把這些模組全部 import 一次。之後背景執行緒再 import 都是
從 `sys.modules` 拿現成的，不會觸發掛勾。

延遲 import 的原始目的（啟動快）在桌面程式裡沒了——反正視窗開起來就要用。
"""

from __future__ import annotations

import importlib

from core.constants import LogCategory
from core.logging_setup import get_logger

log = get_logger(LogCategory.GUI)

#: 所有會在背景執行緒裡第一次被 import 的模組。來源是 controllers/ 底下
#: 所有寫在函式內的 import——那些函式都是 BackgroundTask 的 worker。
WORKER_MODULES: tuple[str, ...] = (
    # 爬取
    "crawler.pipeline",
    "crawler.enrich",
    "crawler.discover",
    "crawler.fetcher",
    "crawler.parser",
    # 驗證與去重
    "verifier.service",
    "verifier.dedupe",
    "verifier.normalize",
    # 匯入匯出
    "exporter.service",
    "exporter.base",
    "exporter.importer",
    "exporter.sample_template",
    # 寄信
    "gmail.sender",
    # 資料庫與憑證
    "database.encryption",
    "database.backup",
    "core.credentials",
    "core.crypto",
)


def preload() -> list[str]:
    """在主執行緒 import 全部 worker 會用到的模組。回傳失敗的模組名稱。

    失敗不擋啟動：少一個選用相依（例如沒裝 playwright）不該讓整個程式打不
    開，那個功能自己會在被用到時報錯。這裡只負責「先 import 過」。
    """
    failed: list[str] = []
    for name in WORKER_MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - 預先載入不該讓程式開不起來
            failed.append(name)
            log.warning("預先載入 {} 失敗（該功能被用到時才會報錯）：{}", name, exc)
    return failed
