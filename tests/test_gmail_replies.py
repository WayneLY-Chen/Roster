"""Tests for gmail/replies.py 與 controllers.mail.ReplyController。

**這個檔案守的是四件事：**

1. **退信不是回覆。** 一封 mailer-daemon 如果被算成回覆，業務階段會被推到
   「已聯絡」——那是跟事實完全相反的結論。
2. **業務階段只往前推。** 使用者手動改成「會議」之後，一封自動回覆不該把它拉
   回「已聯絡」。他辛苦維護的階段被程式一夜之間洗掉，而且不會馬上發現。
3. **退訂只加不減。** 而且判斷刻意寬鬆——漏掉一個明講不要的人，代價遠大於多列
   一筆讓使用者自己取消勾選。
4. **絕不自動回信。** 這個模組連 smtplib 都不該碰。

外加那條貫穿整批的：**只認自己寄過的地址**。收件匣裡任何一封信都不能改動名單。
"""

from __future__ import annotations

import email
from datetime import date, datetime

import pytest

from controllers.mail import REPLY_STAGE, ReplyController, advances
from core.constants import EmailStatus, PipelineStage
from database.models import Company, EmailMessage
from gmail.replies import (
    Reply,
    iter_replies,
    looks_automatic,
    parse_reply,
    since_query,
    wants_out,
)

OUR_ID = "<abc123@example.com>"

REPLY = f"""\
From: 王小明 <ming@factory.example>
To: me@example.com
Subject: Re: 合作洽詢
Date: Tue, 19 Aug 2026 10:05:00 +0800
In-Reply-To: {OUR_ID}
Content-Type: text/plain; charset=UTF-8

您好，我們對貴公司的產品有興趣，方便下週約個時間談嗎？
"""

REPLY_NO_THREAD = """\
From: 陳大同 <tong@other.example>
Subject: 關於報價
Date: Tue, 19 Aug 2026 11:00:00 +0800
Content-Type: text/plain; charset=UTF-8

請問最小訂購量是多少？
"""

STRANGER = """\
From: 廣告信 <promo@spam.example>
Subject: 限時優惠
Content-Type: text/plain; charset=UTF-8

買一送一。
"""

UNSUBSCRIBE = """\
From: 李美玲 <ling@factory.example>
Subject: Re: 合作洽詢
Content-Type: text/plain; charset=UTF-8

請不要再寄信給我，謝謝。
"""

OUT_OF_OFFICE = """\
From: 王小明 <ming@factory.example>
Subject: 自動回覆：Re: 合作洽詢
Auto-Submitted: auto-replied
Content-Type: text/plain; charset=UTF-8

我目前休假中，將於 9/1 回覆。
"""

BOUNCE = """\
From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>
Subject: Delivery Status Notification (Failure)
Content-Type: multipart/report; report-type=delivery-status; boundary="B"

--B
Content-Type: message/delivery-status

Final-Recipient: rfc822; ming@factory.example
Action: failed
Status: 5.1.1

--B--
"""

#: 一封正常的回信，而我們自己那句退訂說明躺在下面的引言區塊裡。
QUOTED = (
    """\
From: 王小明 <ming@factory.example>
Subject: Re: 合作洽詢
Content-Type: text/plain; charset=UTF-8

謝謝來信，我們評估後再回覆您。
"""
    + "以下是我們內部討論的一些背景，供您參考。" * 20
    + "\n\n> 若不想再收到這類信件，請回信告知，我們會將您從名單中移除。\n"
)


def _message(raw: str):
    # 用 bytes 解析，跟正式流程一樣（GmailClient.fetch_raw 拿回來的是位元組）。
    # 用 message_from_string 的話，非 ASCII 內文經過 get_payload(decode=True)
    # 會變成一串逃脫字元，測到的東西跟真的收信不一樣。
    return email.message_from_bytes(raw.encode("utf-8"))


# ------------------------------------------------------------------ 認得出來


