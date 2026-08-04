"""Plan and run an outbound mail merge -- where every anti-abuse rule lives.

The core idea: sending mail is split into two steps that are never allowed to
merge into one. :func:`build_plan` decides, for every matching company,
whether it *may* be emailed at all and never touches the network or SMTP.
:func:`send_campaign` then executes only the recipients the plan already
approved, one at a time, writing an audit row before it dares hand anything to
SMTP.

Skip rules are applied in a fixed order (see :func:`build_plan`) so the same
company always gets the same, explainable reason. ``do_not_contact`` is
checked first and no other setting can override it -- there is deliberately no
parameter that bypasses it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select

from core.config import AppConfig, get_config
from core.constants import ActivityType, EmailStatus, EmailVerdict, SkipReason
from core.errors import GmailError
from core.schemas import CompanyFilter
from database.models import Company, EmailMessage, now
from database.types import email_equals
from database.repository import ActivityRepository, CompanyRepository
from database.session import session_scope
from gmail.sender import SmtpSender
from gmail.templates import load_template, render
from verifier.validators import is_valid_email


@dataclass(slots=True)
class RecipientPlan:
    """One company's outcome from :func:`build_plan`."""

    company_id: int
    company_name: str
    to_address: str
    subject: str
    body: str
    will_send: bool
    skip_reason: SkipReason | None = None


@dataclass(slots=True)
class CampaignPlan:
    """The full, reviewable plan for one campaign run."""

    name: str
    recipients: list[RecipientPlan]
    sendable: int
    skipped: int
    skip_counts: dict[str, int]
    daily_remaining: int


@dataclass(slots=True)
class CampaignResult:
    """Outcome of actually executing a :class:`CampaignPlan`."""

    sent: int = 0
    failed: int = 0
    skipped: int = 0
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)


def _midnight_today() -> datetime:
    return datetime.combine(date.today(), datetime.min.time())


def _daily_sent_count(session) -> int:  # noqa: ANN001 - SQLAlchemy Session
    stmt = select(func.count(EmailMessage.id)).where(
        EmailMessage.status == EmailStatus.SENT.value,
        EmailMessage.created_at >= _midnight_today(),
    )
    return int(session.execute(stmt).scalar_one())


def daily_sent_count(config: AppConfig | None = None) -> int:
    """Number of messages already marked ``Sent`` today. Drives the daily cap."""
    config = config or get_config()
    with session_scope() as session:
        return _daily_sent_count(session)


def _classify(
    company: Company,
    *,
    resend_cutoff: datetime,
    require_verified_email: bool,
    remaining_today: int,
    already_planned: int,
) -> SkipReason | None:
    """Apply the skip rules, in the mandated order. ``None`` means send it."""
    if company.do_not_contact:
        return SkipReason.DO_NOT_CONTACT
    if not company.email:
        return SkipReason.NO_EMAIL
    if not is_valid_email(company.email):
        return SkipReason.INVALID_EMAIL
    if require_verified_email and company.email_verdict != EmailVerdict.VALID.value:
        return SkipReason.UNVERIFIED
    if company.last_emailed_at is not None and company.last_emailed_at >= resend_cutoff:
        return SkipReason.RECENTLY_CONTACTED
    if already_planned >= remaining_today:
        return SkipReason.DAILY_CAP
    return None


def build_plan(
    criteria: CompanyFilter,
    template_name: str,
    campaign_name: str,
    config: AppConfig | None = None,
) -> CampaignPlan:
    """Decide who would be emailed, without sending anything or touching SMTP."""
    config = config or get_config()
    mailer = config.mailer
    template = load_template(template_name, config)

    resend_cutoff = now() - timedelta(days=mailer.resend_after_days)

    recipients: list[RecipientPlan] = []
    skip_counts: dict[str, int] = {}
    sendable = 0

    with session_scope() as session:
        repo = CompanyRepository(session)
        companies = repo.search(criteria)
        already_sent_today = _daily_sent_count(session)
        remaining_today = max(mailer.daily_limit - already_sent_today, 0)

        for company in companies:
            skip_reason = _classify(
                company,
                resend_cutoff=resend_cutoff,
                require_verified_email=mailer.require_verified_email,
                remaining_today=remaining_today,
                already_planned=sendable,
            )

            if skip_reason is None:
                view = CompanyRepository.to_view(company)
                subject = render(template.subject, view)
                body = render(template.body, view)
                recipients.append(
                    RecipientPlan(
                        company_id=company.id,
                        company_name=company.company_name,
                        to_address=company.email or "",
                        subject=subject,
                        body=body,
                        will_send=True,
                        skip_reason=None,
                    )
                )
                sendable += 1
            else:
                skip_counts[skip_reason.value] = skip_counts.get(skip_reason.value, 0) + 1
                recipients.append(
                    RecipientPlan(
                        company_id=company.id,
                        company_name=company.company_name,
                        to_address=company.email or "",
                        subject="",
                        body="",
                        will_send=False,
                        skip_reason=skip_reason,
                    )
                )

    return CampaignPlan(
        name=campaign_name,
        recipients=recipients,
        sendable=sendable,
        skipped=len(recipients) - sendable,
        skip_counts=skip_counts,
        daily_remaining=max(remaining_today - sendable, 0),
    )


