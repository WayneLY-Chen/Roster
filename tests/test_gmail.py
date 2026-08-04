"""Tests for gmail/client.py and gmail/harvester.py."""

from __future__ import annotations

import imaplib
from email.header import Header
from email.message import EmailMessage
from email.message import Message as StdMessage

import pytest

from core.errors import GmailError
from database.repository import ActivityRepository, CompanyRepository, ContactRepository
from gmail.client import GmailClient, MailMessage, _decode, _extract_body, gmail_session
from gmail.harvester import GmailHarvestSummary, harvest_inbox, message_to_record


# ------------------------------------------------------------------- _decode


def test_decode_plain_ascii_header_is_unchanged():
    assert _decode("Hello World") == "Hello World"


def test_decode_rfc2047_encoded_header():
    header_value = str(Header("測試主旨", "utf-8"))
    assert _decode(header_value) == "測試主旨"


def test_decode_none_or_empty_returns_empty_string():
    assert _decode(None) == ""
    assert _decode("") == ""


# --------------------------------------------------------------- _extract_body


def test_extract_body_prefers_plain_text_over_html():
    message = EmailMessage()
    message.set_content("plain body")
    message.add_alternative("<html><body>html body</body></html>", subtype="html")
    assert "plain body" in _extract_body(message)
    assert "html body" not in _extract_body(message)


def test_extract_body_falls_back_to_html_when_no_plain_part():
    message = StdMessage()
    message["Content-Type"] = "text/html; charset=utf-8"
    message.set_payload("<html><body><p>only html here</p></body></html>")
    text = _extract_body(message)
    assert "only html here" in text


def test_extract_body_skips_attachments():
    message = EmailMessage()
    message.set_content("body text")
    message.add_attachment(b"binary data", maintype="application", subtype="octet-stream",
                            filename="file.bin")
    text = _extract_body(message)
    assert "body text" in text


def test_extract_body_empty_message_returns_empty_string():
    message = EmailMessage()
    assert _extract_body(message) == ""


# ---------------------------------------------------------------- message_to_record


def _msg(**overrides) -> MailMessage:
    defaults = dict(
        uid="1",
        subject="Hello",
        sender_name="",
        sender_email="",
        date=None,
        body="",
        headers={},
    )
    defaults.update(overrides)
    return MailMessage(**defaults)


def test_message_to_record_skips_free_mail_domain():
    message = _msg(sender_email="user@gmail.com")
    record = message_to_record(message, ignore_domains={"gmail.com"})
    assert record is None


def test_message_to_record_skips_message_without_sender():
    message = _msg(sender_email="")
    assert message_to_record(message, ignore_domains=set()) is None


def test_message_to_record_company_from_organization_header():
    message = _msg(sender_email="a@example.com", headers={"Organization": "Acme Corp"})
    record = message_to_record(message, ignore_domains=set())
    assert record.company_name == "Acme Corp"
    assert record.email == "a@example.com"
    assert record.source == "gmail"
    assert record.source_url == "imap://1"


def test_message_to_record_company_and_contact_details_from_signature():
    body = (
        "您好，謝謝來信。\n\n"
        "Best regards,\n"
        "王大明\n"
        "宏達精密機械股份有限公司\n"
        "Manager\n"
        "TEL: 02-2723-1234\n"
        "https://hongda.com.tw\n"
    )
    message = _msg(
        sender_name="David Wang",
        sender_email="sales@hongda-precision.com.tw",
        body=body,
    )
    record = message_to_record(message, ignore_domains=set())

    assert record.company_name == "宏達精密機械股份有限公司"
    assert record.contact_person == "David Wang"
    assert record.phone == "02-27231234"
    assert record.website == "https://hongda.com.tw"
    assert record.extra["title"] == "Manager"


def test_message_to_record_falls_back_to_sender_domain():
    message = _msg(
        sender_email="sales@nissin-electronics.com.tw",
        body="no signature markers, just plain text with no company line",
    )
    record = message_to_record(message, ignore_domains=set())
    assert record.company_name == "Nissin Electronics"
    assert record.website == "https://nissin-electronics.com.tw"


# ------------------------------------------------------------------ harvest_inbox


class FakeGmailClient:
    """Enough of :class:`GmailClient` for :func:`harvest_inbox` to run offline."""

    def __init__(self, messages: list[MailMessage]) -> None:
        self._messages = messages
        self.connected = False
        self.closed = False

    def connect(self) -> None:  # pragma: no cover - not expected to be called
        self.connected = True

    def close(self) -> None:  # pragma: no cover - not expected to be called
        self.closed = True

    def iter_messages(self, query=None, limit=None):
        yield from self._messages


def test_harvest_inbox_owns_and_closes_a_client_when_none_is_injected(
    db_session, tmp_config, monkeypatch
):
    """When ``client=`` is omitted, ``harvest_inbox`` builds its own
    :class:`GmailClient` and is responsible for connecting and closing it."""
    import gmail.harvester as harvester_module

    fake = FakeGmailClient([])
    monkeypatch.setattr(harvester_module, "GmailClient", lambda config=None: fake)

    summary = harvest_inbox(config=tmp_config)

    assert summary.messages_scanned == 0
    assert fake.connected is True
    assert fake.closed is True


