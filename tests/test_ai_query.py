"""Tests for ai/query.py。

**這個檔案守的是兩條：唯讀，而且答案裡的數字不是模型寫的。**

第二條特別容易被「改得更好用」的重構破壞——讓模型自己組一句話回答，看起來
比較自然，但那一句話裡的數字使用者沒有任何辦法驗證。編出來的「有 12 家」跟
真的長得一模一樣，而他會拿它去做決定。
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from ai.query import (
    CANNOT_TOOL,
    FIND_TOOL,
    MAX_LIMIT,
    PARAMS,
    Answer,
    Query,
    ask,
    build_messages,
    parse_call,
    run,
    to_filters,
)
from core.errors import AIError
from core.schemas import CompanyFilter
from database.models import now
from database.repository import CompanyRepository

TODAY = date(2026, 8, 20)


def _call(**arguments) -> str:
    mode = arguments.pop("_mode", "list")
    return json.dumps(
        {"tool": FIND_TOOL, "mode": mode, "arguments": arguments}, ensure_ascii=False
    )


def _chat(reply: str):
    def chat(_messages):
        return reply

    return chat


# ------------------------------------------------------------ 沒有寫入的工具


def test_the_only_tools_are_read_only():
    """「叫它刪資料時，它做不到」——不是它拒絕，是沒有那個工具。

    這條測的是工具的**存在性**。名字裡出現 delete／update／create 的工具一個
    都不該有；有的話這個功能的整個安全論述就垮了。
    """
    import ai.query as module

    tools = {
        value
        for name, value in vars(module).items()
        if name.endswith("_TOOL") and isinstance(value, str)
    }
    assert tools == {FIND_TOOL, CANNOT_TOOL}


def test_asking_it_to_delete_gets_a_tool_that_does_not_exist():
    """模型「決定」要刪資料時，程式手上沒有那個東西可以執行。"""
    reply = json.dumps({"tool": "delete_companies", "arguments": {"city": "台中"}})

    with pytest.raises(AIError) as caught:
        parse_call(reply)

    message = str(caught.value)
    assert "不存在" in message
    assert "公司資訊" in message      # 告訴他真正該去哪裡改


def test_the_module_never_names_a_writing_method():
    """這是一條「別讓它悄悄長回來」的測試。

    唯讀不是靠 if 擋的，是靠「這個模組根本沒有呼叫那些方法」。有人為了加一個
    「順便標記成已聯絡」的功能而在這裡呼叫 update()，這條會擋下來。
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "ai" / "query.py").read_text(
        encoding="utf-8"
    )
    # 註解與 docstring 裡提到這些名字是可以的（它們正好在解釋為什麼不用），
    # 所以只看實際會執行的那幾行。
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    for forbidden in ("repo.upsert", "repo.update", "repo.delete", "repo.create",
                      "session.add", "session.commit", "session.delete"):
        assert forbidden not in code, f"ai/query.py 不該呼叫 {forbidden}"


# ----------------------------------------------------- 數字不是模型寫的


def test_the_headline_number_comes_from_the_database_not_the_model(db_session, tmp_config):
    """模型在回覆裡塞了一個假數字，它到不了畫面上。"""
    repo = CompanyRepository(db_session)
    for index in range(3):
        repo.create(company_name=f"台中公司{index}", address="台中市西屯區", dedupe_key=f"k{index}")
    db_session.commit()

    # 模型除了條件之外還「順便」寫了 total 與一句話。兩個都該被忽略。
    reply = json.dumps(
        {
            "tool": FIND_TOOL,
            "mode": "count",
            "arguments": {"city": "台中"},
            "total": 999,
            "answer": "總共有 999 家台中的公司。",
        },
        ensure_ascii=False,
    )
    answer = ask("台中有幾家？", _chat(reply), lambda q: run(q, repo))

    assert answer.total == 3
    assert "3" in answer.headline()
    assert "999" not in answer.headline()


