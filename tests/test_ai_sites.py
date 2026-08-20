"""Tests for ai/sites.py。

**這個檔案守的是：模型不能自己生出一個網址，也不能決定要抓什麼。**

第一條在這裡驗（:func:`classify` 只認編號）；第二條在
``tests/test_ai_controller.py`` 驗（候選清單出來時一個候選網站都沒有被碰到）。

兩條都不是靠 prompt 請模型配合。模型「順手」補一個它覺得應該存在的網址，
下一步就會變成程式對一個沒有人要求過的網站發連線——那種錯不會有任何提示。
"""

from __future__ import annotations

import json

import pytest

from ai.sites import (
    COMPANY,
    DIRECTORY,
    MAX_HITS,
    UNJUDGED,
    UNRELATED,
    Candidate,
    SiteSearchResult,
    build_messages,
    classify,
    find_sites,
)
from crawler.websearch import SearchHit

HITS = [
    SearchHit(
        url="https://www.tami.org.tw/members",
        title="台灣工具機公會 會員名錄",
        snippet="本會會員廠商一覽，共 380 家。",
    ),
    SearchHit(
        url="https://www.example-cnc.com.tw/",
        title="精展機械股份有限公司",
        snippet="CNC 車床、銑床製造商。",
    ),
    SearchHit(
        url="https://news.example.com/2026/cnc-market",
        title="工具機市場回溫 業者看好下半年",
        snippet="記者報導……",
    ),
]


def _chat(reply: str):
    def chat(_messages):
        return reply

    return chat


def _labels(*items: dict) -> str:
    return json.dumps(list(items), ensure_ascii=False)


# --------------------------------------------------------------- 只認編號


def test_a_url_the_model_wrote_itself_never_becomes_a_candidate():
    """這是整個功能的安全底線。

    模型除了替既有的三筆貼標籤之外，又「順手」補了一個它覺得應該存在的公會
    網址。那個網址下一步會被真的送出請求——它必須進不來。
    """
    result = classify(
        HITS,
        [
            {"index": 0, "kind": "directory", "reason": "公會會員名冊"},
            {"url": "https://www.made-up-association.org.tw/", "kind": "directory"},
        ],
    )

    urls = [candidate.url for candidate in result.candidates]
    assert "https://www.made-up-association.org.tw/" not in urls
    assert urls == [hit.url for hit in HITS] or set(urls) == {h.url for h in HITS}
    # 而且要算進「對不上的編號」，那個數字就是「這個模型可不可信」的訊號。
    assert result.ignored == 1


@pytest.mark.parametrize("index", [-1, 3, 99])
def test_an_index_outside_the_list_is_ignored(index):
    result = classify(HITS, [{"index": index, "kind": "directory"}])

    assert result.ignored == 1
    assert all(c.kind == UNJUDGED for c in result.candidates)


def test_the_same_index_twice_only_counts_once():
    """模型有時候會把同一筆講兩次，而且兩次講的類型不一樣。"""
    result = classify(
        HITS,
        [
            {"index": 0, "kind": "directory", "reason": "先說是名錄"},
            {"index": 0, "kind": "unrelated", "reason": "又說不相關"},
        ],
    )

    assert result.ignored == 1
    first = next(c for c in result.candidates if c.url == HITS[0].url)
    assert first.kind == DIRECTORY      # 第一次講的算數


@pytest.mark.parametrize("raw", [1, "1", "[1]", " 1 "])
def test_an_index_written_as_a_string_still_works(raw):
    """模型很常把編號寫成字串。那是格式問題，不是資料問題。"""
    result = classify(HITS, [{"index": raw, "kind": "company"}])

    assert result.ignored == 0
    assert next(c for c in result.candidates if c.url == HITS[1].url).kind == COMPANY


# ---------------------------------------------------- 沒判斷 ≠ 判斷為不相關


def test_a_hit_the_model_skipped_is_marked_unjudged_not_unrelated():
    """兩者要分得開。

    「模型看過、說不相關」與「模型根本沒提到」是不一樣的資訊——後者代表模型
    偷懶或回覆被截斷，使用者可能還是想自己看一眼那幾筆。
    """
    result = classify(HITS, [{"index": 0, "kind": "directory"}])

    skipped = [c for c in result.candidates if c.kind == UNJUDGED]
    assert len(skipped) == 2
    assert all("沒有提到" in c.reason for c in skipped)
    assert any("不等於它說不相關" in note for note in result.notes())


