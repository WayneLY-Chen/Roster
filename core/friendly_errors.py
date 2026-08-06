"""把例外翻成「使用者看得懂、而且看了知道要做什麼」的一句中文。

為什麼需要這一層：資料庫層丟出來的例外，字串裡帶著**整句 SQL 與所有參數**。
那些參數包含加密後的欄位——也就是說，介面上會出現一整段密文。使用者看到的
是這種東西：

    IntegrityError: (sqlite3.IntegrityError) UNIQUE constraint failed:
    contacts.company_id, contacts.name
    [SQL: INSERT INTO contacts (company_id, name, email_encrypted, ...)]
    [parameters: (3, '王小明', b'gAAAAABm...', ...)]

三個問題：看不懂、沒說要怎麼辦、而且把不該給人看的東西給人看了。截圖一貼出去
就外流了。

原始訊息不是丟掉，是寫進日誌——要修東西的時候還是得看得到完整的內容。
"""

from __future__ import annotations

import re

from core.errors import (
    BackupError,
    CRMError,
    ExportError,
    GmailError,
    RobotsDisallowedError,
    SourceConfigError,
    VerificationError,
)
from core.constants import LogCategory
from core.logging_setup import get_logger

log = get_logger(LogCategory.GUI)

#: 資料庫的唯一性限制 → 使用者這一步實際上做了什麼。
#:
#: key 是限制名稱裡會出現的字（SQLite 會把它寫進訊息），value 是那件事的說法。
_UNIQUE_HINTS: tuple[tuple[str, str], ...] = (
    ("contacts.company_id, contacts.name", "這家公司底下已經有同名的聯絡人了。"),
    ("companies.dedupe_key", "這家公司已經在名單裡了。"),
    ("companies.tax_id", "已經有另一家公司用這個統一編號了。"),
    ("attachments", "這個附件已經加過了。"),
    ("templates.name", "已經有同名的範本了，換一個名字。"),
)

#: 判斷「這是資料庫的唯一性衝突」。不 import sqlalchemy——這個模組要能在沒有
#: 資料庫的情境下（例如只跑匯出）被載入。
_UNIQUE_MARKERS = ("UNIQUE constraint failed", "IntegrityError", "duplicate key")

#: 訊息裡把 SQL 與參數切掉的位置。SQLAlchemy 一律用這兩個標記接在後面。
_NOISE = re.compile(r"\s*\[(SQL|parameters|parameters:)\b.*", re.S)


def friendly(exc: BaseException) -> str:
    """這個例外要顯示給使用者的那一句話。

    原則：
    1. 說發生了什麼（用使用者的語彙，不是資料表的語彙）。
    2. 能講「怎麼辦」就講。
    3. **絕不**把 SQL、參數、堆疊、加密後的內容放進來。
    """
    log.debug("原始例外：{}: {}", type(exc).__name__, exc)
    raw = str(exc)

    if isinstance(exc, RobotsDisallowedError):
        return (
            "這個網站的 robots.txt 不允許自動抓取這個網址，所以沒有動作。"
            "請改用網站提供的其他方式取得資料。"
        )
    if isinstance(exc, SourceConfigError):
        return f"這個爬蟲來源的設定還不能存：{_trim(raw)}"
    if isinstance(exc, GmailError):
        return f"Gmail 那邊沒有成功：{_trim(raw)}"
    if isinstance(exc, ExportError):
        return f"匯出沒有完成：{_trim(raw)}"
    if isinstance(exc, BackupError):
        return f"備份沒有完成：{_trim(raw)}"
    if isinstance(exc, VerificationError):
        return f"驗證沒有完成：{_trim(raw)}"

    if any(marker in raw for marker in _UNIQUE_MARKERS):
        for needle, sentence in _UNIQUE_HINTS:
            if needle in raw:
                return sentence
        return "這一筆跟名單裡已經有的資料重複了，沒有存進去。"

    if "no such column" in raw or "no such table" in raw:
        return (
            "資料庫的欄位跟這個版本對不起來。"
            "關掉程式再開一次，開啟時會自動補上缺少的欄位。"
        )
    if "database is locked" in raw:
        return "資料庫正在被別的地方使用，稍等一下再試一次。"
    if "InvalidToken" in type(exc).__name__ or "InvalidToken" in raw:
        return (
            "解不開加密的欄位——通常是金鑰檔換過了。"
            "把原本那一份金鑰放回去就讀得到了。"
        )

    if isinstance(exc, CRMError):
        return _trim(raw) or "沒有成功，請再試一次。"

    # 到這裡代表是預期外的東西。仍然不要把原始訊息整段倒出來：它可能夾帶路徑
    # 或參數。給一句話，細節留在日誌裡。
    log.warning("沒有對應說法的例外：{}: {}", type(exc).__name__, exc)
    return "發生了預期外的問題，這一步沒有完成。詳細內容記在日誌裡。"


def _trim(message: str) -> str:
    """把 SQL、參數與換行壓掉，只留人看得懂的那一段。"""
    cleaned = _NOISE.sub("", message).strip()
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > 200:
        cleaned = cleaned[:200] + "…"
    return cleaned
