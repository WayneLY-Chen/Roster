"""Controllers -- the C in MVC.

Pages (views) never open a session, build a query, or import a repository.
They call a controller, which owns the transaction and returns plain DTOs.
That keeps view code free of persistence concerns and makes the controllers
testable without a running Tk event loop.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from core.config import AppConfig, get_config
from core.constants import (
    ActivityType,
    LogCategory,
    PipelineStage,
    Priority,
    RecordStatus,
)
from core.credentials import SecretSource, SecretStatus
from core.errors import CRMError
from core.logging_setup import get_logger, log_file_path
from core.schemas import (
    ActivityView,
    CompanyFilter,
    CompanyView,
    ContactView,
    CrawlSummary,
    DashboardStats,
    VerifySummary,
)
from database.backup import BackupFile, create_backup, list_backups, restore_backup
from database.models import Attachment
from database.repository import (
    ActivityRepository,
    AttachmentRepository,
    CompanyRepository,
    ContactRepository,
    CrawlJobRepository,
    StatsRepository,
    TagRepository,
)
from database.session import session_scope
from core.data_version import bump

log = get_logger(LogCategory.GUI)

ATTACHMENT_DIR_NAME = "attachments"


class DashboardController:
    def stats(self) -> DashboardStats:
        with session_scope() as session:
            return StatsRepository(session).dashboard()

    def recent_crawls(self, limit: int = 8) -> list[CrawlSummary]:
        with session_scope() as session:
            repo = CrawlJobRepository(session)
            return [CrawlJobRepository.to_summary(job) for job in repo.recent(limit)]


class CompanyController:
    """Everything the Companies page and the company detail dialog need."""

    def search(self, criteria: CompanyFilter | None = None) -> list[CompanyView]:
        with session_scope() as session:
            return CompanyRepository(session).search_views(criteria)

    def count(self, criteria: CompanyFilter | None = None) -> int:
        with session_scope() as session:
            return CompanyRepository(session).count(criteria)

    def get(self, company_id: int) -> CompanyView | None:
        with session_scope() as session:
            company = CompanyRepository(session).get(company_id)
            return CompanyRepository.to_view(company) if company else None

    def detail(self, company_id: int) -> dict[str, Any] | None:
        """Company plus its contacts, activity history and attachments."""
        with session_scope() as session:
            repo = CompanyRepository(session)
            company = repo.get(company_id)
            if company is None:
                return None
            return {
                "company": CompanyRepository.to_view(company),
                "contacts": [ContactView.model_validate(c) for c in company.contacts],
                "activities": ActivityRepository(session).for_company(company_id),
                "attachments": [
                    {
                        "id": a.id,
                        "filename": a.filename,
                        "path": a.path,
                        "size_bytes": a.size_bytes,
                        "uploaded_at": a.uploaded_at,
                    }
                    for a in company.attachments
                ],
            }

    def update(self, company_id: int, **fields: Any) -> CompanyView:
        with session_scope() as session:
            company = CompanyRepository(session).update(company_id, **fields)
            view = CompanyRepository.to_view(company)
        bump()
        return view

    def create(self, **fields: Any) -> CompanyView:
        """Add a company by hand, keyed the same way a crawled one would be."""
        from verifier.dedupe import build_dedupe_key
        from verifier.normalize import company_name_key

        name = fields.get("company_name", "").strip()
        fields.setdefault("source", "manual")
        fields["name_key"] = company_name_key(name)
        fields["dedupe_key"] = build_dedupe_key(
            name, fields.get("tax_id"), fields.get("email"),
            fields.get("phone"), fields.get("website"),
        )
        with session_scope() as session:
            company = CompanyRepository(session).create(**fields)
            view = CompanyRepository.to_view(company)
        bump()
        return view

    def delete(self, company_id: int) -> bool:
        with session_scope() as session:
            deleted = CompanyRepository(session).delete(company_id)
        if deleted:
            bump()
        return deleted

    def set_stage(self, company_id: int, stage: str) -> None:
        with session_scope() as session:
            CompanyRepository(session).set_stage(
                company_id, PipelineStage.coerce(stage, PipelineStage.NEW)
            )
        bump()

    def set_tags(self, company_id: int, tags: list[str]) -> None:
        with session_scope() as session:
            CompanyRepository(session).set_tags(company_id, tags)
        bump()

    def set_follow_up(self, company_id: int, when: date | None) -> None:
        with session_scope() as session:
            CompanyRepository(session).update(company_id, follow_up_date=when)
        bump()

    def distinct(self, field: str) -> list[str]:
        with session_scope() as session:
            return CompanyRepository(session).distinct_values(field)

    def all_tags(self) -> list[str]:
        with session_scope() as session:
            return TagRepository(session).names()

    def crawl_dates(self) -> list[tuple[Any, int]]:
        """``(日期, 家數)``，最近的排前面。給公司頁的日期篩選用。"""
        with session_scope() as session:
            return CompanyRepository(session).crawl_dates()

    def delete_by_date(self, day: Any) -> int:
        """刪掉某一天收集到的所有公司。"""
        with session_scope() as session:
            removed = CompanyRepository(session).delete_by_date(day)
        if removed:
            bump()
        return removed

    def duplicate_groups(self) -> list[list[CompanyView]]:
        with session_scope() as session:
            repo = CompanyRepository(session)
            return [
                [CompanyRepository.to_view(c) for c in group]
                for group in repo.find_duplicate_groups()
            ]

    def merge(self, keep_id: int, drop_ids: list[int]) -> None:
        with session_scope() as session:
            CompanyRepository(session).merge(keep_id, drop_ids)
        bump()

    # --- related records -------------------------------------------------

    def add_contact(self, company_id: int, **fields: Any) -> ContactView:
        with session_scope() as session:
            contact = ContactRepository(session).add(company_id, **fields)
            view = ContactView.model_validate(contact)
        bump()
        return view

    def delete_contact(self, contact_id: int) -> bool:
        with session_scope() as session:
            deleted = ContactRepository(session).delete(contact_id)
        if deleted:
            bump()
        return deleted

    def add_activity(
        self, company_id: int, type_name: str, subject: str, body: str = ""
    ) -> ActivityView:
        with session_scope() as session:
            activity = ActivityRepository(session).add(
                company_id,
                ActivityType.coerce(type_name, ActivityType.NOTE),
                subject or None,
                body or None,
            )
            return ActivityView.model_validate(activity)

    def add_attachment(self, company_id: int, file_path: str | Path) -> str:
        """Copy a file into the app's data directory and record it."""
        source = Path(file_path).expanduser()
        if not source.exists():
            raise FileNotFoundError(f"file not found: {source}")

        config = get_config()
        sqlite_path = config.database.sqlite_path
        base = (sqlite_path.parent if sqlite_path else Path.cwd()) / ATTACHMENT_DIR_NAME
        target_dir = base / str(company_id)
        target_dir.mkdir(parents=True, exist_ok=True)

        target = target_dir / source.name
        counter = 1
        while target.exists():
            target = target_dir / f"{source.stem}-{counter}{source.suffix}"
            counter += 1
        shutil.copy2(source, target)

        with session_scope() as session:
            AttachmentRepository(session).add(
                company_id,
                filename=target.name,
                path=str(target),
                size_bytes=target.stat().st_size,
            )
        return str(target)

    def delete_attachment(self, attachment_id: int, remove_file: bool = True) -> bool:
        with session_scope() as session:
            attachment = session.get(Attachment, attachment_id)
            if attachment is None:
                return False
            path = Path(attachment.path)
            deleted = AttachmentRepository(session).delete(attachment_id)
        if deleted and remove_file:
            path.unlink(missing_ok=True)
        return deleted


