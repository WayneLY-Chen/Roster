"""Tests for gmail/sender.py, gmail/templates.py and gmail/campaign.py.

Nothing here ever touches a real SMTP server or the network: ``smtplib.SMTP``
is monkeypatched to a small in-memory fake that records what it was asked to
send, so the anti-abuse rules can be asserted with certainty and no
side effects escape the test process.
"""

from __future__ import annotations

import itertools
from datetime import timedelta

import pytest
import smtplib
from sqlalchemy import select

import gmail.sender as sender_module
from core.constants import EmailStatus, EmailVerdict, SkipReason
from core.errors import GmailError
from core.schemas import CompanyFilter
from database.models import EmailMessage, now
from database.repository import CompanyRepository
from database.session import session_scope
from gmail import campaign, templates
from gmail.sender import SmtpSender

_counter = itertools.count()


# --------------------------------------------------------------------- fakes


class FakeSMTP:
    """Drop-in for ``smtplib.SMTP``: records calls instead of opening a socket."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):  # noqa: ANN001 - test double
        self.host = host
        self.port = port
        self.sent_messages: list = []
        self.logged_in = False
        self.quit_called = False
        FakeSMTP.instances.append(self)

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, address, password):  # noqa: ANN001
        if password != "good-app-password":
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials")
        self.logged_in = True

    def send_message(self, message):  # noqa: ANN001
        self.sent_messages.append(message)

    def quit(self):
        self.quit_called = True


class RefusingSMTP(FakeSMTP):
    """A fake that must never be reached -- used to prove dry-run never connects."""

    def __init__(self, *a, **k):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("SMTP must not be used in dry-run mode")


@pytest.fixture(autouse=True)
def _reset_fake_smtp():
    FakeSMTP.instances.clear()
    yield
    FakeSMTP.instances.clear()


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def gmail_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "good-app-password")


@pytest.fixture
def mail_config(tmp_config, tmp_path, gmail_env):
    """``tmp_config`` with a mailer section pointed at an isolated templates dir."""
    templates_dir = tmp_path / "templates" / "mail"
    templates_dir.mkdir(parents=True)
    return tmp_config.model_copy(
        update={
            "mailer": tmp_config.mailer.model_copy(
                update={
                    "templates_dir": str(templates_dir),
                    "dry_run": True,
                    "daily_limit": 100,
                    "delay_seconds": 0.0,
                    "resend_after_days": 30,
                    "require_verified_email": True,
                }
            )
        }
    )


def _with_mailer(config, **overrides):
    return config.model_copy(update={"mailer": config.mailer.model_copy(update=overrides)})


def _make_company(db_session, **overrides):
    repo = CompanyRepository(db_session)
    n = next(_counter)
    fields = dict(
        company_name=f"測試公司{n}",
        name_key=f"測試公司{n}",
        dedupe_key=f"n:{n}",
        email=f"a{n}@example.com",
        email_verdict=EmailVerdict.VALID.value,
    )
    fields.update(overrides)
    company = repo.create(**fields)
    db_session.commit()
    return company


def _reload_company(company_id: int):
    """Re-fetch a company through its own session, past ``send_campaign``'s
    own (committed and closed) session -- avoids relying on a stale, already
    identity-mapped instance from the test's own ``db_session``."""
    with session_scope() as session:
        company = CompanyRepository(session).get(company_id)
        assert company is not None
        _ = company.activities  # force the selectin-loaded relationship in
        return company


def _save_template(config, name: str = "test-template") -> None:
    templates.save_template(
        name,
        subject="Hi {company_name}",
        body="{contact_person}，關於 {company_name} 的合作提案。",
        config=config,
    )


# ------------------------------------------------------------------- templates


def test_render_substitutes_known_placeholders_with_defaults():
    text = templates.render(
        "{contact_person}您好，{company_name}從事{industry}。",
        {"company_name": "測試公司"},
    )
    assert "您好" in text  # default contact_person
    assert "測試公司" in text
    assert "貴產業" in text  # default industry


def test_render_unknown_placeholder_raises_gmail_error():
    with pytest.raises(GmailError):
        templates.render("{not_a_real_field}", {"company_name": "X"})


