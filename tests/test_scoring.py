"""名單品質分數：先聯絡哪一批。

一份剛爬回來的名單裡，每一列的可用程度差很多——有的只有公司名稱，有的信箱
電話地址齊全。這個分數存在的唯一理由是排序：讓使用者不必一列一列看，就能先
處理真的聯絡得上的那些。

這裡測的是「排出來的順序合不合乎常識」，不是每一項的配分等於多少。配分本來
就會調整；順序不能亂——沒有信箱的排在有信箱的前面，這個功能就是壞的。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core.constants import EmailVerdict
from core.scoring import VETO_CEILING, explain, lead_score

NOW = datetime(2026, 8, 6, 12, 0)


def company(**fields) -> SimpleNamespace:
    """一筆什麼都沒有的公司，再蓋上測試在意的欄位。"""
    base = dict(
        company_name="某某企業有限公司",
        email=None,
        email_verdict=EmailVerdict.UNKNOWN.value,
        phone=None,
        website=None,
        address=None,
        contact_person=None,
        tax_id=None,
        capital_amount=None,
        registration_status=None,
        do_not_contact=False,
        updated_at=NOW,
        created_at=NOW,
    )
    base.update(fields)
    return SimpleNamespace(**base)


# ------------------------------------------------------------ 基本順序


def test_a_record_with_nothing_but_a_name_scores_low():
    """整份名單裡最沒用的那一種，不該因為「剛抓到所以很新」就排到前面。"""
    assert lead_score(company(updated_at=NOW), NOW) <= 15


def test_a_verified_email_outranks_every_other_field_combined():
    """驗證過的信箱是這套工具唯一真正需要的東西。它要重過其他所有欄位加起來。

    刻意只主張「驗證過的」。一個沒驗證過的信箱可能一寄就退，那時候一筆有
    電話、有聯絡人、有地址的資料反而更能用——那個順序是對的，不該為了讓
    「信箱最大」這句話成立而硬調配分。
    """
    with_email = company(email="a@b.com", email_verdict=EmailVerdict.VALID.value)
    without = company(phone="02-1234-5678", website="https://x.tw", address="台北市",
                      contact_person="王先生", tax_id="22099131")

    assert lead_score(with_email, NOW) > lead_score(without, NOW)


def test_between_two_identical_records_the_one_with_an_email_wins():
    both = dict(phone="02-1234-5678", website="https://x.tw", address="台北市")

    assert lead_score(company(**both, email="a@b.com"), NOW) > lead_score(
        company(**both), NOW
    )


def test_a_verified_email_outranks_an_unchecked_one():
    checked = company(email="a@b.com", email_verdict=EmailVerdict.VALID.value)
    unchecked = company(email="a@b.com", email_verdict=EmailVerdict.UNKNOWN.value)

    assert lead_score(checked, NOW) > lead_score(unchecked, NOW)


def test_an_email_that_failed_verification_is_worth_almost_nothing():
    """寄不出去的信箱不是資產。它跟「沒有信箱」的差距要很小。"""
    bad = company(email="a@nowhere.invalid", email_verdict=EmailVerdict.NO_MX.value)
    none_at_all = company()

    assert lead_score(bad, NOW) - lead_score(none_at_all, NOW) <= 10


def test_more_complete_records_score_higher():
    sparse = company(email="a@b.com")
    full = company(
        email="a@b.com", phone="02-1234-5678", website="https://x.tw",
        address="台北市中正區", contact_person="王先生", tax_id="22099131",
    )

    assert lead_score(full, NOW) > lead_score(sparse, NOW)


# ------------------------------------------------------------ 資本額


@pytest.mark.parametrize(
    "smaller, bigger",
    [(None, 500_000), (500_000, 5_000_000), (5_000_000, 50_000_000),
     (50_000_000, 500_000_000)],
)
def test_bigger_capital_scores_higher(smaller, bigger):
    assert lead_score(company(capital_amount=smaller), NOW) < lead_score(
        company(capital_amount=bigger), NOW
    )


def test_capital_that_is_not_a_number_does_not_crash():
    """開放資料回來的東西不保證是數字。壞一格不該讓整張表排不出來。"""
    assert lead_score(company(capital_amount="不詳"), NOW) >= 0


# ------------------------------------------------------------ 資料新舊


def test_fresh_data_outranks_stale_data():
    fresh = company(updated_at=NOW - timedelta(days=3))
    stale = company(updated_at=NOW - timedelta(days=800))

    assert lead_score(fresh, NOW) > lead_score(stale, NOW)


def test_a_record_with_no_timestamps_is_not_treated_as_brand_new():
    assert lead_score(company(updated_at=None, created_at=None), NOW) <= lead_score(
        company(updated_at=NOW), NOW
    )


# ------------------------------------------------------------ 一票否決


@pytest.mark.parametrize("status", ["解散", "撤銷", "廢止", "命令解散", "停業"])
def test_a_company_that_no_longer_exists_is_pushed_to_the_bottom(status):
    """名錄網站不會把倒掉的會員刪掉。資料再完整，也不該排在還在營業的前面。"""
    dead = company(
        email="a@b.com", email_verdict=EmailVerdict.VALID.value,
        phone="02-1234-5678", capital_amount=500_000_000,
        registration_status=status,
    )

    assert lead_score(dead, NOW) <= VETO_CEILING
    assert lead_score(dead, NOW) < lead_score(company(email="a@b.com"), NOW)


def test_an_active_registration_is_not_penalised():
    active = company(email="a@b.com", registration_status="核准設立")

    assert lead_score(active, NOW) == lead_score(company(email="a@b.com"), NOW)


def test_do_not_contact_is_pushed_to_the_bottom():
    """使用者說過不要再聯絡的公司，排序上不能再冒出來。"""
    suppressed = company(
        email="a@b.com", email_verdict=EmailVerdict.VALID.value,
        phone="02-1234-5678", do_not_contact=True,
    )

    assert lead_score(suppressed, NOW) <= VETO_CEILING


# ------------------------------------------------------------ 邊界


def test_the_score_never_leaves_zero_to_a_hundred():
    everything = company(
        email="a@b.com", email_verdict=EmailVerdict.VALID.value,
        phone="02-1234-5678", website="https://x.tw", address="台北市",
        contact_person="王先生", tax_id="22099131",
        capital_amount=280_500_000_000, registration_status="核准設立",
    )

    assert 0 <= lead_score(company(), NOW) <= 100
    assert 0 <= lead_score(everything, NOW) <= 100


def test_a_view_without_the_registry_columns_still_scores():
    """分數要能算在任何「像公司的東西」上，不能因為少一個屬性就爆掉。"""
    minimal = SimpleNamespace(company_name="某公司", email="a@b.com")

    assert lead_score(minimal, NOW) > 0


# ------------------------------------------------------------ 說明


def test_the_explanation_adds_up_to_the_score():
    """詳細資料上寫的配分，加起來要等於旁邊那個數字。對不上比不寫還糟。"""
    subject = company(email="a@b.com", phone="02-1234-5678", tax_id="22099131")

    assert sum(points for _, points in explain(subject, NOW)) == lead_score(subject, NOW)


def test_the_explanation_says_why_a_dead_company_was_pushed_down():
    subject = company(email="a@b.com", registration_status="撤銷")
    reasons = [name for name, _ in explain(subject, NOW)]

    assert any("撤銷" in name for name in reasons)
    assert sum(points for _, points in explain(subject, NOW)) == lead_score(subject, NOW)


# ------------------------------------------------------------ 真的排得動

# 分數本身算對了還不夠——它要真的能拿來排整份名單。名單品質不是資料庫欄位，
# SQL 排不了它，所以 repository 走的是另一條路；那條路壞掉的話畫面上會安靜地
# 退回「照更新時間排」，看起來完全正常。


def _store(session, name: str, **fields):
    from database.models import Company

    row = Company(company_name=name, dedupe_key=f"name:{name}", **fields)
    session.add(row)
    session.commit()
    return row


def test_the_company_list_can_be_sorted_by_quality(db_session):
    from core.schemas import CompanyFilter
    from core.scoring import LEAD_SCORE_ORDER
    from database.repository import CompanyRepository

    _store(db_session, "只有名字")
    _store(db_session, "有信箱有電話", email="a@b.com", phone="02-1234-5678")
    _store(db_session, "只有電話", phone="02-8765-4321")

    rows = CompanyRepository(db_session).search(
        CompanyFilter(order_by=LEAD_SCORE_ORDER, descending=True)
    )

    assert [row.company_name for row in rows] == ["有信箱有電話", "只有電話", "只有名字"]


def test_sorting_by_quality_works_together_with_a_filter(db_session):
    """篩選之後再排序。兩條路（Python 端篩選、Python 端排序）會互相影響，
    只測其中一條的話另一條壞了不會被發現。"""
    from core.schemas import CompanyFilter
    from core.scoring import LEAD_SCORE_ORDER
    from database.repository import CompanyRepository

    _store(db_session, "甲公司", industry="紡織", email="a@b.com")
    _store(db_session, "乙公司", industry="紡織")
    _store(db_session, "丙公司", industry="機械", email="c@d.com", phone="02-1111-2222")

    rows = CompanyRepository(db_session).search(
        CompanyFilter(industry="紡織", order_by=LEAD_SCORE_ORDER, descending=True)
    )

    assert [row.company_name for row in rows] == ["甲公司", "乙公司"]


def test_the_score_reaches_the_view_the_gui_and_exports_read(db_session):
    from database.repository import CompanyRepository

    _store(db_session, "有信箱", email="a@b.com")

    view = CompanyRepository(db_session).search_views()[0]

    assert view.lead_score == lead_score(view)
    assert view.lead_score > 0
