"""Turn inbox messages into CRM records.

Business mail carries a signature block, and a signature block is a contact
record: company, name, title, phone, website. This module reads those out of
messages already in the user's own mailbox and feeds them through the same
cleaning and deduplication pipeline as crawled data.

Free-mail senders (``gmail.com`` and friends, configurable) are skipped -- their
domain says nothing about a company.

## 挖跟存是兩步

:func:`scan_for_contacts` 只讀信箱、只判斷，**一個字都不寫**；
:func:`store` 才動資料庫。中間那一步是使用者的眼睛：他看得到每一筆會變成
「新增一家公司」還是「併入某某公司」，勾掉不要的再按下去。

理由跟退信、回覆那兩張表一樣（見 ``controllers/mail.py``）：這些資料是從
**別人寄來的信**裡挖出來的，而簽名檔的格式千奇百怪。猜錯一筆的代價是名單裡
多一家不存在的公司，而且沒有人會發現。

命令列（``python main.py gmail``）走 :func:`harvest_inbox`，一次做完——那裡
沒有一雙眼睛可以看。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from core.config import AppConfig, get_config
from core.constants import ActivityType, LogCategory
from core.errors import CRMError
from core.logging_setup import get_logger
from core.schemas import CleanCompany, RawCompany
from database.repository import ActivityRepository, CompanyRepository, ContactRepository
from database.session import session_scope
from gmail.client import GmailClient, MailMessage
from verifier.dedupe import deduplicate_batch
from verifier.mx import MXChecker
from verifier.normalize import (
    normalize_company_name,
    normalize_person_name,
    normalize_phone,
    normalize_website,
)
from verifier.service import CleaningService

log = get_logger(LogCategory.CRAWL)

# Signature markers, in the order a scanner should trust them.
_SIGNATURE_MARKERS = ("-- ", "--\n", "敬祝", "順頌商祺", "Best regards", "Kind regards", "Regards,")

_COMPANY_LINE = re.compile(
    r"^(?P<name>[^\n]{2,60}?(?:股份有限公司|有限公司|企業社|工作室|實業|科技|"
    r"Co\.?,?\s*Ltd\.?|Inc\.?|Corp\.?|LLC))\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_TITLE_LINE = re.compile(
    r"^(?P<title>[^\n]{0,30}?(?:經理|總監|協理|副理|主任|工程師|業務|專員|負責人|"
    r"Manager|Director|Engineer|Sales|CEO|CTO))\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_PHONE_LINE = re.compile(
    r"(?:TEL|Tel|電話|手機|Mobile|M)\s*[:：]?\s*"
    r"((?:\+886|0)[\d\s\-()]{7,15}(?:\s*(?:#|轉|分機)\s*\d{1,6})?)"
)
_URL_LINE = re.compile(r"(https?://[^\s<>\"]+|www\.[^\s<>\"]+)")


@dataclass
class GmailHarvestSummary:
    """Outcome of one inbox scan."""

    messages_scanned: int = 0
    messages_skipped: int = 0
    records_found: int = 0
    records_new: int = 0
    records_merged: int = 0
    contacts_created: int = 0
    #: 挖出來、但清理那一關沒過的筆數（名稱不像公司、疑似廣告）。
    #:
    #: 以前這個數字被丟掉了：``clean_many`` 回傳的第二個值沒有人接。使用者
    #: 看到「挖到 12 筆、新增 9 家」會以為程式算錯，實際上是有 3 筆被擋掉了
    #: 而沒有人告訴他。
    records_rejected: int = 0
    error: str | None = None

    def describe(self) -> str:
        """存完之後給人看的一句話。"""
        parts = []
        if self.records_new:
            parts.append(f"新增 {self.records_new} 家公司")
        if self.records_merged:
            parts.append(f"{self.records_merged} 家併進已經有的資料裡")
        if self.contacts_created:
            parts.append(f"多了 {self.contacts_created} 位聯絡人")
        return "，".join(parts) or "沒有任何改動"


@dataclass(slots=True)
class HarvestCandidate:
    """一封信裡挖出來的一筆，**還沒有存**。

    ``record`` 是清理過、可以直接 upsert 的那一份；``None`` 代表這一筆過不了
    清理那一關，不會存。
    """

    company_name: str
    email: str | None = None
    phone: str | None = None
    person: str | None = None
    title: str | None = None
    subject: str = ""
    #: 收到時間，ISO 字串（信件標頭給什麼就是什麼，這裡不解析）。
    received: str = ""
    record: CleanCompany | None = None
    #: 名單裡已經有的那一家（用 upsert 同一套比對規則找的）。
    existing: str | None = None
    #: 為什麼不會存。``None`` 代表可以存。
    rejected: str | None = None

    @property
    def storable(self) -> bool:
        return self.record is not None

    @property
    def action(self) -> str:
        """按下去之後實際會發生的事，直接寫在表格那一欄上。"""
        if self.rejected:
            return self.rejected
        if self.existing:
            return f"併入「{self.existing}」"
        return "新增一家公司"


@dataclass(slots=True)
class HarvestScan:
    """讀一次信箱的結果。**唯讀，什麼都沒寫。**"""

    candidates: list[HarvestCandidate] = field(default_factory=list)
    messages_scanned: int = 0
    messages_skipped: int = 0
    #: 同一家公司在這一趟裡重複出現、被併掉的筆數。
    duplicates: int = 0

    def describe(self) -> str:
        parts = [f"看了 {self.messages_scanned} 封信，挖出 {len(self.candidates)} 家公司"]
        if self.messages_skipped:
            parts.append(f"{self.messages_skipped} 封看不出是哪家公司，略過")
        if self.duplicates:
            parts.append(f"{self.duplicates} 封是同一家重複寄來的")
        return "；".join(parts)


def _signature_block(body: str) -> str:
    """The tail of a message most likely to be the signature."""
    if not body:
        return ""
    for marker in _SIGNATURE_MARKERS:
        index = body.rfind(marker)
        if index != -1:
            return body[index : index + 1200]
    # No marker: the last few non-empty lines are the next best guess.
    lines = [line for line in body.splitlines() if line.strip()]
    return "\n".join(lines[-12:])


def message_to_record(
    message: MailMessage, ignore_domains: set[str]
) -> RawCompany | None:
    """Extract a company record from one message, or ``None`` to skip it."""
    if not message.sender_email or "@" not in message.sender_email:
        return None
    if message.sender_domain in ignore_domains:
        return None

    signature = _signature_block(message.body)

    company = message.headers.get("Organization") or ""
    if not company:
        match = _COMPANY_LINE.search(signature)
        if match:
            company = match.group("name")
    if not company:
        # Fall back to the domain: "nissin-electronics.com.tw" -> "Nissin Electronics".
        label = message.sender_domain.split(".")[0]
        company = label.replace("-", " ").replace("_", " ").title()

    phone = None
    phone_match = _PHONE_LINE.search(signature) or _PHONE_LINE.search(message.body)
    if phone_match:
        phone = normalize_phone(phone_match.group(1))

    website = None
    url_match = _URL_LINE.search(signature)
    if url_match:
        website = normalize_website(url_match.group(1))
    if not website:
        website = normalize_website(message.sender_domain)

    title_match = _TITLE_LINE.search(signature)

    return RawCompany(
        company_name=normalize_company_name(company) or company,
        email=message.sender_email,
        phone=phone,
        website=website,
        contact_person=normalize_person_name(message.sender_name),
        source="gmail",
        source_url=f"imap://{message.uid}",
        extra={
            "subject": message.subject,
            "title": title_match.group("title").strip() if title_match else None,
            "received": message.date.isoformat() if message.date else None,
        },
    )


def scan_for_contacts(
    query: str | None = None,
    limit: int | None = None,
    config: AppConfig | None = None,
    client: GmailClient | None = None,
    *,
    report: Callable[[object], None] | None = None,
    cancel_event: Any = None,
) -> HarvestScan:
    """讀一次信箱，把挖得到的公司列出來。**唯讀，什麼都不寫。**

    信箱是唯讀的（``BODY.PEEK``，不標已讀、不刪信、不搬信），資料庫也是。

    順序刻意是「先讀完信箱、再開資料庫交易」：IMAP 那一段可能要幾十秒，整段
    包在交易裡的話 SQLite 會被佔著，同時間背景在跑的爬取就寫不進去。跟
    ``controllers/mail.py`` 的退信掃描同一個理由。

    **清理也在這裡做完**，不是留到存的時候。預覽表上寫「會新增」的那一筆，
    按下去就必須真的是新增——預覽跟結果不一樣的話，那張表就沒有存在的意義。

    ``client`` 給的話就用那一條既有的連線，生命週期歸呼叫的人管。
    """
    config = config or get_config()
    scan = HarvestScan()
    ignore = set(config.gmail.ignore_domains)

    owned_client = client is None
    client = client or GmailClient(config)
    if owned_client:
        client.connect()

    raw: list[RawCompany] = []
    try:
        for message in client.iter_messages(query, limit):
            if cancel_event is not None and cancel_event.is_set():
                raise CRMError("已取消。")
            scan.messages_scanned += 1
            record = message_to_record(message, ignore)
            if record is None:
                scan.messages_skipped += 1
                continue
            raw.append(record)
            if report is not None and scan.messages_scanned % 10 == 0:
                report(f"看過 {scan.messages_scanned} 封了…")
    finally:
        if owned_client:
            client.close()

    unique, scan.duplicates = deduplicate_batch(raw)

    if report is not None and unique:
        report("正在跟現有的名單比對…")

    #: 這一趟裡先出現過的身分訊號 -> 那一筆的公司名稱。
    #:
    #: 同一家公司的兩個人各寄一封信是常有的事（業務跟他的助理），而他們的
    #: 地址不一樣，去重那一關分不出來。少了這一段，預覽會對兩列都寫「新增
    #: 一家公司」，實際存下去卻是一家新增、一家併入——**預覽說謊，而說謊的
    #: 預覽比沒有預覽更糟**。
    seen: dict[str, str] = {}

    def identity_keys(record: CleanCompany) -> list[str]:
        """跟 ``find_match`` 同一組身分訊號。兩邊要看同樣的東西才會得到同樣的答案。"""
        keys = [f"key:{record.dedupe_key}"]
        if record.tax_id:
            keys.append(f"tax:{record.tax_id}")
        if record.email:
            keys.append(f"mail:{record.email}")
        if record.name_key:
            keys.append(f"name:{record.name_key}")
        return keys

    with session_scope() as session:
        repo = CompanyRepository(session)
        mx = MXChecker(config, session) if config.verifier.check_mx else None
        cleaner = CleaningService(config, mx)

        for record in unique:
            cleaned = cleaner.clean(record)
            existing = repo.find_match(cleaned) if cleaned is not None else None
            existing_name = existing.company_name if existing is not None else None
            if cleaned is not None:
                keys = identity_keys(cleaned)
                if existing_name is None:
                    existing_name = next(
                        (seen[key] for key in keys if key in seen), None
                    )
                for key in keys:
                    seen.setdefault(key, existing_name or cleaned.company_name)
            scan.candidates.append(
                HarvestCandidate(
                    company_name=cleaned.company_name if cleaned else record.company_name,
                    email=(cleaned.email if cleaned else record.email),
                    phone=(cleaned.phone if cleaned else record.phone),
                    person=record.contact_person,
                    title=record.extra.get("title"),
                    subject=record.extra.get("subject") or "",
                    received=record.extra.get("received") or "",
                    record=cleaned,
                    existing=existing_name,
                    # 清理那一關不會說明理由（它只回 None），但日誌裡有：
                    # 名稱不合用或被廣告過濾擋下來。這裡講得夠讓人知道要去哪看。
                    rejected=None if cleaned else "不會存：看起來不是公司資料",
                )
            )

    log.info(
        "gmail scan: {} 封看過、{} 封略過、挖出 {} 家",
        scan.messages_scanned, scan.messages_skipped, len(scan.candidates),
    )
    return scan


def store_candidates(
    candidates: Iterable[HarvestCandidate],
    config: AppConfig | None = None,
    *,
    tag: str | None = None,
    report: Callable[[object], None] | None = None,
    cancel_event: Any = None,
) -> GmailHarvestSummary:
    """把選好的那幾筆存進名單。**這是唯一會寫資料庫的一步。**

    收到的是 :func:`scan_for_contacts` 挖出來、而且使用者勾起來的那些。清理
    在掃描時就做完了，這裡不再做一次——做兩次的話兩次的結果可以不一樣，而
    使用者看到的是第一次的。

    ``tag`` 有值時（從某個 Gmail 標籤挖的），存進去的每一家都貼上這個標籤。
    """
    config = config or get_config()
    summary = GmailHarvestSummary()

    chosen = list(candidates)
    storable = [c for c in chosen if c.record is not None]
    summary.records_found = len(storable)
    summary.records_rejected = len(chosen) - len(storable)
    if not storable:
        return summary

    with session_scope() as session:
        repo = CompanyRepository(session)
        contact_repo = ContactRepository(session)
        activity_repo = ActivityRepository(session)
        # 用前後差額算而不是逐筆累加：聯絡人可能在這裡建立，也可能是 upsert
        # 從解析出來的名字建的，而「這一趟多了幾位聯絡人」指的是總數。
        contacts_before = contact_repo.count()

        for candidate in storable:
            if cancel_event is not None and cancel_event.is_set():
                raise CRMError("已取消。")
            record = candidate.record
            assert record is not None
            company, merged = repo.upsert(record)
            if tag:
                repo.add_tags(company, [tag])
            if merged:
                summary.records_merged += 1
            else:
                summary.records_new += 1
                activity_repo.add(
                    company.id,
                    ActivityType.SYSTEM,
                    subject="從收件匣挖到的",
                    body=(
                        f"來自 {record.email or candidate.email or '一封信'} 寄來的信"
                        + (f"（主旨：{candidate.subject}）" if candidate.subject else "")
                    ),
                )

            person = candidate.person
            if person:
                contact = next(
                    (c for c in company.contacts if (c.name or "").strip() == person),
                    None,
                )
                if contact is None:
                    contact = contact_repo.add(
                        company.id,
                        name=person,
                        email=record.email,
                        phone=record.phone,
                        is_primary=not company.contacts,
                    )
                # 職稱只會出現在信件簽名檔裡，所以這是唯一補得到它的地方
                # ——upsert 可能已經從解析出來的名字建過這一列了。
                if not contact.title:
                    contact.title = candidate.title

        session.flush()
        summary.contacts_created = contact_repo.count() - contacts_before

    if report is not None:
        report(summary.describe())
    log.info(
        "gmail harvest stored: {} new, {} merged, {} contacts",
        summary.records_new, summary.records_merged, summary.contacts_created,
    )
    return summary


def harvest_inbox(
    query: str | None = None,
    limit: int | None = None,
    config: AppConfig | None = None,
    client: GmailClient | None = None,
) -> GmailHarvestSummary:
    """掃一次信箱並把挖到的東西全部存起來。

    命令列（``python main.py gmail``）用的。畫面上走的是分開的兩步——那裡有
    一張預覽表可以看，命令列沒有。

    ``client`` is injectable so tests can supply a fake without an IMAP server.
    """
    scan = scan_for_contacts(query, limit, config, client)
    summary = store_candidates(scan.candidates, config)
    summary.messages_scanned = scan.messages_scanned
    summary.messages_skipped = scan.messages_skipped
    return summary
