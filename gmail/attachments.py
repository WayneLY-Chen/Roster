"""信件附件庫：檔案放 ``attachments/``，索引與後設資料放資料庫。

## 為什麼要複製一份，而不是直接記住原始路徑

使用者從桌面挑一個檔案，接著把它移到別的資料夾、或改名、或刪掉——如果只
記路徑，寄信時就會找不到檔案。排程寄信更明顯：設定的當下檔案在，真正寄出
是幾天後的事。所以選檔的當下就複製進 ``attachments/``，之後寄的永遠是當初
選的那一份。

## 為什麼檔案內容不放進資料庫

單一附件可以到 20MB。塞進 SQLite 會讓資料庫肥到幾百 MB，而這支程式預設就會
定期備份整個資料庫——每天複製幾百 MB 只為了存幾個型錄，不划算。資料庫只存
索引：顯示名稱、備註、加入時間、用過幾次。

## 為什麼上限是 20MB 而不是 Gmail 說的 25MB

附件在 MIME 裡是 base64 編碼的，體積會膨脹約 4/3。Gmail 的 25MB 限制算的是
編碼後的大小，所以原始檔 20MB 才是安全線。超過的話是寄出去才失敗，而使用者
在介面上完全看不出原因——寧可在選檔的當下就擋下來。

## 資料夾與資料庫怎麼對帳

兩邊都可能被單方面改動：使用者會直接開資料夾丟檔案進去，也會直接把檔案拖走。
:func:`sync` 每次列出附件前都會跑一次：

* 資料夾有、資料庫沒有 → 收編成新的一筆（使用者手動丟進來的）
* 資料庫有、資料夾沒有 → **保留那一筆**，只標記檔案不存在

第二種刻意不自動刪除。檔案可能只是暫時被移走，而那一筆帶著使用者自己打的
顯示名稱與備註——為了一個可能是暫時的狀況，把使用者輸入的東西丟掉並不合理。
介面會把它標成「檔案不見了」，要不要刪由使用者決定。
"""

from __future__ import annotations

import mimetypes
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.config import AppConfig, get_config
from core.constants import LogCategory
from core.errors import GmailError
from core.logging_setup import get_logger
from database.models import MailAttachment, now
from database.session import session_scope

#: 沿用 gmail/sender.py 的做法：沒有獨立的 mail 分類，寄信歸在 CRAWL。
log = get_logger(LogCategory.CRAWL)

#: 檔名裡不允許出現的字元。除了各作業系統的保留字元，也擋掉路徑分隔符號
#: ——附件名稱會被拿去組路徑，"../../.env" 不能變成一個附件。
_UNSAFE_CHARS = '<>:"/\\|?*'

#: 就算作業系統允許，超過這個長度的檔名在別人的信箱裡也只會被截斷。
_MAX_NAME_LENGTH = 120


@dataclass(frozen=True, slots=True)
class AttachmentInfo:
    """附件庫裡的一筆，給介面顯示用的快照。

    刻意是不可變的普通資料類別，而不是直接把 ORM 物件交出去：介面拿到之後
    session 早就關了，碰到任何延遲載入的屬性都會炸。
    """

    id: int
    name: str
    label: str
    path: Path
    size_bytes: int
    mime_type: str
    note: str
    added_at: datetime
    last_used_at: datetime | None
    use_count: int
    #: 檔案現在還在不在。每次列出時去問檔案系統，不存成資料庫欄位。
    exists: bool

    @property
    def display_name(self) -> str:
        return self.label.strip() or self.name

    @property
    def human_size(self) -> str:
        return human_size(self.size_bytes)

    @property
    def status_text(self) -> str:
        if not self.exists:
            return "檔案不見了"
        if self.use_count:
            return f"已寄出 {self.use_count} 次"
        return "尚未寄出"


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


def _guess_mime(name: str) -> str:
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def _to_info(row: MailAttachment, directory: Path) -> AttachmentInfo:
    path = directory / row.filename
    return AttachmentInfo(
        id=row.id,
        name=row.filename,
        label=row.label or "",
        path=path,
        size_bytes=row.size_bytes,
        mime_type=row.mime_type or "application/octet-stream",
        note=row.note or "",
        added_at=row.added_at,
        last_used_at=row.last_used_at,
        use_count=row.use_count,
        exists=path.is_file(),
    )


# ------------------------------------------------------------------- 對帳


