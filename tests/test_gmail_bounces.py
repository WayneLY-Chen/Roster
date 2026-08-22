"""Tests for gmail/bounces.py 與 controllers.mail.BounceController。

**這個檔案守的是三件事：**

1. **只認自己寄過的地址。** 收件匣裡任何一封信都不能改動名單——不然只要寄一封
   長得像退信的信給使用者，就能讓他從此不再聯絡某一家公司。那是一個從外面打
   得到的洞。
2. **軟退信不標死。** ``4.x.x`` 是暫時失敗（信箱滿了、對方伺服器忙），過幾天
   就好了。標死一個真實客戶的代價是從此再也不寄給他，而他不會知道。
3. **標了就要真的生效。** 標成「退過信」之後，那家公司必須出現在寄送計畫的
   「跳過」清單裡——而且不管使用者有沒有打開信箱驗證。漏了這一條，前面兩條
   都只是在資料庫裡寫字。

另外驗一條容易漏的：**重新驗證不能把退信洗掉**。MX 查詢只證明那個網域收信，
證明不了那個信箱存在；讓它把退信升級回 Valid 的話，使用者按一次「重新驗證」
就會再寄一輪死信箱。
"""

from __future__ import annotations

import email
from datetime import datetime

import pytest

from controllers.mail import BounceController
from core.constants import EmailStatus, EmailVerdict, SkipReason
from database.models import Company, EmailMessage
from gmail.bounces import (
    Bounce,
    iter_bounces,
    looks_like_bounce,
    parse_bounce,
)

# --------------------------------------------------------------- 假的退信

HARD_DSN = """\
From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>
To: me@example.com
Subject: Delivery Status Notification (Failure)
Date: Mon, 18 Aug 2026 09:12:44 +0800
Content-Type: multipart/report; report-type=delivery-status; boundary="B"

--B
Content-Type: text/plain; charset=UTF-8

Address not found. Your message wasn't delivered to dead@factory.example.

--B
Content-Type: message/delivery-status

Reporting-MTA: dns; googlemail.com

Final-Recipient: rfc822; dead@factory.example
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550 5.1.1 The email account that you tried to reach does not exist.

--B--
"""

SOFT_DSN = """\
From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>
To: me@example.com
Subject: Delivery Status Notification (Delay)
Date: Mon, 18 Aug 2026 09:20:00 +0800
Content-Type: multipart/report; report-type=delivery-status; boundary="B"

--B
Content-Type: message/delivery-status

Final-Recipient: rfc822; busy@factory.example
Action: delayed
Status: 4.2.2
Diagnostic-Code: smtp; 452 4.2.2 The recipient's inbox is out of storage space.

--B--
"""

DELIVERED_DSN = """\
From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>
Subject: Delivery Status Notification (Success)
Content-Type: multipart/report; report-type=delivery-status; boundary="B"

--B
Content-Type: message/delivery-status

Final-Recipient: rfc822; fine@factory.example
Action: delivered
Status: 2.0.0

--B--
"""

TWO_RECIPIENTS = """\
From: postmaster@relay.example
Subject: Undeliverable
Content-Type: multipart/report; report-type=delivery-status; boundary="B"

--B
Content-Type: message/delivery-status

Final-Recipient: rfc822; one@factory.example
Action: failed
Status: 5.0.0

Final-Recipient: rfc822; two@factory.example
Action: failed
Status: 5.1.1

--B--
"""

PLAIN_BOUNCE = """\
From: MAILER-DAEMON@oldserver.example
Subject: Returned mail: see transcript for details
Content-Type: text/plain; charset=UTF-8

The original message was received but could not be delivered.

   ----- The following addresses had permanent fatal errors -----
<gone@factory.example>
    (reason: 550 5.1.1 user unknown)
"""

NOT_A_BOUNCE = """\
From: 王小明 <ming@factory.example>
Subject: Re: 合作洽詢
Content-Type: text/plain; charset=UTF-8

您好，我們對貴公司的產品有興趣。
"""


