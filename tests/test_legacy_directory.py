"""舊式公協會名錄的整條路徑：編碼 → 版面分析 → 標籤欄位 → 自由欄位。

這一類頁面是 2000 年代的 ASP／PHP 產物：巢狀 ``<table>`` 排版、只有
``<font>``、沒有一個 class、內容是 Big5、欄位以「標籤︰值」的文字形式列出。
它們在台灣的公會名錄裡佔了相當大的比例，而以 CSS 選擇器為主的分析對它們
幾乎完全無效——這個檔案就是為了釘住那條替代路徑。
"""

from __future__ import annotations

import pytest

from crawler.discover import discover_from_html
from crawler.fetcher import decode_bytes
from crawler.parser import sniff_declared_encoding

COMPANIES = [
    # 名稱, 英文名, 負責人, 電話, 傳真, 統編, 信箱, 網址文字
    ("東晟旅行社股份有限公司", "TRANS CONTINENTAL TRAVEL CO.,LTD.",
     "負責人 曾怡嘉", "02-25651515", "02-25651498", "11233532",
     "david@example.test", "www.transcon.example"),
    ("大通旅行社股份有限公司", "DAI TSU TRAVEL SERVICE CO.,LTD.",
     "董事長 林寬仁", "02-27115788", "02-27728848", "11022852",
     "lin@example.test", ""),
    ("首都旅行社股份有限公司", "CAPITAL EXPRESS INTERNATIONAL CO.,LTD.",
     "董事長 林明發", "02-25966923", "02-25965975", "34031489",
     "capital@example.test", ""),
    ("世達通運股份有限公司", "WORLD EXPRESS INC.",
     "經理 趙聿倩", "02-25033030", "02-25053151", "11217316",
     "admin@example.test", "www.worldexpress.example"),
    ("陽達旅行社股份有限公司", "NEW SUNSHINE TRAVEL SERVICE CO.,LTD.",
     "董事長 楊重義", "02-25033266", "02-25054898", "14065658",
     "yang@example.test", ""),
    ("萬商旅行社股份有限公司", "HAPPY FORMOSA TRAVEL SERVICE CO.,LTD.",
     "總經理 朱永達", "02-25064372", "02-25167146", "12106611",
     "maggie@example.test", "www.formosa.example"),
]

#: 這個名錄把每一筆的 ``<a href>`` 都寫死成第一家的網址，只有連結文字是對的。
#: 實際遇過的版面 bug，故意保留在測試裡。
STALE_HREF = "http://www.transcon.example"


def _record_table(row: tuple[str, ...]) -> str:
    name, english, person, tel, fax, tax_id, email, website = row
    return f"""
    <table border=0 cellpadding=0 width="90%" align="center">
      <tr><td colspan=3 bgcolor="#C8C8FF"><font size="2">{name}</font></td></tr>
      <tr><td width="6%"><font size="2"></font></td>
          <td colspan=2><font size="2">{english}</font></td></tr>
      <tr><td width="6%"><font size="2"></font></td>
          <td colspan=2><font size="2">負責人︰</font>
              <font size="2">{person}</font></td></tr>
      <tr><td width="6%"><font size="2"></font></td>
          <td colspan=2><font size="2">會員代表︰</font>
              <font size="2">總經理 王大明</font></td></tr>
      <tr><td width="6%"><font size="2"></font></td>
          <td colspan=2><font size="2">地址︰</font>
              <font size="2">台北市中山區中山北路二段45號</font></td></tr>
      <tr><td width="6%"><font size="2"></font></td>
          <td><font size="2">Tel︰ {tel}</font></td>
          <td><font size="2">FAX︰ {fax}</font></td></tr>
      <tr><td width="6%"><font size="2"></font></td>
          <td colspan=2><font size="2">入會年月日︰ 1970 年 10 月 31 日</font></td></tr>
      <tr><td width="6%"><font size="2"></font></td>
          <td><font size="2">註冊編號︰ 0003</font></td>
          <td><font size="2">統一編號︰ {tax_id}</font></td></tr>
      <tr><td width="6%"><font size="2"></font></td>
          <td colspan=2><font size="2">營業範圍︰ 甲種旅行業</font></td></tr>
      <tr><td width="6%"><font size="2"></font></td>
          <td colspan=2><font size="2">E-MAIL︰<a href="mailto:{email}">{email}</a><br>
              網址:<a href="{STALE_HREF}" target="_blank">{website}</a></font></td></tr>
    </table>
    """