def test_available_placeholders_documents_every_supported_token():
    placeholders = templates.available_placeholders()
    for name in ("company_name", "contact_person", "industry", "email", "phone", "website", "city"):
        assert name in placeholders
        assert placeholders[name]  # has a Chinese description


def test_save_and_load_template_roundtrip(mail_config):
    _save_template(mail_config)
    loaded = templates.load_template("test-template", mail_config)
    assert loaded.subject == "Hi {company_name}"
    assert "合作提案" in loaded.body
    assert "test-template" in templates.list_templates(mail_config)


def test_load_missing_template_raises_gmail_error(mail_config):
    with pytest.raises(GmailError):
        templates.load_template("does-not-exist", mail_config)


# ------------------------------------------------------------------- build_plan


def test_no_email_is_skipped(db_session, mail_config):
    _save_template(mail_config)
    _make_company(db_session, email=None)
    plan = campaign.build_plan(CompanyFilter(), "test-template", "camp", mail_config)
    assert plan.sendable == 0
    assert plan.recipients[0].skip_reason == SkipReason.NO_EMAIL


def test_invalid_email_is_skipped(db_session, mail_config):
    _save_template(mail_config)
    _make_company(db_session, email="not-an-email")
    plan = campaign.build_plan(CompanyFilter(), "test-template", "camp", mail_config)
    assert plan.sendable == 0
    assert plan.recipients[0].skip_reason == SkipReason.INVALID_EMAIL


def test_unverified_email_is_skipped_when_required(db_session, mail_config):
    _save_template(mail_config)
    _make_company(db_session, email_verdict=EmailVerdict.UNKNOWN.value)
    plan = campaign.build_plan(CompanyFilter(), "test-template", "camp", mail_config)
    assert plan.sendable == 0
    assert plan.recipients[0].skip_reason == SkipReason.UNVERIFIED


def test_unverified_email_is_allowed_when_not_required(db_session, mail_config):
    _save_template(mail_config)
    _make_company(db_session, email_verdict=EmailVerdict.UNKNOWN.value)
    lenient = _with_mailer(mail_config, require_verified_email=False)
    plan = campaign.build_plan(CompanyFilter(), "test-template", "camp", lenient)
    assert plan.sendable == 1


def test_recently_contacted_is_skipped(db_session, mail_config):
    _save_template(mail_config)
    _make_company(db_session, last_emailed_at=now() - timedelta(days=1))
    plan = campaign.build_plan(CompanyFilter(), "test-template", "camp", mail_config)
    assert plan.sendable == 0
    assert plan.recipients[0].skip_reason == SkipReason.RECENTLY_CONTACTED


def test_stale_contact_outside_resend_window_is_sendable(db_session, mail_config):
    _save_template(mail_config)
    _make_company(db_session, last_emailed_at=now() - timedelta(days=60))
    plan = campaign.build_plan(CompanyFilter(), "test-template", "camp", mail_config)
    assert plan.sendable == 1


def test_daily_cap_stops_further_recipients(db_session, mail_config):
    _save_template(mail_config)
    for _ in range(3):
        _make_company(db_session)
    capped = _with_mailer(mail_config, daily_limit=2)
    plan = campaign.build_plan(CompanyFilter(), "test-template", "camp", capped)
    assert plan.sendable == 2
    assert plan.skip_counts.get(SkipReason.DAILY_CAP.value) == 1
    assert plan.daily_remaining == 0


def test_daily_cap_accounts_for_messages_already_sent_today(db_session, mail_config):
    _save_template(mail_config)
    sent_already = _make_company(db_session)
    _make_company(db_session)  # a second, distinct company

    db_session.add(
        EmailMessage(
            company_id=sent_already.id,
            to_address=sent_already.email,
            subject="previous",
            status=EmailStatus.SENT.value,
        )
    )
    db_session.commit()

    capped = _with_mailer(mail_config, daily_limit=1)
    plan = campaign.build_plan(CompanyFilter(), "test-template", "camp", capped)
    assert plan.sendable == 0
    assert plan.recipients[0].skip_reason == SkipReason.DAILY_CAP


