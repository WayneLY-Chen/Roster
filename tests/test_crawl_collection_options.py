"""爬取時的兩個收集選項：來源預設產業、只收集勾選的欄位。

兩者都是「寫進資料庫之前先改一下資料」，所以錯了不會有例外，只會安靜地
存進錯的東西——測試是唯一會發現的方式。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from core.schemas import RawCompany
from crawler.pipeline import COLLECTABLE_FIELDS, _apply_default_industry, _keep_only
from database.models import Company
from database.repository import CompanyRepository


def _record(**overrides) -> RawCompany:
    fields = dict(
        company_name="測試公司",
        tax_id="12345678",
        email="a@example.com",
        phone="02-1234-5678",
        website="https://example.com",
        address="台北市信義區",
        industry=None,
        contact_person="王小明",
    )
    fields.update(overrides)
    return RawCompany(**fields)


# --------------------------------------------------------------- 預設產業


def test_default_industry_fills_in_the_blank_ones():
    """名錄網站幾乎都是一個分類一頁，分類寫在麵包屑不在每一列——逐列抓的
    欄位規則抓不到它，產業欄就永遠是空的。"""
    records = [_record(), _record(industry="")]
    filled = _apply_default_industry(records, "精密機械")

    assert filled == 2
    assert all(r.industry == "精密機械" for r in records)


def test_default_industry_never_overwrites_what_the_page_provided():
    """頁面自己有寫的話那個比較準。"""
    records = [_record(industry="電子零組件")]
    assert _apply_default_industry(records, "精密機械") == 0
    assert records[0].industry == "電子零組件"


@pytest.mark.parametrize("value", ["", "   ", None])
def test_no_default_industry_configured_changes_nothing(value):
    records = [_record()]
    assert _apply_default_industry(records, value) == 0
    assert records[0].industry is None


# --------------------------------------------------------------- 欄位篩選


def test_unchecked_fields_are_cleared():
    """只想要公司名稱與信箱時，多抓回來的地址與統編只是雜訊。"""
    records = [_record()]
    _keep_only(records, {"email", "company_name"})

    record = records[0]
    assert record.company_name == "測試公司"
    assert record.email == "a@example.com"
    assert record.phone is None
    assert record.address is None
    assert record.tax_id is None
    assert record.industry is None
    assert record.contact_person is None


def test_none_means_keep_everything():
    """沒有指定就是完全不過濾——這是改動前的行為，不能因為加了功能就變。"""
    records = [_record()]
    _keep_only(records, None)
    assert records[0].phone == "02-1234-5678"


def test_company_name_is_not_a_collectable_field():
    """公司名稱是必填的。不收集它等於整筆不要，那是「不要爬」而不是
    「不要這個欄位」——放進勾選清單只會讓人以為可以取消。"""
    assert "company_name" not in dict(COLLECTABLE_FIELDS)


def _source(**overrides):
    """generic_html 來源的必填欄位不少，這幾個測試只在意 collect_fields。"""
    from core.config import FieldRule, PaginationRule, SourceConfig

    fields = dict(
        name="s",
        type="generic_html",
        start_url="https://example.test/",
        list_selector="div.item",
        pagination=PaginationRule(type="none"),
        fields={"company_name": FieldRule(selector="h3")},
    )
    fields.update(overrides)
    return SourceConfig(**fields)


def test_company_name_is_kept_even_when_it_is_not_in_the_selected_set():
    """公司名稱是必填欄位，清掉它整筆資料就沒有意義了。"""
    from crawler.pipeline import CrawlPipeline

    assert CrawlPipeline._fields_for(_source(collect_fields=["email"])) == {
        "email",
        "company_name",
    }


def test_no_selection_means_collect_everything():
    """留空＝不過濾，這是預設也是改動前的行為。"""
    from crawler.pipeline import CrawlPipeline

    assert CrawlPipeline._fields_for(_source()) is None


def test_collect_fields_live_on_the_source_so_the_scheduler_uses_them():
    """設定放在來源上而不是畫面上——排程爬取跑的時候沒有人在介面前面勾選。"""
    assert _source(collect_fields=["email", "phone"]).collect_fields == ["email", "phone"]


# ----------------------------------------------------- 依日期分組與刪除


def _make(session, name: str, created: datetime) -> Company:
    company = Company(
        company_name=name,
        name_key=name,
        dedupe_key=f"n:{name}",
        created_at=created,
        updated_at=created,
    )
    session.add(company)
    session.flush()
    return company


def test_crawl_dates_groups_by_day_newest_first(db_session):
    today = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)

    _make(db_session, "今天甲", today)
    _make(db_session, "今天乙", today.replace(hour=23))
    _make(db_session, "昨天甲", yesterday)
    db_session.commit()

    dates = CompanyRepository(db_session).crawl_dates()

    assert dates[0] == (today.date(), 2)
    assert dates[1] == (yesterday.date(), 1)


def test_delete_by_date_only_removes_that_day(db_session):
    """整批刪除是不可復原的，多刪一天就是災難。"""
    today = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)

    _make(db_session, "今天甲", today)
    _make(db_session, "今天乙", today.replace(hour=23, minute=59, second=59))
    _make(db_session, "昨天甲", yesterday)
    db_session.commit()

    removed = CompanyRepository(db_session).delete_by_date(today.date())
    db_session.commit()

    assert removed == 2
    remaining = [c.company_name for c in CompanyRepository(db_session).all()]
    assert remaining == ["昨天甲"]


def test_delete_by_date_on_an_empty_day_removes_nothing(db_session):
    _make(db_session, "今天甲", datetime.now())
    db_session.commit()

    removed = CompanyRepository(db_session).delete_by_date(date(1999, 1, 1))

    assert removed == 0
    assert CompanyRepository(db_session).count() == 1


def test_deleting_a_day_also_removes_the_contacts_underneath(db_session):
    """逐筆 delete 而不是 bulk delete，聯絡人才會被 cascade 一起清掉。
    繞過 ORM 會留下指向不存在公司的孤兒資料。"""
    from database.repository import ContactRepository

    today = datetime.now()
    company = _make(db_session, "今天甲", today)
    ContactRepository(db_session).add(company.id, name="王小明")
    db_session.commit()

    CompanyRepository(db_session).delete_by_date(today.date())
    db_session.commit()

    from database.models import Contact

    assert db_session.query(Contact).count() == 0
