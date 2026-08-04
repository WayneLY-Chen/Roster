"""Send outbound mail through the user's own Gmail account over SMTP.

Uses ``smtplib`` with STARTTLS on Gmail's submission port and the same Google
App Password credentials already used for IMAP in :mod:`gmail.client`
(``GMAIL_ADDRESS`` / ``GMAIL_APP_PASSWORD`` in ``.env``). This module has one
job: hand one already-approved message to Gmail's SMTP relay. Every decision
about *whether* a message should be sent at all (suppression, daily caps,
resend windows) belongs to :mod:`gmail.campaign`, not here.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr
from pathlib import Path
from typing import Protocol

from core.config import AppConfig, get_config
from core.constants import LogCategory
from core.errors import GmailError
from core.logging_setup import get_logger

log = get_logger(LogCategory.CRAWL)

_APP_PASSWORD_HELP = (
    "請至 https://myaccount.google.com/apppasswords 產生應用程式密碼"
    "（需先在 Google 帳戶開啟兩步驟驗證），並將帳號與密碼設定於 .env 的 "
    "GMAIL_ADDRESS 與 GMAIL_APP_PASSWORD。"
)


class SendableMessage(Protocol):
    """Anything with these three attributes can be sent.

    ``database.models.EmailMessage`` satisfies this without :mod:`gmail.sender`
    ever importing the ORM layer, which keeps this module usable outside the
    database (dry-run previews, tests with a bare dataclass, ...).
    """

    to_address: str
    subject: str
    body: str | None


class SmtpSender:
    """Thin wrapper around ``smtplib.SMTP`` for the user's own Gmail account."""

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()
        self.settings = self.config.mailer
        self._connection: smtplib.SMTP | None = None

    def connect(self) -> None:
        """Log in to the SMTP relay. Raises :class:`GmailError` on any failure."""
        address, password = self.settings.address, self.settings.app_password
        if not address or not password:
            raise GmailError(
                "尚未設定寄件帳號：GMAIL_ADDRESS 與 GMAIL_APP_PASSWORD 必須存在於 .env。"
                + _APP_PASSWORD_HELP
            )
        try:
            connection = smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=30)
            connection.ehlo()
            if self.settings.use_tls:
                connection.starttls()
                connection.ehlo()
            connection.login(address, password)
        except smtplib.SMTPAuthenticationError as exc:
            raise GmailError(
                f"Gmail 寄件帳號 {address} 登入失敗，請確認應用程式密碼是否正確、"
                f"是否已開啟兩步驟驗證。{_APP_PASSWORD_HELP} ({exc})"
            ) from exc
        except smtplib.SMTPException as exc:
            raise GmailError(f"連線 {self.settings.smtp_host} 時發生 SMTP 錯誤：{exc}") from exc
        except OSError as exc:
            raise GmailError(f"無法連線至 {self.settings.smtp_host}：{exc}") from exc

        self._connection = connection
        log.info("connected to {} as {}", self.settings.smtp_host, address)

    def close(self) -> None:
        if self._connection is None:
            return
        try:
            self._connection.quit()
        except (smtplib.SMTPException, OSError):  # pragma: no cover - teardown
            pass
        finally:
            self._connection = None

    def send(self, message: SendableMessage) -> None:
        """Compose and hand one message to the SMTP relay.

        ``message`` only needs ``to_address``, ``subject`` and ``body`` --
        typically a :class:`database.models.EmailMessage` row, but any
        duck-typed stand-in works (useful in tests).
        """
        if self._connection is None:
            raise GmailError("尚未連線；請先呼叫 connect()")

        to_address = getattr(message, "to_address", "") or ""
        if not to_address:
            raise GmailError("寄送的訊息缺少收件人地址 (to_address)")
        subject = getattr(message, "subject", "") or ""
        body = getattr(message, "body", "") or ""

        address = self.settings.address
        mime = MimeMessage()
        mime["From"] = (
            formataddr((self.settings.sender_name, address))
            if self.settings.sender_name
            else address
        )
        mime["To"] = to_address
        mime["Subject"] = subject
        if self.settings.reply_to:
            mime["Reply-To"] = self.settings.reply_to

        # Recipients must always have an obvious way out, whatever the body
        # text says -- this header is honoured by mailbox providers directly.
        unsubscribe_target = self.settings.reply_to or address
        mime["List-Unsubscribe"] = f"<mailto:{unsubscribe_target}?subject=unsubscribe>"

        self._set_body(mime, body)

        try:
            self._connection.send_message(mime)
        except smtplib.SMTPException as exc:
            raise GmailError(f"寄送給 {to_address} 失敗：{exc}") from exc

    def _set_body(self, mime: MimeMessage, body: str) -> None:
        """Attach the body, as plain text or as HTML with inline images.

        An HTML message always carries a plain-text alternative too: not every
        mail client renders HTML, and an HTML-only message scores noticeably
        worse with spam filters.

        Images referenced by the body are attached and rewritten to ``cid:``
        references. They cannot be left as ``data:`` URIs -- Gmail strips those
        out of received mail, so the recipient would see a broken image.
        """
        from gmail.richtext import html_to_plain_text, looks_like_html

        if not looks_like_html(body):
            mime.set_content(body, charset="utf-8")
            return

        html_body, images = self._resolve_images(body)
        mime.set_content(html_to_plain_text(body), charset="utf-8")
        mime.add_alternative(html_body, subtype="html", charset="utf-8")

        if not images:
            return

        # The <img> tags live in the HTML part, so the related images have to be
        # attached to that part -- not to the top-level message, where clients
        # would show them as ordinary downloadable attachments instead.
        html_part = mime.get_payload()[-1]
        for cid, (data, subtype) in images.items():
            html_part.add_related(data, "image", subtype, cid=f"<{cid}>")

    def _resolve_images(self, body: str) -> tuple[str, dict[str, tuple[bytes, str]]]:
        """Swap ``src="images/x.png"`` for ``src="cid:..."`` and read the files.

        A missing or unreadable image is dropped from the message rather than
        aborting the send: one broken logo should not stop a campaign.
        """
        import mimetypes
        import re
        import uuid

        images_dir = self.settings.resolved_templates_dir / "images"
        found: dict[str, tuple[bytes, str]] = {}

        def _swap(match: "re.Match[str]") -> str:
            source = match.group(1)
            if source.startswith(("http://", "https://", "cid:")):
                return match.group(0)

            path = (images_dir / Path(source).name).resolve()
            try:
                # Confine reads to the images folder: a template is an editable
                # file, and "images/../../../.env" must not become an attachment.
                path.relative_to(images_dir.resolve())
                data = path.read_bytes()
            except (OSError, ValueError):
                log.warning("信件圖片讀不到，已略過：{}", source)
                return ""

            guessed, _ = mimetypes.guess_type(path.name)
            subtype = (guessed or "image/png").split("/")[-1]
            cid = f"{uuid.uuid4().hex}@taiwanb2bcrm"
            found[cid] = (data, subtype)
            return f'<img src="cid:{cid}"'

        rewritten = re.sub(r'<img\s+src="([^"]*)"', _swap, body)
        return rewritten, found

    def __enter__(self) -> "SmtpSender":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
