"""Read-only Gmail access over IMAP.

Authentication uses a Google **App Password** (``GMAIL_ADDRESS`` and
``GMAIL_APP_PASSWORD`` in ``.env``), which is the supported way for a desktop
program to read a mailbox the user owns. Nothing here sends mail, deletes mail,
or marks messages as read -- ``BODY.PEEK`` keeps the unread state intact.
"""

from __future__ import annotations

import email
import imaplib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime

from core.config import AppConfig, get_config
from core.constants import LogCategory
from core.errors import GmailError
from core.logging_setup import get_logger

log = get_logger(LogCategory.CRAWL)

_MAX_BODY_CHARS = 20_000


@dataclass(slots=True)
class MailMessage:
    """The parts of a message this app cares about."""

    uid: str
    subject: str = ""
    sender_name: str = ""
    sender_email: str = ""
    date: datetime | None = None
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def sender_domain(self) -> str:
        return self.sender_email.rpartition("@")[2].lower()


def decode_header_value(raw: object) -> str:
    """把一個標頭解成純文字。

    兩件事一起做，而且兩件都會咬人：

    * **RFC 2047 的編碼字**（``=?UTF-8?B?...?=``）要解開，不然主旨全是亂碼。
    * **回傳值不一定是字串。** 標頭裡有非 ASCII 位元組時，email 套件回的是一個
      ``Header`` 物件而不是 ``str``——直接對它呼叫 ``.lower()`` 會炸，而那只有
      在真的收到一封中文主旨的信時才會發生。
    """
    if raw is None:
        return ""
    text = raw if isinstance(raw, str) else str(raw)
    if not text:
        return ""
    try:
        return str(make_header(decode_header(text))).strip()
    except (UnicodeDecodeError, LookupError, ValueError):
        return text.strip()


#: 舊名字，這個模組內部還在用。
_decode = decode_header_value


def _extract_body(message: Message) -> str:
    """Best-effort plain-text body, preferring ``text/plain`` over HTML."""
    plain: list[str] = []
    html: list[str] = []

    for part in message.walk() if message.is_multipart() else [message]:
        content_type = part.get_content_type()
        if part.get_filename():
            continue
        if content_type not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        (plain if content_type == "text/plain" else html).append(text)

    if plain:
        return "\n".join(plain)[:_MAX_BODY_CHARS]
    if html:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup("\n".join(html), "html.parser")
        return soup.get_text("\n", strip=True)[:_MAX_BODY_CHARS]
    return ""


class GmailClient:
    """Minimal IMAP reader for a single mailbox."""

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()
        self.settings = self.config.gmail
        self._connection: imaplib.IMAP4_SSL | None = None

    def connect(self) -> None:
        address, password = self.settings.address, self.settings.app_password
        if not address or not password:
            raise GmailError(
                "GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set in .env. "
                "Create an App Password at https://myaccount.google.com/apppasswords"
            )
        try:
            connection = imaplib.IMAP4_SSL(self.settings.imap_host, self.settings.imap_port)
            connection.login(address, password)
            connection.select(self.settings.mailbox, readonly=True)
        except imaplib.IMAP4.error as exc:
            raise GmailError(
                f"Gmail login failed for {address}. Check the app password and "
                f"that IMAP is enabled in Gmail settings. ({exc})"
            ) from exc
        except OSError as exc:
            raise GmailError(f"could not reach {self.settings.imap_host}: {exc}") from exc

        self._connection = connection
        log.info("connected to {} as {}", self.settings.imap_host, address)

    def close(self) -> None:
        if self._connection is None:
            return
        try:
            self._connection.close()
            self._connection.logout()
        except (imaplib.IMAP4.error, OSError):  # pragma: no cover - teardown
            pass
        finally:
            self._connection = None

    def search(self, query: str | None = None, limit: int | None = None) -> list[str]:
        """UIDs matching an IMAP search query, newest first."""
        if self._connection is None:
            raise GmailError("not connected; call connect() first")

        criterion = (query or self.settings.search or "ALL").strip()
        try:
            status, data = self._connection.uid("SEARCH", None, criterion)
        except imaplib.IMAP4.error as exc:
            raise GmailError(f"IMAP search {criterion!r} failed: {exc}") from exc
        if status != "OK":
            raise GmailError(f"IMAP search {criterion!r} returned {status}")

        uids = data[0].split() if data and data[0] else []
        ordered = [uid.decode() for uid in reversed(uids)]
        return ordered[: limit or self.settings.max_messages]

    def fetch_raw(self, uid: str) -> bytes | None:
        """整封信的原始位元組，不改變已讀狀態。

        退信通知要的東西在 ``message/delivery-status`` 那個 MIME 區塊裡，而
        :meth:`fetch` 只留純文字內文——那一層會把非 ``text/*`` 的區塊丟掉。
        要解析 DSN 就得看到完整的 MIME 樹（見 :mod:`gmail.bounces`）。
        """
        if self._connection is None:
            raise GmailError("not connected; call connect() first")
        try:
            status, data = self._connection.uid("FETCH", uid, "(BODY.PEEK[])")
        except imaplib.IMAP4.error as exc:
            log.warning("could not fetch message {}: {}", uid, exc)
            return None
        if status != "OK" or not data or not isinstance(data[0], tuple):
            return None
        return data[0][1]

    def fetch(self, uid: str) -> MailMessage | None:
        """Fetch one message without changing its read state."""
        raw = self.fetch_raw(uid)
        if raw is None:
            return None

        message = email.message_from_bytes(raw)
        sender_name, sender_email = parseaddr(_decode(message.get("From")))

        date_value: datetime | None = None
        raw_date = message.get("Date")
        if raw_date:
            try:
                date_value = parsedate_to_datetime(raw_date)
            except (TypeError, ValueError):
                date_value = None

        return MailMessage(
            uid=uid,
            subject=_decode(message.get("Subject")),
            sender_name=sender_name,
            sender_email=sender_email.lower(),
            date=date_value,
            body=_extract_body(message),
            headers={
                "Organization": _decode(message.get("Organization")),
                "Reply-To": _decode(message.get("Reply-To")),
            },
        )

    def iter_messages(
        self, query: str | None = None, limit: int | None = None
    ) -> Iterator[MailMessage]:
        for uid in self.search(query, limit):
            message = self.fetch(uid)
            if message is not None:
                yield message

    def __enter__(self) -> "GmailClient":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


@contextmanager
def gmail_session(config: AppConfig | None = None) -> Iterator[GmailClient]:
    """Connected client as a context manager."""
    client = GmailClient(config)
    client.connect()
    try:
        yield client
    finally:
        client.close()