def test_a_bounce_is_never_a_reply():
    """**第一條。** 死信箱不能把業務階段推到「已聯絡」。"""
    assert parse_reply(_message(BOUNCE), sent_to=["ming@factory.example"]) is None


def test_a_stranger_is_ignored():
    """**只認自己寄過的地址。** 廣告信改不動使用者的名單。"""
    assert parse_reply(_message(STRANGER), sent_to=["ming@factory.example"]) is None


def test_a_threaded_reply_matches_by_message_id():
    """對得回我們寄出的那一封，是唯一確定的證據。"""
    reply = parse_reply(
        _message(REPLY), uid="5", sent_ids={OUR_ID: 42}, sent_to=[]
    )

    assert reply is not None
    assert reply.matched_by == "message-id"
    assert reply.email_message_id == 42
    assert reply.confidence == "確定"
    assert reply.address == "ming@factory.example"


def test_a_reply_without_the_thread_headers_falls_back_to_the_address():
    """有些寄件伺服器會換掉送出去的 Message-ID，那時候只剩地址可以比對。"""
    reply = parse_reply(_message(REPLY_NO_THREAD), sent_to=["tong@other.example"])

    assert reply is not None
    assert reply.matched_by == "address"
    assert reply.confidence == "用地址比對"
    assert reply.email_message_id is None


def test_the_message_id_route_does_not_need_the_address_list():
    """對到 Message-ID 就夠了——那封信本來就是我們寄的。"""
    reply = parse_reply(_message(REPLY), sent_ids={OUR_ID: 7}, sent_to=[])

    assert reply is not None and reply.email_message_id == 7


def test_an_out_of_office_is_marked_as_automatic():
    reply = parse_reply(_message(OUT_OF_OFFICE), sent_to=["ming@factory.example"])

    assert reply is not None
    assert reply.automatic
    assert reply.kind == "自動回覆"


@pytest.mark.parametrize(
    "text",
    ["請不要再寄", "退訂", "取消訂閱", "unsubscribe please", "Please remove me",
     "opt out", "stop emailing me"],
)
def test_the_usual_ways_of_saying_stop_are_caught(text):
    """判斷刻意寬鬆：漏掉一個明講不要的人，代價遠大於多列一筆。"""
    assert wants_out(text, "")
    assert wants_out("", text)


def test_a_plain_reply_is_not_an_unsubscribe():
    assert not wants_out("Re: 合作洽詢", "我們對貴公司的產品有興趣。")


def test_our_own_unsubscribe_note_in_the_quote_does_not_count():
    """**這一條會咬人。**

    每一封寄出去的信尾都印著「不想再收到請回信告知」。對方回信時那段話會躺在
    引言區塊裡——整封信都掃的話，**每一封回信都會變成退訂**。
    """
    reply = parse_reply(_message(QUOTED), sent_to=["ming@factory.example"])

    assert reply is not None
    assert not reply.unsubscribe


def test_an_unsubscribe_reply_says_so():
    reply = parse_reply(_message(UNSUBSCRIBE), sent_to=["ling@factory.example"])

    assert reply is not None and reply.unsubscribe
    assert reply.kind == "要求不要再寄"


def test_the_snippet_is_short_enough_for_a_table_cell():
    reply = parse_reply(_message(QUOTED), sent_to=["ming@factory.example"])

    assert reply is not None
    assert len(reply.snippet) <= 80
    assert "\n" not in reply.snippet


def test_an_auto_reply_header_is_enough_on_its_own():
    assert looks_automatic(_message(OUT_OF_OFFICE))
    assert not looks_automatic(_message(REPLY))


# ------------------------------------------------------------------ 搜尋條件


def test_the_date_filter_uses_english_month_names():
    """**這一條是回歸測試。**

    IMAP 規定日期是 ``dd-Mon-yyyy``，而且月份縮寫一定要英文。在中文語系的
    機器上用 ``strftime("%b")`` 會產生「8月」，伺服器直接回錯誤——而那個錯誤
    只會在中文 Windows 上出現，開發機上看不到。
    """
    assert since_query(30, today=date(2026, 9, 10)) == "SINCE 11-Aug-2026"
    assert since_query(1, today=date(2026, 1, 1)) == "SINCE 31-Dec-2025"


