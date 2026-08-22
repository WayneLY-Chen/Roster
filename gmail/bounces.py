"""讀自己的收件匣，把「寄不到的信箱」找出來。

## 為什麼需要這個

信寄出去之後發生的事，以前一件都不會回到名單上。最要命的是退信：沒有人讀那封
mailer-daemon，``email_verdict`` 就永遠停在「看起來可以」，下一批繼續寄同一個
死信箱——而**被拉低的是使用者自己 Gmail 帳號的送達率**，嚴重的話會被限制寄送。
那是他自己的資產，不是我們的。

MX 檢查證明的是「那個網域收信」，不是「那個信箱存在」。這兩件事差很多，而唯一
能分辨的證據就是真的寄一次然後看有沒有退回來。

## 這個模組只認，不寫

它讀信、解析、回一份清單。**沒有任何一條路會寫資料庫**——要不要標記是
:mod:`controllers.mail` 拿去問使用者之後才決定的。這跟 v1.22 的預覽表格、
v1.23 的候選清單是同一個道理：看過才寫。

## 硬退信與軟退信

RFC 3463 的狀態碼 ``x.y.z``：``5.x.x`` 是永久失敗（沒這個人、網域不存在），
``4.x.x`` 是暫時失敗（信箱滿了、對方伺服器忙）。

**分不出來的一律當軟退信。** 這個預設是刻意的：把一個真實客戶的信箱標死，代價
是從此再也不寄給他，而且他不會知道、使用者也不會發現。反過來漏標一個死信箱，
代價只是下次再收到一封退信。兩邊不對稱。

## 讀信箱這件事本身

全程唯讀，走 :class:`~gmail.client.GmailClient`：``BODY.PEEK`` 不會把信標成
已讀，也不刪信、不搬信。使用者的信箱不是這支程式的東西——掃過之後那幾封信在
Gmail 上看起來要跟沒被碰過一樣。
"""

from __future__ import annotations

import email
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from email.message import Message
from email.utils import parsedate_to_datetime

from core.constants import LogCategory
from core.logging_setup import get_logger
from gmail.client import decode_header_value

log = get_logger(LogCategory.CRAWL)

#: 預設的 IMAP 搜尋條件。
#:
#: 三個條件取聯集（IMAP 的 ``OR`` 是前綴寫法，兩個一組，所以三項要寫兩個 OR）：
#: 寄件者是 mailer-daemon 或 postmaster，或者這封信自己宣告是投遞狀態報告。
#: 第三項是 RFC 3464 規定的，所以連不是 Gmail 發出來的退信也涵蓋得到。
#:
#: 為什麼不直接掃整個收件匣：一個用了幾年的信箱有幾萬封信，全部抓下來只為了
#: 找幾封退信，要等很久而且沒有必要。
DEFAULT_QUERY = (
    'OR OR FROM "mailer-daemon" FROM "postmaster" '
    'HEADER "Content-Type" "report-type=delivery-status"'
)

#: 一次最多看幾封。
DEFAULT_LIMIT = 200

#: 「為什麼退回」那一句最多留幾個字。整段 SMTP 對話塞進表格會把欄寬撐爆。
MAX_REASON_CHARS = 160

#: 寄件者 local part 長這樣就是退信通知。
_DAEMONS = ("mailer-daemon", "postmaster", "mail-daemon")

#: RFC 3463 的狀態碼。
_STATUS = re.compile(r"\b([245])\.(\d{1,3})\.(\d{1,3})\b")

#: 內文裡的 SMTP 回應碼，解析不到 DSN 區塊時的退路。
_SMTP_CODE = re.compile(r"\b([45]\d{2})[ -]")

_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