def test_every_answer_says_what_it_filtered_on(db_session, tmp_config):
    """「有 12 家」沒有辦法被驗證，「地址包含 = 台中、還沒聯絡過：12 家」可以。"""
    repo = CompanyRepository(db_session)
    answer = ask(
        "哪些台中的公司還沒聯絡過？",
        _chat(_call(city="台中", never_emailed=True)),
        lambda q: run(q, repo),
    )

    basis = " ".join(answer.notes())
    assert "依據：" in basis
    assert "地址包含 = 台中" in basis
    assert "還沒聯絡過" in basis


def test_a_query_with_no_filters_says_so_rather_than_leaving_it_blank(db_session, tmp_config):
    """空白的「依據」比沒有依據更糟：使用者會以為有條件而沒有顯示出來。"""
    repo = CompanyRepository(db_session)
    answer = ask("我總共有幾家？", _chat(_call(_mode="count")), lambda q: run(q, repo))

    assert "整個名單" in " ".join(answer.notes())


# ------------------------------------------------------------- 答不出來


def test_a_question_the_database_cannot_answer_is_not_forced_into_a_query():
    """查出來的東西跟他問的不是同一件事，比誠實說不知道糟得多。"""
    reply = json.dumps(
        {"tool": CANNOT_TOOL, "reason": "資料庫裡沒有存員工人數"}, ensure_ascii=False
    )
    call = parse_call(reply)

    assert call == "資料庫裡沒有存員工人數"


def test_cannot_answer_reaches_the_user_with_the_reason(db_session, tmp_config):
    repo = CompanyRepository(db_session)
    answer = ask(
        "哪一家的員工最多？",
        _chat(json.dumps({"tool": CANNOT_TOOL, "reason": "沒有存員工人數"})),
        lambda q: run(q, repo),
    )

    assert answer.answered is False
    assert "沒有存員工人數" in answer.headline()
    assert answer.total == 0
    assert answer.companies == []


def test_a_reply_with_no_json_is_an_error_not_a_silent_empty_query():
    """看不懂的回覆不能退化成「沒有條件」——那會回傳整個資料庫當答案。"""
    with pytest.raises(AIError):
        parse_call("我不太確定你的意思。")


# ------------------------------------------------------------ 條件白名單


def test_a_condition_that_is_not_on_the_whitelist_is_dropped_and_listed():
    """模型自己發明的條件不會變成查詢的一部分，而且使用者看得到它試過。"""
    call = parse_call(_call(city="台中", employee_count=100, revenue="over 1e8"))

    assert isinstance(call, Query)
    assert call.arguments == {"city": "台中"}
    assert set(call.ignored) == {"employee_count", "revenue"}
    assert "employee_count" in " ".join(
        Answer(query=call).notes()
    )


def test_a_stage_outside_the_pipeline_is_ignored_rather_than_guessed():
    call = parse_call(_call(stage="很有興趣"))
    assert isinstance(call, Query)
    assert "stage" not in call.arguments


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(True, True), ("true", True), ("是", True), (False, False), ("false", False)],
)
def test_booleans_written_in_the_usual_model_habits_still_work(raw, expected):
    call = parse_call(_call(never_emailed=raw))
    assert call.arguments["never_emailed"] is expected


def test_a_limit_beyond_the_cap_is_clamped(db_session, tmp_config):
    repo = CompanyRepository(db_session)
    for index in range(3):
        repo.create(company_name=f"公司{index}", dedupe_key=f"k{index}")
    db_session.commit()

    total, shown = run(Query(arguments={"limit": MAX_LIMIT * 10}), repo)
    assert total == 3 and len(shown) == 3


def test_mode_defaults_to_list_when_the_model_does_not_say():
    """列出來的清單本來就看得到數量，反過來則不然。"""
    assert parse_call(json.dumps({"tool": FIND_TOOL, "arguments": {}})).mode == "list"


# ------------------------------------------------------------ 條件怎麼翻