LEGACY_HTML = f"""
<html>
<head><meta http-equiv="Content-Type" content="text/html; charset=big5"></head>
<body>
<form method="post" action="memqry.asp">
  <a href="memqry.asp?page=1">第一頁</a>
  <a href="memqry.asp?page=2">下一頁</a>
  <a href="memqry.asp?page=147">最末頁</a>
</form>
<table width="592" border="0" align="center">
  <tr><td>
    <table width="90%" border="0" cellspacing="0" cellpadding="0"
           align="center" class="t_body">
      <tr><td>
        {"".join(_record_table(row) for row in COMPANIES)}
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>
"""

URL = "http://directory.example/main/memqry.asp"


@pytest.fixture(scope="module")
def result():
    return discover_from_html(LEGACY_HTML, URL)


# ------------------------------------------------------------------ 編碼


def test_a_declared_legacy_charset_is_detected():
    """標頭常常只寫 text/html 不附 charset，答案在頁面自己的 meta 標籤裡。"""
    assert sniff_declared_encoding(LEGACY_HTML.encode("utf-8")) == "big5"


def test_the_short_form_meta_charset_is_detected_too():
    assert sniff_declared_encoding(b'<meta charset="Big5">') == "big5"


def test_utf8_needs_no_special_handling():
    """宣告 UTF-8 等於「照原本的做就好」，不必再解一次。"""
    assert sniff_declared_encoding(b'<meta charset="utf-8">') is None
    assert sniff_declared_encoding(b"<html><body>hi</body></html>") is None


def test_big5_is_decoded_with_its_superset():
    """部分台灣站台宣告的 Big5 含有標準 Big5 沒有的字（罕見姓名用字），
    改用 big5hkscs 解碼涵蓋得到，而且對標準 Big5 完全相容。"""
    assert decode_bytes("罕見字：熹".encode("big5hkscs"), "big5") == "罕見字：熹"


def test_an_unknown_encoding_does_not_lose_the_page():
    assert decode_bytes("測試".encode("utf-8"), "definitely-not-a-codec") == "測試"


# -------------------------------------------------------------- 版面分析


def test_each_company_is_one_record(result):
    """外層的分組容器裡有幾家公司就該是幾筆。"""
    assert result.item_count == len(COMPANIES)


def test_the_list_selector_is_scoped_so_it_cannot_match_layout_tables(result):
    """``table`` 這種只有標籤名的選擇器會把排版用的表格一起選進來。
    偵測階段靠「兄弟節點分組」找到清單，那個資訊存進設定就沒了，
    所以範圍必須寫進選擇器本身。"""
    assert result.list_selector == "table.t_body table"


def test_the_company_name_is_found_without_headings_links_or_classes(result):
    """這種頁面沒有 h1-h6、沒有 class，名稱就是卡片裡的第一段文字。"""
    assert "company_name" in result.fields
    assert result.fields["company_name"].samples[0] == COMPANIES[0][0]


def test_the_records_are_not_broken_apart_into_their_links(result):
    """每張卡片裡的連結數量比卡片多，光比數量它們一定贏——結果是
    一筆完整的公司資料被拆成一堆只有幾個字的碎片。"""
    assert " a" not in result.list_selector


# ---------------------------------------------------------- 標籤式的欄位


def test_labelled_fields_are_collected(result):
    first = result.preview[0]
    assert first.phone == COMPANIES[0][3]
    assert first.fax == COMPANIES[0][4]
    assert first.tax_id == COMPANIES[0][5]
    assert first.industry == "甲種旅行業"
    assert first.contact_person == COMPANIES[0][2]


def test_the_english_name_comes_from_the_bilingual_heading(result):
    assert result.preview[0].english_name == COMPANIES[0][1]


def test_fields_with_no_column_of_their_own_are_kept_under_their_own_name(result):
    assert result.extra_field_samples["會員代表"][0] == "總經理 王大明"
    assert result.extra_field_samples["入會年月日"][0] == "1970 年 10 月 31 日"
    assert result.extra_field_samples["註冊編號"][0] == "0003"
    assert result.preview[0].extra_fields["註冊編號"] == "0003"


