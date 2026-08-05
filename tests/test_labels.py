"""「標籤︰值」文字排版的解析（``crawler.labels``）。

這種排版是台灣公協會名錄的一大類：整頁只有 ``<table>``、``<font>`` 與
``<br>``，沒有任何 class，唯一的線索是標籤就寫在值的前面。
"""

from __future__ import annotations

from crawler.labels import MIN_PAIRS, parse_record, split_cjk_english

# 一筆真實名錄的形狀（旅行公會），把版面剝掉之後就是這一串文字。
TRAVEL_RECORD = (
    "東晟旅行社股份有限公司 TRANS CONTINENTAL TRAVEL SERVICE CO.,LTD.(IATA) "
    "負責人︰ 負責人 曾怡嘉 Chairman:Ms.Tseng Yi Chia "
    "會員代表︰ 會員代表 陳萬中 "
    "地址︰ 台北市中山區中山北路二段45巷23號三樓之三(10450) "
    "Address︰ 3F-3,No.23,Lane 45,Sec.2,Chung Shan N.Rd.Taipei "
    "Tel︰ 02-25651515 FAX︰ 02-25651498 "
    "入會年月日︰ 1970 年 10 月 31 日 "
    "註冊編號︰ 0003 統一編號︰ 11233532 "
    "營業範圍︰ 甲種旅行業 "
    "E-MAIL︰ david@transcon.com.tw 網址: www.transcon.com.tw"
)


# ------------------------------------------------------------ 已知欄位


def test_known_labels_map_onto_fields():
    parsed = parse_record(TRAVEL_RECORD)
    assert parsed.fields["phone"] == "02-25651515"
    assert parsed.fields["fax"] == "02-25651498"
    assert parsed.fields["tax_id"] == "11233532"
    assert parsed.fields["email"] == "david@transcon.com.tw"
    assert parsed.fields["website"] == "www.transcon.com.tw"
    assert parsed.fields["industry"] == "甲種旅行業"
    assert parsed.fields["address"].startswith("台北市中山區")


def test_the_heading_is_whatever_comes_before_the_first_label():
    """公司名稱在這種版面上不帶標籤，就寫在最前面。"""
    parsed = parse_record(TRAVEL_RECORD)
    assert parsed.heading.startswith("東晟旅行社股份有限公司")


def test_english_labels_count_too():
    """``Tel``、``FAX``、``E-MAIL`` 是這類舊站最常見的寫法。"""
    parsed = parse_record("某某公司 Tel: 02-1234-5678 FAX: 02-8765-4321")
    assert parsed.fields["phone"] == "02-1234-5678"
    assert parsed.fields["fax"] == "02-8765-4321"


# ------------------------------------------------------------ 自由欄位


def test_unknown_labels_are_kept_under_their_own_name():
    """每個公會列的欄位都不一樣，程式沒有立場決定哪些「不重要」。"""
    parsed = parse_record(TRAVEL_RECORD)
    assert parsed.extra["會員代表"] == "會員代表 陳萬中"
    assert parsed.extra["入會年月日"] == "1970 年 10 月 31 日"
    assert parsed.extra["註冊編號"] == "0003"


def test_a_second_label_for_a_filled_field_becomes_a_free_form_field():
    """名錄常常中英文各列一次地址。先到的填欄位，後到的原樣留著，
    兩個都收才不會有一個被蓋掉。"""
    parsed = parse_record(TRAVEL_RECORD)
    assert parsed.fields["address"].startswith("台北市")
    assert parsed.extra["Address"].startswith("3F-3")


# ------------------------------------------------------- 不該被切開的東西


def test_a_url_is_not_split_at_its_colon():
    """``http://`` 的冒號後面不是空白，所以不是分隔符號。"""
    parsed = parse_record("某某公司 電話︰ 02-1 傳真︰ 02-2 說明 http://example.com/a:b")
    assert "http" not in parsed.extra
    assert parsed.fields["phone"] == "02-1"


def test_an_english_transliteration_is_not_a_new_label():
    """``Chairman:Mr.Lin`` 沒有空白，不算標籤——否則負責人的值會被攔腰切斷。"""
    parsed = parse_record("某某公司 負責人︰ 董事長 林寬仁 Chairman:Mr.Lin Kuan Jen 電話︰ 02-1")
    assert "Chairman" not in parsed.extra


def test_a_person_field_drops_the_english_transliteration():
    """英文譯名對寄信與稱呼都沒有用，留著只會讓聯絡人欄變成一整行字。"""
    parsed = parse_record("某某公司 負責人︰ 董事長 林寬仁 Chairman:Mr.Lin Kuan Jen 電話︰ 02-1")
    assert parsed.fields["contact_person"] == "董事長 林寬仁"


def test_tel_is_not_treated_as_a_stopword():
    """曾經把 ``tel`` 列進停用字擋 URL，結果是所有寫「Tel︰」的名錄
    都抓不到電話。半形冒號要後接空白的規則已經處理掉 URL 了。"""
    parsed = parse_record("某某公司 Tel︰ 02-1234-5678 地址︰ 台北市")
    assert parsed.fields["phone"] == "02-1234-5678"


# ------------------------------------------------------------ 門檻與清理


def test_a_single_colon_is_not_this_layout():
    """一個冒號可能只是內文。要有兩組以上才當成這種排版。"""
    parsed = parse_record("這是一段介紹文字，重點：品質第一。")
    assert parsed.pair_count < MIN_PAIRS


def test_line_breaks_inside_a_value_are_collapsed():
    """版面把數字拆成好幾行，值本身不該跟著帶換行。"""
    parsed = parse_record("某某公司 入會年月日︰ \n\n 1970\n\n 年 \n\n 10\n\n 月 電話︰ 02-1")
    assert parsed.extra["入會年月日"] == "1970 年 10 月"


def test_a_label_with_no_value_is_dropped():
    """網址欄空白就是空白，不要記一個空字串進去。"""
    parsed = parse_record("某某公司 電話︰ 02-1 傳真︰ 02-2 網址:")
    assert "website" not in parsed.fields


# --------------------------------------------------------- 中英並排的拆分


def test_split_cjk_english_separates_a_bilingual_heading():
    chinese, english = split_cjk_english(
        "東晟旅行社股份有限公司 TRANS CONTINENTAL TRAVEL SERVICE CO.,LTD."
    )
    assert chinese == "東晟旅行社股份有限公司"
    assert english == "TRANS CONTINENTAL TRAVEL SERVICE CO.,LTD."


def test_split_cjk_english_leaves_a_chinese_only_value_alone():
    assert split_cjk_english("金龍電工機械廠") == ("金龍電工機械廠", "")


def test_split_cjk_english_leaves_an_english_only_value_alone():
    """整段都是英文時不能切——切了會把公司名稱整個丟掉。"""
    assert split_cjk_english("GREAT STAR TRAVEL SERVICE") == ("GREAT STAR TRAVEL SERVICE", "")