# ------------------------------------------------------------------- 掃信箱


class _FakeClient:
    def __init__(self, messages: dict[str, str]) -> None:
        self.messages = messages
        self.queries: list[str] = []

    def search(self, query, limit):
        self.queries.append(query)
        return list(self.messages)

    def fetch_raw(self, uid):
        raw = self.messages.get(uid)
        return raw.encode("utf-8") if raw else None


def test_scanning_skips_bounces_and_strangers():
    client = _FakeClient({"1": REPLY, "2": BOUNCE, "3": STRANGER})

    found = list(iter_replies(client, sent_to=["ming@factory.example"]))

    assert [item.address for item in found] == ["ming@factory.example"]


def test_a_broken_message_does_not_stop_the_scan():
    client = _FakeClient({"1": "\x00 壞掉", "2": REPLY})

    found = list(iter_replies(client, sent_to=["ming@factory.example"]))

    assert len(found) == 1


# -------------------------------------------------------------- 階段只往前


@pytest.mark.parametrize("stage", [PipelineStage.NEW, PipelineStage.QUALIFIED])
def test_earlier_stages_move_forward(stage):
    assert advances(stage.value)


@pytest.mark.parametrize(
    "stage",
    [
        PipelineStage.CONTACTED,
        PipelineStage.MEETING,
        PipelineStage.PROPOSAL,
        PipelineStage.WON,
        PipelineStage.LOST,
        PipelineStage.INACTIVE,
    ],
)
def test_later_and_terminal_stages_are_left_alone(stage):
    """**第二條。**

    ``Lost``／``Inactive`` 是使用者自己下的結論。一封回信——很可能只是「謝謝，
    目前沒有需求」——沒有資格推翻它。
    """
    assert not advances(stage.value)


# ------------------------------------------------------------------ 寫回去


@pytest.fixture
def reply_world(db_session, monkeypatch, tmp_config):
    """兩家寄過信的公司，加上一個假的信箱。"""
    made: dict[str, Company] = {}
    for name, address, key, stage in (
        ("大安精密工業股份有限公司", "ming@factory.example", "r1", PipelineStage.NEW),
        ("遠東鑄造股份有限公司", "ling@factory.example", "r2", PipelineStage.MEETING),
    ):
        company = Company(
            company_name=name,
            email=address,
            dedupe_key=key,
            pipeline_stage=stage.value,
        )
        db_session.add(company)
        db_session.flush()
        db_session.add(
            EmailMessage(
                company_id=company.id,
                to_address=address,
                message_id=OUR_ID if key == "r1" else None,
                status=EmailStatus.SENT.value,
                sent_at=datetime(2026, 8, 18, 9, 0),
            )
        )
        made[key] = company
    db_session.commit()

    def use(raw_by_uid: dict[str, str]):
        from contextlib import contextmanager

        import gmail.client as client_module

        @contextmanager
        def fake_session(_config=None):
            yield _FakeClient(raw_by_uid)

        monkeypatch.setattr(client_module, "gmail_session", fake_session)
        return ReplyController(tmp_config)

    return made, use


def test_a_reply_moves_a_new_company_to_contacted(reply_world, db_session):
    made, use = reply_world
    controller = use({"1": REPLY})

    scan = controller.scan()
    assert [hit.company_name for hit in scan.hits] == ["大安精密工業股份有限公司"]
    assert scan.hits[0].action.endswith("」")
    result = controller.apply(scan.hits)

    assert result.advanced == 1
    db_session.expire_all()
    stored = db_session.get(Company, made["r1"].id)
    assert stored.pipeline_stage == REPLY_STAGE.value
    assert any("收到回信" in (item.subject or "") for item in stored.activities)


