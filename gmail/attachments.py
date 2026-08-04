"""信件附件的存放與檢查。

## 為什麼要複製一份，而不是直接記住原始路徑

使用者從桌面挑一個檔案，接著把它移到別的資料夾、或改名、或刪掉——如果只
記路徑，寄信時就會找不到檔案。排程寄信更明顯：設定的當下檔案在，真正寄出
是幾天後的事。所以選檔的當下就複製進 ``attachments/``，之後寄的永遠是當初
選的那一份。

## 為什麼上限是 20MB 而不是 Gmail 說的 25MB

附件在 MIME 裡是 base64 編碼的，體積會膨脹約 4/3。Gmail 的 25MB 限制算的是
編碼後的大小，所以原始檔 20MB 才是安全線。超過的話是寄出去才失敗，而使用者
在介面上完全看不出原因——寧可在選檔的當下就擋下來。
"""

from __future__ import annotations

import mimetypes
import shutil
from dataclasses import dataclass
from pathlib import Path

from core.config import AppConfig, get_config
from core.constants import LogCategory
from core.errors import GmailError
from core.logging_setup import get_logger

#: 沿用 gmail/sender.py 的做法：沒有獨立的 mail 分類，寄信歸在 CRAWL。
log = get_logger(LogCategory.CRAWL)

#: 檔名裡不允許出現的字元。除了各作業系統的保留字元，也擋掉路徑分隔符號
#: ——附件名稱會被拿去組路徑，"../../.env" 不能變成一個附件。
_UNSAFE_CHARS = '<>:"/\\|?*'

#: 就算作業系統允許，超過這個長度的檔名在別人的信箱裡也只會被截斷。
_MAX_NAME_LENGTH = 120


@dataclass(frozen=True, slots=True)
class StoredAttachment:
    """已經放進 ``attachments/`` 的一個檔案。"""

    name: str
    path: Path
    size_bytes: int
    mime_type: str

    @property
    def human_size(self) -> str:
        return human_size(self.size_bytes)


def human_size(size_bytes: int) -> str:
    """給介面顯示用的大小，例如 ``1.4 MB``。"""
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def safe_name(name: str) -> str:
    """把使用者的檔名整理成可以安全用來組路徑的形式。"""
    cleaned = "".join("_" if ch in _UNSAFE_CHARS else ch for ch in Path(name).name).strip()
    cleaned = cleaned.strip(". ")           # Windows 不接受結尾的點或空白
    if not cleaned:
        cleaned = "attachment"
    if len(cleaned) > _MAX_NAME_LENGTH:
        stem, dot, suffix = cleaned.rpartition(".")
        keep = _MAX_NAME_LENGTH - len(suffix) - 1 if dot else _MAX_NAME_LENGTH
        cleaned = f"{stem[:keep]}{dot}{suffix}" if dot else cleaned[:_MAX_NAME_LENGTH]
    return cleaned


def attachments_dir(config: AppConfig | None = None) -> Path:
    directory = (config or get_config()).mailer.resolved_attachments_dir
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def resolve(name: str, config: AppConfig | None = None) -> Path:
    """把附件名稱轉成實際路徑，並確保它真的在附件資料夾裡面。

    ``name`` 來自設定檔或資料庫，都是可以被人手動編輯的地方，所以這裡不能
    假設它乾淨——跟 :mod:`gmail.sender` 處理內文圖片時同一個理由。
    """
    directory = attachments_dir(config)
    candidate = (directory / Path(name).name).resolve()
    try:
        candidate.relative_to(directory.resolve())
    except ValueError as exc:
        raise GmailError(f"附件路徑不在附件資料夾內：{name}") from exc
    return candidate


def _describe(path: Path) -> StoredAttachment:
    guessed, _ = mimetypes.guess_type(path.name)
    return StoredAttachment(
        name=path.name,
        path=path,
        size_bytes=path.stat().st_size,
        mime_type=guessed or "application/octet-stream",
    )


def list_stored(config: AppConfig | None = None) -> list[StoredAttachment]:
    """附件資料夾裡的檔案，依名稱排序。"""
    directory = attachments_dir(config)
    return sorted(
        (_describe(path) for path in directory.iterdir() if path.is_file()),
        key=lambda item: item.name.lower(),
    )


def _unique_target(directory: Path, name: str) -> Path:
    """同名檔案不要互相覆蓋，加上 ``(2)``、``(3)`` 之類的序號。"""
    target = directory / name
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for index in range(2, 1000):
        candidate = directory / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    raise GmailError(f"同名附件太多了，請先整理 {directory}")


def store(source: str | Path, config: AppConfig | None = None) -> StoredAttachment:
    """把 ``source`` 複製進附件資料夾，回傳存好的那一份。"""
    config = config or get_config()
    origin = Path(source).expanduser()

    if not origin.is_file():
        raise GmailError(f"找不到檔案：{origin}")

    size = origin.stat().st_size
    limit = config.mailer.max_attachment_bytes
    if size > limit:
        raise GmailError(
            f"「{origin.name}」有 {human_size(size)}，超過單封信 "
            f"{human_size(limit)} 的上限。"
        )
    if size == 0:
        raise GmailError(f"「{origin.name}」是空檔案，寄出去對方也開不了。")

    directory = attachments_dir(config)
    target = _unique_target(directory, safe_name(origin.name))
    shutil.copy2(origin, target)
    log.info("附件已存入 {}（{}）", target.name, human_size(size))
    return _describe(target)


def remove(name: str, config: AppConfig | None = None) -> None:
    """從附件資料夾刪掉一個檔案。已經不在了就當作成功。"""
    path = resolve(name, config)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise GmailError(f"刪不掉附件「{name}」：{exc}") from exc
    log.info("附件已刪除：{}", name)


def check_total_size(names: list[str], config: AppConfig | None = None) -> int:
    """檢查這批附件加起來有沒有超過上限，回傳總位元組數。

    上限是對「一封信」而言的，所以要看總和而不是逐一檢查——三個 8MB 的
    檔案各自都合法，加起來卻寄不出去。
    """
    config = config or get_config()
    total = 0
    missing: list[str] = []
    for name in names:
        path = resolve(name, config)
        if not path.is_file():
            missing.append(name)
            continue
        total += path.stat().st_size

    if missing:
        raise GmailError("這些附件已經不在附件資料夾裡：" + "、".join(missing))

    limit = config.mailer.max_attachment_bytes
    if total > limit:
        raise GmailError(
            f"附件總共 {human_size(total)}，超過單封信 {human_size(limit)} 的上限。"
            "請移除幾個檔案，或改用雲端連結。"
        )
    return total


def load_for_sending(
    names: list[str], config: AppConfig | None = None
) -> list[tuple[str, bytes, str]]:
    """讀出要寄的附件，回傳 ``(檔名, 內容, MIME type)``。

    在這裡一次讀完而不是邊寄邊讀：一批信件會重複用同一組附件，讀一次就好；
    而且檔案不見要在開始寄之前就發現，不能寄到第 37 封才中斷。
    """
    check_total_size(names, config)
    loaded: list[tuple[str, bytes, str]] = []
    for name in names:
        path = resolve(name, config)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise GmailError(f"讀不到附件「{name}」：{exc}") from exc
        guessed, _ = mimetypes.guess_type(path.name)
        loaded.append((path.name, data, guessed or "application/octet-stream"))
    return loaded
