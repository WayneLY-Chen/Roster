"""Domain vocabulary shared across layers.

These enums are the single source of truth for the values stored in the
database and rendered in the GUI. They are plain ``str`` enums so SQLAlchemy
stores readable text and exports need no translation step.
"""

from __future__ import annotations

from enum import Enum

PROJECT_NAME = "Roster"
#: 介面標題用的中文名稱。PROJECT_NAME 保持英文，因為它也用在 User-Agent、
#: 匯出檔案的 generator 欄位等對外字串上——爬取時對方站台看到的就是這個名字，
#: 所以它要能對應到一個真實存在的東西。
DISPLAY_NAME = "名單匠"
#: 版本號跟著 CHANGELOG.md 走——那份是「這一版改了什麼」的唯一出處，
#: 「更新資訊」頁直接讀它，不另外維護一份會過期的副本。
VERSION = "1.9.0"


class StrEnum(str, Enum):
    """``str`` enum with a stable ``str()`` (Python 3.11 backport semantics)."""

    def __str__(self) -> str:
        return str(self.value)

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]

    @classmethod
    def coerce(cls, value: object, default: "StrEnum | None" = None) -> "StrEnum":
        """Best-effort parse; falls back to ``default`` instead of raising."""
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            for member in cls:
                if member.value.lower() == value.strip().lower():
                    return member
        if default is not None:
            return default
        raise ValueError(f"{value!r} is not a valid {cls.__name__}")


class PipelineStage(StrEnum):
    """CRM sales pipeline position."""

    NEW = "New"
    QUALIFIED = "Qualified"
    CONTACTED = "Contacted"
    MEETING = "Meeting"
    PROPOSAL = "Proposal"
    NEGOTIATION = "Negotiation"
    WON = "Won"
    LOST = "Lost"
    INACTIVE = "Inactive"


class RecordStatus(StrEnum):
    """Lifecycle of the *record*, independent of the sales pipeline."""

    ACTIVE = "Active"
    DUPLICATE = "Duplicate"
    INVALID = "Invalid"
    ARCHIVED = "Archived"


class Priority(StrEnum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    URGENT = "Urgent"


class ActivityType(StrEnum):
    NOTE = "Note"
    CALL = "Call"
    EMAIL = "Email"
    MEETING = "Meeting"
    STAGE_CHANGE = "Stage Change"
    SYSTEM = "System"


class EmailVerdict(StrEnum):
    """Outcome of contact-data validation, from cheapest check to strongest."""

    UNKNOWN = "Unknown"          # not checked yet
    EMPTY = "Empty"              # no address on record
    INVALID_SYNTAX = "Invalid Syntax"
    DISPOSABLE = "Disposable"
    NO_MX = "No MX"
    VALID = "Valid"              # syntax ok and domain accepts mail


class EmailStatus(StrEnum):
    """Lifecycle of one outbound message."""

    PENDING = "Pending"
    SENT = "Sent"
    FAILED = "Failed"
    SKIPPED = "Skipped"      # suppressed, unverifiable, or already contacted
    DRY_RUN = "Dry Run"      # rendered and checked, deliberately not sent


class SkipReason(StrEnum):
    """Why a recipient was left out of a send. Shown before anything goes out."""

    NO_EMAIL = "沒有電子信箱"
    INVALID_EMAIL = "信箱格式不正確"
    UNVERIFIED = "信箱未通過驗證"
    DO_NOT_CONTACT = "已標記為請勿聯絡"
    RECENTLY_CONTACTED = "近期已寄送過"
    DAILY_CAP = "已達今日寄送上限"


class CrawlStatus(StrEnum):
    PENDING = "Pending"
    RUNNING = "Running"
    SUCCESS = "Success"
    PARTIAL = "Partial"
    FAILED = "Failed"
    CANCELLED = "Cancelled"


class LogCategory(StrEnum):
    """Log sinks required by the spec; one file per category under ``logs/``."""

    CRAWL = "crawl"
    DATABASE = "database"
    EXPORT = "export"
    GUI = "gui"
    ERROR = "error"
