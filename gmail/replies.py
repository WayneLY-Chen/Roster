"""讀自己的收件匣，找出「有人回信了」與「有人叫我不要再寄」。

## 為什麼需要這個

沒有這一段，信寄出去之後只剩兩種下場：使用者自己去 Gmail 一封一封看再回來手動
改業務階段，或者根本不看。第二種比較常發生，而它的代價是**對一個已經回過信的
人再寄一次冷開發信**——那是整條流程裡最尷尬的失敗。

退訂更嚴重一點：每一封信都帶著 ``List-Unsubscribe`` 標頭、信尾也印著退訂說明，
等於明著邀請對方回信說不要。邀請了卻沒有人去收，那不只是難看。

## 怎麼認出「這是回我的信」

兩層，強的先試：

1. **``In-Reply-To``／``References`` 對得回我們寄出去的 ``Message-ID``。** 這是
   唯一確定的證據——它直接指到我們資料庫裡的那一封，連公司都不用猜。
2. **寄件者地址是我們寄過的地址。** 退路。這一層會把「同一個人寄來的任何一封
   信」都算成回覆，包含跟這次開發完全無關的信。所以它只用來推進業務階段（一個
   可以手動改回去的欄位），不做任何不可逆的事。

第一層不是每次都成立：有些寄件伺服器會把送出去的 ``Message-ID`` 換成自己的，
那時候收件端看到的根本不是我們存的那個值。所以兩層都要有，而且**哪一層對上的
要告訴使用者**——他才知道這一筆有多可信。

## 這個模組不寫資料庫，也不寄信

它讀信、解析、回一份清單。要不要採用是 :mod:`controllers.mail` 拿去問使用者之後
才決定的。

**而且它不會回信。** 這裡沒有 import 任何寄信的東西，也不該有。自動回覆是一個
「一次設定錯就對著幾百個真實客戶連續發生」的功能，不是這支程式該替使用者做的
決定。
"""

from __future__ import annotations

import email
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime

from core.constants import LogCategory
from core.logging_setup import get_logger
from gmail.bounces import looks_like_bounce
from gmail.client import decode_header_value

log = get_logger(LogCategory.CRAWL)

#: 往回看幾天。
#:
#: 開發信的回覆幾乎都在一兩週內。看太久遠只是把幾千封舊信抓下來解析一遍，
#: 而那些早就處理過了。
DEFAULT_DAYS = 30

#: 一次最多看幾封。
DEFAULT_LIMIT = 300

#: 判斷退訂時，內文只看前面這幾個字。
#:
#: 退訂的意思一定寫在最前面。往下看整封信的話，引言區塊裡我們自己那句「不想再
#: 收到請回信告訴我」會被當成對方在退訂——每一封回信都會變成退訂。
UNSUBSCRIBE_BODY_CHARS = 400

#: 摘要在畫面上留幾個字。
SNIPPET_CHARS = 80

#: 退訂的講法。刻意寬鬆——寧可多列一個讓使用者自己看，也不要漏掉。
#:
#: 漏掉的代價是繼續寄給一個已經明講不要的人；多列一個的代價是使用者在預覽表格
#: 上把它取消勾選。兩邊差很多。
_UNSUBSCRIBE = re.compile(
    r"退訂|取消訂閱|取消電子報|請勿再寄|不要再寄|不要再發|停止寄送|停寄|勿再來信"
    r"|移除我|從名單移除"
    r"|unsubscribe|remove me|opt[\s-]?out|take me off|stop emailing",
    re.IGNORECASE,
)

