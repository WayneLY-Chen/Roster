"""Controller behind the outbound mail merge page.

Keeps :mod:`gui.pages.mail` from importing ``gmail.*`` directly, exactly like
every other page talks to a controller in :mod:`gui.controllers`. Every
failure is normalized into a :class:`~core.errors.CRMError` carrying a Chinese
message -- a dry run or (especially) a real send must never fail silently or
surface an English stack trace the user cannot act on.
"""

from __future__ import annotations

from collections.abc import Callable
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
        report: Callable[[Any], None] | None = None,
        cancel_event: Any = None,
    ) -> CampaignPlan:
        """Decide who would be emailed. Suitable as a :class:`BackgroundTask` worker."""
        if not (template_name or "").strip():
            raise CRMError("請先選擇一個樣板。")
        try:
            return campaign_module.build_plan(criteria, template_name, campaign_name, self.config)
        except CRMError as exc:
            raise CRMError(f"產生寄送名單失敗：{exc}") from exc

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
