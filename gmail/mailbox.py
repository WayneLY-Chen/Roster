"""IMAP 信箱名稱：Gmail 的標籤在 IMAP 裡就是一個個信箱（資料夾）。

兩件 imaplib 不替你做、但中文標籤一定會遇到的事：

* **名稱編碼。** IMAP 的信箱名稱只能是 ASCII，非 ASCII 要寫成「修改版 UTF-7」
  （RFC 3501 §5.1.3）。Gmail 的「客戶」在 IMAP 裡長這樣：``&W6JiNg-``。
  直接把中文送過去，Gmail 會回「找不到這個信箱」。
* **加引號。** imaplib 不會替參數加引號，而 ``[Gmail]/All Mail`` 中間有空格，
  不加引號會被拆成兩個參數。
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass

#: 名單匠寫進 Gmail 的標籤都放在這一層底下。不要了，整個刪掉就乾淨。
ROSTER_ROOT = "名單匠"

#: Gmail 系統信箱的上層。有些地區叫 ``[Google Mail]``。
_SYSTEM_PREFIXES = ("[Gmail]/", "[Google Mail]/")

#: RFC 6154 的特殊用途旗標。有這些的是系統信箱（寄件備份、垃圾桶⋯⋯），不是使用者的標籤。
_SPECIAL_USE = frozenset(
    {"\\all", "\\archive", "\\drafts", "\\flagged", "\\important", "\\junk", "\\sent", "\\trash"}
)


def encode_mailbox(name: str) -> str:
    """把標籤名稱轉成 IMAP 的修改版 UTF-7。``客戶/台中`` -> ``&W6JiNg-/&U,BOLQ-``。"""
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            data = base64.b64encode("".join(run).encode("utf-16-be")).decode("ascii")
            out.append("&" + data.rstrip("=").replace("/", ",") + "-")
            run.clear()

    for char in name:
        if 0x20 <= ord(char) <= 0x7E:
            flush()
            out.append("&-" if char == "&" else char)
        else:
            run.append(char)
    flush()
    return "".join(out)


_ENCODED_RUN = re.compile(r"&([^-]*)-")


def decode_mailbox(raw: str) -> str:
    """:func:`encode_mailbox` 的反向。"""

    def expand(match: re.Match[str]) -> str:
        body = match.group(1)
        if not body:
            return "&"
        data = body.replace(",", "/")
        data += "=" * (-len(data) % 4)
        return base64.b64decode(data).decode("utf-16-be")

    return _ENCODED_RUN.sub(expand, raw)


def quote_mailbox(raw: str) -> str:
    """加上 IMAP 的雙引號。``raw`` 是已經編碼過的名稱。"""
    return '"' + raw.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass(frozen=True, slots=True)
class GmailLabel:
    """Gmail 的一個標籤（或系統信箱）。"""

    #: 顯示用的名稱，已經解碼：``客戶/台中``。
    name: str
    #: IMAP 裡的原始名稱，沒有引號：``&W6JiNg-/&U,BOLQ-``。
    mailbox: str
    #: 小寫的旗標，例如 ``\\noselect``、``\\all``。
    flags: frozenset[str] = frozenset()

    @property
    def is_all_mail(self) -> bool:
        """這是「所有郵件」嗎？每一封信都在這裡，不管有沒有貼標籤。"""
        return "\\all" in self.flags

    @property
    def is_user_label(self) -> bool:
        """這是使用者自己建的標籤嗎？收件匣、系統信箱、名單匠自己的都不算。"""
        if "\\noselect" in self.flags or self.flags & _SPECIAL_USE:
            return False
        if self.mailbox.upper() == "INBOX" or self.name.startswith(_SYSTEM_PREFIXES):
            return False
        return not (self.name == ROSTER_ROOT or self.name.startswith(ROSTER_ROOT + "/"))


_LIST_LINE = re.compile(
    rb'^\((?P<flags>[^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$'
)


def parse_list_line(line: bytes) -> GmailLabel | None:
    """解析 ``LIST`` 回應的一行。看不懂的回 ``None``。

    ``(\\HasNoChildren) "/" "&W6JiNg-"`` -> ``GmailLabel("客戶", "&W6JiNg-", ...)``
    """
    match = _LIST_LINE.match(line.strip())
    if match is None:
        return None
    raw = match.group("name").decode("ascii", errors="replace").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        raw = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    flags = frozenset(flag.lower() for flag in match.group("flags").decode("ascii").split())
    return GmailLabel(name=decode_mailbox(raw), mailbox=raw, flags=flags)
