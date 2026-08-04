"""改用作業系統的憑證信任庫來驗證 HTTPS。

為什麼需要這個：Python 自帶的 `certifi` 憑證庫搭配 OpenSSL 3.x 對憑證鏈的
檢查較嚴格，而**很多台灣網站**（政府開放資料 API、貿協、各種黃頁）使用
TWCA 台灣網路認證簽發的憑證，其中介憑證缺少 Subject Key Identifier 欄位，
於是連線一律失敗並回報：

    certificate verify failed: Missing Subject Key Identifier

這不是對方網站有問題，而是驗證路徑的差異。Windows 的憑證存放區本來就內含
TWCA 根憑證，也能正確處理這些憑證鏈。

**重點是：這不會降低安全性。** 憑證仍然被完整驗證，只是改由作業系統
（Windows SChannel / macOS Security.framework）執行——那正是瀏覽器採用的
同一套機制。任何情況下都不會停用驗證。
"""

from __future__ import annotations

from core.constants import LogCategory
from core.logging_setup import get_logger

log = get_logger(LogCategory.CRAWL)

_installed = False


def install_os_trust_store() -> bool:
    """讓所有 HTTPS 連線改用系統憑證信任庫。回傳是否成功套用。

    可重複呼叫。必須在建立任何 SSL 連線之前執行，因此由程式進入點
    （CLI 的 ``_bootstrap`` 與 GUI 的 ``run_gui``）負責呼叫。
    """
    global _installed
    if _installed:
        return True

    try:
        import truststore
    except ImportError:
        log.debug("未安裝 truststore，沿用 Python 內建憑證庫")
        return False

    try:
        truststore.inject_into_ssl()
    except Exception as exc:  # pragma: no cover - 取決於平台
        log.warning("無法套用系統憑證信任庫，沿用內建憑證庫：{}", exc)
        return False

    _installed = True
    log.debug("已改用作業系統憑證信任庫驗證 HTTPS")
    return True