def test_a_reply_never_drags_a_later_stage_backwards(reply_world, db_session):
    """使用者手動排到「會議」的公司，一封回信不能把它拉回「已聯絡」。"""
    made, use = reply_world
    controller = use({"1": UNSUBSCRIBE.replace("請不要再寄信給我，謝謝。", "好的，再聊。")})

    scan = controller.scan()
    result = controller.apply(scan.hits)

    assert result.advanced == 0
    assert result.noted == 1
    db_session.expire_all()
    assert (
        db_session.get(Company, made["r2"].id).pipeline_stage
        == PipelineStage.MEETING.value
    )


def test_an_unsubscribe_marks_do_not_contact(reply_world, db_session):
    made, use = reply_world
    controller = use({"1": UNSUBSCRIBE})

    scan = controller.scan()
    assert scan.hits[0].action == "標記為請勿聯絡"
    result = controller.apply(scan.hits)

    assert result.unsubscribed == 1
    db_session.expire_all()
    assert db_session.get(Company, made["r2"].id).do_not_contact


def test_an_unsubscribe_does_not_also_move_the_stage(reply_world, db_session):
    """兩件事分開：叫我們別再寄，不代表這是一次成功的接觸。"""
    made, use = reply_world
    controller = use({"1": UNSUBSCRIBE})

    controller.apply(controller.scan().hits)

    db_session.expire_all()
    assert (
        db_session.get(Company, made["r2"].id).pipeline_stage
        == PipelineStage.MEETING.value
    )


def test_the_scan_writes_nothing_by_itself(reply_world, db_session):
    made, use = reply_world

    use({"1": UNSUBSCRIBE, "2": REPLY}).scan()

    db_session.expire_all()
    assert not db_session.get(Company, made["r2"].id).do_not_contact
    assert db_session.get(Company, made["r1"].id).pipeline_stage == PipelineStage.NEW.value


def test_an_auto_reply_is_listed_but_not_suggested(reply_world):
    """證明信寄到了，不證明有人讀過。"""
    _made, use = reply_world

    scan = use({"1": OUT_OF_OFFICE}).scan()

    assert len(scan.hits) == 1
    assert not scan.hits[0].suggested


def test_nothing_sent_means_no_connection_is_even_opened(tmp_config, db_session, monkeypatch):
    """一封都還沒寄過的時候，收件匣裡不可能有「回我的信」。"""
    import gmail.client as client_module

    monkeypatch.setattr(
        client_module,
        "gmail_session",
        lambda *a, **k: pytest.fail("沒寄過信卻連了信箱"),
    )

    assert ReplyController(tmp_config).scan().hits == []


def test_only_the_most_important_reply_per_address_is_shown(reply_world):
    """同一個人回好幾封時，「要求不要再寄」那一封最該被看到。"""
    _made, use = reply_world
    chatty = UNSUBSCRIBE.replace("ling@", "ming@")

    scan = use({"1": REPLY, "2": chatty}).scan()

    assert len(scan.hits) == 1
    assert scan.hits[0].reply.unsubscribe


# ------------------------------------------------------------ 絕不自動回信


def test_this_module_cannot_send_anything():
    """**第四條。**

    自動回覆是一個「一次設定錯就對著幾百個真實客戶連續發生」的功能。這裡不做，
    而且要擋住之後有人順手加進來——所以檢查的是「有沒有那個能力」，不是「有沒有
    寫那段邏輯」。
    """
    from pathlib import Path

    source = Path("gmail/replies.py").read_text(encoding="utf-8")
    for forbidden in ("smtplib", "GmailSender", "send_message", "sendmail"):
        assert forbidden not in source, f"gmail/replies.py 不該碰 {forbidden}"


def test_a_reply_knows_how_to_describe_itself():
    assert Reply("a@b.example", unsubscribe=True).kind == "要求不要再寄"
    assert Reply("a@b.example", automatic=True).kind == "自動回覆"
    assert Reply("a@b.example").kind == "回覆"
