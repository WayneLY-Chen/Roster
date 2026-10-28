"""Gmail 標籤：讀進來分類公司、從標籤挖新公司、把分類寫回 Gmail。

整份用一個假的 Gmail（:class:`FakeGmail`）跑，不連網路。它照 Gmail 的行為
運作：標籤就是信箱、名稱用修改版 UTF-7、COPY 到標籤就是加上那個標籤。
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest

from controllers.labels import GmailLabelController
from core.constants import EmailVerdict
from database.repository import CompanyRepository
from gmail.mailbox import (
    GmailLabel,
    decode_mailbox,
    encode_mailbox,
    parse_list_line,
)

ME = "me@mycompany.com.tw"
ALL_MAIL = "[Gmail]/" + encode_mailbox("所有郵件")


class FakeGmail:
    """一個很小的 Gmail。``messages`` 的 key 是 UID，值是標頭與標籤。"""

    def __init__(self) -> None:
        self.messages: dict[int, dict] = {}
        self.labels: set[str] = {encode_mailbox("客戶"), encode_mailbox("供應商")}
        self.selected: str | None = None
        self.readonly = True
        self.writes: list[tuple] = []

    def add(self, uid: int, sender: str, to: str, labels: tuple[str, ...] = (), body: str = ""):
        self.messages[uid] = {
            "from": sender, "to": to, "labels": {encode_mailbox(l) for l in labels}, "body": body,
        }

    # --- imaplib.IMAP4_SSL 的介面 -------------------------------------

    def __call__(self, host, port, ssl_context=None):
        return self

    def login(self, user, password):
        return "OK", [b"logged in"]

    def list(self):
        lines = [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
            f'(\\All \\HasNoChildren) "/" "{ALL_MAIL}"'.encode(),
            b'(\\Sent \\HasNoChildren) "/" "[Gmail]/Sent Mail"',
        ]
        lines += [f'(\\HasNoChildren) "/" "{name}"'.encode() for name in sorted(self.labels)]
        return "OK", lines

    def select(self, mailbox, readonly=False):
        name = mailbox.strip('"')
        if name not in ("INBOX", ALL_MAIL) and name not in self.labels:
            return "NO", [b"no such mailbox"]
        self.selected, self.readonly = name, readonly
        return "OK", [b"1"]

    def create(self, mailbox):
        name = mailbox.strip('"')
        if name in self.labels:
            return "NO", [b"[ALREADYEXISTS]"]
        self.labels.add(name)
        self.writes.append(("CREATE", decode_mailbox(name)))
        return "OK", [b"created"]

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [" ".join(str(u) for u in self._search(args[1])).encode()]
        if command == "FETCH":
            return "OK", self._fetch(args[0], args[1])
        if command == "COPY":
            label = args[1].strip('"')
            for uid in args[0].split(","):
                self.messages[int(uid)]["labels"].add(label)
            self.writes.append(("COPY", decode_mailbox(label), args[0]))
            return "OK", [b"copied"]
        raise AssertionError(f"unexpected IMAP command {command}")

    def close(self):
        return "OK", []

    def logout(self):
        return "BYE", []

    # --- 內部 ---------------------------------------------------------

    def _visible(self) -> list[int]:
        if self.selected == ALL_MAIL:
            return sorted(self.messages)
        if self.selected == "INBOX":
            return sorted(u for u, m in self.messages.items() if not m["labels"])
        return sorted(u for u, m in self.messages.items() if self.selected in m["labels"])

    def _search(self, criterion: str) -> list[int]:
        if criterion == "ALL":
            return self._visible()
        term = re.match(r'OR FROM "([^"]+)" TO "([^"]+)"', criterion).group(1)
        return [
            uid for uid in self._visible()
            if term in self.messages[uid]["from"] or term in self.messages[uid]["to"]
        ]

    def _fetch(self, uid_set: str, items: str) -> list:
        out: list = []
        for uid in uid_set.split(","):
            message = self.messages[int(uid)]
            if "HEADER.FIELDS" in items:
                data = f"From: {message['from']}\r\nTo: {message['to']}\r\n\r\n".encode()
            else:
                data = (
                    f"From: {message['from']}\r\nTo: {message['to']}\r\nSubject: hi\r\n"
                    f"Date: Mon, 1 Jun 2026 10:00:00 +0800\r\n\r\n{message['body']}"
                ).encode()
            out += [(f"{uid} (UID {uid} BODY[] {{{len(data)}}}".encode(), data), b")"]
        return out


@pytest.fixture
def gmail(monkeypatch):
    fake = FakeGmail()
    monkeypatch.setattr("gmail.client.imaplib.IMAP4_SSL", fake)
    monkeypatch.setenv("GMAIL_ADDRESS", ME)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    return fake


@pytest.fixture
def controller(patch_config, gmail):
    return GmailLabelController(patch_config)


def _company(db_session, name: str, **fields):
    company = CompanyRepository(db_session).create(
        company_name=name, name_key=name, dedupe_key=f"n:{name}", **fields
    )
    db_session.commit()
    return company


def _tags(db_session, company) -> set[str]:
    db_session.refresh(company)
    return {tag.name for tag in company.tags}


# ------------------------------------------------------------- 標籤名稱


def test_chinese_label_names_round_trip_through_imap_encoding():
    for name in ("客戶", "客戶/台中", "A&B 公司", "名單匠/已回覆", "Work"):
        assert decode_mailbox(encode_mailbox(name)) == name
    assert encode_mailbox("客戶") == "&W6JiNg-"
    assert encode_mailbox("A&B") == "A&-B"


def test_only_user_labels_are_offered():
    def label(line: bytes) -> GmailLabel:
        parsed = parse_list_line(line)
        assert parsed is not None
        return parsed

    assert label(b'(\\HasNoChildren) "/" "&W6JiNg-"').is_user_label
    assert not label(b'(\\HasNoChildren) "/" "INBOX"').is_user_label
    assert not label(b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"').is_user_label
    assert not label(b'(\\HasChildren \\Noselect) "/" "[Gmail]"').is_user_label
    roster = encode_mailbox("名單匠/客戶")
    assert not label(f'(\\HasNoChildren) "/" "{roster}"'.encode()).is_user_label


def test_the_label_list_hides_system_mailboxes(controller, gmail):
    assert controller.labels() == ["供應商", "客戶"]


# ------------------------------------------------------------- 讀進來：分類


def test_companies_in_a_gmail_label_get_the_same_tag(db_session, controller, gmail):
    precision = _company(db_session, "測試精密", email="sales@precision.com.tw")
    boss = _company(db_session, "宏達五金", email="hongda@gmail.com")
    other = _company(db_session, "無關企業", email="info@other.com.tw")
    gmail.add(1, "Lin <lin@precision.com.tw>", ME, labels=("客戶",))     # 同網域的另一個人
    gmail.add(2, ME, "hongda@gmail.com", labels=("客戶",))             # 我寄出去的
    gmail.add(3, "stranger@gmail.com", ME, labels=("客戶",))           # gmail 不能用網域比
    gmail.add(4, "info@other.com.tw", ME, labels=("供應商",))           # 別的標籤

    scan = controller.scan_classify("客戶", 100)

    assert {m.company_name for m in scan.matches} == {"測試精密", "宏達五金"}
    assert scan.unmatched == 1
    assert gmail.writes == []  # 預覽什麼都沒寫

    assert controller.apply_classify(scan.matches) == 2
    assert _tags(db_session, precision) == {"客戶"}
    assert _tags(db_session, boss) == {"客戶"}
    assert _tags(db_session, other) == set()


def test_classifying_keeps_the_tags_a_company_already_has(db_session, controller, gmail):
    company = _company(db_session, "測試精密", email="sales@precision.com.tw")
    CompanyRepository(db_session).set_tags(company.id, ["展覽認識"])
    db_session.commit()
    gmail.add(1, "sales@precision.com.tw", ME, labels=("客戶",))

    controller.apply_classify(controller.scan_classify("客戶", 100).matches)

    assert _tags(db_session, company) == {"展覽認識", "客戶"}


def test_a_domain_shared_by_two_companies_is_not_guessed(db_session, controller, gmail):
    _company(db_session, "集團 A", email="a@group.com.tw")
    _company(db_session, "集團 B", website="https://group.com.tw")
    gmail.add(1, "someone@group.com.tw", ME, labels=("客戶",))

    assert controller.scan_classify("客戶", 100).matches == []


# ------------------------------------------------------------- 讀進來：挖新公司


def test_companies_harvested_from_a_label_are_tagged_with_it(db_session, controller, gmail):
    gmail.add(
        1, "Wang <wang@newco.com.tw>", ME, labels=("客戶",),
        body="您好\n\n-- \n王大明\n新創科技股份有限公司\nTel: 02-2723-1234\nwww.newco.com.tw",
    )
    gmail.add(2, "x@ignored.com.tw", ME, body="收件匣裡的信不該被看到")

    scan = controller.scan_harvest("客戶", 50)
    assert [c.company_name for c in scan.candidates] == ["新創科技股份有限公司"]

    controller.apply_harvest(scan.candidates, "客戶")

    company = CompanyRepository(db_session).search()[0]
    assert company.company_name == "新創科技股份有限公司"
    assert {tag.name for tag in company.tags} == {"客戶"}


# ------------------------------------------------------------- 寫回去


def test_tags_and_status_are_written_back_as_roster_labels(db_session, controller, gmail):
    client = _company(db_session, "測試精密", email="sales@precision.com.tw")
    CompanyRepository(db_session).set_tags(client.id, ["VIP"])
    client.last_replied_at = datetime(2026, 6, 1)
    bounced = _company(db_session, "退信公司", email="old@gone.com.tw")
    bounced.email_verdict = EmailVerdict.BOUNCED.value
    db_session.commit()
    gmail.add(1, "sales@precision.com.tw", ME)
    gmail.add(2, ME, "boss@precision.com.tw", labels=("客戶",))
    gmail.add(3, "x@precision.com.tw.evil.example", ME)  # 子字串對得上，地址不是
    gmail.add(4, ME, "old@gone.com.tw")

    preview = controller.sync(include_tags=True, include_status=True, apply=False)
    counts = {c.label: c.messages for c in preview.counts}
    assert counts == {"名單匠/VIP": 2, "名單匠/已回覆": 2, "名單匠/退信": 1}
    assert gmail.writes == []  # 預覽什麼都沒貼

    controller.sync(include_tags=True, include_status=True, apply=True)

    vip, replied, gone = (encode_mailbox(n) for n in ("名單匠/VIP", "名單匠/已回覆", "名單匠/退信"))
    assert vip in gmail.messages[1]["labels"] and replied in gmail.messages[1]["labels"]
    assert vip in gmail.messages[2]["labels"]
    assert encode_mailbox("客戶") in gmail.messages[2]["labels"]  # 原本的標籤還在
    assert gmail.messages[3]["labels"] == set()
    assert gone in gmail.messages[4]["labels"]
    # 只建標籤、只複製，從來沒有刪除或改旗標。
    assert {write[0] for write in gmail.writes} == {"CREATE", "COPY"}


def test_status_only_leaves_ordinary_tags_alone(db_session, controller, gmail):
    company = _company(db_session, "測試精密", email="sales@precision.com.tw")
    CompanyRepository(db_session).set_tags(company.id, ["VIP"])
    company.do_not_contact = True
    db_session.commit()
    gmail.add(1, "sales@precision.com.tw", ME)

    result = controller.sync(include_tags=False, include_status=True, apply=False)

    assert [c.label for c in result.counts] == ["名單匠/不再聯絡"]


def test_companies_without_any_address_are_counted_not_guessed(db_session, controller, gmail):
    company = _company(db_session, "只有名字")
    CompanyRepository(db_session).set_tags(company.id, ["VIP"])
    db_session.commit()

    result = controller.sync(include_tags=True, include_status=False, apply=False)

    assert result.counts == []
    assert result.no_address == 1