def _message(raw: str):
    return email.message_from_string(raw)


# ------------------------------------------------------------------ 認得出來


def test_a_normal_reply_is_not_a_bounce():
    """把一封真的回信當成退信，會把一個正在談的客戶標死。"""
    assert not looks_like_bounce(_message(NOT_A_BOUNCE))
    assert parse_bounce(_message(NOT_A_BOUNCE)) == []


@pytest.mark.parametrize("raw", [HARD_DSN, SOFT_DSN, PLAIN_BOUNCE, TWO_RECIPIENTS])
def test_the_usual_shapes_are_recognised(raw):
    assert looks_like_bounce(_message(raw))


def test_a_hard_bounce_carries_its_code_and_reason():
    bounce = parse_bounce(_message(HARD_DSN), uid="7")[0]

    assert bounce.address == "dead@factory.example"
    assert bounce.hard
    assert bounce.code == "5.1.1"
    assert "does not exist" in bounce.reason
    assert bounce.uid == "7"
    assert isinstance(bounce.received_at, datetime)


def test_a_soft_bounce_is_not_hard():
    """**這一條是那條「軟退信不標死」的第一道關。**"""
    bounce = parse_bounce(_message(SOFT_DSN))[0]

    assert bounce.address == "busy@factory.example"
    assert not bounce.hard
    assert bounce.code == "4.2.2"
    assert bounce.kind == "軟退信"


def test_a_delivered_report_is_not_a_bounce():
    """DSN 也會用來報「送到了」。

    把成功的那幾封當成退信處理，會把好好的信箱標死，而且完全看不出來為什麼。
    """
    assert parse_bounce(_message(DELIVERED_DSN)) == []


def test_one_notice_can_report_several_addresses():
    bounces = parse_bounce(_message(TWO_RECIPIENTS))

    assert [item.address for item in bounces] == [
        "one@factory.example",
        "two@factory.example",
    ]
    assert all(item.hard for item in bounces)


def test_an_old_style_bounce_falls_back_to_reading_the_text():
    """沒有 DSN 區塊的舊伺服器也還在，而且它們退回來的信一樣要處理。"""
    bounces = parse_bounce(
        _message(PLAIN_BOUNCE), sent_to=["gone@factory.example"]
    )

    assert [item.address for item in bounces] == ["gone@factory.example"]
    assert bounces[0].hard


def test_the_text_fallback_only_picks_addresses_we_sent_to():
    """退路那條寬鬆得多，所以出口要窄。

    退信內文裡常常夾著原信的完整標頭——寄件者、副本、簽名檔裡的地址全在裡面。
    不篩的話會把自己的地址、甚至客戶簽名檔裡同事的地址一起標成死信箱。
    """
    bounces = parse_bounce(_message(PLAIN_BOUNCE), sent_to=["someone@else.example"])

    assert bounces == []


def test_a_daemon_address_never_becomes_a_victim():
    """mailer-daemon 自己的地址不是收件人。"""
    bounces = parse_bounce(_message(PLAIN_BOUNCE))

    assert all("mailer-daemon" not in item.address for item in bounces)


# ------------------------------------------------------------------ 掃信箱


class _FakeClient:
    """假的 IMAP。這個模組要驗的是解析，不是 imaplib。"""

    def __init__(self, messages: dict[str, str]) -> None:
        self.messages = messages
        self.queries: list[str] = []

    def search(self, query, limit):
        self.queries.append(query)
        return list(self.messages)[:limit]

    def fetch_raw(self, uid):
        raw = self.messages.get(uid)
        return raw.encode("utf-8") if raw else None


def test_scanning_walks_every_message_and_skips_the_broken_one():
    """一封解析不了的信不該讓整次掃描停下來。"""
    client = _FakeClient({"1": HARD_DSN, "2": "\x00 這不是一封信", "3": SOFT_DSN})

    found = list(iter_bounces(client))

    assert {item.address for item in found} == {
        "dead@factory.example",
        "busy@factory.example",
    }