def test_the_user_is_told_which_free_form_fields_were_found(result):
    """使用者要能在存下來源之前就看到「這些也會收」。"""
    assert any("會員代表" in note for note in result.notes)


# ------------------------------------------------------------ 網址的來源


def test_the_website_comes_from_the_link_text_not_a_stale_href(result):
    """這個名錄在公司沒有網址時，直接把上一家的 href 留在原地，只清空連結
    文字。照 href 抓的話，一整串沒有網站的公司會被填上別人的網址——而畫面上
    根本看不到那個網址。連結文字才是使用者看得到的東西。"""
    websites = [record.website for record in result.preview]
    assert websites[0] == COMPANIES[0][7]
    assert websites[1] is None
    assert STALE_HREF not in [w for w in websites if w]


def test_pagination_is_detected_from_numbered_links(result):
    assert result.page_count == 147
    assert result.page_url_template == "http://directory.example/main/memqry.asp?page={page}"


# ------------------------------------------------- 從偵測到真的爬一次


def _run_the_crawl(result, tmp_config, *, encoding="big5", label_fields=True):
    """把偵測出來的設定拿去跑真正的爬取流程。

    偵測與爬取是兩段不同的程式碼。預覽對了不代表爬出來的一樣——這一段就是
    為了確認「預覽看到什麼，存進資料庫的就是什麼」。

    模擬的回應照著真站台的樣子：內容是 Big5，而 HTTP 標頭不附 charset。
    """
    import httpx

    from crawler.fetcher import HttpxFetcher
    from crawler.robots import RobotsPolicy
    from crawler.sources.generic_html import GenericHtmlSource

    body = LEGACY_HTML.encode("big5hkscs" if encoding else "utf-8")
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, content=body, headers={"Content-Type": "text/html"}
            )
        )
    )
    # SourceConfig 是凍結的（設定讀進來之後就不該被改），所以複製一份。
    source_config = result.to_source_config("legacy").model_copy(
        update={"encoding": encoding, "label_fields": label_fields}
    )

    source = GenericHtmlSource(
        source_config,
        fetcher=HttpxFetcher(
            config=tmp_config, robots=RobotsPolicy("ua", enabled=False), client=client
        ),
        config=tmp_config,
    )
    return list(source.iter_pages())


def test_the_saved_source_carries_the_pages_encoding():
    """編碼要跟著來源存起來，否則下一次爬取又是一整頁亂碼。"""
    from crawler.discover import DiscoveryResult, FieldGuess

    detected = DiscoveryResult(url=URL, list_selector="div.item", encoding="big5")
    detected.fields["company_name"] = FieldGuess(field="company_name", selector="h3")

    assert detected.to_source_config("legacy").encoding == "big5"


def test_a_real_crawl_produces_the_same_values_as_the_preview(result, tmp_config):
    records = _run_the_crawl(result, tmp_config)[0].records

    assert len(records) == len(COMPANIES)
    first = records[0]
    assert first.company_name == COMPANIES[0][0]
    assert first.english_name == COMPANIES[0][1]
    assert first.contact_person == COMPANIES[0][2]
    assert first.phone == COMPANIES[0][3]
    assert first.fax == COMPANIES[0][4]
    assert first.tax_id == COMPANIES[0][5]
    assert first.extra_fields["註冊編號"] == "0003"


def test_label_harvesting_can_be_switched_off(result, tmp_config):
    """頁面內文大量使用冒號時，自由欄位會被灌進雜訊；要有得關。"""
    first = _run_the_crawl(result, tmp_config, label_fields=False)[0].records[0]

    assert first.extra_fields == {}
    assert first.contact_person is None
    # 電話仍然抓得到——那是頁面全文掃描的功勞，跟標籤解析是兩回事。
    assert first.phone == COMPANIES[0][3]


def test_without_the_right_encoding_the_names_come_out_as_mojibake(result, tmp_config):
    """這是不做編碼偵測的後果：不會報錯，只會安靜地存進一堆看不懂的字。
    留著這個測試，是為了讓「編碼設對了」有一個對照組。"""
    first = _run_the_crawl(result, tmp_config, encoding=None)[0].records[0]
    assert first.company_name == COMPANIES[0][0]