def test_never_emailed_maps_to_the_last_emailed_column():
    """「還沒聯絡過」問的是程式蓋的章，不是使用者手動標的業務階段。

    業務階段很常整批停在 New，因為沒有人回頭去改它——用它回答這個問題會得到
    一個看起來合理但錯的數字。
    """
    criteria = to_filters(Query(arguments={"never_emailed": True}), today=TODAY)[0]
    assert criteria.emailed is False
    assert criteria.stages == []


def test_recent_days_becomes_a_real_date(tmp_config):
    criteria = to_filters(
        Query(arguments={"created_within_days": 30}), today=TODAY
    )[0]

    assert criteria.created_after == datetime(2026, 7, 21)


def test_both_spellings_of_tai_are_searched(db_session, tmp_config):
    """「台」與「臺」在同一個資料庫裡混著出現是常態。

    只查一種寫法的話，漏掉的部分完全看不出來——答案只是安靜地變少，而使用者
    會拿那個數字去做決定。
    """
    repo = CompanyRepository(db_session)
    repo.create(company_name="甲公司", address="台中市西屯區一號", dedupe_key="a")
    repo.create(company_name="乙公司", address="臺中市南屯區二號", dedupe_key="b")
    repo.create(company_name="丙公司", address="高雄市前鎮區三號", dedupe_key="c")
    db_session.commit()

    total, companies = run(Query(arguments={"city": "台中"}), repo)

    assert total == 2
    assert {c.company_name for c in companies} == {"甲公司", "乙公司"}


def test_a_city_without_tai_is_only_searched_once():
    assert len(to_filters(Query(arguments={"city": "新北"}))) == 1


def test_the_same_company_is_not_counted_twice_when_both_spellings_match(
    db_session, tmp_config
):
    """兩次查詢合併時要以 id 去重，否則「台臺」都寫的地址會被算兩次。"""
    repo = CompanyRepository(db_session)
    repo.create(company_name="兩種都寫", address="台中市（臺中市）一號", dedupe_key="a")
    db_session.commit()

    total, _ = run(Query(arguments={"city": "台中"}), repo)
    assert total == 1


# --------------------------------------------------------------- 送出的字


def test_the_model_is_told_todays_date():
    """模型不知道今天幾號，而「最近一個月」全靠它。"""
    messages = build_messages("最近一個月收了幾家？", today=TODAY)
    assert "2026-08-20" in messages[1].content


def test_the_prompt_lists_every_whitelisted_condition():
    """參數清單只該有一份。程式碼加了一個條件而 prompt 沒有，模型永遠不會用它。"""
    system = build_messages("隨便問", today=TODAY)[0].content
    for param in PARAMS:
        assert param.name in system


def test_the_prompt_tells_the_model_not_to_write_numbers():
    """程式擋是一回事，讓模型的回答跟程式的行為一致是另一回事。"""
    system = build_messages("隨便問", today=TODAY)[0].content
    assert "不要自己寫出任何數字" in system


# ------------------------------------------------------------ 真的查得對


def test_the_number_matches_what_the_same_filter_returns(db_session, tmp_config):
    """算做完了的條件之一：數字要跟自己去「公司資訊」頁篩出來的一樣。

    所以這裡拿同一組 CompanyFilter 直接問 repository 再比一次——兩邊不一樣就
    代表這一層在某個地方多做或少做了什麼。
    """
    repo = CompanyRepository(db_session)
    for index in range(5):
        company = repo.create(
            company_name=f"金屬公司{index}",
            industry="金屬加工",
            address="台中市西屯區",
            dedupe_key=f"m{index}",
        )
        if index < 2:
            company.last_emailed_at = now()      # 這兩家寄過信了
    repo.create(company_name="別的產業", industry="食品", dedupe_key="x")
    db_session.commit()

    query = Query(arguments={"industry": "金屬加工", "never_emailed": True})
    total, _ = run(query, repo)

    same = repo.count(CompanyFilter(industry="金屬加工", emailed=False))
    assert total == same == 3