def test_the_scan_reports_progress_per_message():
    seen: list[str] = []
    client = _FakeClient({"1": HARD_DSN, "2": SOFT_DSN})

    list(iter_bounces(client, on_message=seen.append))

    assert seen == ["1", "2"]


# ------------------------------------------------- 只認自己寄過的地址


def _sent(session, company: Company, address: str) -> None:
    session.add(
        EmailMessage(
            company_id=company.id,
            to_address=address,
            subject="開發信",
            status=EmailStatus.SENT.value,
            sent_at=datetime(2026, 8, 17, 10, 0),
        )
    )
    session.flush()


@pytest.fixture
def bounce_world(db_session, monkeypatch, tmp_config):
    """一家寄過信的公司，加上一個假的信箱。"""
    company = Company(
        company_name="遠東鑄造股份有限公司",
        email="dead@factory.example",
        dedupe_key="b1",
        email_verdict=EmailVerdict.VALID.value,
    )
    db_session.add(company)
    db_session.flush()
    _sent(db_session, company, "dead@factory.example")
    db_session.commit()

    def use(raw_by_uid: dict[str, str]):
        import gmail.client as client_module

        from contextlib import contextmanager

        @contextmanager
        def fake_session(_config=None):
            yield _FakeClient(raw_by_uid)

        monkeypatch.setattr(client_module, "gmail_session", fake_session)
        return BounceController(tmp_config)

    return company, use


def test_a_bounce_for_an_address_we_never_sent_to_is_ignored(bounce_world):
    """**這是那條「只認自己寄過的地址」。**

    少了它，只要寄一封長得像退信的信給使用者，就能讓他從此不再聯絡某一家公司。
    """
    _company, use = bounce_world
    stranger = HARD_DSN.replace("dead@factory.example", "stranger@nowhere.example")

    scan = use({"1": stranger}).scan()

    assert scan.hits == []


def test_a_bounce_we_did_send_lands_on_the_right_company(bounce_world):
    company, use = bounce_world

    scan = use({"1": HARD_DSN}).scan()

    assert len(scan.hits) == 1
    hit = scan.hits[0]
    assert hit.company_id == company.id
    assert hit.company_name == company.company_name
    assert hit.bounce.hard
    assert not hit.already


def test_the_scan_writes_nothing_by_itself(bounce_world, db_session):
    """看過才寫。掃描只是列出來。"""
    company, use = bounce_world

    use({"1": HARD_DSN}).scan()

    db_session.expire_all()
    assert db_session.get(Company, company.id).email_verdict == EmailVerdict.VALID.value


# ------------------------------------------------------------- 標記與生效


def test_marking_a_hard_bounce_sticks_and_leaves_a_trail(bounce_world, db_session):
    company, use = bounce_world
    controller = use({"1": HARD_DSN})
    scan = controller.scan()

    result = controller.apply(scan.hits)

    assert result.marked == 1
    db_session.expire_all()
    stored = db_session.get(Company, company.id)
    assert stored.email_verdict == EmailVerdict.BOUNCED.value
    # 三個月後看到一個被標死的信箱，要查得出來為什麼。
    trail = [item for item in stored.activities if "退信" in (item.subject or "")]
    assert trail, "沒有留下任何紀錄"
    assert "5.1.1" in (trail[0].body or "")


def test_marking_a_soft_bounce_only_writes_a_note(bounce_world, db_session):
    """**軟退信永遠不標死。** 信箱滿了，過幾天就好了。"""
    company, use = bounce_world
    db_session.add(
        EmailMessage(
            company_id=company.id,
            to_address="busy@factory.example",
            status=EmailStatus.SENT.value,
            sent_at=datetime(2026, 8, 17, 10, 0),
        )
    )
    db_session.commit()

    controller = use({"1": SOFT_DSN})
    scan = controller.scan()
    result = controller.apply(scan.hits)

    assert result.marked == 0
    assert result.noted == 1
    db_session.expire_all()
    assert db_session.get(Company, company.id).email_verdict == EmailVerdict.VALID.value