@dataclass(frozen=True, slots=True)
class Bounce:
    """一封退信通知裡的一個收件地址。

    一封 DSN 可以一次報好幾個地址（同一批寄出去、同一台伺服器退回來），所以
    解析出來的是一串而不是一個。
    """

    address: str
    #: ``True`` = 永久失敗（``5.x.x``）。分不出來時一律 ``False``。
    hard: bool = False
    #: RFC 3463 的狀態碼，例如 ``5.1.1``。解析不到就空的。
    code: str = ""
    #: 對方伺服器說的原因，一句話。
    reason: str = ""
    subject: str = ""
    received_at: datetime | None = None
    #: 這封通知在信箱裡的 UID。留著是為了讓 Activity 寫得出「是哪一封信」。
    uid: str = ""

    @property
    def kind(self) -> str:
        return "硬退信" if self.hard else "軟退信"


def _clean_address(raw: str) -> str:
    """``rfc822; <Foo@Bar.com>`` → ``foo@bar.com``。"""
    text = (raw or "").strip()
    if ";" in text:
        text = text.partition(";")[2]
    match = _ADDRESS.search(text)
    return match.group(0).strip().lower() if match else ""


def _one_line(text: str) -> str:
    collapsed = " ".join((text or "").split())
    if len(collapsed) > MAX_REASON_CHARS:
        collapsed = collapsed[:MAX_REASON_CHARS] + "…"
    return collapsed


def looks_like_bounce(message: Message) -> bool:
    """這封信是不是投遞失敗通知。

    三個判準取聯集，跟 :data:`DEFAULT_QUERY` 對齊——搜尋條件是給伺服器過濾用
    的粗篩，這裡才是真的判斷。使用者自己改過搜尋條件時，這一層仍然守得住。
    """
    sender = decode_header_value(message.get("From")).lower()
    if any(f"{name}@" in sender for name in _DAEMONS):
        return True
    content_type = (
        decode_header_value(message.get("Content-Type")).lower().replace(" ", "")
    )
    if "report-type=delivery-status" in content_type:
        return True
    return any(
        part.get_content_type() == "message/delivery-status" for part in message.walk()
    )


def _delivery_status_blocks(message: Message) -> list[Message]:
    """``message/delivery-status`` 裡的每一段「像標頭的欄位群」。

    Python 的 email 套件會把這種區塊解析成一串 :class:`Message`：第一段是整封
    通知的欄位（回報的伺服器是誰），後面每一段對應一個收件地址。
    """
    blocks: list[Message] = []
    for part in message.walk():
        if part.get_content_type() != "message/delivery-status":
            continue
        payload = part.get_payload()
        if isinstance(payload, list):
            blocks.extend(item for item in payload if isinstance(item, Message))
    return blocks


def _from_dsn(message: Message) -> list[tuple[str, bool, str, str]]:
    """從標準的 DSN 區塊讀出 ``(地址, 硬退信, 代碼, 原因)``。"""
    found: list[tuple[str, bool, str, str]] = []
    for block in _delivery_status_blocks(message):
        address = _clean_address(
            block.get("Final-Recipient") or block.get("Original-Recipient") or ""
        )
        if not address:
            continue

        action = (block.get("Action") or "").strip().lower()
        if action in ("delivered", "relayed", "expanded"):
            # DSN 也會用來報「送到了」。把成功的那幾封當成退信處理，會把
            # 一個好好的信箱標死——而且完全看不出來為什麼。
            continue

        status = (block.get("Status") or "").strip()
        match = _STATUS.search(status)
        code = match.group(0) if match else ""
        # action 明講是延遲的，就算狀態碼寫 5 開頭也當軟退信：那是對方伺服器
        # 自己說「還會再試」，我們沒有理由比它更肯定。
        hard = bool(code.startswith("5")) and action != "delayed"

        diagnostic = (block.get("Diagnostic-Code") or "").strip()
        if diagnostic.lower().startswith("smtp;"):
            diagnostic = diagnostic.partition(";")[2]
        found.append((address, hard, code, _one_line(diagnostic)))
    return found