def _with_unsubscribe_note(body: str, note: str) -> str:
    """Guarantee the opt-out sentence is present, without duplicating it.

    An HTML body gets an HTML note. Appending bare text to markup would leave
    the one sentence recipients need in order to opt out jammed onto the end of
    the last paragraph, or swallowed entirely by an unclosed tag.
    """
    if not note:
        return body
    if note.strip() in body:
        return body

    from gmail.richtext import looks_like_html

    if looks_like_html(body):
        import html as html_module

        return f'{body.rstrip()}\n<p style="color:#888;font-size:12px">{html_module.escape(note)}</p>'
    return f"{body.rstrip()}\n\n{note}"


def _sleep_cancellable(seconds: float, cancel_event: threading.Event | None) -> None:
    if seconds <= 0:
        return
    if cancel_event is None:
        time.sleep(seconds)
    else:
        cancel_event.wait(seconds)


def send_campaign(
    plan: CampaignPlan,
    config: AppConfig | None = None,
    report=None,  # Callable[[RecipientPlan, EmailStatus], None] | None
    cancel_event: threading.Event | None = None,
) -> CampaignResult:
    """Execute a plan already produced by :func:`build_plan`.

    Every message is written to ``email_messages`` as ``Pending`` and committed
    *before* it is handed to SMTP (or, in dry-run, before it is merely marked
    ``Dry Run``), so a crash mid-batch leaves an audit trail instead of a
    silent gap. ``dry_run`` never opens an SMTP connection at all.
    """
    config = config or get_config()
    mailer = config.mailer
    result = CampaignResult(dry_run=mailer.dry_run)

    to_send = [r for r in plan.recipients if r.will_send]
    already_skipped = len(plan.recipients) - len(to_send)

    sender: SmtpSender | None = None
    if not mailer.dry_run and to_send:
        sender = SmtpSender(config)
        sender.connect()

    processed = 0
    try:
        for index, recipient in enumerate(to_send):
            if cancel_event is not None and cancel_event.is_set():
                break

            body = _with_unsubscribe_note(recipient.body, mailer.unsubscribe_note)

            with session_scope() as session:
                message = EmailMessage(
                    company_id=recipient.company_id,
                    campaign=plan.name,
                    to_address=recipient.to_address,
                    subject=recipient.subject,
                    body=body,
                    status=EmailStatus.PENDING.value,
                )
                session.add(message)
                session.flush()
                message_id = message.id

            status = EmailStatus.DRY_RUN if mailer.dry_run else EmailStatus.SENT
            error_text: str | None = None

            if not mailer.dry_run:
                try:
                    assert sender is not None
                    sender.send(message)
                except GmailError as exc:
                    status = EmailStatus.FAILED
                    error_text = str(exc)
                    result.failed += 1
                    result.errors.append(f"{recipient.company_name}: {exc}")

            with session_scope() as session:
                stored = session.get(EmailMessage, message_id)
                if stored is not None:
                    stored.status = status.value
                    stored.error = error_text
                    stored.sent_at = now() if status != EmailStatus.FAILED else None

                if status in (EmailStatus.SENT, EmailStatus.DRY_RUN):
                    result.sent += 1
                    company_repo = CompanyRepository(session)
                    company = company_repo.get(recipient.company_id)
                    if company is not None and status is EmailStatus.SENT:
                        # Dry runs must not affect real outreach bookkeeping --
                        # nothing was actually delivered.
                        company.last_emailed_at = now()
                        company.email_count += 1
                    if status is EmailStatus.SENT:
                        ActivityRepository(session).add(
                            recipient.company_id,
                            ActivityType.EMAIL,
                            subject=recipient.subject,
                            body=body,
                        )

            processed += 1
            if report is not None:
                report(recipient, status)

            if index < len(to_send) - 1:
                _sleep_cancellable(mailer.delay_seconds, cancel_event)
    finally:
        if sender is not None:
            sender.close()

    result.skipped = already_skipped + (len(to_send) - processed)
    return result


def mark_do_not_contact(company_id: int) -> bool:
    """Suppress every future send to one company. Nothing ever overrides this."""
    with session_scope() as session:
        repo = CompanyRepository(session)
        company = repo.get(company_id)
        if company is None:
            return False
        company.do_not_contact = True
        ActivityRepository(session).add(
            company_id,
            ActivityType.SYSTEM,
            subject="標記為請勿聯絡",
            body="已將此公司標記為請勿聯絡，日後的開發信寄送將自動略過此對象。",
        )
        return True


def unsubscribe_by_email(address: str) -> int:
    """Suppress every company record sharing ``address``. Returns how many."""
    clean = (address or "").strip().lower()
    if not clean:
        return 0
    with session_scope() as session:
        stmt = select(Company).where(email_equals(Company.email, clean))
        companies = list(session.execute(stmt).scalars())
        activity_repo = ActivityRepository(session)
        for company in companies:
            company.do_not_contact = True
            activity_repo.add(
                company.id,
                ActivityType.SYSTEM,
                subject="取消訂閱",
                body=f"{clean} 要求取消訂閱，已標記為請勿聯絡，不再寄送開發信。",
            )
        return len(companies)
