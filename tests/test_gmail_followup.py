"""Tests for 追蹤信（``gmail.campaign.FollowUp`` 與它在 ``_classify`` 裡那幾條）。

**這個檔案守的是「不要變成一台得罪人的機器」。**

追蹤信跟第一封只差在「誰該收到」，而那個差別全部是負面表列：

1. **回過信的不追。** 一個已經回過信的人再收到一封罐頭追蹤信，等於告訴他
   「我根本沒在看你的回信」——比完全不追還糟。
2. **退訂的、退信的不追。** 這兩條在第一封那邊就有了，追蹤信不能繞過去。
3. **同一家有封數上限。** 沒有上限的話，一個從來不回信的地址會被無限期追下
   去，那正是垃圾信的定義，而付出代價的是使用者自己的網域信譽。
4. **沒收過第一封的不會被當成追蹤對象。** 否則「追蹤信」會變成另一種群發。

還有一條形狀上的：**它走的是同一條寄送流程**——同一張預覽表、同一個每日上限、
同一份稽核紀錄。追蹤信不該有自己的一套「比較寬鬆」的路。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core.constants import EmailVerdict, PipelineStage, SkipReason
from core.schemas import CompanyFilter
from database.models import Company, now
from gmail import campaign
from gmail.campaign import FollowUp, build_plan
from gmail.templates import save_template

RULES = FollowUp(after_days=7, max_emails=3)


@pytest.fixture
def mail_config(tmp_config, tmp_path, monkeypatch):
    """``tmp_config`` 但樣板資料夾自己一份，而且是演練模式。

    跟 ``tests/test_gmail_sender.py`` 的同名 fixture 一樣的做法——那一份是
    定義在那個檔案裡的，不是 conftest，所以這裡要自己來。
    """
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    templates_dir = tmp_path / "templates" / "mail"
    templates_dir.mkdir(parents=True)
    return tmp_config.model_copy(
        update={
            "mailer": tmp_config.mailer.model_copy(
                update={
                    "templates_dir": str(templates_dir),
                    "dry_run": True,
                    "daily_limit": 100,
                    "delay_seconds": 1.0,
                    "resend_after_days": 30,
                    "require_verified_email": True,
                }
            )
        }
    )


def _company(**overrides) -> Company:
    """一家「寄過第一封、隔了十天、還沒回」的公司——也就是該被追的那一種。"""
    fields = dict(
        company_name="大安精密工業股份有限公司",
        email="sales@daan.example",
        email_verdict=EmailVerdict.VALID.value,
        pipeline_stage=PipelineStage.CONTACTED.value,
        last_emailed_at=now() - timedelta(days=10),
        last_replied_at=None,
        email_count=1,
        do_not_contact=False,
    )
    fields.update(overrides)
    return Company(**fields)


def _classify(company: Company, *, rules: FollowUp | None = RULES, **overrides):
    kwargs = dict(
        resend_cutoff=datetime(2000, 1, 1),
        require_verified_email=True,
        remaining_today=100,
        already_planned=0,
        follow_up=rules,
        follow_up_cutoff=rules.cutoff() if rules else None,
    )
    kwargs.update(overrides)
    return campaign._classify(company, **kwargs)


# ------------------------------------------------------------------ 該追的


def test_someone_who_was_emailed_and_never_replied_gets_followed_up():
    assert _classify(_company()) is None


def test_the_same_company_is_not_a_first_send_target_any_more():
    """對照組：同一家公司在「第一封」的規則下會被當成近期已寄送過。"""
    reason = _classify(
        _company(), rules=None, resend_cutoff=now() - timedelta(days=30)
    )
    assert reason is SkipReason.RECENTLY_CONTACTED


# ------------------------------------------------------------------ 不該追的


def test_a_company_that_replied_is_never_followed_up():
    """**第一條，也是這一版存在的理由。**"""
    reason = _classify(_company(last_replied_at=now() - timedelta(days=2)))

    assert reason is SkipReason.ALREADY_REPLIED


def test_replying_wins_over_the_count_and_the_delay():
    """理由要挑最有意義的那一個顯示。

    一家「回過信、而且才剛寄過、而且已經三封」的公司，使用者需要看到的是
    「他回過信了」——那是他該去看信箱的訊號，不是「還不夠久」。
    """
    reason = _classify(
        _company(
            last_replied_at=now(),
            last_emailed_at=now(),
            email_count=9,
        )
    )
    assert reason is SkipReason.ALREADY_REPLIED


def test_an_unsubscribed_company_is_never_followed_up():
    assert _classify(_company(do_not_contact=True)) is SkipReason.DO_NOT_CONTACT


def test_a_bounced_address_is_never_followed_up():
    """退過信的地址不會因為「這是追蹤信」就變得寄得到。"""
    reason = _classify(_company(email_verdict=EmailVerdict.BOUNCED.value))

    assert reason is SkipReason.BOUNCED


def test_the_bounce_rule_holds_even_without_email_verification():
    """跟第一封那邊同一條：這是事實，不是設定。"""
    reason = _classify(
        _company(email_verdict=EmailVerdict.BOUNCED.value),
        require_verified_email=False,
    )
    assert reason is SkipReason.BOUNCED


def test_a_company_that_was_never_emailed_is_not_a_follow_up_target():
    """否則「追蹤信」只是換一個名字的群發。"""
    reason = _classify(_company(last_emailed_at=None, email_count=0))

    assert reason is SkipReason.NEVER_CONTACTED


def test_too_soon_is_left_alone():
    reason = _classify(_company(last_emailed_at=now() - timedelta(days=2)))

    assert reason is SkipReason.TOO_SOON


def test_the_day_before_the_cutoff_still_counts_as_too_soon():
    """邊界：剛好差一點的那一天要被擋下來，不是四捨五入過去。"""
    assert _classify(_company(last_emailed_at=now() - timedelta(days=6, hours=23))) is (
        SkipReason.TOO_SOON
    )
    assert _classify(_company(last_emailed_at=now() - timedelta(days=7, hours=1))) is None


def test_a_company_at_the_limit_is_not_chased_again():
    """**第三條。** 上限含第一封在內。"""
    reason = _classify(_company(email_count=3))

    assert reason is SkipReason.ENOUGH_FOLLOW_UPS


def test_one_below_the_limit_still_gets_one_more():
    assert _classify(_company(email_count=2)) is None


def test_the_daily_cap_still_applies_to_follow_ups():
    """追蹤信不能有一條比較寬鬆的路繞過每日上限。"""
    reason = _classify(_company(), remaining_today=5, already_planned=5)

    assert reason is SkipReason.DAILY_CAP


# ---------------------------------------------------------------- 整條流程


@pytest.fixture
def sendable(db_session, mail_config):
    """一份名單：一家該追、一家回過信、一家還沒寄過。"""
    save_template("追蹤", "還在嗎？", "上次寄的信不知道您看到沒有。", mail_config)
    rows = [
        Company(
            company_name="該追的公司",
            email="chase@factory.example",
            dedupe_key="f1",
            email_verdict=EmailVerdict.VALID.value,
            last_emailed_at=now() - timedelta(days=10),
            email_count=1,
        ),
        Company(
            company_name="回過信的公司",
            email="replied@factory.example",
            dedupe_key="f2",
            email_verdict=EmailVerdict.VALID.value,
            last_emailed_at=now() - timedelta(days=10),
            last_replied_at=now() - timedelta(days=9),
            email_count=1,
        ),
        Company(
            company_name="還沒寄過的公司",
            email="fresh@factory.example",
            dedupe_key="f3",
            email_verdict=EmailVerdict.VALID.value,
        ),
    ]
    for row in rows:
        db_session.add(row)
    db_session.commit()
    return rows


def test_the_plan_only_contains_the_one_worth_chasing(sendable, mail_config):
    plan = build_plan(
        CompanyFilter(), "追蹤", "追蹤-1", mail_config, follow_up=RULES
    )

    going = [r.company_name for r in plan.recipients if r.will_send]
    assert going == ["該追的公司"]


def test_the_plan_says_why_each_one_was_left_out(sendable, mail_config):
    """使用者要看得出「回過信的那家為什麼沒在裡面」，不是只看到一個數字。"""
    plan = build_plan(
        CompanyFilter(), "追蹤", "追蹤-1", mail_config, follow_up=RULES
    )

    reasons = {
        r.company_name: r.skip_reason for r in plan.recipients if not r.will_send
    }
    assert reasons["回過信的公司"] is SkipReason.ALREADY_REPLIED
    assert reasons["還沒寄過的公司"] is SkipReason.NEVER_CONTACTED


def test_without_the_follow_up_rules_the_same_filter_behaves_normally(
    sendable, mail_config
):
    """同一組篩選條件，不開追蹤模式時挑的是完全不同的人。"""
    plan = build_plan(CompanyFilter(), "追蹤", "第一封-1", mail_config)

    going = [r.company_name for r in plan.recipients if r.will_send]
    assert going == ["還沒寄過的公司"]


def test_a_follow_up_plan_is_still_a_dry_run_by_default(sendable, mail_config):
    """追蹤信沒有自己的一套安全機制，它就是走同一條。"""
    assert mail_config.mailer.dry_run is True
    plan = build_plan(
        CompanyFilter(), "追蹤", "追蹤-1", mail_config, follow_up=RULES
    )
    assert plan.sendable == 1


# ------------------------------------------------------------------ 規則物件


def test_the_rules_come_from_the_config(mail_config):
    rules = FollowUp.from_config(mail_config)

    assert rules.after_days == mail_config.mailer.follow_up_after_days
    assert rules.max_emails == mail_config.mailer.follow_up_max_emails


def test_the_cutoff_is_that_many_days_ago():
    stamp = datetime(2026, 8, 20, 12, 0)

    assert FollowUp(after_days=7, max_emails=3).cutoff(now_value=stamp) == datetime(
        2026, 8, 13, 12, 0
    )
