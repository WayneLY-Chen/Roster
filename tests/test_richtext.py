"""信件內文的格式轉換測試。

最重要的性質是**來回轉換無損**：使用者存檔、關掉程式、重新打開，看到的排版必須
和存檔前一樣。這裡的每個 round-trip 測試都在盯這件事——格式在存檔時靜靜地掉一半，
是那種要等到信已經寄出去才會發現的錯。
"""

from __future__ import annotations

import pytest

from gmail.richtext import (
    Document,
    Segment,
    document_to_html,
    html_to_document,
    html_to_plain_text,
    looks_like_html,
)


def _doc(*lines: list[Segment]) -> Document:
    return Document(lines=list(lines))


def _round_trip(document: Document) -> Document:
    return html_to_document(document_to_html(document))


def _flatten(document: Document) -> list[list[tuple[str, frozenset[str], str | None, str | None]]]:
    return [
        [(s.text, s.styles, s.link, s.image) for s in line if s.text or s.image]
        for line in document.lines
    ]


# ------------------------------------------------------------------ 轉成 HTML


def test_plain_paragraph() -> None:
    assert document_to_html(_doc([Segment("您好")])) == "<p>您好</p>"


def test_bold_and_italic() -> None:
    html = document_to_html(_doc([Segment("重要", frozenset({"bold", "italic"}))]))
    assert "<strong>" in html and "<em>" in html and "重要" in html


def test_heading() -> None:
    """信件內文不該出現 <h1>，那是整封信的標題。"""
    assert document_to_html(_doc([Segment("標題", frozenset({"h1"}))])) == "<h2>標題</h2>"


def test_link() -> None:
    html = document_to_html(_doc([Segment("官網", link="https://example.com")]))
    assert html == '<p><a href="https://example.com">官網</a></p>'


def test_image() -> None:
    html = document_to_html(_doc([Segment("", image="images/logo.png")]))
    assert html == '<p><img src="images/logo.png"></p>'


def test_consecutive_bullets_share_one_list() -> None:
    """一行一個 <ul> 會讓收信端把清單畫得item之間都有空行。"""
    html = document_to_html(
        _doc(
            [Segment("甲", frozenset({"bullet"}))],
            [Segment("乙", frozenset({"bullet"}))],
        )
    )
    assert html.count("<ul>") == 1 and html.count("</ul>") == 1
    assert html.count("<li>") == 2


def test_list_is_closed_before_a_normal_paragraph() -> None:
    html = document_to_html(
        _doc([Segment("項目", frozenset({"bullet"}))], [Segment("結尾")])
    )
    assert html.index("</ul>") < html.index("<p>結尾</p>")


def test_blank_lines_are_preserved() -> None:
    """使用者刻意留的空行是排版的一部分，不能被吃掉。"""
    html = document_to_html(_doc([Segment("上")], [], [Segment("下")]))
    assert html == "<p>上</p>\n<p><br></p>\n<p>下</p>"


def test_html_special_characters_are_escaped() -> None:
    html = document_to_html(_doc([Segment('價格 < 100 & "免運"')]))
    assert "&lt;" in html and "&amp;" in html and "&quot;" in html
    assert "<100" not in html


def test_quotes_in_a_link_are_escaped() -> None:
    html = document_to_html(_doc([Segment("點我", link='https://x.test/?a="b"')]))
    assert '"' not in html.split("href=")[1].split(">")[0].strip('"')


# ------------------------------------------------------------------ 讀回來


def test_round_trip_keeps_plain_text() -> None:
    assert _flatten(_round_trip(_doc([Segment("您好")]))) == [[("您好", frozenset(), None, None)]]


@pytest.mark.parametrize("style", ["bold", "italic", "underline"])
def test_round_trip_keeps_inline_styles(style) -> None:
    result = _round_trip(_doc([Segment("字", frozenset({style}))]))
    assert style in result.lines[0][0].styles


@pytest.mark.parametrize("style", ["h1", "h2", "bullet"])
def test_round_trip_keeps_block_styles(style) -> None:
    result = _round_trip(_doc([Segment("字", frozenset({style}))]))
    assert style in result.lines[0][0].styles


def test_round_trip_keeps_links() -> None:
    result = _round_trip(_doc([Segment("官網", link="https://example.com")]))
    assert result.lines[0][0].link == "https://example.com"


def test_round_trip_keeps_images() -> None:
    result = _round_trip(_doc([Segment("", image="images/logo.png")]))
    assert result.lines[0][0].image == "images/logo.png"


def test_round_trip_keeps_a_multi_line_document() -> None:
    original = _doc(
        [Segment("標題", frozenset({"h1"}))],
        [Segment("一般文字，"), Segment("這段是粗體", frozenset({"bold"}))],
        [Segment("甲", frozenset({"bullet"}))],
        [Segment("乙", frozenset({"bullet"}))],
        [Segment("聯絡我們：")],
        [Segment("按這裡", link="https://example.com")],
    )
    assert _flatten(_round_trip(original)) == _flatten(original)