class ContactController:
    def search(self, text: str | None = None, limit: int | None = None) -> list[ContactView]:
        with session_scope() as session:
            return ContactRepository(session).search(text, limit)

    def update(self, contact_id: int, **fields: Any) -> None:
        with session_scope() as session:
            ContactRepository(session).update(contact_id, **fields)

    def delete(self, contact_id: int) -> bool:
        with session_scope() as session:
            return ContactRepository(session).delete(contact_id)


class CrawlController:
    """Wraps the crawl pipeline for :class:`~gui.tasks.BackgroundTask`."""

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()

    def source_names(self) -> list[str]:
        return [s.name for s in self.config.crawler.sources]

    def enabled_source_names(self) -> list[str]:
        return [s.name for s in self.config.crawler.enabled_sources()]

    def clear_progress(self, source: str | None = None) -> int:
        """丟掉「做到哪裡」的紀錄，下一次從頭開始。

        「暫停」與「取消」的差別就只有這個。不做這件事的話兩顆按鈕的行為
        完全一樣，而畫面上會多一顆騙人的按鈕。
        """
        with session_scope() as session:
            return CrawlJobRepository(session).clear_resume(source)

    def run(
        self,
        source: str | None,
        max_pages: int | None = None,
        from_page: int | None = None,
        to_page: int | None = None,
        *,
        report: Callable[[Any], None],
        cancel_event,
    ) -> list[CrawlSummary]:
        from crawler.pipeline import crawl

        def progress(name: str, page: int, stored: int, total: int) -> None:
            report({"source": name, "page": page, "stored": stored, "total": total})


        return crawl(
            source=source,
            config=self.config,
            progress=progress,
            cancel_event=cancel_event,
            max_pages=max_pages,
            page_start=from_page,
            page_end=to_page,
        )