def _from_text(message: Message, *, sent_to: Sequence[str] = ()) -> list[tuple[str, bool, str, str]]:
    """沒有 DSN 區塊時的退路：掃內文找地址與代碼。

    這條路寬鬆得多，所以刻意配一個窄的出口——呼叫端只會採用「自己真的寄過」
    的地址（見 :mod:`controllers.mail`）。而且代碼讀不到明確的 5 開頭時一律
    當軟退信，也就是預設不勾。
    """
    body_parts: list[str] = []
    for part in message.walk() if message.is_multipart() else [message]:
        if part.get_content_type() != "text/plain":
            continue
        payload = part.get_payload(decode=True)
        if payload:
            charset = part.get_content_charset() or "utf-8"
            try:
                body_parts.append(payload.decode(charset, errors="replace"))
            except LookupError:
                body_parts.append(payload.decode("utf-8", errors="replace"))
    body = "\n".join(body_parts)
    if not body:
        return []

    status = _STATUS.search(body)
    code = status.group(0) if status else ""
    if not code:
        smtp = _SMTP_CODE.search(body)
        code = smtp.group(1) if smtp else ""
    hard = code.startswith("5")

    known = {address.lower() for address in sent_to}
    candidates: list[str] = []
    for raw in _ADDRESS.findall(body):
        address = raw.lower()
        if any(f"{name}@" in address for name in _DAEMONS):
            continue
        if known and address not in known:
            continue
        if address not in candidates:
            candidates.append(address)

    reason = _one_line(next((line for line in body.splitlines() if code and code in line), ""))
    return [(address, hard, code, reason) for address in candidates]


def parse_bounce(
    message: Message, *, uid: str = "", sent_to: Sequence[str] = ()
) -> list[Bounce]:
    """把一封退信通知解析成一或多筆 :class:`Bounce`。不是退信就回空的。"""
    if not looks_like_bounce(message):
        return []

    subject = decode_header_value(message.get("Subject"))
    received: datetime | None = None
    raw_date = decode_header_value(message.get("Date"))
    if raw_date:
        try:
            received = parsedate_to_datetime(raw_date)
        except (TypeError, ValueError):
            received = None

    rows = _from_dsn(message) or _from_text(message, sent_to=sent_to)
    seen: set[str] = set()
    bounces: list[Bounce] = []
    for address, hard, code, reason in rows:
        if not address or address in seen:
            continue
        seen.add(address)
        bounces.append(
            Bounce(
                address=address,
                hard=hard,
                code=code,
                reason=reason,
                subject=subject,
                received_at=received,
                uid=uid,
            )
        )
    return bounces


def iter_bounces(
    client,
    *,
    query: str | None = None,
    limit: int | None = None,
    sent_to: Sequence[str] = (),
    on_message=None,
) -> Iterator[Bounce]:
    """掃過信箱，一筆一筆吐出退信。

    ``client`` 是一個已經連上的 :class:`~gmail.client.GmailClient`。做成參數
    而不是在這裡開連線，是為了讓測試給一個假的——這個模組要驗的是解析，不是
    IMAP。

    ``sent_to`` 是「我真的寄過的地址」，只有走內文退路時才用得到（見
    :func:`_from_text`）。標準 DSN 不需要它。
    """
    for uid in client.search(query or DEFAULT_QUERY, limit or DEFAULT_LIMIT):
        raw = client.fetch_raw(uid)
        if on_message is not None:
            on_message(uid)
        if not raw:
            continue
        try:
            message = email.message_from_bytes(raw)
        except Exception as exc:  # 壞掉的一封不該讓整次掃描停下來
            log.warning("退信 {} 解析失敗：{}", uid, exc)
            continue
        yield from parse_bounce(message, uid=uid, sent_to=sent_to)


__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_QUERY",
    "MAX_REASON_CHARS",
    "Bounce",
    "iter_bounces",
    "looks_like_bounce",
    "parse_bounce",
]
