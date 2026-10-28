"""Tests for gui/controllers_mail.py and core/credentials.py.

No Tk widget is ever created here -- only the controller layer (which is what
gui/pages/mail.py and gui/pages/settings.py actually call) and the credential
storage logic. The system credential vault is never touched: ``keyring`` is
replaced with a small in-memory fake for every test in this module.
"""

from __future__ import annotations

import os

import pytest
import smtplib

import core.credentials as credentials
from core.errors import CRMError
from core.schemas import CompanyFilter
from database.repository import CompanyRepository
from controllers.mail import MailController


# ------------------------------------------------------------------ fixtures


class FakeKeyring:
    """In-memory stand-in for the ``keyring`` module.

    ``available=False`` makes :func:`core.credentials.keyring_available`
    return ``False`` (its ``get_keyring()`` call raises), simulating a machine
    with no usable credential vault backend.
    """

    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.store: dict[tuple[str, str], str] = {}

    def get_keyring(self):
        if not self.available:
            raise RuntimeError("no keyring backend configured")
        return self

    def get_password(self, service: str, name: str) -> str | None:
        return self.store.get((service, name))

    def set_password(self, service: str, name: str, value: str) -> None:
        self.store[(service, name)] = value

    def delete_password(self, service: str, name: str) -> None:
        if (service, name) not in self.store:
            raise KeyError(f"no such password: {service}/{name}")
        del self.store[(service, name)]


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    """A working (available) fake vault -- never the real OS credential store.

    ``tests/__init__.py`` sets ``CRM_DISABLE_KEYRING=1`` for the whole suite so
    a developer's real, already-configured Gmail credentials can never leak
    into a test; this fixture explicitly lifts that block (after swapping in
    the fake backend) for the tests that need to exercise a *working* vault.
    """
    fake = FakeKeyring(available=True)
    monkeypatch.setattr(credentials, "keyring", fake, raising=False)
    monkeypatch.setattr(credentials, "_KEYRING_IMPORTED", True)
    monkeypatch.delenv(credentials.DISABLE_ENV_VAR, raising=False)
    return fake


@pytest.fixture
def unavailable_keyring(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    """A vault that exists in code but cannot actually be used."""
    fake = FakeKeyring(available=False)
    monkeypatch.setattr(credentials, "keyring", fake, raising=False)
    monkeypatch.setattr(credentials, "_KEYRING_IMPORTED", True)
    monkeypatch.setenv(credentials.DISABLE_ENV_VAR, "1")
    return fake


@pytest.fixture(autouse=True)
def _clear_gmail_env(monkeypatch: pytest.MonkeyPatch):
    """Isolate every test here from the developer's real .env / environment."""
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)


@pytest.fixture
def mail_config(tmp_config, tmp_path):
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
                    "enabled": True,
                }
            )
        }
    )


def _make_company(db_session, **overrides):
    from core.constants import EmailVerdict

    repo = CompanyRepository(db_session)
    fields = dict(
        company_name="測試公司",
        name_key="測試公司",
        dedupe_key="n:1",
        email="a@example.com",
        email_verdict=EmailVerdict.VALID.value,
    )
    fields.update(overrides)
    company = repo.create(**fields)
    db_session.commit()
    return company


# -------------------------------------------------------------- credentials


def test_keyring_roundtrip_set_get_describe_delete(fake_keyring):
    assert credentials.describe("gmail_address").source is credentials.SecretSource.UNSET

    result = credentials.set_secret("gmail_address", "me@example.com")
    assert result is credentials.SecretSource.KEYRING

    assert credentials.get_secret("gmail_address") == "me@example.com"

    status = credentials.describe("gmail_address")
    assert status.source is credentials.SecretSource.KEYRING
    assert status.is_set is True
    assert status.is_secure is True

    assert credentials.delete_secret("gmail_address") is True
    assert credentials.get_secret("gmail_address") == ""
    assert credentials.describe("gmail_address").source is credentials.SecretSource.UNSET


def test_set_secret_returns_unset_when_vault_unavailable_and_never_touches_env(
    unavailable_keyring,
):
    result = credentials.set_secret("gmail_address", "someone@example.com")

    assert result is credentials.SecretSource.UNSET
    # Must not have silently fallen back to writing the plaintext env var.
    assert credentials.get_secret("gmail_address") == ""
    assert "GMAIL_ADDRESS" not in os.environ


