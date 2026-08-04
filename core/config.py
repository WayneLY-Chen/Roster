"""Typed configuration loaded from ``config.yaml`` + ``.env``.

The whole app reads settings through :func:`get_config`, which caches a single
validated :class:`AppConfig`. Relative paths in the YAML are resolved against
the project root so the app behaves the same from any working directory.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.constants import PROJECT_NAME
from core.errors import ConfigError

def _project_root() -> Path:
    """使用者的資料與設定放哪裡。

    打包成 exe 之後不能再用 ``__file__`` 推算——那會指到 PyInstaller 解壓出來
    的 ``_internal`` 資料夾裡面。結果是設定檔讀到打包進去的副本（使用者在
    exe 旁邊改的那份永遠不生效），資料庫也建在 ``_internal/data/`` 底下，
    打開程式看到的是一個全新的空資料庫。

    所以打包後一律以「exe 所在的資料夾」為準：設定、資料庫、日誌、備份、
    匯出檔全部放在使用者看得到、搬得動、備份得到的地方。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _resource_root() -> Path:
    """唯讀資源（預設設定檔、範例樣板）打包後被解壓到哪裡。"""
    bundled = getattr(sys, "_MEIPASS", None)
    return Path(bundled) if bundled else _project_root()


PROJECT_ROOT = _project_root()
RESOURCE_ROOT = _resource_root()
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

#: 第一次執行時要從打包內容複製到使用者資料夾的東西。
_FIRST_RUN_COPIES: tuple[str, ...] = ("config.yaml", "templates")


def ensure_user_files() -> None:
    """第一次執行時，把預設設定與範例樣板複製到 exe 旁邊。

    只在打包版才有意義，而且只補「還不存在」的檔案——絕不覆蓋使用者改過的
    設定。從原始碼執行時什麼都不做。
    """
    if not getattr(sys, "frozen", False):
        return
    if RESOURCE_ROOT == PROJECT_ROOT:  # pragma: no cover - 理論上不會發生
        return

    import shutil

    for name in _FIRST_RUN_COPIES:
        source = RESOURCE_ROOT / name
        target = PROJECT_ROOT / name
        if not source.exists() or target.exists():
            continue
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)

# Sources the user creates in the GUI live in their own file. Keeping them out
# of config.yaml means saving one never rewrites the hand-edited, commented
# config -- and the user can delete this file to drop every custom source.
CUSTOM_SOURCES_PATH = PROJECT_ROOT / "custom_sources.yaml"

# Settings flipped from the GUI, for the same reason: rewriting config.yaml
# programmatically would strip every comment out of a file whose comments are
# most of its value. Delete this file to fall back to config.yaml entirely.
USER_SETTINGS_PATH = PROJECT_ROOT / "user_settings.yaml"


def _resolve(path: str | Path) -> Path:
    """Resolve a config path against the project root when it is relative."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate.resolve()


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AppSection(_Base):
    name: str = "Taiwan B2B CRM"
    locale: str = "zh_TW"
    theme: Literal["system", "light", "dark"] = "system"


class DatabaseSection(_Base):
    url: str = "sqlite:///./data/crm.db"
    echo: bool = False

    #: 是否加密個資欄位（信箱、電話、地址、聯絡人、備註、信件內文）。
    #:
    #: 打開之後金鑰會存進系統憑證保管庫，**不會**留在專案資料夾裡。換一台電腦
    #: 或清掉認證管理員之後，加密欄位就再也還原不了——備份資料庫時請一併確認
    #: 金鑰還在。無法加密的環境（沒有保管庫）會自動退回明文並在啟動時警告。
    encrypt: bool = True

    @property
    def sqlite_path(self) -> Path | None:
        """Filesystem path of the SQLite file, or ``None`` for other backends."""
        prefix = "sqlite:///"
        if not self.url.startswith(prefix):
            return None
        raw = self.url[len(prefix) :]
        if raw in ("", ":memory:"):
            return None
        return _resolve(raw)

    @property
    def resolved_url(self) -> str:
        """URL with a relative SQLite path rewritten to an absolute one."""
        path = self.sqlite_path
        return f"sqlite:///{path.as_posix()}" if path else self.url


class LoggingSection(_Base):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    dir: str = "./logs"
    rotation: str = "10 MB"
    retention: str = "30 days"
    console: bool = True

    @property
    def resolved_dir(self) -> Path:
        return _resolve(self.dir)


class PlaywrightSection(_Base):
    headless: bool = True
    wait_until: Literal["load", "domcontentloaded", "networkidle", "commit"] = "networkidle"
    nav_timeout_ms: int = Field(default=30_000, ge=1_000, le=300_000)


class FieldRule(_Base):
    """How to pull one field out of a list item's markup."""

    selector: str
    attr: str = "text"          # "text" or an HTML attribute name
    regex: str | None = None    # optional capture applied to the raw value
    multiple: bool = False      # join all matches instead of taking the first
    separator: str = " "