def test_harvest_inbox_creates_company_and_primary_contact(db_session, tmp_config):
    body = (
        "Best regards,\n"
        "王大明\n"
        "宏達精密機械股份有限公司\n"
        "Manager\n"
        "TEL: 02-2723-1234\n"
    )
    message = MailMessage(
        uid="1",
        subject="Inquiry",
        sender_name="David Wang",
        sender_email="sales@hongda-precision.com.tw",
        body=body,
    )
    client = FakeGmailClient([message])

    summary = harvest_inbox(config=tmp_config, client=client)

    assert isinstance(summary, GmailHarvestSummary)
    assert summary.messages_scanned == 1
    assert summary.messages_skipped == 0
    assert summary.records_new == 1
    assert summary.contacts_created == 1
    assert client.connected is False  # injected client is never (re)connected
    assert client.closed is False  # ...nor closed; the caller owns its lifecycle

    repo = CompanyRepository(db_session)
    companies = repo.all()
    assert len(companies) == 1
    company = companies[0]
    assert company.company_name == "宏達精密機械股份有限公司"

    contacts = ContactRepository(db_session).for_company(company.id)
    assert len(contacts) == 1
    assert contacts[0].name == "David Wang"
    assert contacts[0].is_primary is True

    activities = ActivityRepository(db_session).for_company(company.id)
    assert any(a.subject == "Imported from Gmail" for a in activities)


def test_harvest_inbox_skips_ignored_domains(db_session, tmp_config):
    config = tmp_config.model_copy(
        update={"gmail": tmp_config.gmail.model_copy(update={"ignore_domains": ["gmail.com"]})}
    )
    message = MailMessage(uid="1", sender_email="user@gmail.com", body="hi")
    client = FakeGmailClient([message])

    summary = harvest_inbox(config=config, client=client)

    assert summary.messages_scanned == 1
    assert summary.messages_skipped == 1
    assert summary.records_new == 0

    repo = CompanyRepository(db_session)
    assert repo.all() == []


def test_harvest_inbox_merges_repeated_sender_across_runs(db_session, tmp_config):
    message = MailMessage(
        uid="1", sender_name="Jane", sender_email="jane@example-biz.com.tw", body=""
    )
    client = FakeGmailClient([message])

    first = harvest_inbox(config=tmp_config, client=client)
    assert first.records_new == 1

    second = harvest_inbox(config=tmp_config, client=FakeGmailClient([message]))
    assert second.records_merged == 1
    assert second.records_new == 0

    repo = CompanyRepository(db_session)
    assert repo.count() == 1


# ------------------------------------------------------------------ GmailClient
#
# ``imaplib.IMAP4_SSL`` is replaced with a scripted fake -- no socket is ever
# opened, so these stay entirely offline.


class FakeIMAP:
    """Stands in for ``imaplib.IMAP4_SSL``."""

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.logged_in = False
        self.closed = False
        self.logged_out = False

    def login(self, address, password):
        self.logged_in = True

    def select(self, mailbox, readonly=True):
        self.selected = mailbox

    def close(self):
        self.closed = True

    def logout(self):
        self.logged_out = True

    def uid(self, command, *args):
        raise AssertionError(f"unexpected uid({command!r}) call")


def _connected_client(monkeypatch, tmp_config, fake=None):
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    fake = fake or FakeIMAP(None, None)
    monkeypatch.setattr("gmail.client.imaplib.IMAP4_SSL", lambda host, port: fake)
    client = GmailClient(tmp_config)
    client.connect()
    return client, fake


def test_gmail_client_connect_requires_credentials(monkeypatch, tmp_config):
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    client = GmailClient(tmp_config)
    with pytest.raises(GmailError):
        client.connect()


def test_gmail_client_connect_success(monkeypatch, tmp_config):
    client, fake = _connected_client(monkeypatch, tmp_config)
    assert fake.logged_in is True
    assert fake.selected == tmp_config.gmail.mailbox


def test_gmail_client_connect_wraps_imap_login_error(monkeypatch, tmp_config):
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")

    class FailingIMAP(FakeIMAP):
        def login(self, address, password):
            raise imaplib.IMAP4.error("bad credentials")

    monkeypatch.setattr("gmail.client.imaplib.IMAP4_SSL", lambda host, port: FailingIMAP(host, port))
    client = GmailClient(tmp_config)
    with pytest.raises(GmailError):
        client.connect()


def test_gmail_client_connect_wraps_os_error(monkeypatch, tmp_config):
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")

    def boom(host, port):
        raise OSError("network unreachable")

    monkeypatch.setattr("gmail.client.imaplib.IMAP4_SSL", boom)
    client = GmailClient(tmp_config)
    with pytest.raises(GmailError):
        client.connect()


def test_gmail_client_close_is_a_no_op_when_never_connected(tmp_config):
    client = GmailClient(tmp_config)
    client.close()  # must not raise


