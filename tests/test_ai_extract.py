"""Tests for ai/extract.py。

**這個檔案守的是同一件事：模型編出來的東西不准進到名單裡。**

其餘（HTML 怎麼轉純文字、JSON 怎麼容錯）都是繞著它的細節。改壞比對那一關的
後果不會有任何錯誤訊息——名單看起來一樣整齊，只是裡面多了幾十個不存在的信箱，
而發現的時機是使用者寄出去之後收到一堆退信。
"""

from __future__ import annotations

import json

import pytest

from ai.extract import (
    MAX_PAGE_CHARS,
    MAX_RECORDS,
    DroppedValue,
    ExtractResult,
    extract_from_html,
    ground,
    html_to_text,
    parse_reply,
    trim_for_model,
)
from core.errors import AIError

PAGE = """
<html><head><title>會員名錄</title>
  <style>.x{color:red}</style>
  <script>var members=[{"email":"script@example.test"}];</script>
</head>
<body>
  <nav>首頁 | 關於我們 | 聯絡我們</nav>
  <table>
    <tr><td>大安精密工業股份有限公司</td><td>統編：22099131</td>
        <td>電話：(02 )27407278</td><td>sales@daan-precision.test</td>
        <td>台北市大安區信義路四段1號</td></tr>
    <tr><td>臺中鑄造有限公司</td><td>電話：04-22345678</td>
        <td>www.taichung-casting.test</td></tr>
  </table>
</body></html>
"""


def _reply(*records: dict) -> str:
    return json.dumps(list(records), ensure_ascii=False)


def _chat(reply: str):
    """一個假的模型：不管問什麼都回同一段。"""

    def chat(_messages):
        return reply

    return chat


# --------------------------------------------------------------- HTML 轉純文字


def test_script_and_style_never_reach_the_model():
    """一個現代網頁八成的位元組是 JavaScript，送出去只是讓使用者多付錢。

    而且 script 裡常常有信箱格式的字串（追蹤碼、範例資料），留著會讓模型把
    它們當成頁面上的聯絡資訊——那種值還會通過比對，因為它「真的在頁面上」。
    """
    text = html_to_text(PAGE)

    assert "script@example.test" not in text
    assert "var members" not in text
    assert "color:red" not in text
    assert "大安精密工業股份有限公司" in text


def test_the_page_title_survives():
    """單一公司的官網上，``<title>`` 常常是唯一寫出全名的地方。

    連同 head 一起丟掉的話，那種頁面一筆都抽不到——而模型會「看到」名稱只在
    標題列，卻因為對不回內文而被整筆丟棄。
    """
    text = html_to_text(
        "<html><head><title>大安精密工業股份有限公司 - 官網</title>"
        "<style>.a{}</style></head><body><p>02-1234</p></body></html>"
    )
    assert "大安精密工業股份有限公司" in text
    assert ".a{}" not in text


def test_each_cell_lands_on_its_own_line():
    """用換行分隔而不是空白：名錄的一列就是一格一格的欄位，黏起來模型會切錯。"""
    text = html_to_text("<table><tr><td>甲公司</td><td>02-1111</td></tr></table>")
    assert text.splitlines() == ["甲公司", "02-1111"]


def test_a_page_longer_than_the_budget_is_cut_and_says_so():
    long_page = "一" * (MAX_PAGE_CHARS + 500)
    sent, truncated = trim_for_model(long_page)

    assert truncated is True
    assert len(sent) == MAX_PAGE_CHARS


def test_a_short_page_is_not_marked_as_truncated():
    sent, truncated = trim_for_model("很短")
    assert (sent, truncated) == ("很短", False)


# ------------------------------------------------------- 每個值都要對得回原文


