"""個資欄位加密。

## 為什麼是欄位級而不是整庫

SQLCipher 能對整個 SQLite 檔案透明加密，但它在 Python 3.14 / Windows 上沒有
預編譯套件，自行編譯需要 OpenSSL 與 C 編譯器——對一個要能直接複製給人用的
桌面程式不可行。因此改為只加密**個資欄位**（信箱、電話、地址、聯絡人姓名），
公司名稱、統編、產業等商業資訊維持明文。

這正好符合《個人資料保護法》關心的重點：受規範的是個人資料，不是公司名稱。

## 為什麼是「確定性」加密

一般 AES-GCM 每次用隨機 nonce，同一個信箱每次加密結果都不同。那會讓
`WHERE email = ?` 查不到、唯一索引失效、去重鍵無法比對——整個資料層都要改寫。

這裡改用 SIV 式構造：nonce 由 `HMAC(nonce_key, 明文)` 推導而非隨機產生，
因此**相同明文永遠得到相同密文**，等值查詢、唯一索引、去重全部照舊可用。

代價是會洩漏「這兩筆的信箱相同」這件事。這是刻意的取捨：常見的替代方案
（另存一個 HMAC 盲索引欄位供查詢）會洩漏完全一樣的資訊，卻還要多維護一組
欄位與遷移邏輯。拿到資料庫檔案的人仍然**無法還原任何信箱、電話或地址**。

## 金鑰

主金鑰由系統亂數產生，存在作業系統的憑證保管庫（Windows 認證管理員），
專案資料夾內不會有金鑰。**備份資料庫時請一併確認金鑰仍在**——換一台電腦
或清掉認證管理員後，加密欄位將無法還原。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

from core.constants import LogCategory
from core.errors import CRMError
from core.logging_setup import get_logger

log = get_logger(LogCategory.DATABASE)

#: 密文前綴。用來分辨「這欄已加密」與「這是尚未遷移的明文」。
PREFIX = "enc:v1:"

#: 憑證保管庫中的金鑰名稱。
KEY_NAME = "db_master_key"

#: 停用加密的環境變數（測試與疑難排解用）。
DISABLE_ENV_VAR = "CRM_DISABLE_ENCRYPTION"

_NONCE_BYTES = 12
_KEY_BYTES = 32
_TAG_BYTES = 16          # AES-GCM 驗證標籤

_cached_key: bytes | None = None


class EncryptionUnavailable(CRMError):
    """加密無法使用（缺套件或金鑰取不到）。"""


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover
        raise EncryptionUnavailable(
            "未安裝 cryptography 套件，無法加密。請執行：pip install cryptography"
        ) from exc
    return AESGCM


def encryption_disabled() -> bool:
    return os.getenv(DISABLE_ENV_VAR, "").strip().lower() in ("1", "true", "yes")


def available() -> bool:
    """目前環境是否真的能加密。"""
    if encryption_disabled():
        return False
    try:
        _aesgcm()
    except EncryptionUnavailable:
        return False
    from core.credentials import keyring_available

    return keyring_available()


def get_key(create: bool = True) -> bytes:
    """取得主金鑰；不存在時產生一把並存入保管庫。

    ``create=False`` 用於「只想知道有沒有金鑰」的場合，不會意外生出新的一把
    ——那會讓既有密文永遠解不開。
    """
    global _cached_key
    if _cached_key is not None:
        return _cached_key

    from core.credentials import get_secret, set_secret, SecretSource

    stored = get_secret(KEY_NAME)
    if stored:
        try:
            key = base64.urlsafe_b64decode(stored.encode())
        except (ValueError, TypeError) as exc:
            raise EncryptionUnavailable(f"保管庫中的金鑰格式損毀：{exc}") from exc
        if len(key) != _KEY_BYTES:
            raise EncryptionUnavailable("保管庫中的金鑰長度不正確")
        _cached_key = key
        return key

    if not create:
        raise EncryptionUnavailable("尚未建立資料庫金鑰")

    key = secrets.token_bytes(_KEY_BYTES)
    source = set_secret(KEY_NAME, base64.urlsafe_b64encode(key).decode())
    if source is not SecretSource.KEYRING:
        raise EncryptionUnavailable(
            "系統憑證保管庫不可用，無法安全保存資料庫金鑰。"
            "請改用 BitLocker 等磁碟加密，或設定 CRM_DISABLE_ENCRYPTION=1 停用欄位加密。"
        )
    log.warning(
        "已產生新的資料庫金鑰並存入系統憑證保管庫。"
        "更換電腦或清除認證管理員後，加密欄位將無法還原，請一併備份。"
    )
    _cached_key = key
    return key


def reset_key_cache() -> None:
    """清掉記憶體中的金鑰快取（測試與換金鑰時用）。"""
    global _cached_key
    _cached_key = None


def export_key() -> str:
    """把金鑰匯出成一串文字，讓使用者自己保管。

    金鑰只存在作業系統的憑證保管庫裡，而保管庫**跟著 Windows 使用者帳號走，
    不會跟著資料庫檔案一起被複製**。少了這個功能，硬碟壞掉重灌之後連
    ``backups/`` 裡的備份都解不開——備份還在，卻一個字都讀不出來。

    這比「搬家前先把整個資料庫解密回明文」實際得多：匯出一次收好，之後換機、
    重灌、還原備份都只要匯入回去。

    不會產生新金鑰：沒有金鑰時直接報錯，而不是生一把讓人以為備份成功了。
    """
    return base64.urlsafe_b64encode(get_key(create=False)).decode("ascii")


def import_key(value: str, force: bool = False) -> None:
    """把匯出的金鑰寫回保管庫。

    保管庫裡已經有**不同**的金鑰時會拒絕，除非 ``force=True``——覆蓋掉舊金鑰
    會讓現有的密文全部變成永遠讀不出來的亂碼，那不該是一個打錯字就會發生的事。
    """
    global _cached_key

    text = (value or "").strip()
    if not text:
        raise EncryptionUnavailable("金鑰不可為空")

    try:
        key = base64.urlsafe_b64decode(text.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise EncryptionUnavailable(f"金鑰格式不正確：{exc}") from exc
    if len(key) != _KEY_BYTES:
        raise EncryptionUnavailable(
            f"金鑰長度不正確（應為 {_KEY_BYTES} 位元組，實際 {len(key)}）"
        )

    from core.credentials import SecretSource, get_secret, set_secret

    existing = get_secret(KEY_NAME)
    if existing and existing.strip() != text and not force:
        raise EncryptionUnavailable(
            "保管庫中已經有一把不同的金鑰。覆蓋它會讓現有的加密資料永遠無法還原；"
            "確定要覆蓋請加上 --force。"
        )

    if set_secret(KEY_NAME, text) is not SecretSource.KEYRING:
        raise EncryptionUnavailable("系統憑證保管庫不可用，無法寫入金鑰")

    _cached_key = key
    log.info("資料庫金鑰已匯入系統憑證保管庫")


def _derive_nonce(key: bytes, plaintext: bytes) -> bytes:
    """由明文推導 nonce，使加密具確定性（SIV 式構造）。

    用獨立的子金鑰，避免 nonce 推導與加密共用同一把金鑰。
    """
    subkey = hmac.new(key, b"nonce-derivation", hashlib.sha256).digest()
    return hmac.new(subkey, plaintext, hashlib.sha256).digest()[:_NONCE_BYTES]


def is_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith(PREFIX)


def ciphertext_length(plaintext_length: int) -> int:
    """``plaintext_length`` 個字元加密後最長會佔幾個字元。

    用來宣告加密欄位的 ``VARCHAR`` 長度。SQLite 本身不強制長度，但把它算對，
    schema 才誠實反映實際存放的內容，日後換到會強制長度的資料庫也不會被截斷。

    加密是對 **UTF-8 位元組**做的，欄位長度卻是以**字元**計。中文一個字佔 3 個
    位元組，所以這裡以 3 倍取上界——寧可宣告得寬鬆，也不要在地址欄塞滿中文時
    才發現不夠長。
    """
    raw = _NONCE_BYTES + plaintext_length * 3 + _TAG_BYTES
    return len(PREFIX) + ((raw + 2) // 3) * 4


def encrypt(plaintext: str | None) -> str | None:
    """加密一段文字。``None`` 與空字串原樣回傳。"""
    if plaintext is None or plaintext == "":
        return plaintext
    if is_encrypted(plaintext):
        return plaintext          # 已經加密過，不重複加密

    key = get_key()
    raw = plaintext.encode("utf-8")
    nonce = _derive_nonce(key, raw)
    ciphertext = _aesgcm()(key).encrypt(nonce, raw, None)
    return PREFIX + base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def has_key() -> bool:
    """保管庫裡是否已經有金鑰。不會順手生一把。"""
    try:
        get_key(create=False)
    except EncryptionUnavailable:
        return False
    return True


def decrypt(token: str | None) -> str | None:
    """解密。**尚未加密的明文原樣回傳**，讓遷移前後的資料都讀得到。

    任何失敗都回傳 ``None``——包含「金鑰根本不存在」。取金鑰放在 try 裡面是刻意
    的：換了電腦而沒有匯入金鑰時，如果這裡丟例外，那就不是某一欄讀不出來，而是
    公司列表整頁炸掉。真正該擋下這種狀況的地方是啟動時的檢查
    （:func:`database.encryption.apply`），它會直接拒絕開啟並告訴使用者匯入金鑰。
    """
    if not token or not is_encrypted(token):
        return token

    try:
        key = get_key(create=False)
        blob = base64.urlsafe_b64decode(token[len(PREFIX):].encode("ascii"))
        nonce, ciphertext = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
        return _aesgcm()(key).decrypt(nonce, ciphertext, None).decode("utf-8")
    except Exception as exc:
        # 金鑰不對或資料損毀。回傳 None 而不是丟例外——一筆讀不出來的欄位
        # 不該讓整頁清單掛掉，但也絕不能回傳看起來像明文的垃圾。
        log.error("欄位解密失敗（金鑰不符或資料損毀）：{}", type(exc).__name__)
        return None
