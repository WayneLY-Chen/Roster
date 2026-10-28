"""Gmail access over IMAP.

Authentication uses a Google **App Password** (``GMAIL_ADDRESS`` and
``GMAIL_APP_PASSWORD`` in ``.env``), which is the supported way for a desktop
program to read a mailbox the user owns. Nothing here sends mail, deletes mail,
or marks messages as read -- ``BODY.PEEK`` keeps the unread state intact.

The one write is :meth:`GmailClient.add_label`, which only *adds* a Gmail
label (an IMAP COPY). Mailboxes are opened read-only unless a caller asks for
that one operation.
"""

from __future__ import annotations

import email
import imaplib
import re
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from core.config import AppConfig, get_config
from core.constants import LogCategory
from core.errors import GmailError
from core.logging_setup import get_logger
from gmail.mailbox import GmailLabel, decode_mailbox, encode_mailbox, parse_list_line, quote_mailbox

log = get_logger(LogCategory.CRAWL)

_MAX_BODY_CHARS = 20_000

#: 一次 FETCH／COPY 帶幾個 UID。太長的指令有些伺服器會拒絕。
_FETCH_BATCH = 200
_COPY_BATCH = 500

_UID_RE = re.compile(rb"UID (\d+)")


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


def parse_mail_date(raw: object) -> datetime | None:
    """把信件的 ``Date`` 標頭變成這個專案用的「不帶時區的當地時間」。

    **信件的日期一定帶時區偏移**（``+0800``、``-0500``），而整個資料庫的時間
    欄位都是 naive 的當地時間（見 :func:`database.models.now`）。兩者直接相比
    會丟 ``TypeError: can't compare offset-naive and offset-aware datetimes``。

    這個例外很陰險：它只有在「已經有上一次的時間可以比」的時候才會發生。第一
    次收到回信不會炸，第二次才會——而那時候使用者已經在用了。

    對方的時區要真的**換算**，不是把 ``tzinfo`` 拔掉了事：一封 ``-0500`` 寄出
    的信直接拔掉偏移，時間會變成比實際早十三個小時。
    """
    text = decode_header_value(raw)
    if not text:
        return None
    try:
        value = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    return value


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
                "還沒有設定 Gmail。請到「設定」頁填入 Gmail 帳號與應用程式密碼"
                "（在 https://myaccount.google.com/apppasswords 產生）。"
            )
        try:
            # 一定要傳 ssl_context。不傳的話 imaplib 用的是**不驗證憑證**的
            # 預設值，公共 Wi-Fi 上的中間人就能拿到應用程式密碼。
            connection = imaplib.IMAP4_SSL(
                self.settings.imap_host,
                self.settings.imap_port,
                ssl_context=ssl.create_default_context(),
            )
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
        criterion = (query or self.settings.search or "ALL").strip()
        return self.search_all(criterion)[: limit or self.settings.max_messages]

    def search_all(self, criterion: str) -> list[str]:
        """符合條件的每一封信的 UID，新的排前面，不截斷。"""
        connection = self._require_connection()
        try:
            status, data = connection.uid("SEARCH", None, criterion)
        except imaplib.IMAP4.error as exc:
            raise GmailError(f"IMAP search {criterion!r} failed: {exc}") from exc
        if status != "OK":
            raise GmailError(f"IMAP search {criterion!r} returned {status}")

        uids = data[0].split() if data and data[0] else []
        return [uid.decode() for uid in reversed(uids)]

    # ---------------------------------------------------------------- 標籤

    def list_labels(self) -> list[GmailLabel]:
        """Gmail 裡所有的標籤與系統信箱。"""
        connection = self._require_connection()
        status, data = connection.list()
        if status != "OK":
            raise GmailError(f"沒辦法列出 Gmail 的標籤（{status}）")
        labels = (parse_list_line(line) for line in data if isinstance(line, bytes))
        return [label for label in labels if label is not None]

    def open_mailbox(self, mailbox: str, *, writable: bool = False) -> None:
        """改看另一個標籤。``mailbox`` 是 IMAP 的原始名稱（已編碼、沒有引號）。

        預設唯讀。``writable`` 只給貼標籤用：貼標籤是 COPY，不會改動來源的信，
        但唯讀模式下有些伺服器會拒絕 COPY。
        """
        connection = self._require_connection()
        try:
            status, data = connection.select(quote_mailbox(mailbox), readonly=not writable)
        except imaplib.IMAP4.error as exc:
            raise GmailError(f"沒辦法打開「{decode_mailbox(mailbox)}」：{exc}") from exc
        if status != "OK":
            raise GmailError(f"沒辦法打開「{decode_mailbox(mailbox)}」：{data}")

    def open_all_mail(self, *, writable: bool = False) -> None:
        """打開「所有郵件」。它的名稱跟著 Gmail 的介面語言變，所以用旗標找。"""
        for label in self.list_labels():
            if label.is_all_mail:
                self.open_mailbox(label.mailbox, writable=writable)
                return
        raise GmailError(
            "找不到「所有郵件」。請到 Gmail 的設定 → 轉寄和 POP/IMAP，"
            "確認「所有郵件」有勾選在 IMAP 中顯示。"
        )

    def fetch_addresses(self, uids: list[str]) -> dict[str, list[str]]:
        """每封信的寄件者、收件者與副本地址（小寫）。只抓這三個標頭，不抓內文。"""
        connection = self._require_connection()
        found: dict[str, list[str]] = {}
        for start in range(0, len(uids), _FETCH_BATCH):
            batch = ",".join(uids[start:start + _FETCH_BATCH])
            try:
                status, data = connection.uid(
                    "FETCH", batch, "(UID BODY.PEEK[HEADER.FIELDS (FROM TO CC)])"
                )
            except imaplib.IMAP4.error as exc:
                log.warning("could not fetch headers: {}", exc)
                continue
            if status != "OK":
                continue
            for item in data:
                if not isinstance(item, tuple):
                    continue
                uid = _UID_RE.search(item[0])
                if uid is None:
                    continue
                headers = email.message_from_bytes(item[1])
                values = [str(v) for key in ("From", "To", "Cc") for v in headers.get_all(key, [])]
                found[uid.group(1).decode()] = [
                    address.lower() for _name, address in getaddresses(values) if "@" in address
                ]
        return found

    def add_label(self, uids: list[str], label: str) -> None:
        """替目前打開的信箱裡這幾封信加上 Gmail 標籤，標籤不存在就先建。

        用的是 COPY：在 Gmail 裡把信「複製」到一個標籤，就是替它加上那個標籤，
        信本身不動、原本的標籤也都還在。這支程式從來不送 STORE 或 EXPUNGE，
        所以不會刪信、不會改已讀。
        """
        connection = self._require_connection()
        mailbox = quote_mailbox(encode_mailbox(label))
        # 已經有這個標籤的話 Gmail 回 NO，那正是我們要的狀態，不算錯。
        connection.create(mailbox)
        for start in range(0, len(uids), _COPY_BATCH):
            batch = ",".join(uids[start:start + _COPY_BATCH])
            status, data = connection.uid("COPY", batch, mailbox)
            if status != "OK":
                raise GmailError(f"沒辦法貼上標籤「{label}」：{data}")

    def _require_connection(self) -> imaplib.IMAP4_SSL:
        if self._connection is None:
            raise GmailError("not connected; call connect() first")
        return self._connection

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

        date_value = parse_mail_date(message.get("Date"))

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