class PaginationRule(_Base):
    type: Literal["query", "next_link", "none"] = "query"
    next_selector: str | None = None

    @model_validator(mode="after")
    def _need_selector_for_next_link(self) -> "PaginationRule":
        if self.type == "next_link" and not self.next_selector:
            raise ValueError("pagination.next_selector is required when type='next_link'")
        return self


class SourceConfig(_Base):
    name: str
    type: Literal["sample", "generic_html"]
    enabled: bool = True
    start_url: str | None = None
    #: 起始頁碼（含）。query 分頁時會代入 {page}；next_link 分頁時代表
    #: 要先略過幾頁才開始收錄。
    page_start: int = Field(default=1, ge=0)
    #: 結束頁碼（含）。留空表示只受 max_pages 限制。
    page_end: int | None = Field(default=None, ge=0)
    max_pages: int | None = Field(default=None, ge=1)
    list_selector: str | None = None
    pagination: PaginationRule = Field(default_factory=PaginationRule)
    fields: dict[str, FieldRule] = Field(default_factory=dict)

    #: 列表頁與分頁請求使用的 HTTP 方法；明細頁一律用 GET。多數目錄網站是
    #: GET，但少數公會網站（例如 TCA）的查詢是 POST 表單。
    method: Literal["GET", "POST"] = "GET"
    #: method="POST" 時要送出的表單欄位（application/x-www-form-urlencoded）。
    #: 值支援與 start_url 相同的 {page} 佔位，換頁時代入目前頁碼。
    form_data: dict[str, str] | None = None
    #: 強制以指定編碼解碼回應內容，取代 HTTP 標頭或自動偵測。用於回應宣告
    #: 編碼錯誤或乾脆沒宣告的舊站台（例如 Big5 編碼的網站）。
    encoding: str | None = None

    # Taiwanese trade-association directories almost all follow the same shape:
    # the list page carries only the company name, and the contact details live
    # on a per-company detail page behind it. ``detail_link`` says how to find
    # that page from a list item; ``detail_fields`` are extracted from it and
    # merged over whatever the list page gave.
    detail_link: FieldRule | None = None
    detail_fields: dict[str, FieldRule] = Field(default_factory=dict)
    #: Cap on detail pages per run; each one is a separate request.
    max_details: int = Field(default=100, ge=0, le=2000)

    # Written verbatim to Company.source when omitted; defaults to ``name``.
    label: str | None = None

    @model_validator(mode="after")
    def _page_range_is_sane(self) -> "SourceConfig":
        if self.page_end is not None and self.page_end < self.page_start:
            raise ValueError(
                f"source {self.name!r}: page_end ({self.page_end}) "
                f"不能小於 page_start ({self.page_start})"
            )
        return self

    @property
    def page_count(self) -> int | None:
        """頁碼範圍換算成的頁數；未指定 page_end 時回傳 None。"""
        if self.page_end is None:
            return None
        return self.page_end - self.page_start + 1

    @model_validator(mode="after")
    def _detail_needs_link(self) -> "SourceConfig":
        if self.detail_fields and self.detail_link is None:
            raise ValueError(
                f"source {self.name!r}: detail_fields requires detail_link"
            )
        return self

    @model_validator(mode="after")
    def _validate_generic_html(self) -> "SourceConfig":
        if self.type != "generic_html":
            return self
        if not self.start_url:
            raise ValueError(f"source {self.name!r}: start_url is required for generic_html")
        if not self.list_selector:
            raise ValueError(f"source {self.name!r}: list_selector is required for generic_html")
        if "company_name" not in self.fields:
            raise ValueError(f"source {self.name!r}: fields.company_name is required")
        if self.pagination.type == "query":
            # POST 表單分頁時，頁碼可能是代入 form_data 而不是網址（例如
            # 查詢動作固定用同一個 URL，換頁只是換送出的欄位值）。
            page_in_url = "{page}" in self.start_url
            page_in_form = any("{page}" in v for v in (self.form_data or {}).values())
            if not page_in_url and not page_in_form:
                raise ValueError(
                    f"source {self.name!r}: start_url must contain '{{page}}' "
                    "when pagination.type='query' (or use form_data with a "
                    "'{page}' placeholder for POST forms)"
                )
        return self

    @model_validator(mode="after")
    def _form_data_requires_post(self) -> "SourceConfig":
        if self.form_data and self.method != "POST":
            raise ValueError(
                f"source {self.name!r}: form_data is only meaningful when method='POST'"
            )
        return self

    @property
    def source_label(self) -> str:
        return self.label or self.name


