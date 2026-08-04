"""Tests for the automatic discovery engine (``crawler.discover``).

Uses :func:`discover_from_html` throughout so nothing here touches the
network -- the offline directory fixture in ``templates/`` stands in for a
real page.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.config import SourceConfig
from crawler.discover import discover_from_html

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "templates" / "sample_directory_page1.html"
FIXTURE_URL = "https://example.test/directory/page1.html"


@pytest.fixture
def fixture_html() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


def test_discover_finds_the_repeated_company_card(fixture_html):
    result = discover_from_html(fixture_html, FIXTURE_URL)
    assert result.list_selector == "div.company-card"
    assert result.item_count == 5


def test_discover_detects_core_contact_fields(fixture_html):
    result = discover_from_html(fixture_html, FIXTURE_URL)
    assert "company_name" in result.fields
    assert "email" in result.fields
    assert "phone" in result.fields

    assert result.fields["company_name"].hit_rate == pytest.approx(1.0)
    # Two of the five records are missing an email or omit a phone value in a
    # format the guesser cannot parse, so these are found often but not always.
    assert result.fields["email"].hit_rate >= 0.35
    assert result.fields["phone"].hit_rate >= 0.35


def test_discover_preview_has_correct_company_names(fixture_html):
    result = discover_from_html(fixture_html, FIXTURE_URL)
    names = [record.company_name for record in result.preview]
    assert names == [
        "宏達精密機械股份有限公司",
        "日新電子（股）公司",
        "全泰化工有限公司",
        "綠光紡織企業社",
        "海通物流股份有限公司",
    ]


def test_discover_finds_the_next_page_link(fixture_html):
    result = discover_from_html(fixture_html, FIXTURE_URL)
    assert result.next_selector is not None
    assert result.ok is True


def test_discover_reports_failure_when_nothing_repeats():
    html = "<html><body><p>Just one paragraph, nothing repeated here.</p></body></html>"
    result = discover_from_html(html, "https://example.test/none")

    assert result.ok is False
    assert result.list_selector == ""
    assert result.notes  # explains why, in Traditional Chinese, for the GUI


# --------------------------------------------------- 公司名稱不能抓到樣板文字


def _directory_with_boilerplate_links() -> str:
    """每張卡片都有「詳細資料」「加入最愛」——真實名錄幾乎都長這樣。

    這正是舊版會誤抓的形狀：那兩個連結在每一列都出現，命中率是滿分的
    100%，比公司名稱還「可靠」，於是舊的計分方式會選中它們。
    """
    cards = "".join(
        f"""
        <div class="item">
          <a class="name" href="/c/{index}">{name}</a>
          <span class="tel">02-2345-67{index:02d}</span>
          <a class="detail" href="/c/{index}">詳細資料</a>
          <a class="fav" href="#">加入最愛</a>
        </div>
        """
        for index, name in enumerate(
            [
                "宏遠精密工業股份有限公司",
                "台興electronics有限公司",
                "南方化工企業社",
                "東立紡織股份有限公司",
                "北大物流有限公司",
            ],
            start=1,
        )
    )
    return f"<html><body><div class='list'>{cards}</div></body></html>"


def test_company_name_is_not_a_repeated_boilerplate_link():
    """每一列都相同的文字不可能是公司名稱，不管它命中率多高。"""
    result = discover_from_html(
        _directory_with_boilerplate_links(), "https://example.test/dir"
    )

    assert "company_name" in result.fields
    names = [record.company_name for record in result.preview]

    assert "詳細資料" not in names
    assert "加入最愛" not in names
    assert names[0] == "宏遠精密工業股份有限公司"
    # 每一筆都必須是不同的公司，而不是同一段樣板文字重複五次。
    assert len(set(names)) == len(names)


def test_company_name_guess_prefers_the_column_that_reads_like_names():
    """相異率相同時，帶「有限公司」這類組織型態的欄位才是名稱。"""
    cards = "".join(
        f"""
        <div class="item">
          <span class="code">A-{index:04d}</span>
          <span class="title">{name}</span>
        </div>
        """
        for index, name in enumerate(
            ["元大機械有限公司", "正新輪胎股份有限公司", "大同電子企業社",
             "中華食品有限公司", "永豐紙業股份有限公司"],
            start=1,
        )
    )
    html = f"<html><body><div class='list'>{cards}</div></body></html>"

    result = discover_from_html(html, "https://example.test/dir")
    names = [record.company_name for record in result.preview]

    # 流水編號每一列也都不同，相異率一樣是 1.0，只有內容看得出差別。
    assert not any(name.startswith("A-") for name in names)
    assert names[0] == "元大機械有限公司"


def test_small_business_names_without_the_word_company_are_recognised():
    """台灣中小企業常常沒有「公司」二字，只以單一個字結尾。

    這不是假想的情境：實際資料庫的 215 筆裡有 8 筆長這樣，只認
    「有限公司」會讓整個小型商家名錄的評分被低估。
    """
    from crawler.discover import _has_company_marker

    real_names = [
        "祥發包裝材料行", "新發鐵捲門行", "金龍電工機械廠",
        "慶建汽車冷氣材料行", "豫味開封包子店", "香霖農產行",
        "艋舺蒸餾水行", "力業鐵工廠",
    ]
    for name in real_names:
        assert _has_company_marker(name), f"{name} 應該要被認出是公司名稱"

    # 放寬字尾規則不能讓導覽文字跟著過關。
    for noise in ["更多", "本店", "回上頁", "詳細資料"]:
        assert not _has_company_marker(noise), f"{noise} 不該被當成公司名稱"


def test_no_company_name_is_better_than_a_wrong_one():
    """名稱那一欄全是樣板文字時要老實說找不到，不要硬塞。

    卡片本身有電話與地址，所以清單會被正確辨識出來——缺的只有名稱，
    這才是「該回報找不到」而不是「整頁都認不得」的情境。
    """
    cards = "".join(
        f"""
        <div class="item">
          <span class="tel">02-2345-67{index:02d}</span>
          <span class="addr">台北市中正區忠孝東路一段{index}號</span>
          <a class="detail" href="/x">詳細資料</a>
          <a class="fav" href="#">加入最愛</a>
        </div>
        """
        for index in range(1, 6)
    )
    html = f"<html><body><div class='list'>{cards}</div></body></html>"

    result = discover_from_html(html, "https://example.test/dir")

    assert "company_name" not in result.fields
    assert result.ok is False
    assert any("公司名稱" in note for note in result.notes), result.notes


def test_discover_to_source_config_produces_a_valid_source(fixture_html):
    result = discover_from_html(fixture_html, FIXTURE_URL)
    source = result.to_source_config("my_directory")

    assert isinstance(source, SourceConfig)
    assert source.name == "my_directory"
    assert source.type == "generic_html"
    assert source.start_url == FIXTURE_URL
    assert source.list_selector == "div.company-card"
    assert "company_name" in source.fields
    assert source.pagination.type == "next_link"
    assert source.pagination.next_selector == result.next_selector
