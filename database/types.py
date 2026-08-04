"""個資欄位的透明加密型別。

:class:`EncryptedString` 是一層 SQLAlchemy ``TypeDecorator``：寫入資料庫時加密、
讀出時解密。Repository、匯出、GUI 全部照舊拿到明文，不需要知道底下有加密這回事
——這正是把加密放在型別層而不是散在各個 Repository 方法裡的理由。

## 哪些欄位加密

加密的是**個人資料**：信箱、電話、地址、聯絡人姓名、備註、寄信內容。
維持明文的是**商業識別資訊**：公司名稱、統一編號、產業別、網站、資料來源。

這條線是刻意畫的。《個人資料保護法》規範的是得以識別自然人的資料，公司名稱與
統一編號不在其中；而把公司名稱留在明文，`WHERE company_name LIKE '%…%'` 這種
模糊搜尋才能交給 SQLite 去做。加密欄位的模糊搜尋改在 Python 端執行
（見 :mod:`database.repository`），在桌面應用的資料量下沒有問題。

## 為什麼等值查詢還能用

:mod:`core.crypto` 用的是確定性加密：相同明文永遠得到相同密文。因此
``WHERE email = ?`` 只要讓參數走過同一層加密就會命中，唯一索引、去重鍵、
``ix_companies_name_phone`` 複合索引全部不必改寫。

不能用的只有**大小比較與模糊比對**——密文的字典序與明文無關，所以
``ORDER BY email`` 與 ``email LIKE '%abc%'`` 都必須繞到 Python 端。

## 開關

``config.yaml`` 的 ``database.encrypt``。關掉之後**新寫入**的資料是明文，但既有
密文仍然讀得出來（解密會自動略過沒有前綴的值）。真正要讓資料庫回到全明文，得跑
:func:`database.encryption.apply`，那也是啟動時自動做的事。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import String, Text, func
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.types import TypeDecorator

from core import crypto

_active: bool | None = None


def encryption_active() -> bool:
    """現在寫入資料庫的值到底會不會被加密。

    兩個條件都要成立：設定檔打開了 ``database.encrypt``，而且這台機器真的能加密
    （裝了 cryptography、系統憑證保管庫可用）。結果會快取，因為每一個繫結參數都
    會問一次。
    """
    global _active
    if _active is None:
        _active = _compute_active()
    return _active


def _compute_active() -> bool:
    from core.config import get_config

    try:
        if not get_config().database.encrypt:
            return False
    except Exception:
        # 設定壞掉時不要順手把資料寫成密文——那會讓問題更難救回來。
        return False
    return crypto.available()


def reset_encryption_state() -> None:
    """清掉快取，讓下一次 :func:`encryption_active` 重新判斷。

    設定變更後與測試中呼叫。
    """
    global _active
    _active = None


class _EncryptedValue:
    """加解密邏輯本體，由下面兩個具體型別共用。"""

    #: 信箱要在加密前轉小寫，否則 ``a@X.com`` 與 ``a@x.com`` 會變成兩份不同的
    #: 密文，等值查詢與唯一索引就抓不到同一個人。
    lowercase = False

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        text = str(value)
        if self.lowercase:
            text = text.strip().lower()
        return crypto.encrypt(text) if encryption_active() else text

    def process_result_value(self, value: Any, dialect: Any) -> str | None:
        # 解密永遠執行，不看開關：尚未遷移的明文會原樣通過，而剛關掉加密、
        # 資料還沒轉回去的那段期間，密文也還讀得出來。
        return crypto.decrypt(value)


class EncryptedString(_EncryptedValue, TypeDecorator):
    """加密的 ``VARCHAR``。``length`` 以**明文**字元數指定。"""

    impl = String
    cache_ok = True

    def __init__(self, length: int | None = None, lowercase: bool = False) -> None:
        self.lowercase = lowercase
        super().__init__(crypto.ciphertext_length(length) if length else None)


class EncryptedText(_EncryptedValue, TypeDecorator):
    """加密的 ``TEXT``，給沒有長度上限的備註與信件內文。"""

    impl = Text
    cache_ok = True


def is_encrypted_column(column: Any) -> bool:
    """這個欄位是不是加密欄位。

    看的是**型別宣告**而不是 :func:`encryption_active`：即使加密被關掉，仍然一律
    走 Python 端的搜尋與排序。兩條路徑在不同設定下給出不同結果，比多花那點時間
    危險得多。
    """
    return isinstance(getattr(column, "type", None), _EncryptedValue)


def email_equals(column: Any, address: str | None) -> ColumnElement[bool]:
    """信箱的等值比對，加密與明文欄位都適用。

    明文欄位用 ``lower()`` 做大小寫不敏感比對；加密欄位不能這樣做（密文轉小寫毫
    無意義），改為靠 :class:`EncryptedString` 在加密前先轉小寫，兩邊自然對齊。
    """
    clean = (address or "").strip().lower()
    if is_encrypted_column(column):
        return column == clean
    return func.lower(column) == clean