class CrawlerSection(_Base):
    #: 爬蟲對外表明身分用的。``{contact}`` 由設定檔的聯絡信箱填入——對方站長
    #: 要抱怨或要求停止時找得到人，是「只爬公開資料」以外的基本禮貌。
    #: 名稱走 PROJECT_NAME，改名時不會像以前那樣漏掉這裡。
    user_agent: str = f"{PROJECT_NAME}/1.0 (+contact: {{contact}})"
    engine: Literal["httpx", "playwright"] = "httpx"
    respect_robots: bool = True
    delay_seconds: float = Field(default=2.0, ge=0.0)
    delay_jitter: float = Field(default=0.5, ge=0.0)
    request_timeout: float = Field(default=20.0, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)
    retry_backoff: float = Field(default=2.0, gt=0)
    max_pages: int = Field(default=10, ge=1)
    stop_on_empty_page: bool = True
    playwright: PlaywrightSection = Field(default_factory=PlaywrightSection)
    sources: list[SourceConfig] = Field(default_factory=list)

    @field_validator("sources")
    @classmethod
    def _unique_names(cls, sources: list[SourceConfig]) -> list[SourceConfig]:
        seen: set[str] = set()
        for source in sources:
            if source.name in seen:
                raise ValueError(f"duplicate crawler source name: {source.name!r}")
            seen.add(source.name)
        return sources

    def resolved_user_agent(self) -> str:
        """User-Agent with ``{contact}`` filled from the environment."""
        contact = os.getenv("CRM_CRAWLER_CONTACT", "").strip() or "unset@example.com"
        return self.user_agent.replace("{contact}", contact)

    def source(self, name: str) -> SourceConfig:
        for source in self.sources:
            if source.name == name:
                return source
        known = ", ".join(s.name for s in self.sources) or "(none)"
        raise ConfigError(f"unknown crawler source {name!r}; configured sources: {known}")

    def enabled_sources(self) -> list[SourceConfig]:
        return [s for s in self.sources if s.enabled]