def test_describe_reports_env_source_when_only_env_var_is_set(
    unavailable_keyring, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("GMAIL_ADDRESS", "legacy@example.com")

    status = credentials.describe("gmail_address")

    assert status.source is credentials.SecretSource.ENV
    assert status.is_set is True
    assert status.is_secure is False


def test_set_secret_with_empty_value_deletes_and_reports_unset(fake_keyring):
    credentials.set_secret("gmail_address", "someone@example.com")
    result = credentials.set_secret("gmail_address", "   ")
    assert result is credentials.SecretSource.UNSET
    assert credentials.get_secret("gmail_address") == ""


def test_keyring_available_false_when_backend_raises(unavailable_keyring):
    assert credentials.keyring_available() is False


def test_keyring_available_true_for_working_backend(fake_keyring):
    assert credentials.keyring_available() is True


# ------------------------------------------------------------- MailController


def test_mailer_status_reports_account_not_ready_when_unset(mail_config, unavailable_keyring):
    controller = MailController(mail_config)
    status = controller.mailer_status()
    assert status["account_ready"] == "否"
    assert status["address"] == ""


def test_mailer_status_reports_account_ready_once_credentials_are_set(mail_config, fake_keyring):
    credentials.set_secret("gmail_address", "me@example.com")
    credentials.set_secret("gmail_app_password", "app-password")
    controller = MailController(mail_config)
    status = controller.mailer_status()
    assert status["account_ready"] == "是"
    assert status["address"] == "me@example.com"
    assert status["enabled"] == "是"
    assert status["dry_run"] == "是"


def test_build_plan_without_template_name_raises_chinese_crm_error(mail_config):
    controller = MailController(mail_config)
    with pytest.raises(CRMError, match="請先選擇一個樣板"):
        controller.build_plan(CompanyFilter(), "", "campaign")


def test_build_plan_wraps_missing_template_error_in_chinese(mail_config, db_session):
    controller = MailController(mail_config)
    with pytest.raises(CRMError, match="產生寄送名單失敗"):
        controller.build_plan(CompanyFilter(), "does-not-exist", "campaign")


def test_save_requires_a_name(mail_config):
    controller = MailController(mail_config)
    with pytest.raises(CRMError, match="請輸入樣板名稱"):
        controller.save("", "subject", "body")


def test_load_missing_template_raises_chinese_crm_error(mail_config):
    controller = MailController(mail_config)
    with pytest.raises(CRMError, match="無法讀取樣板"):
        controller.load("does-not-exist")


def test_send_wraps_smtp_authentication_failure_in_chinese(
    mail_config, db_session, monkeypatch, fake_keyring
):
    import gmail.sender as sender_module
    from gmail import templates

    templates.save_template(
        "camp-template", subject="Hi {company_name}", body="Body", config=mail_config
    )
    _make_company(db_session)
    credentials.set_secret("gmail_address", "me@example.com")
    credentials.set_secret("gmail_app_password", "wrong-password")

    class FailingSMTP:
        def __init__(self, host, port, timeout=None):
            pass

        def ehlo(self):
            pass

        def starttls(self, *, context=None):
            pass

        def login(self, address, password):
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials")

    monkeypatch.setattr(sender_module.smtplib, "SMTP", FailingSMTP)

    real_config = mail_config.model_copy(
        update={"mailer": mail_config.mailer.model_copy(update={"dry_run": False})}
    )
    controller = MailController(real_config)
    plan = controller.build_plan(CompanyFilter(), "camp-template", "camp")
    assert plan.sendable == 1

    with pytest.raises(CRMError, match="寄送過程發生錯誤"):
        controller.send(plan, dry_run=False)


def test_send_dry_run_never_touches_smtp(mail_config, db_session, monkeypatch, fake_keyring):
    import gmail.sender as sender_module
    from gmail import templates

    templates.save_template(
        "camp-template", subject="Hi {company_name}", body="Body", config=mail_config
    )
    _make_company(db_session)

    class RefusingSMTP:
        def __init__(self, *a, **k):
            raise AssertionError("SMTP must not be used in dry-run mode")

    monkeypatch.setattr(sender_module.smtplib, "SMTP", RefusingSMTP)

    controller = MailController(mail_config)  # dry_run=True
    plan = controller.build_plan(CompanyFilter(), "camp-template", "camp")
    result = controller.send(plan, dry_run=True)

    assert result.dry_run is True
    assert result.sent == 1


def test_the_dry_run_button_never_opens_smtp_even_when_the_config_says_send(
    mail_config, db_session, monkeypatch, fake_keyring
):
    import gmail.sender as sender_module
    from gmail import templates

    templates.save_template(
        "camp-template", subject="Hi {company_name}", body="Body", config=mail_config
    )
    _make_company(db_session)

    class RefusingSMTP:
        def __init__(self, *a, **k):
            raise AssertionError("SMTP must not be used when dry_run=True")

    monkeypatch.setattr(sender_module.smtplib, "SMTP", RefusingSMTP)

    real_config = mail_config.model_copy(
        update={"mailer": mail_config.mailer.model_copy(update={"dry_run": False})}
    )
    controller = MailController(real_config)
    plan = controller.build_plan(CompanyFilter(), "camp-template", "camp")

    result = controller.send(plan, dry_run=True)

    assert result.dry_run is True
    assert result.sent == 1
    # The controller's own config must be left untouched.
    assert controller.config.mailer.dry_run is False


def test_the_send_button_really_sends_even_when_the_config_says_dry_run(
    mail_config, db_session, monkeypatch, fake_keyring
):
    """「開始寄送」就是真的寄。以前它跟著 mailer.dry_run 走，按了卻只是演練。"""
    import gmail.sender as sender_module
    from gmail import templates

    templates.save_template(
        "camp-template", subject="Hi {company_name}", body="Body", config=mail_config
    )
    _make_company(db_session)
    credentials.set_secret("gmail_address", "me@example.com")
    credentials.set_secret("gmail_app_password", "app-password")
    sent: list = []

    class RecordingSMTP:
        def __init__(self, *a, **k):
            pass

        def ehlo(self):
            pass

        def starttls(self, *, context=None):
            pass

        def login(self, *a):
            pass

        def send_message(self, message):
            sent.append(message)

        def quit(self):
            pass

    monkeypatch.setattr(sender_module.smtplib, "SMTP", RecordingSMTP)
    dry_config = mail_config.model_copy(
        update={"mailer": mail_config.mailer.model_copy(update={"dry_run": True})}
    )
    controller = MailController(dry_config)
    plan = controller.build_plan(CompanyFilter(), "camp-template", "camp")

    result = controller.send(plan, dry_run=False)

    assert result.dry_run is False
    assert len(sent) == 1


def test_preview_first_includes_unsubscribe_note(mail_config, db_session):
    from gmail import templates

    templates.save_template(
        "camp-template", subject="Hi {company_name}", body="內容", config=mail_config
    )
    _make_company(db_session)

    controller = MailController(mail_config)
    plan = controller.build_plan(CompanyFilter(), "camp-template", "camp")

    preview = controller.preview_first(plan)
    assert preview is not None
    subject, body = preview
    assert "Hi" in subject
    assert mail_config.mailer.unsubscribe_note in body


def test_preview_first_returns_none_when_nothing_is_sendable(mail_config, db_session):
    from gmail import templates

    templates.save_template(
        "camp-template", subject="Hi {company_name}", body="內容", config=mail_config
    )
    _make_company(db_session, do_not_contact=True)

    controller = MailController(mail_config)
    plan = controller.build_plan(CompanyFilter(), "camp-template", "camp")
    assert controller.preview_first(plan) is None


# ---------------------------------------------------------- daily send limit


@pytest.fixture
def user_settings_file(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """把 user_settings.yaml 導到 tmp_path，別碰到真正的專案資料夾。

    與 ``tests/test_config.py`` 裡的同名 fixture一樣：``save_user_setting``
    是靠 ``get_config()`` 重新載入來驗證整份設定，所以會實際讀到專案根目錄的
    ``config.yaml``（唯讀），只有 ``user_settings.yaml`` 的寫入位置被導開。
    """
    import core.config as config_module

    target = tmp_path / "user_settings.yaml"
    monkeypatch.setattr(config_module, "USER_SETTINGS_PATH", target)
    yield target
    config_module.reset_config()


def test_set_daily_limit_persists_and_refreshes_controller_config(
    mail_config, user_settings_file
):
    from core.config import read_user_settings

    controller = MailController(mail_config)
    controller.set_daily_limit(500)

    assert read_user_settings()["mailer"]["daily_limit"] == 500
    # The controller's own config has to reach the new value too, or the
    # Mail page's status strip would keep showing the value from before the
    # save until the whole app restarted.
    assert controller.daily_limit() == 500


def test_set_daily_limit_rejects_out_of_range_value_and_rolls_back(
    mail_config, user_settings_file
):
    from core.config import read_user_settings

    controller = MailController(mail_config)
    controller.set_daily_limit(500)

    with pytest.raises(CRMError, match="每日寄送上限"):
        controller.set_daily_limit(5000)

    # A value the config validator rejects must not be left half-written.
    assert read_user_settings()["mailer"]["daily_limit"] == 500



class _RecordingSMTP:
    """記下寄出去的信，不連任何伺服器。"""

    sent: list = []

    def __init__(self, *a, **k):
        pass

    def ehlo(self):
        pass

    def starttls(self, *, context=None):
        pass

    def login(self, *a):
        pass

    def send_message(self, message):
        _RecordingSMTP.sent.append(message)

    def quit(self):
        pass


@pytest.fixture
def recording_smtp(monkeypatch, fake_keyring):
    import gmail.sender as sender_module

    _RecordingSMTP.sent = []
    monkeypatch.setattr(sender_module.smtplib, "SMTP", _RecordingSMTP)
    credentials.set_secret("gmail_address", "me@example.com")
    credentials.set_secret("gmail_app_password", "app-password")
    return _RecordingSMTP


def test_a_test_email_goes_only_to_yourself_and_leaves_no_trace(
    mail_config, db_session, recording_smtp
):
    from gmail import templates

    templates.save_template(
        "camp-template", subject="您好 {company_name}", body="內容", config=mail_config
    )
    company = _make_company(db_session)
    controller = MailController(mail_config)
    plan = controller.build_plan(CompanyFilter(), "camp-template", "camp")

    address = controller.send_test("camp-template", plan)

    assert address == "me@example.com"
    [message] = recording_smtp.sent
    assert message["To"] == "me@example.com"
    assert message["Subject"].startswith("[測試] 您好")
    assert company.company_name in message["Subject"]
    # 不算寄過那家公司，但算進今日已寄——Google 的每日額度也算了這一封。
    db_session.refresh(company)
    assert company.last_emailed_at is None
    assert controller.daily_sent() == 1


def test_a_test_email_is_never_mistaken_for_outreach(mail_config, db_session, recording_smtp):
    """寄給自己的測試信會出現在自己的收件匣裡，不能被回信／退信偵測當成寄過的對象。"""
    from controllers.mail import BounceController, ReplyController
    from gmail import templates

    templates.save_template("camp-template", subject="s", body="b", config=mail_config)
    MailController(mail_config).send_test("camp-template")

    assert BounceController(mail_config)._sent_addresses() == set()
    assert ReplyController(mail_config)._sent_index() == (set(), {})


def test_a_test_email_works_before_any_list_is_built(mail_config, db_session, recording_smtp):
    from gmail import templates

    templates.save_template(
        "camp-template", subject="您好 {company_name}", body="內容", config=mail_config
    )

    MailController(mail_config).send_test("camp-template")

    assert "範例股份有限公司" in recording_smtp.sent[0]["Subject"]


def test_campaign_names_list_the_most_recent_first(mail_config, db_session):
    from database.models import EmailMessage

    company = _make_company(db_session)
    for name in ("春季開發信", "展覽後追蹤", "春季開發信"):
        db_session.add(
            EmailMessage(company_id=company.id, campaign=name, to_address="a@b.tw",
                         subject="s", body="b", status="Sent")
        )
    db_session.commit()

    assert MailController(mail_config).campaign_names() == ["春季開發信", "展覽後追蹤"]


def test_set_delay_seconds_persists_and_refreshes_controller_config(mail_config, user_settings_file):
    from core.config import read_user_settings

    controller = MailController(mail_config)
    controller.set_delay_seconds(15)

    assert read_user_settings()["mailer"]["delay_seconds"] == 15.0
    assert controller.config.mailer.delay_seconds == 15.0


def test_the_sample_template_does_not_carry_its_own_unsubscribe_line():
    """程式寄信時會自動補退訂說明。樣板自己再寫一句（字不一樣）就會變成兩句。"""
    from pathlib import Path

    sample = Path(__file__).resolve().parent.parent / "templates" / "mail" / "範例-初次開發信.txt"
    assert "取消訂閱" not in sample.read_text(encoding="utf-8")
