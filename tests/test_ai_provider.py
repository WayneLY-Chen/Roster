"""Tests for ai/provider.py and ai/prompts.py.

沒有任何一個測試會連到真的網路。三家供應商的 HTTP 往來都用
``httpx.MockTransport`` 假的，重點放在「我們送出去的東西長什麼樣」與
「對方回什麼我們怎麼解讀」——那兩件事才是這一層會出錯的地方。
"""

from __future__ import annotations

import json

import httpx
import pytest

from ai.prompts import BASE_SYSTEM_PROMPT, build_system_prompt
from ai.provider import (
    ANTHROPIC_KEY_SECRET,
    OPENROUTER_KEY_SECRET,
    AnthropicProvider,
    ChatMessage,
    OllamaProvider,
    OpenRouterProvider,
    available_providers,
    get_provider,
)
from core.errors import AIError, AINotConfigured


@pytest.fixture
def no_secrets(monkeypatch):
    """預設什麼金鑰都沒有。要有的測試自己塞。"""
    store: dict[str, str] = {}
    monkeypatch.setattr("ai.provider.get_secret", lambda name: store.get(name, ""))
    return store


def _transport(handler):
    """把 httpx 的三個入口都導向假的傳輸層。

    ``httpx.get``/``post``/``stream`` 各自建立自己的 Client，所以三個都要
    換掉——只換一個的話，測試會在另一條路上真的連出去。
    """
    transport = httpx.MockTransport(handler)

    def fake_get(url, **kwargs):
        with httpx.Client(transport=transport) as client:
            return client.get(url, **kwargs)

    def fake_post(url, **kwargs):
        with httpx.Client(transport=transport) as client:
            return client.post(url, **kwargs)

    def fake_stream(method, url, **kwargs):
        client = httpx.Client(transport=transport)
        return client.stream(method, url, **kwargs)

    return fake_get, fake_post, fake_stream


@pytest.fixture
def mock_http(monkeypatch):
    def install(handler):
        get, post, stream = _transport(handler)
        monkeypatch.setattr(httpx, "get", get)
        monkeypatch.setattr(httpx, "post", post)
        monkeypatch.setattr(httpx, "stream", stream)

    return install


# ------------------------------------------------------------------ prompts


def test_user_prompt_is_appended_never_prepended():
    """使用者的指示一定在內建那段的後面。

    順序就是位階。反過來的話，使用者寫的東西會變成模型最先讀到的規則，
    而內建那幾條（不要編造資料、不要幫忙繞過網站限制）就成了可以被覆寫的
    建議——那正是這個函式存在的理由。
    """
    combined = build_system_prompt("只抓工具機業")
    assert combined.startswith(BASE_SYSTEM_PROMPT.rstrip())
    assert combined.index(BASE_SYSTEM_PROMPT.rstrip()) < combined.index("只抓工具機業")


def test_empty_user_prompt_adds_nothing():
    assert build_system_prompt("") == BASE_SYSTEM_PROMPT.rstrip()
    assert build_system_prompt(None) == BASE_SYSTEM_PROMPT.rstrip()
    assert build_system_prompt("   ") == BASE_SYSTEM_PROMPT.rstrip()


@pytest.mark.parametrize(
    "topic",
    ["抓網頁", "繞過", "robots.txt", "編造", "歧視", "髒話"],
)
def test_base_prompt_covers_each_guardrail(topic):
    """這幾條被刪掉時要有人發現。

    這是一條「內容」測試，平常很少寫——但這段文字是產品行為的一部分，
    有人為了省 token 精簡它的時候，少掉哪一條完全看不出來。
    """
    assert topic in BASE_SYSTEM_PROMPT


def test_prompt_does_not_frame_web_scraping_itself_as_forbidden():
    """「模型自己抓不了網頁」是能力描述，不是禁令，兩者不可以混在一起。

    這是使用者實際提出的疑問：「外面的 AI 都能爬，我這哪有違規？」——他說得
    對，自動化蒐集公開資料本來就正當，而且主流 AI 的瀏覽功能同樣遵守
    robots.txt。這支程式也一樣：v1.22 起模型讀得到網頁了，但**發出請求的一直
    是程式**（見 crawler/fetcher.py），模型手上沒有任何連網的工具。講清楚是為
    了防止它在對話裡說「我去幫你查」然後編一個信箱出來；那跟「不准爬」是兩件事。

    寫混的話模型會過度拒絕——使用者叫它幫忙看一份已經抓回來的名錄，它回
    「抱歉我不能爬網站」。
    """
    assert "不是禁止你上網" in BASE_SYSTEM_PROMPT
    assert "正當" in BASE_SYSTEM_PROMPT
    # 真正的規則限制的是「繞過對方明示的拒絕」，不是蒐集公開資料本身。
    assert "繞過對方明示的拒絕" in BASE_SYSTEM_PROMPT