class VerifierSection(_Base):
    #: Drop records that look like promotional articles rather than companies.
    #: Directory listings often render ads with the same markup as company
    #: cards, and structural detection cannot tell them apart.
    filter_advertisements: bool = True
    check_mx: bool = True
    mx_timeout: float = Field(default=5.0, gt=0)
    mx_cache_hours: int = Field(default=168, ge=0)
    reject_disposable: bool = True
    disposable_domains: list[str] = Field(default_factory=list)

    @field_validator("disposable_domains")
    @classmethod
    def _lowercase(cls, domains: list[str]) -> list[str]:
        return [d.strip().lower() for d in domains if d.strip()]


class ExporterSection(_Base):
    output_dir: str = "./output"
    excel_sheet_name: str = "Companies"
    date_format: str = "%Y-%m-%d %H:%M:%S"
    columns: list[str] = Field(default_factory=list)

    @property
    def resolved_output_dir(self) -> Path:
        return _resolve(self.output_dir)


class BackupSection(_Base):
    dir: str = "./backups"
    daily: bool = True
    weekly: bool = True
    keep_daily: int = Field(default=14, ge=1)
    keep_weekly: int = Field(default=8, ge=1)

    @property
    def resolved_dir(self) -> Path:
        return _resolve(self.dir)


class GmailSection(_Base):
    enabled: bool = False
    imap_host: str = "imap.gmail.com"
    imap_port: int = Field(default=993, ge=1, le=65535)
    mailbox: str = "INBOX"
    search: str = "UNSEEN"
    max_messages: int = Field(default=50, ge=1, le=1000)
    ignore_domains: list[str] = Field(default_factory=list)

    @field_validator("ignore_domains")
    @classmethod
    def _lowercase(cls, domains: list[str]) -> list[str]:
        return [d.strip().lower() for d in domains if d.strip()]

    @property
    def address(self) -> str:
        from core.credentials import get_secret

        return get_secret("gmail_address")

    @property
    def app_password(self) -> str:
        from core.credentials import get_secret

        return get_secret("gmail_app_password")


class MailerSection(_Base):
    """Outbound email settings.

    The defaults are deliberately cautious. Gmail itself cuts off a personal
    account at roughly 500 recipients a day and will lock an account that
    looks like a spam source, so ``daily_limit`` below that is protective, not
    decorative. ``dry_run`` starts on: the first run of a new template should
    render and be reviewed, not delivered.
    """

    enabled: bool = False
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = Field(default=587, ge=1, le=65535)
    use_tls: bool = True
    sender_name: str = ""
    reply_to: str = ""
    dry_run: bool = True
    daily_limit: int = Field(default=100, ge=1, le=2000)
    delay_seconds: float = Field(default=8.0, ge=1.0)
    #: Do not email the same company again within this many days.
    resend_after_days: int = Field(default=30, ge=0)
    #: Only send to addresses that passed MX verification.
    require_verified_email: bool = True
    #: Appended to every message. Required: recipients must be able to opt out.
    unsubscribe_note: str = (
        "若不希望再收到本信件，請直接回覆本信並註明「取消訂閱」，我們會立即將您移除。"
    )
    templates_dir: str = "./templates/mail"

    @property
    def resolved_templates_dir(self) -> Path:
        return _resolve(self.templates_dir)

    @property
    def address(self) -> str:
        """Sending account, shared with the Gmail reading credentials."""
        from core.credentials import get_secret

        return get_secret("gmail_address")

    @property
    def app_password(self) -> str:
        """App password from the OS credential vault, falling back to ``.env``."""
        from core.credentials import get_secret

        return get_secret("gmail_app_password")