def test_do_not_contact_is_never_sent_even_with_permissive_settings(db_session, mail_config):
    _save_template(mail_config)
    _make_company(
        db_session,
        do_not_contact=True,
        email_verdict=EmailVerdict.UNKNOWN.value,
        last_emailed_at=now(),
    )
    # Every other guard rail loosened as far as possible.
    permissive = _with_mailer(
        mail_config,
        require_verified_email=False,
        resend_after_days=0,
        daily_limit=999,
        dry_run=False,
    )
    plan = campaign.build_plan(CompanyFilter(), "test-template", "camp", permissive)
    assert plan.sendable == 0
    assert plan.recipients[0].skip_reason == SkipReason.DO_NOT_CONTACT


# ------------------------------------------------------------------- send_campaign


def test_dry_run_never_calls_smtp(db_session, mail_config, monkeypatch):
    monkeypatch.setattr(sender_module.smtplib, "SMTP", RefusingSMTP)
    _save_template(mail_config)
    _make_company(db_session)

    plan = campaign.build_plan(CompanyFilter(), "test-template", "camp", mail_config)
    assert plan.sendable == 1

    result = campaign.send_campaign(plan, mail_config)
    assert result.dry_run is True
    assert result.sent == 1
    assert result.failed == 0
    assert not FakeSMTP.instances  # RefusingSMTP would have raised if constructed


def test_unsubscribe_note_always_present_in_stored_body(db_session, mail_config):
    _save_template(mail_config)
    _make_company(db_session)
    plan = campaign.build_plan(CompanyFilter(), "test-template", "camp", mail_config)
    campaign.send_campaign(plan, mail_config)

    stored = db_session.execute(select(EmailMessage)).scalars().first()
    assert stored is not None
    assert mail_config.mailer.unsubscribe_note in stored.body


def test_unsubscribe_note_not_duplicated_when_already_present(db_session, mail_config):
    note = mail_config.mailer.unsubscribe_note
    templates.save_template(
        "with-note",
        subject="Hi",
        body=f"內容。\n\n{note}",
        config=mail_config,
    )
    _make_company(db_session)
    plan = campaign.build_plan(CompanyFilter(), "with-note", "camp", mail_config)
    campaign.send_campaign(plan, mail_config)

    stored = db_session.execute(select(EmailMessage)).scalars().first()
    assert stored.body.count(note) == 1


def test_real_send_updates_company_and_activity(db_session, mail_config, monkeypatch):
    monkeypatch.setattr(sender_module.smtplib, "SMTP", FakeSMTP)
    _save_template(mail_config)
    company = _make_company(db_session)
    real = _with_mailer(mail_config, dry_run=False)

    plan = campaign.build_plan(CompanyFilter(), "test-template", "camp", real)
    result = campaign.send_campaign(plan, real)

    assert result.sent == 1
    assert result.failed == 0
    assert len(FakeSMTP.instances) == 1
    assert len(FakeSMTP.instances[0].sent_messages) == 1

    reloaded = _reload_company(company.id)
    assert reloaded.last_emailed_at is not None
    assert reloaded.email_count == 1
    assert any(a.type == "Email" for a in reloaded.activities)


def test_real_send_failure_is_recorded_as_failed(db_session, mail_config, monkeypatch):
    monkeypatch.setattr(sender_module.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "wrong-password")
    _save_template(mail_config)
    _make_company(db_session)
    real = _with_mailer(mail_config, dry_run=False)

    plan = campaign.build_plan(CompanyFilter(), "test-template", "camp", real)
    with pytest.raises(GmailError):
        campaign.send_campaign(plan, real)


def test_daily_sent_count_counts_only_sent_status_today(db_session, mail_config):
    company = _make_company(db_session)

    db_session.add_all(
        [
            EmailMessage(
                company_id=company.id, to_address=company.email, subject="a",
                status=EmailStatus.SENT.value,
            ),
            EmailMessage(
                company_id=company.id, to_address=company.email, subject="b",
                status=EmailStatus.DRY_RUN.value,
            ),
            EmailMessage(
                company_id=company.id, to_address=company.email, subject="c",
                status=EmailStatus.FAILED.value,
            ),
        ]
    )
    db_session.commit()

    assert campaign.daily_sent_count(mail_config) == 1


# ------------------------------------------------------------------- suppression


def test_mark_do_not_contact_suppresses_future_plans(db_session, mail_config):
    _save_template(mail_config)
    company = _make_company(db_session)

    assert campaign.mark_do_not_contact(company.id) is True
    assert _reload_company(company.id).do_not_contact is True

    plan = campaign.build_plan(CompanyFilter(), "test-template", "camp", mail_config)
    assert plan.recipients[0].skip_reason == SkipReason.DO_NOT_CONTACT