def test_an_invented_email_is_dropped_and_listed():
    """這是整個功能的安全底線。

    模型抄對了公司名稱與電話，但**自己補了一個信箱**——那個信箱格式正確、網域
    也像那家公司，混在三千筆名單裡沒有人會發現。它必須被丟掉，而且必須被列
    出來，否則使用者沒有辦法判斷這個模型在這個網站上可不可信。
    """
    page = html_to_text(PAGE)
    result = ground(
        [
            {
                "company_name": "大安精密工業股份有限公司",
                "phone": "(02 )27407278",
                "email": "info@daan-precision.test",   # 頁面上沒有這一個
            }
        ],
        page,
        "https://example.test/members",
    )

    assert len(result.records) == 1
    assert result.records[0].email is None
    assert result.records[0].phone == "(02 )27407278"

    assert result.dropped_values == 1
    dropped = result.dropped[0]
    assert dropped.field == "email"
    assert dropped.value == "info@daan-precision.test"
    assert dropped.whole_record is False
    assert "info@daan-precision.test" in dropped.describe()


def test_a_company_name_that_is_not_on_the_page_drops_the_whole_record():
    """名稱是這筆資料的身分。身分是編的，底下的電話配給誰就沒有意義了。"""
    page = html_to_text(PAGE)
    result = ground(
        [{"company_name": "永豐機械有限公司", "phone": "(02 )27407278"}],
        page,
        "https://example.test/members",
    )

    assert result.records == []
    assert result.dropped_records == 1
    assert result.dropped[0].whole_record is True
    assert "整筆丟棄" in result.dropped[0].describe()


def test_whitespace_and_fullwidth_differences_are_tolerated():
    """比對要寬鬆到能容忍寫法的差異，但不能寬鬆到用模糊比對。

    頁面把電話寫成 ``(02 )27407278``（資料庫欄位補空格直接印出來，公協會名錄
    很常見），模型回的是 ``(02)2740-7278``。那是同一串數字的兩種寫法，不是
    兩個值。
    """
    page = html_to_text(PAGE)
    result = ground(
        [
            {
                "company_name": "大安精密工業股份有限公司",
                "phone": "(02)2740-7278",
                "tax_id": "２２０９９１３１",           # 全形
                "address": "台北市大安區信義路四段 1 號",  # 多了空白
            }
        ],
        page,
        "https://example.test/members",
    )

    assert result.dropped == []
    record = result.records[0]
    assert record.phone == "(02)2740-7278"
    assert record.tax_id == "２２０９９１３１"
    assert record.address == "台北市大安區信義路四段 1 號"


def test_tai_wan_spelling_does_not_split_a_company_in_two():
    """頁面寫「臺中」、模型抄成「台中」（或反過來）是同一家公司。"""
    page = html_to_text(PAGE)
    result = ground([{"company_name": "台中鑄造有限公司"}], page, "https://example.test/")

    assert [r.company_name for r in result.records] == ["台中鑄造有限公司"]
    assert result.dropped == []


def test_a_website_the_model_prefixed_with_a_scheme_still_counts():
    """``https://`` 是寫法不是資料。頁面印 ``www.x.test``、模型補成
    ``https://www.x.test`` 時，被補的那一段不該讓整個值被丟掉。"""
    page = html_to_text(PAGE)
    result = ground(
        [
            {
                "company_name": "臺中鑄造有限公司",
                "website": "https://www.taichung-casting.test",
            }
        ],
        page,
        "https://example.test/",
    )

    assert result.dropped == []
    assert result.records[0].website == "https://www.taichung-casting.test"


def test_placeholder_values_are_treated_as_empty_not_as_data():
    """模型很愛在沒有資料的格子裡填「無」。那不是一個電話號碼。"""
    page = html_to_text(PAGE)
    result = ground(
        [{"company_name": "臺中鑄造有限公司", "phone": "無", "email": "N/A"}],
        page,
        "https://example.test/",
    )

    record = result.records[0]
    assert record.phone is None
    assert record.email is None
    # 沒有填的東西不算「被丟掉的值」——那會讓那份清單充滿雜訊，而它的用途是
    # 讓使用者一眼看出模型在不在編東西。
    assert result.dropped == []