def test_only_directories_and_company_sites_are_worth_crawling_by_default():
    """預設全勾的話，模型判斷失準的那幾筆會安靜地變成真的請求。"""
    result = classify(
        HITS,
        [
            {"index": 0, "kind": "directory"},
            {"index": 1, "kind": "company"},
            {"index": 2, "kind": "unrelated"},
        ],
    )

    assert [c.url for c in result.worth_crawling] == [HITS[0].url, HITS[1].url]


def test_an_unrecognised_kind_falls_back_to_unrelated():
    """往保守的那一邊猜：抓錯網站要花使用者的時間與對方的頻寬。"""
    result = classify(HITS, [{"index": 0, "kind": "名錄"}, {"index": 1, "kind": ""}])

    assert all(not c.worth_crawling for c in result.candidates)


def test_directories_come_first():
    """一頁很多家，那是使用者最想先看到的。"""
    result = classify(
        HITS,
        [
            {"index": 0, "kind": "unrelated"},
            {"index": 1, "kind": "company"},
            {"index": 2, "kind": "directory"},
        ],
    )

    assert [c.kind for c in result.candidates] == [DIRECTORY, COMPANY, UNRELATED]


# ------------------------------------------------------------------ 送出的字


def test_the_model_gets_numbered_entries_and_the_query():
    """模型回的是編號，所以它拿到的一定要有編號。"""
    messages = build_messages("台中 CNC 加工", HITS)

    user = messages[1].content
    assert "[0]" in user and "[1]" in user and "[2]" in user
    assert "台中 CNC 加工" in user
    assert HITS[0].url in user
    # 系統訊息裡要講清楚不准自己補網址——程式擋是一回事，讓模型的回答跟程式
    # 的行為一致是另一回事。
    assert "不要新增清單上沒有的網址" in messages[0].content


def test_a_long_reason_is_cut_so_the_table_stays_readable():
    result = classify(HITS, [{"index": 0, "kind": "directory", "reason": "很長" * 100}])

    reason = next(c for c in result.candidates if c.url == HITS[0].url).reason
    assert len(reason) <= 61          # MAX_REASON_CHARS + 省略號
    assert reason.endswith("…")


# ------------------------------------------------------------------ 端到端


def test_find_sites_never_asks_the_model_when_there_is_nothing_to_judge():
    """搜不到東西時連問都不必問——那是一次白花的錢。"""

    def explode(_messages):
        raise AssertionError("沒有搜尋結果時不該送給模型")

    result = find_sites("找不到的東西", [], explode)
    assert result.candidates == []
    assert result.notes() == []


def test_find_sites_labels_every_hit():
    result = find_sites(
        "台中 CNC 加工",
        HITS,
        _chat(
            _labels(
                {"index": 0, "kind": "directory", "reason": "公會的會員名冊"},
                {"index": 1, "kind": "company", "reason": "看起來是單一公司官網"},
                {"index": 2, "kind": "unrelated", "reason": "新聞報導"},
            )
        ),
    )

    assert result.found == 3
    assert len(result.worth_crawling) == 2
    assert result.ignored == 0


def test_more_hits_than_the_cap_are_not_sent():
    """每多一頁搜尋結果就是對 DuckDuckGo 多一次請求，而它會限流。"""
    many = [SearchHit(url=f"https://example{i}.test/") for i in range(MAX_HITS + 5)]
    result = find_sites("很多結果", many, _chat("[]"))

    assert result.found == MAX_HITS


def test_notes_say_how_many_of_each_kind():
    result = SiteSearchResult(
        query="x",
        found=3,
        candidates=[
            Candidate(url="a", kind=DIRECTORY),
            Candidate(url="b", kind=COMPANY),
            Candidate(url="c", kind=UNRELATED),
        ],
    )

    notes = " ".join(result.notes())
    assert "名錄 1" in notes and "單一公司 1" in notes