def test_round_trip_keeps_escaped_characters() -> None:
    result = _round_trip(_doc([Segment('價格 < 100 & "免運"')]))
    assert result.lines[0][0].text == '價格 < 100 & "免運"'


def test_unknown_tags_keep_their_text() -> None:
    """掉格式可以接受，掉內容不行。"""
    document = html_to_document("<p>前<span style='color:red'>中間</span>後</p>")
    text = "".join(segment.text for segment in document.lines[0])
    assert "前" in text and "中間" in text and "後" in text


def test_legacy_plain_text_templates_still_load() -> None:
    """舊的樣板都是純文字存的，不能因為改用 HTML 就讀不出來。"""
    document = html_to_document("第一行\n第二行")
    assert [seg.text for line in document.lines for seg in line] == ["第一行", "第二行"]


def test_empty_source_gives_one_empty_line() -> None:
    assert html_to_document("").lines == [[]]


def test_document_is_empty() -> None:
    assert _doc([], [Segment("   ")]).is_empty()
    assert not _doc([Segment("字")]).is_empty()
    assert not _doc([Segment("", image="a.png")]).is_empty()


# --------------------------------------------------------------- looks_like_html


@pytest.mark.parametrize(
    "source", ["<p>hi</p>", "a<br>b", "<strong>x</strong>", '<img src="a.png">']
)
def test_recognises_html(source) -> None:
    assert looks_like_html(source)


@pytest.mark.parametrize(
    "source",
    [
        "",
        "純文字",
        "價格 < 100 > 80",         # 數學符號不是標籤
        "我 <3 這個產品",           # 顏文字也不是
        "寄到 a@b.com",
    ],
)
def test_does_not_mistake_plain_text_for_html(source) -> None:
    assert not looks_like_html(source)


# ------------------------------------------------------------------ 純文字版


def test_plain_text_version_strips_tags() -> None:
    text = html_to_plain_text("<p>您好</p><p><strong>謝謝</strong></p>")
    assert "您好" in text and "謝謝" in text
    assert "<" not in text


def test_plain_text_version_keeps_link_targets() -> None:
    """純文字看不到 href，網址要補出來才點得到。"""
    text = html_to_plain_text('<p><a href="https://example.com">官網</a></p>')
    assert "https://example.com" in text


def test_plain_text_version_marks_bullets() -> None:
    assert "・" in html_to_plain_text("<ul><li>項目</li></ul>")


def test_plain_text_version_notes_images() -> None:
    assert "[圖片" in html_to_plain_text('<p><img src="logo.png"></p>')


def test_plain_text_of_plain_text_is_unchanged() -> None:
    assert html_to_plain_text("已經是純文字了") == "已經是純文字了"


# ------------------------------------------------------------- 空行不會增生


def test_a_blank_paragraph_is_exactly_one_blank_line() -> None:
    """``<p><br></p>`` 是一個空行。

    如果 ``</p>`` 在 ``<br>`` 之後又補一個，每存一次檔空行就多一行——編輯三次
    之後，信裡就會出現一大段莫名其妙的空白。
    """
    document = html_to_document("<p>上</p><p><br></p><p>下</p>")
    texts = ["".join(s.text for s in line).strip() for line in document.lines]
    assert texts == ["上", "", "下"]


def test_blank_lines_survive_repeated_round_trips() -> None:
    source = "<p>上</p>\n<p><br></p>\n<p>下</p>"
    once = document_to_html(html_to_document(source))
    twice = document_to_html(html_to_document(once))
    assert once == twice == source


def test_a_break_inside_a_paragraph_makes_two_lines() -> None:
    document = html_to_document("<p>甲<br>乙</p>")
    texts = ["".join(s.text for s in line).strip() for line in document.lines]
    assert texts == ["甲", "乙"]


def test_an_empty_paragraph_is_a_blank_line() -> None:
    document = html_to_document("<p>上</p><p></p><p>下</p>")
    texts = ["".join(s.text for s in line).strip() for line in document.lines]
    assert texts == ["上", "", "下"]


def test_a_full_document_is_stable_across_round_trips() -> None:
    """編輯、存檔、再打開、再存檔，內容必須一模一樣。"""
    source = (
        "<h2>合作提案</h2>\n"
        "<p>{company_name} 您好，</p>\n"
        "<p>我們是<strong>某某公司</strong>。</p>\n"
        "<ul>\n<li>交期短</li>\n<li>品質穩定</li>\n</ul>\n"
        '<p><a href="https://example.com">公司網站</a></p>\n'
        "<p><br></p>\n"
        "<p>敬祝 商祺</p>"
    )
    once = document_to_html(html_to_document(source))
    assert document_to_html(html_to_document(once)) == once


def test_placeholders_survive_formatting() -> None:
    """{company_name} 這種變數不能被當成 HTML 處理掉。"""
    document = html_to_document("<p>{company_name} 您好</p>")
    assert "{company_name}" in "".join(s.text for s in document.lines[0])