# --------------------------------------------------------------- 供應商選擇


def test_available_providers_lists_auto_first():
    options = available_providers()
    assert list(options)[0] == "auto"
    assert set(options) >= {"auto", "ollama", "openrouter", "anthropic"}


def test_auto_prefers_the_local_provider(no_secrets, mock_http, tmp_config):
    """本機跑得起來就用本機，即使兩把金鑰都設好了。

    這條守的是隱私預設值，不是效能：兩者都可用時選擇本機，代表使用者的
    資料預設不會離開這台機器。
    """
    no_secrets[OPENROUTER_KEY_SECRET] = "sk-or-test"
    no_secrets[ANTHROPIC_KEY_SECRET] = "sk-ant-test"
    mock_http(lambda request: httpx.Response(200, json={"models": []}))

    assert get_provider("auto").name == "ollama"


def test_auto_falls_back_to_a_key_when_no_local_model(no_secrets, mock_http):
    """Ollama 連不上就往下找有金鑰的。"""
    no_secrets[OPENROUTER_KEY_SECRET] = "sk-or-test"

    def handler(request):
        if "11434" in str(request.url):
            raise httpx.ConnectError("no ollama", request=request)
        return httpx.Response(200, json={"data": []})

    mock_http(handler)
    assert get_provider("auto").name == "openrouter"


def test_auto_with_nothing_configured_explains_both_routes(no_secrets, mock_http):
    """什麼都沒設定時的訊息要能讓人動起來，不是「錯誤」兩個字。"""

    def handler(request):
        raise httpx.ConnectError("nothing", request=request)

    mock_http(handler)
    with pytest.raises(AINotConfigured) as excinfo:
        get_provider("auto")
    message = str(excinfo.value)
    assert "ollama.com" in message and "openrouter.ai" in message


def test_unknown_provider_name_is_rejected(no_secrets):
    with pytest.raises(AIError):
        get_provider("chatgpt-plus")


# ------------------------------------------------------------------ OpenRouter


def test_openrouter_without_a_key_says_where_to_put_one(no_secrets):
    with pytest.raises(AINotConfigured) as excinfo:
        OpenRouterProvider().chat([ChatMessage("user", "hi")], "some/model")
    assert "設定" in str(excinfo.value)


def test_openrouter_sends_key_and_identifies_itself(no_secrets, mock_http):
    no_secrets[OPENROUTER_KEY_SECRET] = "sk-or-test"
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["title"] = request.headers.get("x-title")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "嗨"}}]}
        )

    mock_http(handler)
    reply = OpenRouterProvider().chat([ChatMessage("user", "hi")], "anthropic/claude")

    assert reply == "嗨"
    assert seen["auth"] == "Bearer sk-or-test"
    # 表明身分，不是偽裝成別人——跟爬取時的立場一致。
    assert seen["title"] == "Roster"
    assert seen["body"]["model"] == "anthropic/claude"


def test_openrouter_free_models_sort_first(no_secrets, mock_http):
    no_secrets[OPENROUTER_KEY_SECRET] = "sk-or-test"
    mock_http(
        lambda request: httpx.Response(
            200,
            json={
                "data": [
                    {"id": "paid/one", "name": "Zed", "pricing": {"prompt": "0.001", "completion": "0.002"}},
                    {"id": "free/one", "name": "Alpha", "pricing": {"prompt": "0", "completion": "0"}},
                ]
            },
        )
    )
    models = OpenRouterProvider().list_models()
    assert [m.id for m in models] == ["free/one", "paid/one"]
    assert models[0].free is True


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "金鑰"),
        (402, "額度"),
        (429, "太頻繁"),
        (404, "模型"),
    ],
)
def test_http_errors_become_actionable_sentences(no_secrets, mock_http, status, expected):
    """把狀態碼翻成使用者有辦法處理的話。

    「HTTP 402」沒有人知道那是要儲值。
    """
    no_secrets[OPENROUTER_KEY_SECRET] = "sk-or-test"
    mock_http(lambda request: httpx.Response(status, json={"error": {"message": "x"}}))

    with pytest.raises(AIError) as excinfo:
        OpenRouterProvider().chat([ChatMessage("user", "hi")], "m")
    assert expected in str(excinfo.value)


def test_openrouter_streams_chunks_in_order(no_secrets, mock_http):
    no_secrets[OPENROUTER_KEY_SECRET] = "sk-or-test"
    body = (
        'data: {"choices":[{"delta":{"content":"台"}}]}\n'
        'data: {"choices":[{"delta":{"content":"積"}}]}\n'
        "data: [DONE]\n"
    )
    mock_http(lambda request: httpx.Response(200, text=body))

    seen: list[str] = []
    reply = OpenRouterProvider().chat(
        [ChatMessage("user", "hi")], "m", on_chunk=seen.append
    )
    assert seen == ["台", "積"]
    assert reply == "台積"