#: 自動回覆（休假、自動確認）。這一類不是人在回話。
#:
#: 認出來不是為了丟掉——「這個信箱有人管」本身就是有用的資訊，而且它同樣證明
#: 信寄到了。是為了在畫面上標出來，讓使用者知道別急著當成有興趣。
_AUTO_HEADERS = ("auto-submitted", "x-autoreply", "x-autorespond", "x-auto-response-suppress")
_AUTO_SUBJECT = re.compile(
    r"自動回覆|自动回复|外出|休假中|out of office|auto[\s-]?reply|automatic reply",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Reply:
    """收件匣裡一封對得回我們名單的信。"""

    address: str
    subject: str = ""
    received_at: datetime | None = None
    uid: str = ""
    #: ``"message-id"``（確定）或 ``"address"``（用寄件者地址猜的）。
    matched_by: str = "address"
    #: 直接對到的那一封寄出信的 ``EmailMessage.id``。只有 message-id 那條有。
    email_message_id: int | None = None
    #: 看起來是在要求不要再寄信。
    unsubscribe: bool = False
    #: 看起來是自動回覆，不是人在回話。
    automatic: bool = False
    snippet: str = ""

    @property
    def confidence(self) -> str:
        return "確定" if self.matched_by == "message-id" else "用地址比對"

    @property
    def kind(self) -> str:
        if self.unsubscribe:
            return "要求不要再寄"
        return "自動回覆" if self.automatic else "回覆"


def since_query(days: int = DEFAULT_DAYS, *, today: date | None = None) -> str:
    """IMAP 的日期搜尋條件。

    日期格式是 IMAP 規定的 ``dd-Mon-yyyy``，而且月份縮寫**一定要英文**——
    在中文語系的機器上用 ``strftime("%b")`` 會產生「8月」，伺服器直接回錯誤。
    """
    stamp = (today or date.today()) - timedelta(days=max(days, 1))
    months = (
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    )
    return f'SINCE {stamp.day:02d}-{months[stamp.month - 1]}-{stamp.year}'


def _sender(message: Message) -> str:
    pairs = getaddresses([decode_header_value(message.get("From"))])
    for _name, address in pairs:
        if address:
            return address.strip().lower()
    return ""


def _referenced_ids(message: Message) -> list[str]:
    """這封信說它在回哪幾封。

    ``References`` 是整串對話的歷史，``In-Reply-To`` 是直接的上一封。兩個都看，
    因為有些客戶端只送其中一個。
    """
    found: list[str] = []
    for header in ("In-Reply-To", "References"):
        raw = decode_header_value(message.get(header))
        for token in re.findall(r"<[^<>@\s]+@[^<>\s]+>", raw):
            if token not in found:
                found.append(token)
    return found


def _plain_body(message: Message, limit: int) -> str:
    parts: list[str] = []
    for part in message.walk() if message.is_multipart() else [message]:
        if part.get_content_type() != "text/plain" or part.get_filename():
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            parts.append(payload.decode(charset, errors="replace"))
        except LookupError:
            parts.append(payload.decode("utf-8", errors="replace"))
        if sum(len(item) for item in parts) >= limit:
            break
    return "\n".join(parts)[:limit]


def looks_automatic(message: Message) -> bool:
    """這封是機器發的自動回覆嗎。"""
    for header in _AUTO_HEADERS:
        value = decode_header_value(message.get(header)).lower()
        if value and value != "no":
            return True
    if decode_header_value(message.get("Precedence")).lower() in (
        "bulk", "auto_reply", "junk"
    ):
        return True
    return bool(_AUTO_SUBJECT.search(decode_header_value(message.get("Subject"))))


def wants_out(subject: str, body: str) -> bool:
    """對方是不是在說「不要再寄了」。"""
    return bool(_UNSUBSCRIBE.search(subject)) or bool(_UNSUBSCRIBE.search(body))


def parse_reply(
    message: Message,
    *,
    uid: str = "",
    sent_to: Sequence[str] = (),
    sent_ids: dict[str, int] | None = None,
) -> Reply | None:
    """把一封收到的信變成 :class:`Reply`，對不上就回 ``None``。

    ``sent_ids`` 是 ``{我們寄出去的 Message-ID: EmailMessage.id}``。
    """
    if looks_like_bounce(message):
        # 退信不是回覆。當成回覆的話，一個死信箱會把業務階段推到「已聯絡」——
        # 那是完全相反的結論。退信走 gmail/bounces.py。
        return None

    address = _sender(message)
    if not address:
        return None

    known = sent_ids or {}
    matched_by = "address"
    email_message_id: int | None = None
    for token in _referenced_ids(message):
        if token in known:
            matched_by, email_message_id = "message-id", known[token]
            break

    if matched_by == "address" and address not in {item.lower() for item in sent_to}:
        return None

    subject = decode_header_value(message.get("Subject"))
    body = _plain_body(message, UNSUBSCRIBE_BODY_CHARS)

    received: datetime | None = None
    raw_date = decode_header_value(message.get("Date"))
    if raw_date:
        try:
            received = parsedate_to_datetime(raw_date)
        except (TypeError, ValueError):
            received = None

    return Reply(
        address=address,
        subject=subject,
        received_at=received,
        uid=uid,
        matched_by=matched_by,
        email_message_id=email_message_id,
        unsubscribe=wants_out(subject, body),
        automatic=looks_automatic(message),
        snippet=" ".join(body.split())[:SNIPPET_CHARS],
    )


def iter_replies(
    client,
    *,
    query: str | None = None,
    limit: int | None = None,
    sent_to: Sequence[str] = (),
    sent_ids: dict[str, int] | None = None,
    on_message=None,
) -> Iterator[Reply]:
    """掃過信箱，一封一封吐出對得上的回信。

    ``client`` 是一個已經連上的 :class:`~gmail.client.GmailClient`。做成參數而
    不是在這裡開連線，是為了跟退信那一趟**共用同一條連線**——使用者按的是一顆
    按鈕，不該登入兩次。
    """
    for uid in client.search(query or since_query(), limit or DEFAULT_LIMIT):
        raw = client.fetch_raw(uid)
        if on_message is not None:
            on_message(uid)
        if not raw:
            continue
        try:
            message = email.message_from_bytes(raw)
        except Exception as exc:  # 壞掉的一封不該讓整次掃描停下來
            log.warning("信件 {} 解析失敗：{}", uid, exc)
            continue
        reply = parse_reply(message, uid=uid, sent_to=sent_to, sent_ids=sent_ids)
        if reply is not None:
            yield reply


__all__ = [
    "DEFAULT_DAYS",
    "DEFAULT_LIMIT",
    "SNIPPET_CHARS",
    "UNSUBSCRIBE_BODY_CHARS",
    "Reply",
    "iter_replies",
    "looks_automatic",
    "parse_reply",
    "since_query",
    "wants_out",
]