class SchedulerSection(_Base):
    """Unattended crawl scheduling.

    The app is a desktop program, not a service: jobs only run while it is
    open. ``catch_up`` decides what happens to a job whose time passed while
    the machine was off.
    """

    enabled: bool = False
    #: "daily" | "hourly" | "interval"
    mode: Literal["daily", "hourly", "interval"] = "daily"
    #: For mode=daily, 24-hour clock time.
    at: str = "03:00"
    #: For mode=interval, minutes between runs.
    every_minutes: int = Field(default=360, ge=15, le=10_080)
    #: Sources to crawl; empty means every enabled source.
    sources: list[str] = Field(default_factory=list)
    #: Run verification after each scheduled crawl.
    verify_after_crawl: bool = True
    #: Run a missed job once at start-up instead of waiting for the next slot.
    catch_up: bool = True

    @field_validator("at")
    @classmethod
    def _valid_time(cls, value: str) -> str:
        try:
            hour, _, minute = value.partition(":")
            if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise ValueError(f"scheduler.at must be HH:MM, got {value!r}") from exc
        return value


class AppConfig(_Base):
    """Root of the validated configuration tree."""

    app: AppSection = Field(default_factory=AppSection)
    database: DatabaseSection = Field(default_factory=DatabaseSection)
    logging: LoggingSection = Field(default_factory=LoggingSection)
    crawler: CrawlerSection = Field(default_factory=CrawlerSection)
    verifier: VerifierSection = Field(default_factory=VerifierSection)
    exporter: ExporterSection = Field(default_factory=ExporterSection)
    backup: BackupSection = Field(default_factory=BackupSection)
    gmail: GmailSection = Field(default_factory=GmailSection)
    mailer: MailerSection = Field(default_factory=MailerSection)
    scheduler: SchedulerSection = Field(default_factory=SchedulerSection)

    def ensure_directories(self) -> None:
        """Create every directory the app writes to. Safe to call repeatedly."""
        targets = [
            self.logging.resolved_dir,
            self.exporter.resolved_output_dir,
            self.backup.resolved_dir,
        ]
        sqlite_path = self.database.sqlite_path
        if sqlite_path:
            targets.append(sqlite_path.parent)
        for target in targets:
            target.mkdir(parents=True, exist_ok=True)


