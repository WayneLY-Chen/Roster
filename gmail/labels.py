"""Gmail 標籤與名單匠的分類互通：讀進來、也寫回去。

## 讀進來（Gmail 標籤 → 名單匠標籤）

:func:`scan_label` 看一個 Gmail 標籤裡的信，把寄件者／收件者對到名單裡的公司，
列出「哪家公司會貼上哪個標籤」；使用者看過再按 :func:`apply_tags` 才寫進去。
挖新公司那一半沿用 :mod:`gmail.harvester`，只是改讀這個標籤。

## 寫回去（名單匠 → Gmail 標籤）

:func:`sync_labels` 把名單匠的分類貼回 Gmail：公司的每個標籤變成
``名單匠/<標籤>``，狀態變成 ``名單匠/已回覆``、``名單匠/退信``、
``名單匠/不再聯絡``，貼在跟那家公司往來過的每一封信上。

只會**新增**標籤：不刪信、不搬信、不改已讀，原本的標籤都還在。全部放在
``名單匠/`` 底下，不要了在 Gmail 裡把那一層刪掉就乾淨。

## 「跟這家公司往來過的信」怎麼算

公司網域的信箱用網域比對（``@abc.com.tw`` 寄來或寄去的都算）；gmail、hinet
這類免費信箱只能用完整地址比對——否則每一個用 gmail 的人都會被當成同一家
公司。IMAP 的搜尋是子字串比對（``@abc.com`` 也會找到 ``@abc.com.tw``），所以
搜尋完會再抓一次標頭，確認地址真的對得上。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from core.constants import EmailVerdict, LogCategory
from core.errors import CRMError
from core.logging_setup import get_logger
from database.repository import CompanyRepository
from database.session import session_scope
from gmail.client import GmailClient
from gmail.mailbox import ROSTER_ROOT, GmailLabel
from verifier.normalize import website_host
from verifier.validators import is_free_mail

log = get_logger(LogCategory.CRAWL)

#: 依狀態貼的標籤（``名單匠/`` 底下的名稱）。
STATUS_REPLIED = "已回覆"
STATUS_BOUNCED = "退信"
STATUS_DO_NOT_CONTACT = "不再聯絡"


def roster_label(name: str) -> str:
    """名單匠寫進 Gmail 的標籤名稱。``客戶`` -> ``名單匠/客戶``。"""
    return f"{ROSTER_ROOT}/{name}"


# ------------------------------------------------------------- 地址比對


@dataclass(frozen=True, slots=True)
class _Party:
    """名單裡的一家公司，以及代表它的地址。"""

    company_id: int
    company_name: str
    #: 完整地址（免費信箱只能這樣比）。
    emails: frozenset[str]
    #: 公司自己的網域。
    domains: frozenset[str]

    def owns(self, address: str) -> bool:
        address = address.lower()
        return address in self.emails or address.rpartition("@")[2] in self.domains

    def search_terms(self) -> list[str]:
        return sorted(self.emails | {"@" + domain for domain in self.domains})


def _party(company) -> _Party | None:
    """一家公司拿來比對信件的地址。一個都沒有就回 ``None``。"""
    emails: set[str] = set()
    domains: set[str] = set()
    email = (company.email or "").strip().lower()
    if "@" in email:
        if is_free_mail(email):
            emails.add(email)
        else:
            domains.add(email.rpartition("@")[2])
    host = website_host(company.website)
    if host and not is_free_mail("x@" + host):
        domains.add(host)
    if not emails and not domains:
        return None
    return _Party(company.id, company.company_name, frozenset(emails), frozenset(domains))


class _Directory:
    """地址 -> 名單裡的哪一家。同一個網域對到好幾家時不猜，當成對不上。"""

    def __init__(self, parties: Iterable[_Party]) -> None:
        self._by_email: dict[str, _Party] = {}
        self._by_domain: dict[str, list[_Party]] = defaultdict(list)
        for party in parties:
            for email in party.emails:
                self._by_email[email] = party
            for domain in party.domains:
                self._by_domain[domain].append(party)

    def find(self, address: str) -> _Party | None:
        address = address.lower()
        if address in self._by_email:
            return self._by_email[address]
        candidates = self._by_domain.get(address.rpartition("@")[2], [])
        return candidates[0] if len(candidates) == 1 else None


# ------------------------------------------------- 讀進來：Gmail 標籤 → 名單匠


@dataclass(slots=True)
class TagMatch:
    """一家公司會貼上一個標籤。"""

    company_id: int
    company_name: str
    tag: str
    #: 對上的那個地址，讓使用者看得出為什麼是這一家。
    address: str
    #: 這家公司已經有這個標籤了。
    already: bool = False


@dataclass(slots=True)
class LabelScan:
    """看一個 Gmail 標籤的結果。**唯讀，什麼都沒寫。**"""

    label: str
    matches: list[TagMatch] = field(default_factory=list)
    messages_scanned: int = 0
    #: 對不上名單裡任何一家的地址有幾個。
    unmatched: int = 0

    def describe(self) -> str:
        new = sum(1 for match in self.matches if not match.already)
        parts = [
            f"「{self.label}」看了 {self.messages_scanned} 封信，"
            f"對到名單裡 {len(self.matches)} 家公司（{new} 家還沒有這個標籤）"
        ]
        if self.unmatched:
            parts.append(f"{self.unmatched} 個地址不在名單裡，可以用「從標籤挖新公司」")
        return "；".join(parts)


def scan_label(
    client: GmailClient,
    label: GmailLabel,
    limit: int,
    own_address: str = "",
    *,
    report: Callable[[object], None] | None = None,
) -> LabelScan:
    """看 ``label`` 裡最近 ``limit`` 封信，列出會替哪些公司貼上這個標籤。"""
    scan = LabelScan(label=label.name)
    client.open_mailbox(label.mailbox)
    uids = client.search_all("ALL")[:limit]
    if report is not None:
        report(f"「{label.name}」有 {len(uids)} 封信要看…")
    addresses = client.fetch_addresses(uids)
    scan.messages_scanned = len(addresses)

    with session_scope() as session:
        companies = CompanyRepository(session).all()
        directory = _Directory(p for p in map(_party, companies) if p is not None)
        tagged = {
            company.id
            for company in companies
            if any(tag.name.lower() == label.name.lower() for tag in company.tags)
        }

    own = own_address.lower()
    matched: dict[int, TagMatch] = {}
    unmatched: set[str] = set()
    for message_addresses in addresses.values():
        for address in message_addresses:
            if address == own:
                continue
            party = directory.find(address)
            if party is None:
                unmatched.add(address)
            elif party.company_id not in matched:
                matched[party.company_id] = TagMatch(
                    party.company_id, party.company_name, label.name, address,
                    already=party.company_id in tagged,
                )
    scan.matches = sorted(matched.values(), key=lambda match: match.company_name)
    scan.unmatched = len(unmatched)
    return scan


def apply_tags(matches: Iterable[TagMatch]) -> int:
    """把勾起來的那幾筆標籤真的貼上。回傳實際多了標籤的公司數。"""
    added = 0
    with session_scope() as session:
        repo = CompanyRepository(session)
        for match in matches:
            company = repo.get(match.company_id)
            if company is not None and repo.add_tags(company, [match.tag]):
                added += 1
    return added


# --------------------------------------------------- 寫回去：名單匠 → Gmail


@dataclass(slots=True)
class LabelCount:
    """一個 Gmail 標籤會貼在幾家公司、幾封信上。"""

    label: str
    companies: int = 0
    messages: int = 0


@dataclass(slots=True)
class SyncResult:
    """寫回 Gmail 的結果。``applied`` 為 ``False`` 時只是預覽，什麼都沒貼。"""

    counts: list[LabelCount] = field(default_factory=list)
    applied: bool = False
    #: 有分類、但沒有信箱也沒有網站，找不到往來信件的公司。
    no_address: int = 0

    def describe(self) -> str:
        if not self.counts:
            return "沒有要貼的標籤：名單裡的公司都還沒有分類或狀態。"
        messages = sum(count.messages for count in self.counts)
        verb = "貼上了" if self.applied else "會貼上"
        text = f"{verb} {len(self.counts)} 個 Gmail 標籤，共 {messages} 封信"
        if self.no_address:
            text += f"；{self.no_address} 家沒有信箱也沒有網站，找不到往來的信"
        return text


def _status_labels(company) -> list[str]:
    labels = []
    if company.last_replied_at is not None:
        labels.append(STATUS_REPLIED)
    if company.email_verdict == EmailVerdict.BOUNCED.value:
        labels.append(STATUS_BOUNCED)
    if company.do_not_contact:
        labels.append(STATUS_DO_NOT_CONTACT)
    return labels


def _targets(include_tags: bool, include_status: bool) -> tuple[list[tuple[_Party, list[str]]], int]:
    """每家有分類的公司，以及它要貼的 Gmail 標籤。"""
    targets: list[tuple[_Party, list[str]]] = []
    no_address = 0
    with session_scope() as session:
        for company in CompanyRepository(session).all():
            names: list[str] = []
            if include_tags:
                names += [tag.name for tag in company.tags]
            if include_status:
                names += _status_labels(company)
            if not names:
                continue
            party = _party(company)
            if party is None:
                no_address += 1
                continue
            targets.append((party, sorted({roster_label(name) for name in names})))
    return targets, no_address


def _messages_with(client: GmailClient, party: _Party) -> set[str]:
    """「所有郵件」裡跟這家公司往來過的信。"""
    candidates: set[str] = set()
    for term in party.search_terms():
        candidates.update(client.search_all(f'OR FROM "{term}" TO "{term}"'))
    if not candidates:
        return set()
    # 搜尋是子字串比對，再看一次標頭才知道地址是不是真的這一家。
    addresses = client.fetch_addresses(sorted(candidates))
    return {uid for uid, found in addresses.items() if any(party.owns(a) for a in found)}


def sync_labels(
    client: GmailClient,
    *,
    include_tags: bool,
    include_status: bool,
    apply: bool,
    report: Callable[[object], None] | None = None,
    cancel_event: Any = None,
) -> SyncResult:
    """把名單匠的分類貼回 Gmail。``apply=False`` 只算數字、不貼。"""
    result = SyncResult(applied=apply)
    targets, result.no_address = _targets(include_tags, include_status)
    if not targets:
        return result

    client.open_all_mail(writable=apply)
    per_label: dict[str, set[str]] = defaultdict(set)
    companies: dict[str, int] = defaultdict(int)
    for index, (party, labels) in enumerate(targets, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise CRMError("已取消。")
        if report is not None:
            report(f"找往來的信 {index}/{len(targets)}：{party.company_name}")
        uids = _messages_with(client, party)
        if not uids:
            continue
        for label in labels:
            per_label[label] |= uids
            companies[label] += 1

    for label in sorted(per_label):
        if apply:
            if report is not None:
                report(f"貼上「{label}」…")
            client.add_label(sorted(per_label[label], key=int), label)
        result.counts.append(LabelCount(label, companies[label], len(per_label[label])))

    log.info("gmail labels {}: {}", "applied" if apply else "preview", result.describe())
    return result