class VerifyController:
    def run(
        self,
        renormalize: bool = True,
        *,
        report: Callable[[Any], None],
        cancel_event,
    ) -> VerifySummary:
        from verifier.service import VerificationService

        with session_scope() as session:
            service = VerificationService(session)
            targets = CompanyRepository(session).all()
            return service.run(
                targets,
                renormalize=renormalize,
                progress=lambda done, total: report({"done": done, "total": total}),
            )


class EnrichController:
    """Visit each company's own website to find a published address.

    Directory listings very often show a phone number and a link but no e-mail,
    which is why a crawl of 216 companies can come back with 61 addresses. The
    address is usually published on the company's own contact page.
    """

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()

    def missing_email_count(self) -> int:
        """Companies that have a website but no address -- the ones worth visiting."""
        with session_scope() as session:
            return CompanyRepository(session).count_enrichable()

    def run(self, limit: int | None = None, *, report: Callable[[Any], None], cancel_event):
        from crawler.enrich import enrich_missing_emails

        return enrich_missing_emails(
            limit=limit,
            config=self.config,
            progress=lambda index, total, name: report(
                {"done": index, "total": total, "name": name}
            ),
            cancel_event=cancel_event,
        )


class RegistryController:
    """用統一編號去經濟部商業司補公司登記資料。

    補回來最有用的不是資本額，是「這家公司還在不在」——名錄網站不會把倒掉的
    會員刪掉，寄開發信之前先把解散、撤銷、廢止的挑出來，省的是實際的時間。
    """

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()

    def pending_count(self) -> int:
        """有統編、還沒查過（或查很久了）的家數。沒有統編的查不了。"""
        from crawler.registry import RECHECK_AFTER_DAYS

        with session_scope() as session:
            return CompanyRepository(session).count_registrable(RECHECK_AFTER_DAYS)

    def run(self, limit: int | None = None, *, report: Callable[[Any], None], cancel_event):
        from crawler.registry import enrich_registrations

        return enrich_registrations(
            limit=limit,
            config=self.config,
            progress=lambda index, total, name: report(
                {"done": index, "total": total, "name": name}
            ),
            cancel_event=cancel_event,
        )


class ExportController:
    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()

    def formats(self) -> list[str]:
        from exporter.service import available_formats

        return available_formats()

    def columns(self) -> list[str]:
        from exporter.base import resolve_columns

        return resolve_columns(self.config)

    def run(
        self,
        format_name: str,
        path: str | Path | None,
        criteria: CompanyFilter | None,
        columns: list[str] | None = None,
        *,
        report: Callable[[Any], None],
        cancel_event,
    ) -> tuple[Path, int]:
        from exporter.service import export_companies

        report({"stage": "querying"})
        result = export_companies(format_name, path, criteria, self.config, columns)
        report({"stage": "written"})
        return result


class ImportController:
    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()

    def preview(self, path: str | Path, rows: int = 10) -> dict[str, Any]:
        """Headers and the first few rows, so the user can check the mapping."""
        from exporter.importer import _canonical, read_table

        frame = read_table(path)
        mapping = {str(c): _canonical(c) for c in frame.columns}
        return {
            "columns": list(frame.columns),
            "mapping": mapping,
            "rows": frame.head(rows).fillna("").astype(str).values.tolist(),
            "total_rows": len(frame),
        }

    def run(
        self,
        path: str | Path,
        label: str | None = None,
        *,
        report: Callable[[Any], None],
        cancel_event,
    ):
        from exporter.importer import import_file

        report({"stage": "reading"})
        return import_file(path, label, self.config)