# ------------------------------------------------------------------- Ollama


def test_ollama_lists_whatever_is_installed(mock_http):
    """程式不寫死任何模型名稱，裝了什麼就列什麼。

    這是刻意的設計：內建一份模型名單一定會過期，而且使用者拉了新模型之後
    在清單裡找不到它會以為程式壞了。
    """
    mock_http(
        lambda request: httpx.Response(
            200, json={"models": [{"name": "gemma4:e4b"}, {"name": "qwen3:8b"}]}
        )
    )
    models = OllamaProvider().list_models()
    assert [m.id for m in models] == ["gemma4:e4b", "qwen3:8b"]
    assert all(m.free for m in models)


def test_ollama_is_marked_as_keeping_data_on_device():
    """設定畫面靠這個旗標決定要不要顯示隱私警告。"""
    assert OllamaProvider.sends_data_off_device is False
    assert OpenRouterProvider.sends_data_off_device is True
    assert AnthropicProvider.sends_data_off_device is True


def test_ollama_not_running_tells_you_how_to_start_it(mock_http):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    mock_http(handler)
    with pytest.raises(AINotConfigured) as excinfo:
        OllamaProvider().list_models()
    assert "ollama.com" in str(excinfo.value)


def test_ollama_streams_ndjson_not_sse(mock_http):
    """Ollama 一行一個完整 JSON，不是 SSE 的 ``data:`` 格式。"""
    body = (
        '{"message":{"content":"你"},"done":false}\n'
        '{"message":{"content":"好"},"done":true}\n'
    )
    mock_http(lambda request: httpx.Response(200, text=body))

    seen: list[str] = []
    reply = OllamaProvider().chat(
        [ChatMessage("user", "hi")], "gemma4:e4b", on_chunk=seen.append
    )
    assert seen == ["你", "好"]
    assert reply == "你好"


# ---------------------------------------------------------------- Anthropic


def test_anthropic_moves_system_out_of_the_message_list(no_secrets, mock_http):
    """``/v1/messages`` 不吃 ``role: "system"``，留在陣列裡會被退回。

    這是三家裡唯一形狀不同的地方，也是最容易在換供應商時壞掉的地方。
    """
    no_secrets[ANTHROPIC_KEY_SECRET] = "sk-ant-test"
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["key"] = request.headers.get("x-api-key")
        seen["version"] = request.headers.get("anthropic-version")
        return httpx.Response(200, json={"content": [{"type": "text", "text": "好"}]})

    mock_http(handler)
    reply = AnthropicProvider().chat(
        [ChatMessage("system", "規範"), ChatMessage("user", "hi")], "claude-x"
    )

    assert reply == "好"
    assert seen["body"]["system"] == "規範"
    assert [m["role"] for m in seen["body"]["messages"]] == ["user"]
    assert seen["key"] == "sk-ant-test"
    assert seen["version"]


def test_anthropic_joins_multiple_text_blocks(no_secrets, mock_http):
    no_secrets[ANTHROPIC_KEY_SECRET] = "sk-ant-test"
    mock_http(
        lambda request: httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "台"},
                    {"type": "thinking", "thinking": "略"},
                    {"type": "text", "text": "積電"},
                ]
            },
        )
    )
    reply = AnthropicProvider().chat([ChatMessage("user", "hi")], "claude-x")
    assert reply == "台積電"


def test_anthropic_missing_key_mentions_the_subscription_confusion(no_secrets):
    """使用者最常問的就是「我有 Claude Pro 為什麼不能用」。

    訊息裡必須直接回答那個問題，否則他會一直找不到「登入」按鈕在哪。
    """
    with pytest.raises(AINotConfigured) as excinfo:
        AnthropicProvider().list_models()
    message = str(excinfo.value)
    assert "訂閱" in message
    assert "console.anthropic.com" in message


def test_anthropic_streams_text_deltas_only(no_secrets, mock_http):
    no_secrets[ANTHROPIC_KEY_SECRET] = "sk-ant-test"
    body = (
        'data: {"type":"message_start"}\n'
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"你"}}\n'
        'data: {"type":"ping"}\n'
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"好"}}\n'
        'data: {"type":"message_stop"}\n'
    )
    mock_http(lambda request: httpx.Response(200, text=body))

    seen: list[str] = []
    reply = AnthropicProvider().chat(
        [ChatMessage("user", "hi")], "claude-x", on_chunk=seen.append
    )
    assert seen == ["你", "好"]
    assert reply == "你好"
