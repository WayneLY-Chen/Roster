"""Controller behind the outbound mail merge page.

Keeps :mod:`gui.pages.mail` from importing ``gmail.*`` directly, exactly like
every other page talks to a controller in :mod:`gui.controllers`. Every
failure is normalized into a :class:`~core.errors.CRMError` carrying a Chinese
message -- a dry run or (especially) a real send must never fail silently or
surface an English stack trace the user cannot act on.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.config import AppConfig, get_config
from core.errors import CRMError
from core.schemas import CompanyFilter
from gmail import campaign as campaign_module
from gmail import templates as templates_module
from gmail.campaign import CampaignPlan, CampaignResult


class MailController:
    """Templates, recipient plans and sends for the Mail page."""

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()

    # ------------------------------------------------------------- templates

    def templates(self) -> list[str]:
        try:
            return templates_module.list_templates(self.config)
        except CRMError as exc:
            raise CRMError(f"無法讀取樣板清單：{exc}") from exc

    def load(self, name: str) -> tuple[str, str]:
        """Subject and body of one saved template."""
        try:
            template = templates_module.load_template(name, self.config)
        except CRMError as exc:
            raise CRMError(f"無法讀取樣板「{name}」：{exc}") from exc
        return template.subject, template.body

    def save(self, name: str, subject: str, body: str) -> None:
        clean_name = (name or "").strip()
        if not clean_name:
            raise CRMError("請輸入樣板名稱。")
        try:
            templates_module.save_template(clean_name, subject, body, self.config)
        except CRMError as exc:
            raise CRMError(f"儲存樣板「{clean_name}」失敗：{exc}") from exc

    def placeholders(self) -> dict[str, str]:
        return templates_module.available_placeholders()

    # -------------------------------------------------------------- campaign

    def build_plan(
        self,
        criteria: CompanyFilter,
        template_name: str,
        campaign_name: str,
        *,
        attachments: list[str] | None = None,
        report: Callable[[Any], None] | None = None,
        cancel_event: Any = None,
    ) -> CampaignPlan:
        """Decide who would be emailed. Suitable as a :class:`BackgroundTask` worker."""
        if not (template_name or "").strip():
            raise CRMError("請先選擇一個樣板。")
        try:
            return campaign_module.build_plan(
                criteria, template_name, campaign_name, self.config, attachments
            )
        except CRMError as exc:
            raise CRMError(f"產生寄送名單失敗：{exc}") from exc

    # ------------------------------------------------------------- 附件

    def attachments(self) -> list[Any]:
        """``attachments/`` 裡現有的檔案。"""
        from gmail.attachments import list_stored

        return list_stored(self.config)

    def add_attachment(self, source: str) -> Any:
        """把使用者選的檔案複製進附件資料夾。"""
        from gmail.attachments import store

        try:
            return store(source, self.config)
        except CRMError as exc:
            raise CRMError(f"加入附件失敗：{exc}") from exc

    def remove_attachment(self, name: str) -> None:
        from gmail.attachments import remove

        try:
            remove(name, self.config)
        except CRMError as exc:
            raise CRMError(f"移除附件失敗：{exc}") from exc

    def update_attachment(
        self, name: str, label: str | None = None, note: str | None = None
    ) -> None:
        """改顯示名稱或備註。不會動到檔案本身。"""
        from gmail.attachments import update

        try:
            update(name, self.config, label=label, note=note)
        except CRMError as exc:
            raise CRMError(f"更新附件失敗：{exc}") from exc

    def attachment_used_by_schedule(self, name: str) -> bool:
        """刪掉排程正在用的附件，後果是排程在半夜三點失敗而沒有人看到。"""
        from gmail.attachments import used_by_schedule

        return used_by_schedule(name, self.config)

    def attachments_dir(self):
        from gmail.attachments import attachments_dir

        return attachments_dir(self.config)

    def attachment_limit_bytes(self) -> int:
        return self.config.mailer.max_attachment_bytes

    def preview_first(self, plan: CampaignPlan) -> tuple[str, str] | None:
        """Subject and full body (with the unsubscribe note) for the first
        sendable recipient -- exactly what :meth:`send` would hand to SMTP.

        Returns ``None`` when the plan has no sendable recipient.
        """
        sendable = [recipient for recipient in plan.recipients if recipient.will_send]
        if not sendable:
            return None
        recipient = sendable[0]
        note = self.config.mailer.unsubscribe_note
        body = recipient.body
        if note and note.strip() not in body:
            body = f"{body.rstrip()}\n\n{note}"
        return recipient.subject, body

    def send(
        self,
        plan: CampaignPlan,
        *,
        report: Callable[[Any], None] | None = None,
        cancel_event: Any = None,
        force_dry_run: bool = False,
    ) -> CampaignResult:
        """Execute a plan already produced by :meth:`build_plan`.

        ``force_dry_run`` overrides ``mailer.dry_run`` for this one call
        without touching the persisted configuration -- used by the page's
        "演練（不寄出）" button, which must stay safe even when the real
        "開始寄送" button is configured to send for real.
        """

        def _report(recipient: Any, status: Any) -> None:
            if report is None:
                return
            report(
                {
                    "company_name": recipient.company_name,
                    "to_address": recipient.to_address,
                    "status": getattr(status, "value", str(status)),
                }
            )

        config = self.config
        if force_dry_run and not config.mailer.dry_run:
            config = config.model_copy(
                update={"mailer": config.mailer.model_copy(update={"dry_run": True})}
            )

        try:
            return campaign_module.send_campaign(
                plan, config, report=_report, cancel_event=cancel_event
            )
        except CRMError as exc:
            raise CRMError(f"寄送過程發生錯誤：{exc}") from exc

    def daily_sent(self) -> int:
        try:
            return campaign_module.daily_sent_count(self.config)
        except CRMError as exc:
            raise CRMError(f"無法讀取今日已寄送數量：{exc}") from exc

    def daily_limit(self) -> int:
        return self.config.mailer.daily_limit

    def set_daily_limit(self, value: int) -> None:
        """Save a new ``mailer.daily_limit`` from the Settings page.

        Same mechanism as :meth:`set_mailer_option` -- ``user_settings.yaml``,
        not ``config.yaml`` -- and the same validate-before-writing behaviour:
        :func:`~core.config.save_user_setting` rebuilds the whole config
        before committing the change, so an out-of-range limit fails at the
        click instead of at the next start-up.
        """
        from core.config import save_user_setting

        try:
            save_user_setting("mailer", "daily_limit", int(value))
        except CRMError as exc:
            raise CRMError(f"儲存每日寄送上限失敗：{exc}") from exc
        # Same reason as set_mailer_option: the saved value has to reach this
        # controller too, or mailer_status() would keep reporting the old one.
        self.config = get_config()

    def mailer_status(self) -> dict[str, str]:
        """Everything the status bar at the top of the Mail page shows."""
        mailer = self.config.mailer
        address = mailer.address
        password = mailer.app_password
        try:
            sent_today = campaign_module.daily_sent_count(self.config)
        except CRMError:
            sent_today = 0
        return {
            "address": address or "",
            "account_ready": "是" if (address and password) else "否",
            "enabled": "是" if mailer.enabled else "否",
            "dry_run": "是" if mailer.dry_run else "否",
            "daily_sent": str(sent_today),
            "daily_limit": str(mailer.daily_limit),
            "delay_seconds": str(mailer.delay_seconds),
            "sender_name": mailer.sender_name or "",
        }

    def set_mailer_option(self, key: str, value: bool) -> None:
        """Flip ``mailer.enabled`` / ``mailer.dry_run`` from the GUI.

        Saved to ``user_settings.yaml`` rather than ``config.yaml``: rewriting
        the latter programmatically would strip out the comments that explain
        every other setting in it. The two are merged on load, with this file
        winning.
        """
        if key not in ("enabled", "dry_run"):
            raise CRMError(f"不支援的設定項目：{key}")
        from core.config import save_user_setting

        try:
            save_user_setting("mailer", key, bool(value))
        except CRMError as exc:
            raise CRMError(f"儲存設定失敗：{exc}") from exc
        # The saved value has to reach *this* controller too, or the status
        # strip would keep reporting the value from before the click.
        self.config = get_config()

    def mark_do_not_contact(self, company_id: int) -> bool:
        try:
            return campaign_module.mark_do_not_contact(company_id)
        except CRMError as exc:
            raise CRMError(f"標記請勿聯絡失敗：{exc}") from exc


# --------------------------------------------------------------------- 退信
#
# 這一段補的是「信寄出去之後沒有回路」裡最要命的那一塊。詳細理由見
# gmail/bounces.py 的模組說明。


@dataclass(frozen=True, slots=True)
class BounceHit:
    """一封退信，而且對得回名單上的某一家公司。"""

    bounce: Any                 # gmail.bounces.Bounce
    company_id: int
    company_name: str
    email: str
    #: 這家公司的信箱已經標成退過信了。列出來但預設不勾——重複標一次沒有意義。
    already: bool = False


@dataclass(slots=True)
class BounceScan:
    """掃一次信箱的結果。"""

    hits: list[BounceHit] = field(default_factory=list)
    #: 看了幾封退信通知。
    messages: int = 0
    #: 解析出退信、但那個地址**我們從來沒有寄過**，所以不予理會的筆數。
    unmatched: int = 0

    def describe(self) -> str:
        parts = [f"看了 {self.messages} 封退信通知，對得上名單的有 {len(self.hits)} 筆"]
        if self.unmatched:
            parts.append(f"另外 {self.unmatched} 筆的地址這支程式沒有寄過，不予理會")
        return "；".join(parts)


@dataclass(frozen=True, slots=True)
class BounceResult:
    """標記之後的結果。"""

    marked: int = 0
    #: 只留了紀錄、沒有改判定的（軟退信）。
    noted: int = 0

    def describe(self) -> str:
        parts = []
        if self.marked:
            parts.append(f"{self.marked} 個信箱標成「退過信」，之後不會再寄")
        if self.noted:
            parts.append(f"{self.noted} 筆軟退信只留了紀錄，沒有改判定")
        return "，".join(parts) or "沒有任何改動"


class BounceController:
    """讀收件匣找退信，把結果對回名單。

    跟 :class:`MailController` 分開，因為它們相依的東西不一樣：這一個要碰
    IMAP 與資料庫，寄信那一個只碰 SMTP 與樣板。
    """

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()

    def _sent_addresses(self) -> set[str]:
        """這支程式真的寄出去過的每一個地址。

        這就是那條「只認自己寄過的地址」的實作。少了它，收件匣裡**任何一封**
        信都能改動使用者的名單——那是一個從外面打得到的洞：只要寄一封長得像
        退信的信給他，就能讓他從此不再聯絡某一家公司。
        """
        from sqlalchemy import select

        from core.constants import EmailStatus
        from database.models import EmailMessage
        from database.session import session_scope

        with session_scope() as session:
            rows = session.execute(
                select(EmailMessage.to_address).where(
                    EmailMessage.status == EmailStatus.SENT.value
                )
            ).scalars()
            return {address.strip().lower() for address in rows if address}

    def scan(
        self,
        *,
        query: str | None = None,
        limit: int | None = None,
        report: Callable[[object], None] | None = None,
        cancel_event: Any = None,
    ) -> BounceScan:
        """連一次信箱，把退信找出來。**唯讀，什麼都不寫。**

        順序刻意是「先讀完信箱、再開資料庫交易」：IMAP 那一段可能要幾十秒，
        整段包在交易裡的話 SQLite 會被佔著，同時間背景在跑的爬取就寫不進去。
        """
        from sqlalchemy import select

        from core.constants import EmailStatus, EmailVerdict
        from database.models import Company, EmailMessage
        from database.session import session_scope
        from gmail.bounces import iter_bounces
        from gmail.client import gmail_session

        if report is not None:
            report("正在連你的信箱…")
        sent = self._sent_addresses()

        seen: dict[str, Any] = {}
        messages: set[str] = set()

        def note(uid: str) -> None:
            messages.add(uid)
            if report is not None and len(messages) % 10 == 0:
                report(f"看過 {len(messages)} 封了…")

        try:
            with gmail_session(self.config) as client:
                for bounce in iter_bounces(
                    client, query=query, limit=limit, sent_to=sent, on_message=note
                ):
                    if cancel_event is not None and cancel_event.is_set():
                        raise CRMError("已取消。")
                    previous = seen.get(bounce.address)
                    # 同一個地址被退好幾次時留「最嚴重」的那一筆：硬退信優先。
                    # 使用者要看的是「這個信箱到底死了沒」。
                    if previous is None or (bounce.hard and not previous.hard):
                        seen[bounce.address] = bounce
        except CRMError:
            raise
        except Exception as exc:
            raise CRMError(f"讀信箱時出錯：{exc}") from exc

        scan = BounceScan(messages=len(messages))
        if not seen:
            return scan

        if report is not None:
            report("正在對回名單…")
        with session_scope() as session:
            for address, bounce in seen.items():
                if address not in sent:
                    scan.unmatched += 1
                    continue
                company_id = session.execute(
                    select(EmailMessage.company_id)
                    .where(
                        EmailMessage.to_address == address,
                        EmailMessage.status == EmailStatus.SENT.value,
                    )
                    .order_by(EmailMessage.sent_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                company = session.get(Company, company_id) if company_id else None
                if company is None:
                    # 寄過，但那家公司後來被刪掉了。不算 unmatched——那不是
                    # 「來路不明」，只是沒有東西可以標了。
                    continue
                scan.hits.append(
                    BounceHit(
                        bounce=bounce,
                        company_id=company.id,
                        company_name=company.company_name,
                        email=company.email or address,
                        already=company.email_verdict == EmailVerdict.BOUNCED.value,
                    )
                )
        scan.hits.sort(key=lambda hit: (not hit.bounce.hard, hit.company_name))
        return scan

    def apply(
        self,
        hits: "Sequence[BounceHit]",
        *,
        report: Callable[[object], None] | None = None,
        cancel_event: Any = None,
    ) -> BounceResult:
        """把使用者勾起來的那幾筆寫進資料庫。

        **只有硬退信會改判定。** 軟退信（``4.x.x``：信箱滿了、對方伺服器暫時
        掛掉）只留一則紀錄——標死一個真實客戶的信箱，代價是從此再也不寄給他，
        而他不會知道、使用者也不會發現。

        每一筆都留一則 Activity 寫明是哪一封通知造成的。三個月後看到一個被標
        死的信箱，要查得出來為什麼。
        """
        from core.constants import ActivityType, EmailVerdict
        from database.models import now
        from database.repository import ActivityRepository, CompanyRepository
        from database.session import session_scope

        chosen = list(hits)
        if not chosen:
            return BounceResult()

        marked = noted = 0
        with session_scope() as session:
            companies = CompanyRepository(session)
            activities = ActivityRepository(session)
            for hit in chosen:
                company = companies.get(hit.company_id)
                if company is None:
                    continue
                bounce = hit.bounce
                detail = "；".join(
                    part
                    for part in (
                        f"代碼 {bounce.code}" if bounce.code else "",
                        bounce.reason,
                        f"通知主旨：{bounce.subject}" if bounce.subject else "",
                    )
                    if part
                )
                if bounce.hard:
                    company.email_verdict = EmailVerdict.BOUNCED.value
                    company.updated_at = now()
                    marked += 1
                    subject = f"退信（永久）：{bounce.address}"
                else:
                    noted += 1
                    subject = f"退信（暫時）：{bounce.address}"
                activities.add(
                    hit.company_id,
                    ActivityType.SYSTEM,
                    subject=subject,
                    body=detail or "對方伺服器沒有說明原因。",
                )

        result = BounceResult(marked=marked, noted=noted)
        if report is not None:
            report(result.describe())
        return result