def load_config(path: str | Path | None = None) -> AppConfig:
    """Read and validate a config file. Missing file -> documented defaults."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)

    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        if path is not None:
            raise ConfigError(f"config file not found: {config_path}")
        return AppConfig()

    try:
        raw: Any = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path} is not valid YAML: {exc}") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} must contain a YAML mapping at the top level")

    _merge_custom_sources(raw)
    _merge_user_settings(raw)

    try:
        return AppConfig.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError -> friendly message
        raise ConfigError(f"invalid configuration in {config_path}:\n{exc}") from exc


def _merge_custom_sources(raw: dict[str, Any]) -> None:
    """Fold ``custom_sources.yaml`` into the crawler sources, in place.

    A custom source with the same name as one in config.yaml replaces it, so
    editing a source in the GUI always wins over the shipped default.
    """
    entries = read_custom_sources()
    if not entries:
        return

    crawler = raw.setdefault("crawler", {})
    if not isinstance(crawler, dict):
        raise ConfigError("crawler section must be a mapping")
    existing = crawler.setdefault("sources", [])
    if not isinstance(existing, list):
        raise ConfigError("crawler.sources must be a list")

    custom_names = {entry.get("name") for entry in entries}
    crawler["sources"] = [
        source
        for source in existing
        if not (isinstance(source, dict) and source.get("name") in custom_names)
    ] + entries


def _merge_user_settings(raw: dict[str, Any]) -> None:
    """Overlay ``user_settings.yaml`` onto the parsed config, in place.

    Deliberately a *deep* merge one level down (``mailer.dry_run`` overrides
    only that key, not the whole ``mailer`` block), so a toggle saved from the
    GUI never silently resets its neighbours back to their defaults.
    """
    for section, values in read_user_settings().items():
        if not isinstance(values, dict):
            raw[section] = values
            continue
        target = raw.setdefault(section, {})
        if not isinstance(target, dict):
            raise ConfigError(f"{section} section must be a mapping")
        target.update(values)


def read_user_settings() -> dict[str, Any]:
    """Settings saved from the GUI. Missing or broken file -> ``{}``."""
    if not USER_SETTINGS_PATH.exists():
        return {}
    try:
        data = yaml.safe_load(USER_SETTINGS_PATH.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{USER_SETTINGS_PATH.name} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{USER_SETTINGS_PATH.name} must contain a YAML mapping")
    return data


def save_user_setting(section: str, key: str, value: Any) -> Path:
    """Persist one setting the user flipped in the GUI, then reload the config.

    Validates by rebuilding the whole config before writing: a toggle that
    would produce an invalid configuration should fail at the click, not at the
    next start-up.
    """
    settings = read_user_settings()
    previous = settings.get(section, {})
    if not isinstance(previous, dict):
        previous = {}
    settings[section] = {**previous, key: value}

    _write_user_settings(settings)
    reset_config()
    try:
        get_config()
    except ConfigError:
        # Put it back the way it was rather than leaving the app unable to start.
        settings[section] = previous
        _write_user_settings(settings)
        reset_config()
        raise
    return USER_SETTINGS_PATH


def _write_user_settings(settings: dict[str, Any]) -> None:
    header = (
        "# 由應用程式的設定畫面自動產生，會覆蓋 config.yaml 中的同名設定。\n"
        "# 可以手動編輯；刪除整個檔案即可全部回到 config.yaml 的設定。\n"
    )
    body = yaml.safe_dump(settings, allow_unicode=True, sort_keys=False, indent=2)
    USER_SETTINGS_PATH.write_text(header + body, encoding="utf-8")


def read_custom_sources() -> list[dict[str, Any]]:
    """Raw source dicts saved by the GUI. Missing or broken file -> ``[]``."""
    if not CUSTOM_SOURCES_PATH.exists():
        return []
    try:
        data = yaml.safe_load(CUSTOM_SOURCES_PATH.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{CUSTOM_SOURCES_PATH.name} is not valid YAML: {exc}") from exc

    sources = data.get("sources") if isinstance(data, dict) else data
    if sources is None:
        return []
    if not isinstance(sources, list):
        raise ConfigError(f"{CUSTOM_SOURCES_PATH.name}: 'sources' must be a list")
    return [s for s in sources if isinstance(s, dict) and s.get("name")]


def save_custom_source(source: "SourceConfig") -> Path:
    """Persist a user-created source, replacing any earlier one of that name."""
    entry = source.model_dump(mode="json", exclude_none=True)
    entries = [s for s in read_custom_sources() if s.get("name") != source.name]
    entries.append(entry)
    _write_custom_sources(entries)
    reset_config()
    return CUSTOM_SOURCES_PATH


def delete_custom_source(name: str) -> bool:
    """Remove a user-created source. Returns False when it was not there."""
    entries = read_custom_sources()
    remaining = [s for s in entries if s.get("name") != name]
    if len(remaining) == len(entries):
        return False
    _write_custom_sources(remaining)
    reset_config()
    return True


def _write_custom_sources(entries: list[dict[str, Any]]) -> None:
    header = (
        "# 由「爬取」頁面的自訂網址精靈自動產生。\n"
        "# 可以手動編輯；刪除整個檔案即可移除所有自訂來源。\n"
        "# config.yaml 中同名的來源會被這裡的設定取代。\n"
    )
    body = yaml.safe_dump(
        {"sources": entries}, allow_unicode=True, sort_keys=False, indent=2
    )
    CUSTOM_SOURCES_PATH.write_text(header + body, encoding="utf-8")


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Process-wide cached config. Call :func:`reset_config` in tests."""
    return load_config()


def reset_config() -> None:
    """Drop the cached config so the next :func:`get_config` re-reads disk."""
    get_config.cache_clear()

    # database.encrypt is cached separately (it is consulted on every bind
    # parameter). Clearing it here means the two can never disagree -- imported
    # late because database.types reads this module.
    from database.types import reset_encryption_state

    reset_encryption_state()