def test_unsubscribe_by_email_suppresses_matching_companies(db_session, mail_config):
    company = _make_company(db_session, email="unsub@example.com")
    count = campaign.unsubscribe_by_email("UnSub@Example.com")
    assert count == 1
    assert _reload_company(company.id).do_not_contact is True


def test_unsubscribe_by_email_empty_address_is_a_noop():
    assert campaign.unsubscribe_by_email("") == 0
    assert campaign.unsubscribe_by_email("   ") == 0


# ------------------------------------------------------------------- SmtpSender


def test_sender_requires_credentials(mail_config, monkeypatch):
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    sender = SmtpSender(mail_config)
    with pytest.raises(GmailError):
        sender.connect()


def test_sender_wraps_authentication_failure(mail_config, monkeypatch):
    monkeypatch.setattr(sender_module.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "wrong-password")
    sender = SmtpSender(mail_config)
    with pytest.raises(GmailError, match="登入失敗|apppasswords"):
        sender.connect()


def test_sender_send_sets_utf8_and_list_unsubscribe(mail_config, monkeypatch):
    monkeypatch.setattr(sender_module.smtplib, "SMTP", FakeSMTP)

    class Msg:
        to_address = "target@example.com"
        subject = "測試主旨"
        body = "這是中文內容，包含多國語言 café."

    with SmtpSender(mail_config) as sender:
        sender.send(Msg())

    sent = FakeSMTP.instances[0].sent_messages[0]
    assert sent["To"] == "target@example.com"
    assert "List-Unsubscribe" in sent
    assert sent["List-Unsubscribe"].startswith("<mailto:")
    payload = sent.get_body(preferencelist=("plain",))
    assert payload.get_content().strip() == Msg.body
    assert payload.get_content_charset() == "utf-8"
    assert FakeSMTP.instances[0].quit_called


# ------------------------------------------------------------------- 附件


class _Msg:
    to_address = "target@example.com"
    subject = "帶附件的信"
    body = "內文"


def test_send_attaches_files_as_real_attachments(mail_config, monkeypatch):
    """收件者要看得到迴紋針。

    內文圖片是 add_related 掛在 HTML 部分裡、用 cid: 引用，不會出現在附件
    列表；一般附件必須掛在最外層才看得到，這兩者不能搞混。
    """
    monkeypatch.setattr(sender_module.smtplib, "SMTP", FakeSMTP)

    with SmtpSender(mail_config) as sender:
        sender.send(
            _Msg(),
            [
                ("型錄.pdf", b"%PDF-1.4 fake", "application/pdf"),
                ("報價單.xlsx", b"fake xlsx", "application/vnd.ms-excel"),
            ],
        )

    sent = FakeSMTP.instances[0].sent_messages[0]
    attached = {
        part.get_filename(): part.get_payload(decode=True)
        for part in sent.iter_attachments()
    }
    assert set(attached) == {"型錄.pdf", "報價單.xlsx"}
    assert attached["型錄.pdf"] == b"%PDF-1.4 fake"
    # 內文仍然要在，附件不能把本文擠掉。
    assert sent.get_body(preferencelist=("plain",)).get_content().strip() == "內文"


def test_send_without_attachments_produces_no_attachment_parts(mail_config, monkeypatch):
    monkeypatch.setattr(sender_module.smtplib, "SMTP", FakeSMTP)

    with SmtpSender(mail_config) as sender:
        sender.send(_Msg())

    sent = FakeSMTP.instances[0].sent_messages[0]
    assert list(sent.iter_attachments()) == []


def test_unknown_mime_type_falls_back_to_octet_stream(mail_config, monkeypatch):
    """副檔名認不得也要寄得出去，不能整批中斷。"""
    monkeypatch.setattr(sender_module.smtplib, "SMTP", FakeSMTP)

    with SmtpSender(mail_config) as sender:
        sender.send(_Msg(), [("怪檔.qwerty", b"data", "application/octet-stream")])

    part = next(iter(FakeSMTP.instances[0].sent_messages[0].iter_attachments()))
    assert part.get_filename() == "怪檔.qwerty"
    assert part.get_content_type() == "application/octet-stream"