def test_gmail_client_close_logs_out(monkeypatch, tmp_config):
    client, fake = _connected_client(monkeypatch, tmp_config)
    client.close()
    assert fake.closed is True
    assert fake.logged_out is True
    assert client._connection is None


def test_gmail_client_search_requires_connection(tmp_config):
    client = GmailClient(tmp_config)
    with pytest.raises(GmailError):
        client.search()


def test_gmail_client_search_returns_uids_newest_first_and_applies_limit(monkeypatch, tmp_config):
    class SearchIMAP(FakeIMAP):
        def uid(self, command, _none, criterion):
            assert command == "SEARCH"
            return "OK", [b"1 2 3"]

    client, _ = _connected_client(monkeypatch, tmp_config, FakeIMAP(None, None))
    client._connection = SearchIMAP(None, None)
    assert client.search(limit=2) == ["3", "2"]


def test_gmail_client_search_raises_on_bad_status(monkeypatch, tmp_config):
    class BadStatusIMAP(FakeIMAP):
        def uid(self, command, _none, criterion):
            return "NO", [None]

    client, _ = _connected_client(monkeypatch, tmp_config)
    client._connection = BadStatusIMAP(None, None)
    with pytest.raises(GmailError):
        client.search()


def test_gmail_client_search_wraps_imap_error(monkeypatch, tmp_config):
    class ErrorIMAP(FakeIMAP):
        def uid(self, command, _none, criterion):
            raise imaplib.IMAP4.error("search failed")

    client, _ = _connected_client(monkeypatch, tmp_config)
    client._connection = ErrorIMAP(None, None)
    with pytest.raises(GmailError):
        client.search()


def test_gmail_client_fetch_requires_connection(tmp_config):
    client = GmailClient(tmp_config)
    with pytest.raises(GmailError):
        client.fetch("1")


def test_gmail_client_fetch_returns_none_on_imap_error(monkeypatch, tmp_config):
    class ErrorIMAP(FakeIMAP):
        def uid(self, command, uid, spec):
            raise imaplib.IMAP4.error("fetch failed")

    client, _ = _connected_client(monkeypatch, tmp_config)
    client._connection = ErrorIMAP(None, None)
    assert client.fetch("1") is None


def test_gmail_client_fetch_returns_none_for_unexpected_response_shape(monkeypatch, tmp_config):
    class EmptyIMAP(FakeIMAP):
        def uid(self, command, uid, spec):
            return "OK", [None]

    client, _ = _connected_client(monkeypatch, tmp_config)
    client._connection = EmptyIMAP(None, None)
    assert client.fetch("1") is None


def _raw_message_bytes() -> bytes:
    message = EmailMessage()
    message["Subject"] = str(Header("測試主旨", "utf-8"))
    message["From"] = "David Wang <david@example.com>"
    message["Date"] = "Mon, 03 Aug 2026 09:00:00 +0800"
    message["Organization"] = "Example Corp"
    message["Reply-To"] = "reply@example.com"
    message.set_content("Hello there")
    return message.as_bytes()


def test_gmail_client_fetch_parses_a_real_message(monkeypatch, tmp_config):
    raw = _raw_message_bytes()

    class FetchIMAP(FakeIMAP):
        def uid(self, command, uid, spec):
            return "OK", [(b"1 (BODY[] {123}", raw)]

    client, _ = _connected_client(monkeypatch, tmp_config)
    client._connection = FetchIMAP(None, None)
    message = client.fetch("1")

    assert message is not None
    assert message.subject == "測試主旨"
    assert message.sender_name == "David Wang"
    assert message.sender_email == "david@example.com"
    assert message.date is not None
    assert message.headers["Organization"] == "Example Corp"
    assert message.headers["Reply-To"] == "reply@example.com"
    assert "Hello there" in message.body


def test_gmail_client_iter_messages_skips_unfetchable_uids(monkeypatch, tmp_config):
    raw = _raw_message_bytes()

    class MixedIMAP(FakeIMAP):
        def uid(self, command, *args):
            if command == "SEARCH":
                return "OK", [b"1 2"]
            uid = args[0]
            if uid == "2":
                raise imaplib.IMAP4.error("boom")
            return "OK", [(b"1 (BODY[] {1}", raw)]

    client, _ = _connected_client(monkeypatch, tmp_config)
    client._connection = MixedIMAP(None, None)
    messages = list(client.iter_messages())
    assert len(messages) == 1


def test_gmail_client_context_manager_connects_and_closes(monkeypatch, tmp_config):
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    fake = FakeIMAP(None, None)
    monkeypatch.setattr("gmail.client.imaplib.IMAP4_SSL", lambda host, port: fake)

    with GmailClient(tmp_config) as client:
        assert client._connection is fake
    assert fake.logged_out is True


def test_gmail_session_context_manager(monkeypatch, tmp_config):
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    fake = FakeIMAP(None, None)
    monkeypatch.setattr("gmail.client.imaplib.IMAP4_SSL", lambda host, port: fake)

    with gmail_session(tmp_config) as client:
        assert client._connection is fake
    assert fake.logged_out is True