def test_a_marked_company_is_skipped_by_the_next_send(bounce_world, db_session, monkeypatch):
    """**這一條是這一版真正生效的地方。**

    而且要在「沒有打開信箱驗證」的情況下也成立——「這個地址寄不到」是事實，
    跟使用者有沒有勾那個選項無關。
    """
    from gmail import campaign

    company, use = bounce_world
    controller = use({"1": HARD_DSN})
    controller.apply(controller.scan().hits)

    db_session.expire_all()
    stored = db_session.get(Company, company.id)
    reason = campaign._classify(
        stored,
        resend_cutoff=datetime(2000, 1, 1),
        require_verified_email=False,      # 刻意關掉
        remaining_today=100,
        already_planned=0,
    )

    assert reason is SkipReason.BOUNCED


def test_marking_twice_does_not_double_count(bounce_world, db_session):
    """已經標過的預設不勾，而且再標一次也不該讓數字騙人。"""
    _company, use = bounce_world
    controller = use({"1": HARD_DSN})
    controller.apply(controller.scan().hits)

    again = controller.scan()

    assert again.hits[0].already


# --------------------------------------------------- 標了就不能被洗掉


def test_re_verifying_does_not_wash_a_bounce_away(bounce_world, db_session, tmp_config):
    """MX 查詢只證明那個網域收信，證明不了那個信箱存在。

    讓「重新驗證」把退信升級回 Valid 的話，使用者按一次那顆按鈕就會再寄一輪
    死信箱——而他完全不會知道自己剛剛撤銷了什麼。
    """
    from verifier.service import VerificationService

    company, use = bounce_world
    controller = use({"1": HARD_DSN})
    controller.apply(controller.scan().hits)

    db_session.expire_all()
    VerificationService(db_session, config=tmp_config).run()

    db_session.expire_all()
    assert db_session.get(Company, company.id).email_verdict == EmailVerdict.BOUNCED.value


def test_changing_the_email_clears_the_bounce(bounce_world, db_session):
    """這是唯一的解除方式，而且它必須存在。

    沒有它的話，一個被標死的地址被使用者手動改對之後，程式仍然會永遠跳過它，
    而畫面上看不出來為什麼。
    """
    from database.repository import CompanyRepository

    company, use = bounce_world
    controller = use({"1": HARD_DSN})
    controller.apply(controller.scan().hits)
    db_session.commit()

    CompanyRepository(db_session).update(company.id, email="new@factory.example")

    db_session.expire_all()
    assert db_session.get(Company, company.id).email_verdict == EmailVerdict.UNKNOWN.value


def test_a_re_crawl_of_the_same_address_cannot_upgrade_it(
    bounce_world, db_session, tmp_config
):
    """爬回來的頁面只是又看到同一個字串，它不知道那個信箱已經死了。"""
    from core.schemas import RawCompany
    from database.repository import CompanyRepository
    from verifier.service import CleaningService

    company, use = bounce_world
    controller = use({"1": HARD_DSN})
    controller.apply(controller.scan().hits)

    db_session.expire_all()
    cleaned = CleaningService(tmp_config, None).clean(
        RawCompany(
            company_name=company.company_name,
            email="dead@factory.example",
            source="test",
        )
    )
    assert cleaned is not None and cleaned.email_verdict is not EmailVerdict.BOUNCED
    CompanyRepository(db_session).upsert(cleaned)

    db_session.expire_all()
    assert db_session.get(Company, company.id).email_verdict == EmailVerdict.BOUNCED.value


# ------------------------------------------------------------------ 顯示


def test_the_summary_says_what_was_ignored():
    from controllers.mail import BounceScan

    scan = BounceScan(messages=12, unmatched=3)

    assert "12" in scan.describe()
    assert "沒有寄過" in scan.describe()


def test_a_bounce_knows_how_to_call_itself():
    assert Bounce("a@b.example", hard=True).kind == "硬退信"
    assert Bounce("a@b.example").kind == "軟退信"
