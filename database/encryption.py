"""把既有資料庫的內容轉成（或轉回）加密狀態。

:mod:`database.types` 只管**新寫入**的值。既有的資料庫裡還躺著一堆明文，而
確定性加密下 ``WHERE email = ?`` 的參數是密文——不轉換的話，舊資料查不到，
爬蟲會把同一家公司當成新的再寫一筆。所以開啟加密與資料轉換必須同時發生，
這就是 :func:`apply` 在每次啟動時被呼叫的原因。

轉換一律走**原始 SQL**。用 ORM 讀寫會經過 :class:`~database.types.EncryptedString`，
讀出來已經解過密、寫回去又加一次密，看不到也控制不了實際存了什麼。這裡要處理
的正是「實際存了什麼」，所以必須繞開那一層。

安全性：轉換前會先自動建立一份備份。密文與明文用 ``enc:v1:`` 前綴分辨，所以整
個流程是**冪等**的——中途斷電再跑一次會從沒轉完的地方接下去，不會重複加密。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from core import crypto
from core.constants import LogCategory
from core.errors import DatabaseError
from core.logging_setup import get_logger
from database.models import Base
from database.types import is_encrypted_column

log = get_logger(LogCategory.DATABASE)


def encrypted_columns() -> dict[str, tuple[str, ...]]:
    """``{資料表: (加密欄位, ...)}``，直接由模型推導而來。

    刻意不維護一份手寫清單：加了一個加密欄位卻忘了同步清單，遷移就會靜靜地漏掉
    它，而那種錯誤要等到資料查不到才會被發現。
    """
    result: dict[str, tuple[str, ...]] = {}
    for table in Base.metadata.tables.values():
        names = tuple(c.name for c in table.columns if is_encrypted_column(c))
        if names:
            result[table.name] = names
    return result


@dataclass(frozen=True, slots=True)
class EncryptionStatus:
    """資料庫目前的加密狀況，供設定頁與 CLI 顯示。"""

    configured: bool
    """設定檔要求加密。"""

    usable: bool
    """這台機器真的能加密（有 cryptography、保管庫可用）。"""

    encrypted_values: int
    plaintext_values: int

    key_present: bool = True
    """保管庫裡有沒有金鑰。"""

    @property
    def unreadable(self) -> bool:
        """資料是密文，金鑰卻不在——換電腦沒帶金鑰就是這個狀況。"""
        return self.encrypted_values > 0 and not self.key_present

    @property
    def active(self) -> bool:
        """新寫入的資料現在會不會被加密。"""
        return self.configured and self.usable

    @property
    def fully_converted(self) -> bool:
        """儲存的內容已經與設定一致，不需要再轉換。"""
        return self.pending == 0

    @property
    def pending(self) -> int:
        """還有幾個值和設定不一致。"""
        if not self.configured:
            return self.encrypted_values      # 要轉回明文
        if not self.usable:
            return 0                          # 這台機器什麼都做不了
        return self.plaintext_values          # 要加密

    def describe(self) -> str:
        if not self.configured:
            return "已停用（資料以明文儲存）"
        if not self.usable:
            return "設定為啟用，但此環境無法加密——資料仍為明文"
        if self.plaintext_values:
            return f"啟用中，尚有 {self.plaintext_values} 個欄位值待加密"
        return f"啟用中（已加密 {self.encrypted_values} 個欄位值）"


@dataclass
class ConversionReport:
    """一次轉換做了什麼。"""

    encrypted: int = 0
    decrypted: int = 0
    failed: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return self.encrypted + self.decrypted


def _tables(engine: Engine) -> dict[str, tuple[str, ...]]:
    """實際存在於資料庫中的加密欄位。"""
    present = set(inspect(engine).get_table_names())
    return {t: c for t, c in encrypted_columns().items() if t in present}


def _primary_key(table_name: str) -> str:
    table = Base.metadata.tables[table_name]
    return list(table.primary_key.columns)[0].name


def status(engine: Engine) -> EncryptionStatus:
    """數一數每個加密欄位裡現在存的是密文還是明文。"""
    from core.config import get_config

    try:
        configured = bool(get_config().database.encrypt)
    except Exception:
        configured = False

    encrypted = plaintext = 0
    try:
        with engine.connect() as connection:
            for table_name, columns in _tables(engine).items():
                for column in columns:
                    rows = connection.execute(
                        text(f'SELECT "{column}" FROM "{table_name}"')
                    )
                    for (value,) in rows:
                        if not value:
                            continue          # NULL 與空字串沒有東西可加密
                        if crypto.is_encrypted(value):
                            encrypted += 1
                        else:
                            plaintext += 1
    except SQLAlchemyError as exc:
        raise DatabaseError(f"無法讀取加密狀態：{exc}") from exc

    return EncryptionStatus(
        configured=configured,
        usable=crypto.available(),
        key_present=crypto.has_key(),
        encrypted_values=encrypted,
        plaintext_values=plaintext,
    )


def convert(engine: Engine, *, to_encrypted: bool) -> ConversionReport:
    """把所有加密欄位轉成密文（``to_encrypted=True``）或轉回明文。

    冪等：已經是目標狀態的值會直接跳過。
    """
    report = ConversionReport()

    try:
        with engine.begin() as connection:
            for table_name, columns in _tables(engine).items():
                key_column = _primary_key(table_name)
                for column in columns:
                    rows = connection.execute(
                        text(f'SELECT "{key_column}", "{column}" FROM "{table_name}"')
                    ).all()

                    for key, value in rows:
                        if not value:
                            continue
                        if crypto.is_encrypted(value) == to_encrypted:
                            continue          # 已經是目標狀態

                        converted = (
                            crypto.encrypt(value)
                            if to_encrypted
                            else crypto.decrypt(value)
                        )
                        if converted is None:
                            # 解密失敗（金鑰不符）。**保留原值**——把讀不出來的
                            # 欄位覆寫成 NULL 等於替使用者把資料刪掉。
                            report.failed.append(f"{table_name}.{column}#{key}")
                            continue

                        connection.execute(
                            text(
                                f'UPDATE "{table_name}" SET "{column}" = :value '
                                f'WHERE "{key_column}" = :key'
                            ),
                            {"value": converted, "key": key},
                        )
                        if to_encrypted:
                            report.encrypted += 1
                        else:
                            report.decrypted += 1
    except crypto.EncryptionUnavailable:
        raise
    except SQLAlchemyError as exc:
        raise DatabaseError(f"加密轉換失敗：{exc}") from exc

    if report.failed:
        log.error(
            "有 {} 個欄位值無法解密（金鑰不符或資料損毀），已保留原值未更動",
            len(report.failed),
        )
    return report


def apply(engine: Engine) -> ConversionReport:
    """讓儲存的內容符合目前的設定。啟動時自動呼叫，冪等。

    三種情況：

    * 設定開啟且環境可用 → 把還是明文的值加密。
    * 設定關閉 → 把密文解回明文（否則等值查詢會全部失效）。
    * 設定開啟但環境不可用 → **什麼都不做**，只警告。此時強行解密會因為沒有金鑰
      而失敗，強行加密則根本做不到；保持原狀是唯一不會弄壞資料的選擇。

    另外有一種必須**直接擋下來**的狀況：資料是密文、金鑰卻不在（換了電腦、重灌、
    或清掉了認證管理員）。放行的話每個加密欄位都會讀成空白，使用者一存檔就把
    還救得回來的密文覆蓋成空值——資料就真的沒了。所以這裡丟例外而不是警告。
    """
    current = status(engine)

    if current.unreadable:
        raise DatabaseError(
            f"資料庫裡有 {current.encrypted_values} 個加密欄位值，但系統憑證保管庫中"
            "找不到對應的金鑰。\n"
            "這通常表示資料庫是從別台電腦複製過來的，或是重灌過系統。\n"
            "請執行 python main.py encrypt --import-key 貼回先前匯出的金鑰。\n"
            "（在還沒匯入金鑰前程式不會開啟——否則畫面上的個資會全部變成空白，"
            "一存檔就會把原本還救得回來的資料覆蓋掉。）"
        )

    if current.configured and not current.usable:
        if current.encrypted_values:
            log.error(
                "設定要求加密，但此環境無法取得金鑰。資料庫中有 {} 個已加密的欄位值"
                "現在讀不出來。請確認系統憑證保管庫可用，或還原一份未加密的備份。",
                current.encrypted_values,
            )
        elif current.plaintext_values:
            # 資料庫是空的就沒什麼好說——沒有個資被寫成明文。
            log.warning(
                "設定要求加密，但此環境無法加密（缺少 cryptography 或系統憑證保管庫），"
                "{} 個個資欄位值仍以明文儲存",
                current.plaintext_values,
            )
        return ConversionReport()

    to_encrypted = current.active
    pending = current.plaintext_values if to_encrypted else current.encrypted_values
    if pending == 0:
        return ConversionReport()

    # 轉換會改寫每一列。先留一份轉換前的樣子，出了任何事都還救得回來。
    _backup_before_conversion(pending, to_encrypted)

    log.info(
        "開始{}資料庫個資欄位（{} 個值）",
        "加密" if to_encrypted else "解密",
        pending,
    )
    report = convert(engine, to_encrypted=to_encrypted)
    log.info(
        "轉換完成：加密 {}、解密 {}、失敗 {}",
        report.encrypted,
        report.decrypted,
        len(report.failed),
    )
    return report


def _backup_before_conversion(pending: int, to_encrypted: bool) -> None:
    """轉換前的自動備份。失敗只警告，不擋住轉換。"""
    from database.backup import create_backup
    from core.errors import BackupError

    try:
        backup = create_backup(kind="encrypt")
    except BackupError as exc:
        log.warning("轉換前的自動備份失敗（{}），仍繼續進行轉換", exc)
        return
    log.info(
        "轉換前已備份至 {}（{} 個值待{}）",
        backup.name,
        pending,
        "加密" if to_encrypted else "解密",
    )
