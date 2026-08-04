"""把使用者的問題回報寄給作者。

## 為什麼有兩條路

直接用 SMTP 寄需要使用者已經設定好自己的 Gmail。但會想回報問題的人，很多
正是「還沒設定起來」或「設定失敗」的那一群——要求他們先設定完 Gmail 才能
回報「Gmail 設定不起來」，這個功能就等於不存在。

所以沒設定時退回 ``mailto:``：開啟他們自己的郵件軟體，主旨與內文都已經填好，
按寄出即可。代價是附件要自己拖進去（``mailto:`` 沒有帶附件的標準做法），
介面會明講這件事。

## 附上的診斷資訊

版本、作業系統、Python 版本——沒有這些，回報通常要來回問三輪才問得出來。

**不包含任何路徑。** 完整路徑在 Windows 上一定含使用者帳號名稱，而回報的人
不會預期自己的帳號名稱被夾帶出去。
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass, field
from urllib.parse import quote

from core.config import AppConfig, get_config
from core.constants import LogCategory, VERSION
from core.errors import CRMError, GmailError
from core.logging_setup import get_logger

log = get_logger(LogCategory.GUI)


@dataclass(slots=True)
class Feedback:
    """一則待送出的回報。"""

    message: str
    reply_to: str = ""
    #: ``attachments/`` 底下的檔名，通常是使用者剛加進來的截圖。
    attachments: list[str] = field(default_factory=list)

    def subject(self) -> str:
        first_line = self.message.strip().splitlines()[0] if self.message.strip() else ""
        summary = first_line[:40] or "（沒有標題）"
        return f"[Roster 回報] {summary}"

    def body(self) -> str:
        parts = [self.message.strip(), "", "---", diagnostics()]
        if self.reply_to.strip():
            parts.insert(0, f"回信給：{self.reply_to.strip()}\n")
        return "\n".join(parts)


def diagnostics() -> str:
    """附在回報末尾的環境資訊。刻意不含任何路徑。"""
    return "\n".join(
        [
            f"版本：Roster {VERSION}",
            f"作業系統：{platform.system()} {platform.release()}",
            f"Python：{sys.version.split()[0]}",
        ]
    )


def validate(feedback: Feedback) -> None:
    """送出前的檢查。空的回報寄出去只會浪費雙方時間。"""
    if not feedback.message.strip():
        raise CRMError("請先寫下你遇到的問題或建議。")
    reply_to = feedback.reply_to.strip()
    if reply_to:
        from verifier.validators import is_valid_email

        if not is_valid_email(reply_to):
            raise CRMError(f"「{reply_to}」看起來不是有效的信箱，請再確認一次。")


def can_send_directly(config: AppConfig | None = None) -> bool:
    """使用者的 Gmail 設定好了沒。沒有的話要走 mailto: 那條路。"""
    mailer = (config or get_config()).mailer
    try:
        return bool(mailer.address and mailer.app_password)
    except Exception:      # 憑證保管庫不可用等等，一律當作不能直接寄
        return False


def send(feedback: Feedback, config: AppConfig | None = None) -> None:
    """用使用者自己的 Gmail 帳號寄出回報。"""
    config = config or get_config()
    validate(feedback)

    from gmail.attachments import load_for_sending
    from gmail.sender import SmtpSender

    loaded = load_for_sending(feedback.attachments, config) if feedback.attachments else []

    class _Message:
        to_address = config.app.feedback_email
        subject = feedback.subject()
        body = feedback.body()

    try:
        with SmtpSender(config) as sender:
            sender.send(_Message(), loaded)
    except GmailError as exc:
        raise CRMError(f"回報寄送失敗：{exc}") from exc

    log.info("已寄出使用者回報（{} 個附件）", len(loaded))


def mailto_url(feedback: Feedback, config: AppConfig | None = None) -> str:
    """開啟使用者自己郵件軟體的連結，主旨與內文都已填好。

    附件帶不進去——``mailto:`` 沒有這種標準做法，各家郵件軟體也不支援。
    介面必須明講「截圖請自己拖進去」，不能讓使用者以為附件跟著走了。
    """
    config = config or get_config()
    validate(feedback)
    return (
        f"mailto:{config.app.feedback_email}"
        f"?subject={quote(feedback.subject())}"
        f"&body={quote(feedback.body())}"
    )
