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
import ssl
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr, make_msgid
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


#: 連線、登入、一般指令最多等幾秒。
_CONNECT_TIMEOUT = 30

#: 送信時假設上傳速度至少有這麼快（位元組／秒），用來決定要等多久。
#: 50 KB/s 是很慢的網路；實際上大多快得多，這裡只是不要太早放棄。
_MIN_UPLOAD_BYTES_PER_SECOND = 50_000


def _send_timeout(size: int) -> float:
    """寄一封 ``size`` 位元組的信，最多等幾秒。

    Python 的 socket 逾時算的是**整個送出動作的總時間**，不是兩次送出之間的
    間隔。以前固定 30 秒：一封帶 10 MB 附件的信，在每秒上傳 300 KB 的網路上
    要 30 幾秒才送得完，於是每一次都在 30 秒整被切斷，畫面上看到的是
    「Server not connected」，重連再寄也一樣送不完。
    """
    return max(float(_CONNECT_TIMEOUT), size / _MIN_UPLOAD_BYTES_PER_SECOND)


def _explain(exc: smtplib.SMTPException) -> str:
    """寄送失敗的原因。是送太久被切斷的話，講清楚是附件太大或網路太慢。"""
    if isinstance(exc.__context__, TimeoutError) or isinstance(exc.__cause__, TimeoutError):
        return f"{exc}（信件送太久被切斷，多半是附件太大或網路上傳太慢）"
    return str(exc)


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
            connection = smtplib.SMTP(
                self.settings.smtp_host, self.settings.smtp_port, timeout=_CONNECT_TIMEOUT
            )
            connection.ehlo()
            if self.settings.use_tls:
                # 一定要傳 context。不傳的話 smtplib 用的是**不驗證憑證**的
                # 預設值，公共 Wi-Fi 上的中間人就能拿到應用程式密碼。
                connection.starttls(context=ssl.create_default_context())
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

    def send(
        self,
        message: SendableMessage,
        attachments: list[tuple[str, bytes, str]] | None = None,
    ) -> str:
        """Compose and hand one message to the SMTP relay.

        ``message`` only needs ``to_address``, ``subject`` and ``body`` --
        typically a :class:`database.models.EmailMessage` row, but any
        duck-typed stand-in works (useful in tests).

        ``attachments`` is ``(檔名, 內容, MIME type)``，已經讀進記憶體。刻意
        不在這裡讀檔：一批信件共用同一組附件，由呼叫端用
        :func:`gmail.attachments.load_for_sending` 讀一次就好，也才能在開始
        寄之前就發現檔案不見，而不是寄到第 37 封才中斷。
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
        # 自己配一個 Message-ID 並且記下來：對方回信時會在 In-Reply-To 帶著
        # 它，那是唯一「這封回信對應到我哪一封」的確定證據。不設的話由伺服器
        # 配，而我們永遠看不到那個值。
        rfc_message_id = make_msgid(domain=address.rpartition("@")[2] or None)
        mime["Message-ID"] = rfc_message_id
        if self.settings.reply_to:
            mime["Reply-To"] = self.settings.reply_to

        # Recipients must always have an obvious way out, whatever the body
        # text says -- this header is honoured by mailbox providers directly.
        unsubscribe_target = self.settings.reply_to or address
        mime["List-Unsubscribe"] = f"<mailto:{unsubscribe_target}?subject=unsubscribe>"

        self._set_body(mime, body)
        self._add_attachments(mime, attachments or [])

        try:
            self._deliver(mime)
        except smtplib.SMTPServerDisconnected:
            # Gmail 會把閒置一陣子的連線關掉（每封間隔設得長的時候最常見），
            # 之後每一封都會是「Server not connected」。重連一次再試；還是不行
            # 才算這一封失敗。
            log.info("SMTP 連線被關掉了，重新連線後再寄一次")
            self.connect()
            try:
                self._deliver(mime)
            except smtplib.SMTPException as exc:
                raise GmailError(f"寄送給 {to_address} 失敗：{_explain(exc)}") from exc
        except smtplib.SMTPException as exc:
            raise GmailError(f"寄送給 {to_address} 失敗：{_explain(exc)}") from exc
        return rfc_message_id

    def _deliver(self, mime: MimeMessage) -> None:
        """把組好的信交給 SMTP，送信的那段時間依信件大小放寬。"""
        assert self._connection is not None
        sock = getattr(self._connection, "sock", None)
        if sock is not None:
            sock.settimeout(_send_timeout(len(mime.as_bytes())))
        try:
            self._connection.send_message(mime)
        finally:
            # 斷線的話 sock 會變成 None；沒斷就把逾時改回一般指令用的長度。
            sock = getattr(self._connection, "sock", None)
            if sock is not None:
                sock.settimeout(_CONNECT_TIMEOUT)

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

    def _add_attachments(
        self, mime: MimeMessage, attachments: list[tuple[str, bytes, str]]
    ) -> None:
        """把檔案掛成一般附件（收件者看得到、可以下載的那種）。

        跟內文圖片不同：圖片是 ``add_related`` 掛在 HTML 部分裡、用 ``cid:``
        引用、不會出現在附件列表；這裡的檔案要 ``add_attachment`` 掛在最外層，
        收件者才看得到迴紋針。
        """
        for filename, data, mime_type in attachments:
            maintype, _, subtype = mime_type.partition("/")
            mime.add_attachment(
                data,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=filename,
            )

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
            cid = f"{uuid.uuid4().hex}@roster"
            found[cid] = (data, subtype)
            return f'<img src="cid:{cid}"'

        rewritten = re.sub(r'<img\s+src="([^"]*)"', _swap, body)
        return rewritten, found

    def __enter__(self) -> "SmtpSender":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