class LogController:
    def categories(self) -> list[str]:
        return [category.value for category in LogCategory]

    def tail(self, category: str, lines: int = 400) -> str:
        """Last ``lines`` lines of a log file."""
        path = log_file_path(LogCategory.coerce(category, LogCategory.GUI))
        if not path.exists():
            return f"(no log file yet at {path})"
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return f"(could not read {path}: {exc})"
        return "\n".join(content[-lines:])

    def clear(self, category: str) -> bool:
        path = log_file_path(LogCategory.coerce(category, LogCategory.GUI))
        if not path.exists():
            return False
        try:
            path.write_text("", encoding="utf-8")
            return True
        except OSError:
            return False


class SettingsController:
    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()

    def summary(self) -> dict[str, str]:
        """設定總覽。鍵與值都已是中文，Settings 頁可直接顯示。

        路徑一律經過 :func:`~core.config.display_path` 縮寫。使用者常常會截圖
        這個畫面來問問題，而完整路徑在 Windows 上一定含使用者名稱。
        """
        from core.config import display_path

        config = self.config
        engines = {"httpx": "httpx（一般網頁）", "playwright": "playwright（JavaScript 網頁）"}
        sqlite_path = config.database.sqlite_path
        return {
            "資料庫": (
                display_path(sqlite_path) if sqlite_path else config.database.resolved_url
            ),
            "爬取引擎": engines.get(config.crawler.engine, config.crawler.engine),
            "User-Agent": config.crawler.resolved_user_agent(),
            "遵守 robots.txt": "是" if config.crawler.respect_robots else "否（僅限自有網站）",
            "爬取延遲": (
                f"{config.crawler.delay_seconds} 秒"
                f"（另加最多 {config.crawler.delay_jitter} 秒隨機）"
            ),
            # 名稱不能寫「每個來源頁數上限」——那個講法在這個值真的是天花板
            # 的時候才對。現在它只是「來源沒指定時的預設」，來源自己填了幾頁
            # 就是幾頁（見 crawler/base.py 的 page_limit）。
            "頁數上限預設值": f"{config.crawler.max_pages} 頁（來源自己有設定時以來源為準）",
            "信箱 MX 驗證": "開啟" if config.verifier.check_mx else "關閉",
            "個資欄位加密": "開啟" if config.database.encrypt else "關閉",
            # 這三個也要縮寫。上一次只改了「資料庫」那一行，結果畫面上仍然
            # 印著三行完整路徑——修一半跟沒修一樣。
            "匯出資料夾": display_path(config.exporter.resolved_output_dir),
            "日誌資料夾": display_path(config.logging.resolved_dir),
            "備份資料夾": display_path(config.backup.resolved_dir),
            # 「Gmail 讀信」看不出是在讀誰的信、讀來做什麼。這個功能是從
            # 使用者自己的收件匣裡，把寄件人的聯絡資訊抓成聯絡人。
            "從 Gmail 收件匣抓聯絡人": "已啟用" if config.gmail.enabled else "已停用",
        }

    def config_path(self) -> Path:
        from core.config import DEFAULT_CONFIG_PATH

        return DEFAULT_CONFIG_PATH

    # --------------------------------------------------- 總覽裡可以改的設定

    #: 總覽表格裡哪幾項可以直接編輯。
    #:
    #: ``顯示名稱 -> (設定區段, 鍵, 型別, 選項, 說明)``
    #:
    #: 沒有列在這裡的就是唯讀，各有各的理由：
    #:
    #: * **遵守 robots.txt** —— 這是唯一一個「關掉會讓程式做出不該做的事」的
    #:   開關。它留在 config.yaml 裡，要關的人必須自己去編輯設定檔並看到那段
    #:   註解。介面上一鍵可切太容易誤按，而誤按的後果由使用者承擔。
    #: * **個資欄位加密** —— 切換會重寫整個資料庫，那件事該在啟動時做完，
    #:   不是在設定頁按一下就開始。
    #: * **各種資料夾與資料庫位置** —— 改了要搬檔案，不是改一個字串而已。
    EDITABLE_SETTINGS: dict[str, tuple[str, str, str, tuple, str]] = {
        "爬取引擎": (
            "crawler", "engine", "choice", ("httpx", "playwright"),
            "playwright 可以爬用 JavaScript 產生內容的網站，但慢很多，"
            "而且要先執行過 playwright install chromium。",
        ),
        "爬取延遲": (
            "crawler", "delay_seconds", "float", (0.5, 60.0),
            "每次請求之間至少等幾秒。調低會增加對方站台的負擔，"
            "也比較容易被判定為異常流量。",
        ),
        "頁數上限預設值": (
            "crawler", "max_pages", "int", (1, 500),
            "來源自己有設定頁數的話以來源為準；這裡只是沒設定時的預設值。",
        ),
        "信箱 MX 驗證": (
            "verifier", "check_mx", "bool", (),
            "查詢信箱網域是不是真的收信。關掉會快很多，但無效信箱會留在名單裡。",
        ),
        "從 Gmail 收件匣抓聯絡人": (
            "gmail", "enabled", "bool", (),
            "開啟後可以用「python main.py gmail」從你自己的收件匣裡，"
            "把往來對象的公司與聯絡資訊抓成聯絡人。需要先設定好 Gmail 帳號。",
        ),
    }

    def is_editable(self, label: str) -> bool:
        return label in self.EDITABLE_SETTINGS

    def setting_spec(self, label: str):
        """``(區段, 鍵, 型別, 選項, 說明, 目前的值)``。"""
        section, key, kind, options, help_text = self.EDITABLE_SETTINGS[label]
        current = getattr(getattr(self.config, section), key)
        return section, key, kind, options, help_text, current

    def update_setting(self, label: str, value: Any) -> None:
        """把總覽裡改的值存起來。

        跟其他設定一樣寫進 user_settings.yaml，不動 config.yaml——後者寫滿了
        解釋每個設定的註解，程式化改寫會把它們全部洗掉。
        """
        from core.config import save_user_setting

        if label not in self.EDITABLE_SETTINGS:
            raise CRMError(f"「{label}」不能在這裡修改。")
        section, key, _kind, _options, _help = self.EDITABLE_SETTINGS[label]
        try:
            save_user_setting(section, key, value)
        except CRMError as exc:
            raise CRMError(f"儲存「{label}」失敗：{exc}") from exc
        except ValueError as exc:
            raise CRMError(f"「{label}」的值不合法：{exc}") from exc
        self.config = get_config()

    # ------------------------------------------------------------- 排程

    def scheduler_settings(self) -> dict[str, Any]:
        """設定頁那張排程表單目前該顯示的值。"""
        settings = self.config.scheduler
        return {
            "enabled": settings.enabled,
            "action": settings.action,
            "mode": settings.mode,
            "at": settings.at,
            "every_minutes": settings.every_minutes,
            "day_of_month": settings.day_of_month,
            "sources": list(settings.sources),
            "verify_after_crawl": settings.verify_after_crawl,
            "catch_up": settings.catch_up,
            "mail_template": settings.mail_template,
            "mail_campaign": settings.mail_campaign,
            "mail_attachments": list(settings.mail_attachments),
            "mail_batch_limit": settings.mail_batch_limit,
            "mail_industry": settings.mail_industry,
            "mail_stage": settings.mail_stage,
            "mail_tag": settings.mail_tag,
            "mail_verified_only": settings.mail_verified_only,
        }

    def save_scheduler_settings(self, values: dict[str, Any]) -> None:
        """整組一起存。

        逐一儲存會失敗：``action`` 設成寄信時 ``mail_template`` 就變成必填，
        先寫哪一個都會在中途產生不合法的設定而被回滾。
        """
        from core.config import save_user_settings

        try:
            save_user_settings("scheduler", values)
        except CRMError as exc:
            raise CRMError(f"儲存排程設定失敗：{exc}") from exc
        except ValueError as exc:
            # pydantic 的驗證錯誤（例如要寄信卻沒選樣板）訊息很長，只留重點。
            raise CRMError(f"排程設定不合法：{exc}") from exc
        self.config = get_config()

    def scheduler_next_run_text(self) -> str:
        """下次執行時間的說明文字，不啟動排程執行緒也算得出來。"""
        from datetime import datetime

        from core.scheduler import load_state, next_run_after

        settings = self.config.scheduler
        if not settings.enabled:
            return "排程已關閉。"
        state = load_state(self.config)
        due = next_run_after(datetime.now(), settings, state.last_run)
        action = {
            "crawl": "爬取",
            "send": "寄信",
            "crawl_and_send": "爬取後寄信",
        }.get(settings.action, settings.action)
        return (
            f"下次{action}：{due:%Y-%m-%d %H:%M}"
            "（排程只在本程式開啟時執行，關掉視窗就不會跑）"
        )

    def crawl_source_names(self) -> list[str]:
        """可以排程的來源名稱。"""
        return [source.name for source in self.config.crawler.sources]

    def mail_template_names(self) -> list[str]:
        from gmail.templates import list_templates

        try:
            return list_templates(self.config)
        except CRMError:
            return []

    def industry_options(self) -> list[str]:
        """資料庫裡實際存在的產業。空的就回空清單，不要造假選項。"""
        try:
            return CompanyController().distinct("industry")
        except CRMError:
            return []

    def tag_options(self) -> list[str]:
        try:
            return CompanyController().all_tags()
        except CRMError:
            return []

    def backups(self) -> list[BackupFile]:
        return list_backups(self.config)

    def create_backup(self) -> BackupFile:
        return create_backup("manual", self.config)

    def restore(self, name: str) -> Path:
        from database.session import reset_engine

        reset_engine()
        return restore_backup(name, self.config)

    def delete_backup(self, name: str) -> Path:
        from database.backup import delete_backup

        return delete_backup(name, self.config)

    def export_day(self, day, format_name: str = "excel") -> tuple[Path, int]:
        """把某一天收集到的公司另存一份，回傳 ``(檔案路徑, 筆數)``。

        這是**匯出**，不是備份，而且刻意分開：SQLite 的備份是整個檔案複製，
        沒有「只備份某一天」這回事——那樣的檔案還原回去會把其他日期的資料
        一起抹掉。這裡產生的是可以重新匯入的資料檔，適合「把這批單獨留存」
        或「交給別人」。要能還原整個資料庫，用上面的完整備份。
        """
        from datetime import datetime

        from exporter.service import export_companies

        criteria = CompanyFilter(
            created_after=datetime.combine(day, datetime.min.time()),
            created_before=datetime.combine(day, datetime.max.time()),
        )
        target = (
            self.config.exporter.resolved_output_dir
            / f"{day:%Y%m%d}-收集資料.{'xlsx' if format_name == 'excel' else format_name}"
        )
        return export_companies(format_name, target, criteria, self.config)

    def crawl_dates(self) -> list[tuple[Any, int]]:
        """``(日期, 家數)``，最近的排前面。"""
        with session_scope() as session:
            return CompanyRepository(session).crawl_dates()

    # ----------------------------------------------------------- 資料庫加密

    def encryption_status(self):
        """個資欄位的加密狀況，供設定頁顯示。

        轉換本身在啟動時就做完了（見 :func:`database.session.init_db`），這裡
        只負責回報——設定頁不該是一個按下去會改寫整個資料庫的地方。
        """
        from database.encryption import status
        from database.session import get_engine

        return status(get_engine())

    def export_encryption_key(self) -> str:
        """金鑰的可保存字串。

        金鑰只存在系統憑證保管庫，而保管庫不會跟著 `crm.db` 一起被複製——少了這個
        功能，硬碟壞掉重灌後連 `backups/` 裡的備份都解不開。
        """
        from core.crypto import export_key

        return export_key()

    def import_encryption_key(self, value: str, force: bool = False) -> None:
        from core.crypto import import_key

        import_key(value, force=force)

    def stage_names(self) -> list[str]:
        return PipelineStage.values()

    def status_names(self) -> list[str]:
        return RecordStatus.values()

    def priority_names(self) -> list[str]:
        return Priority.values()

    # --------------------------------------------------------- Gmail account

    def keyring_available(self) -> bool:
        from core.credentials import keyring_available

        return keyring_available()

    def credential_status(self, name: str) -> SecretStatus:
        from core.credentials import describe

        return describe(name)

    def save_credential(self, name: str, value: str) -> SecretSource:
        from core.credentials import set_secret

        return set_secret(name, value)

    def delete_credential(self, name: str) -> bool:
        from core.credentials import delete_secret

        return delete_secret(name)

    def test_gmail_connection(self, *, report: Callable[[Any], None], cancel_event: Any) -> str:
        """Log in over SMTP and immediately disconnect, as a pure connectivity check.

        Suitable as a :class:`~gui.tasks.BackgroundTask` worker. Raises
        :class:`~core.errors.GmailError` (a :class:`CRMError`) with a Chinese
        explanation on any failure -- missing credentials, bad password, or an
        unreachable host all come back from :meth:`gmail.sender.SmtpSender.connect`
        already translated.
        """
        from gmail.sender import SmtpSender

        report({"stage": "connecting"})
        sender = SmtpSender(self.config)
        sender.connect()
        address = sender.settings.address
        sender.close()
        return f"已成功連線並登入 Gmail 帳號 {address}。"