def test_fields_outside_the_allowed_list_are_ignored():
    """欄位開越多，模型越傾向「每一格都填點東西」，而那正是編造的來源。"""
    page = html_to_text(PAGE)
    result = ground(
        [{"company_name": "臺中鑄造有限公司", "industry": "金屬加工", "products": "鑄件"}],
        page,
        "https://example.test/",
    )

    record = result.records[0]
    assert record.industry is None
    assert record.products is None


def test_no_companies_on_the_page_produces_nothing_at_all():
    """頁面上沒有公司時不會硬湊，也不該報錯。"""
    result = extract_from_html(
        "<html><body><p>本站已搬遷</p></body></html>",
        "https://example.test/",
        _chat("[]"),
    )

    assert result.records == []
    assert result.dropped == []


def test_a_page_with_no_text_never_calls_the_model():
    """空頁面連問都不必問——那是一次白花的錢。"""

    def explode(_messages):
        raise AssertionError("空頁面不該送給模型")

    result = extract_from_html("<html><body></body></html>", "https://x.test/", explode)
    assert result.records == []


def test_the_record_carries_where_it_came_from():
    result = extract_from_html(PAGE, "https://example.test/members", _chat(
        _reply({"company_name": "臺中鑄造有限公司"})
    ))

    record = result.records[0]
    assert record.source == "ai"
    assert record.source_url == "https://example.test/members"


def test_a_runaway_model_is_cut_off_at_the_limit():
    """模型偶爾會陷入重複輸出的迴圈。這是止血點，而且要講出來。"""
    page = html_to_text(PAGE)
    items = [{"company_name": "臺中鑄造有限公司"}] * (MAX_RECORDS + 7)
    result = ground(items, page, "https://example.test/")

    assert result.over_limit == 7
    assert len(result.records) == MAX_RECORDS
    assert any("上限" in note for note in result.notes())


# ------------------------------------------------------------- JSON 怎麼容錯


@pytest.mark.parametrize(
    "reply",
    [
        '[{"company_name": "甲公司"}]',
        '```json\n[{"company_name": "甲公司"}]\n```',
        '```\n[{"company_name": "甲公司"}]\n```',
        '好的，以下是結果：\n[{"company_name": "甲公司"}]\n希望有幫助！',
        '{"companies": [{"company_name": "甲公司"}]}',
    ],
)
def test_json_wrapped_in_the_usual_model_habits_is_still_read(reply):
    """包在程式碼區塊裡、前面加一句客套話——那是格式問題不是資料問題。

    為了它整趟重跑（使用者要再等一次、再付一次錢）不合理。
    """
    assert parse_reply(reply) == [{"company_name": "甲公司"}]


def test_a_reply_with_no_json_at_all_says_what_the_model_actually_said():
    with pytest.raises(AIError) as caught:
        parse_reply("我沒有辦法讀取網頁內容。")

    # 錯誤訊息裡要有模型講的原話，否則使用者只知道「失敗了」。
    assert "我沒有辦法讀取網頁內容" in str(caught.value)


def test_an_empty_reply_is_an_error_not_an_empty_list():
    """「模型什麼都沒回」與「這一頁沒有公司」是兩件事，不能混為一談。"""
    with pytest.raises(AIError):
        parse_reply("   ")


def test_non_dict_elements_are_skipped_rather_than_crashing():
    assert parse_reply('[{"company_name": "甲"}, "垃圾", 3, null]') == [
        {"company_name": "甲"}
    ]


# ------------------------------------------------------------------ 誠實回報


def test_notes_spell_out_everything_that_was_thrown_away():
    result = ExtractResult(
        page_chars=40_000,
        sent_chars=MAX_PAGE_CHARS,
        truncated=True,
        returned=3,
        dropped=[
            DroppedValue("", "company_name", "編的", whole_record=True),
            DroppedValue("甲公司", "email", "a@b.test"),
        ],
    )

    notes = " ".join(result.notes())
    assert "只送了前" in notes
    assert "整筆丟棄" in notes
    assert "1 個值在原始頁面上找不到" in notes