def sync(config: AppConfig | None = None) -> int:
    """讓資料庫追上資料夾的現況，回傳新收編的筆數。

    只收編、不刪除——理由見模組開頭。
    """
    directory = attachments_dir(config)
    adopted = 0

    with session_scope() as session:
        known = {row.filename: row for row in session.query(MailAttachment).all()}

        for path in sorted(directory.iterdir()):
            if not path.is_file():
                continue
            row = known.get(path.name)
            if row is None:
                session.add(
                    MailAttachment(
                        filename=path.name,
                        label="",
                        mime_type=_guess_mime(path.name),
                        size_bytes=path.stat().st_size,
                    )
                )
                adopted += 1
                continue
            # 檔案被外部換掉（同名覆蓋）時，大小要跟著更新，否則總量會算錯。
            actual = path.stat().st_size
            if row.size_bytes != actual:
                row.size_bytes = actual

    if adopted:
        log.info("附件庫收編了 {} 個手動放進資料夾的檔案", adopted)
    return adopted


def library(config: AppConfig | None = None) -> list[AttachmentInfo]:
    """附件庫的完整內容，先對帳再列出。"""
    sync(config)
    directory = attachments_dir(config)
    with session_scope() as session:
        rows = session.query(MailAttachment).order_by(MailAttachment.added_at.desc()).all()
        return [_to_info(row, directory) for row in rows]


#: 舊名稱，保留給既有呼叫端。
def list_stored(config: AppConfig | None = None) -> list[AttachmentInfo]:
    return library(config)


def get(name: str, config: AppConfig | None = None) -> AttachmentInfo | None:
    directory = attachments_dir(config)
    with session_scope() as session:
        row = session.query(MailAttachment).filter_by(filename=name).one_or_none()
        return _to_info(row, directory) if row else None


# ------------------------------------------------------------------ 新增


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


def store(
    source: str | Path,
    config: AppConfig | None = None,
    label: str = "",
    note: str = "",
) -> AttachmentInfo:
    """把 ``source`` 複製進附件資料夾並建檔，回傳存好的那一筆。"""
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

    with session_scope() as session:
        row = MailAttachment(
            filename=target.name,
            label=label.strip(),
            note=note.strip() or None,
            mime_type=_guess_mime(target.name),
            size_bytes=size,
        )
        session.add(row)
        session.flush()
        info = _to_info(row, directory)

    log.info("附件已存入 {}（{}）", target.name, human_size(size))
    return info


# ------------------------------------------------------------------ 修改


def update(
    name: str,
    config: AppConfig | None = None,
    label: str | None = None,
    note: str | None = None,
) -> None:
    """改顯示名稱或備註。不會動到檔案本身。"""
    with session_scope() as session:
        row = session.query(MailAttachment).filter_by(filename=name).one_or_none()
        if row is None:
            raise GmailError(f"附件庫裡沒有「{name}」")
        if label is not None:
            row.label = label.strip()
        if note is not None:
            row.note = note.strip() or None


def remove(name: str, config: AppConfig | None = None) -> None:
    """把檔案與紀錄一起刪掉。已經不在了就當作成功。"""
    path = resolve(name, config)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise GmailError(f"刪不掉附件「{name}」：{exc}") from exc

    with session_scope() as session:
        row = session.query(MailAttachment).filter_by(filename=name).one_or_none()
        if row is not None:
            session.delete(row)

    log.info("附件已刪除：{}", name)


def mark_used(names: list[str], config: AppConfig | None = None) -> None:
    """記錄這批附件真的被寄出去了。

    用來判斷哪些附件還在用、哪些可以清掉——只看「加入時間」看不出這件事。
    """
    if not names:
        return
    stamp = now()
    with session_scope() as session:
        for name in names:
            row = session.query(MailAttachment).filter_by(filename=name).one_or_none()
            if row is not None:
                row.use_count += 1
                row.last_used_at = stamp


# --------------------------------------------------------------- 使用中檢查


def used_by_schedule(name: str, config: AppConfig | None = None) -> bool:
    """這個附件是不是正被自動排程引用。

    刪掉排程正在用的附件，後果是排程在半夜三點失敗，而且沒有人會在當下看到
    錯誤訊息——所以刪除前要先問過使用者。
    """
    config = config or get_config()
    return name in (config.scheduler.mail_attachments or [])


# ------------------------------------------------------------------ 寄送


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
        loaded.append((path.name, data, _guess_mime(path.name)))
    return loaded
