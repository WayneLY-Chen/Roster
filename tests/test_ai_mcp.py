"""Tests for ai/mcp.py。

**這個檔案守的是：接線那一層要笨，而且不能卡住。**

這一層是整個專案裡唯一一個會去執行使用者填的指令的地方，所以它只做四件事：
啟動、握手、列工具、呼叫工具。要不要呼叫是 ``ai/tools.py`` 的事，而那裡的答案
永遠是「使用者按過才算」——那一條在 ``tests/test_ai_tools.py`` 驗。

這裡驗的幾件事都是實際會咬人的：外部程式往標準輸出印垃圾、往標準錯誤印個
不停（不讀的話管線塞滿、子行程整個卡死）、回一份幾 MB 的東西、根本沒啟動
起來。這幾種情況一律不能變成「畫面沒有反應」。

用的是**真的子行程**，不是 mock。這一層的價值就在「它跟一支真的外部程式接得
起來」，把 ``Popen`` mock 掉的話等於什麼都沒驗到。
"""

from __future__ import annotations

import json
import sys
import textwrap

import pytest

from ai.mcp import (
    MAX_RESULT_CHARS,
    McpError,
    McpServer,
    McpTool,
    call_tool,
    connect,
    flatten_content,
    list_tools,
    resolve_env,
)

#: 一支照規格講話的假伺服器。``BEHAVIOUR`` 由每個測試自己填。
_SERVER = '''\
import json, sys

# MCP 規格要求這條管線是 UTF-8。Windows 上子行程的預設編碼是系統的 ANSI 字碼
# 頁（繁中是 cp950），照預設寫出去的中文，這一端用 UTF-8 讀會變成問號。
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")

BEHAVIOUR = {behaviour!r}

if BEHAVIOUR == "noise":
    print("Fake MCP server starting up…", flush=True)
if BEHAVIOUR == "die":
    sys.exit(3)

for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    message = json.loads(raw)
    request_id = message.get("id")
    if request_id is None:
        continue
    method = message.get("method")
    if method == "initialize":
        result = {{
            "protocolVersion": "2025-06-18",
            "capabilities": {{}},
            "serverInfo": {{"name": "fake", "version": "1.0"}},
        }}
    elif method == "tools/list":
        result = {{"tools": [
            {{
                "name": "echo",
                "description": "把收到的字原樣回來",
                "inputSchema": {{
                    "type": "object",
                    "properties": {{"text": {{"type": "string"}}}},
                    "required": ["text"],
                }},
            }},
            {{"name": "quiet", "description": ""}},
        ]}}
    elif method == "tools/call":
        params = message.get("params") or {{}}
        arguments = params.get("arguments") or {{}}
        if BEHAVIOUR == "huge":
            body = "資" * 20000
        elif BEHAVIOUR == "chatty":
            for _ in range(2000):
                print("log line " * 20, file=sys.stderr, flush=True)
            body = "撐過去了"
        else:
            body = str(arguments.get("text", ""))
        result = {{"content": [{{"type": "text", "text": body}}]}}
        if BEHAVIOUR == "failing":
            result["isError"] = True
    else:
        print(json.dumps({{"jsonrpc": "2.0", "id": request_id,
                          "error": {{"code": -32601, "message": "沒有這個方法"}}}}),
              flush=True)
        continue
    print(json.dumps({{"jsonrpc": "2.0", "id": request_id, "result": result}},
                     ensure_ascii=False), flush=True)
'''


@pytest.fixture
def fake_server(tmp_path):
    """做一支真的假伺服器出來，回傳一個 ``McpServer``。"""

    def build(behaviour: str = "plain", name: str = "fake") -> McpServer:
        script = tmp_path / f"server_{behaviour}.py"
        script.write_text(_SERVER.format(behaviour=behaviour), encoding="utf-8")
        return McpServer(name=name, command=sys.executable, args=(str(script),))

    return build


# --------------------------------------------------------------- 接得起來嗎


def test_it_lists_and_calls_a_real_subprocess(fake_server):
    """整條路走一次：啟動、握手、列工具、呼叫、關掉。"""
    server = fake_server()
    with connect(server) as client:
        tools = client.list_tools()
        assert [tool.name for tool in tools] == ["echo", "quiet"]
        assert tools[0].qualified == "fake.echo"
        assert tools[0].schema["properties"]["text"]["type"] == "string"

        output = client.call("echo", {"text": "台中 CNC"})

    assert output.text == "台中 CNC"
    assert not output.failed
    assert not output.truncated


def test_the_subprocess_is_gone_after_the_block_ends(fake_server):
    """離開 ``with`` 之後不能留一個行程在使用者的機器上。

    留下來的話它會一直吃記憶體，而且沒有任何介面看得到——使用者唯一的線索是
    工作管理員裡愈來愈多的同名行程。
    """
    server = fake_server()
    with connect(server) as client:
        process = client._process
        assert process.poll() is None
    assert process.poll() is not None


def test_a_command_that_does_not_exist_says_which_one(fake_server):
    server = McpServer(name="nope", command="這個指令不存在_zzz")
    with pytest.raises(McpError) as caught:
        with connect(server):
            pass
    assert "這個指令不存在_zzz" in str(caught.value)


def test_a_server_that_dies_immediately_does_not_hang(fake_server):
    """它自己結束掉的時候要報錯，不是等到逾時。"""
    server = fake_server("die")
    with pytest.raises(McpError) as caught:
        with connect(server) as client:
            client.list_tools()
    assert "結束" in str(caught.value)


def test_junk_on_stdout_does_not_break_the_handshake(fake_server):
    """有些伺服器會往標準輸出印啟動訊息。

    那不合規格，但把整個連線判定成壞掉太嚴苛——使用者會看到「連不上」，而那支
    程式其實好好的。
    """
    server = fake_server("noise")
    with connect(server) as client:
        assert client.call("echo", {"text": "還是通的"}).text == "還是通的"


def test_a_server_that_floods_stderr_still_answers(fake_server):
    """**這一條是回歸測試。**

    stderr 沒有人一直讀的話，那個管線的緩衝區滿了之後子行程下一次寫 log 就
    會卡住不動，而外面看到的症狀是「工具沒有回應」，完全指不到原因。
    """
    server = fake_server("chatty")
    with connect(server) as client:
        assert client.call("echo", {"text": "x"}).text == "撐過去了"


# ------------------------------------------------------------- 回來的東西


def test_a_huge_result_gets_cut_and_says_so(fake_server):
    """幾 MB 的回應要砍掉，而且要**講**它砍了。

    那些字會進模型的上下文（用連外服務時就是錢），而埋在幾萬字中間的一句
    「忽略先前的指示」，使用者根本不會捲到那裡。
    """
    output = call_tool(fake_server("huge"), "echo", {})
    assert len(output.text) == MAX_RESULT_CHARS
    assert output.truncated


def test_a_tool_that_reports_failure_still_hands_back_its_words(fake_server):
    """工具說自己失敗時，內容照樣交出去。

    那段話常常就是「參數少了一個」——模型自己修得好，而且使用者也需要看到
    到底是哪裡不對。
    """
    output = call_tool(fake_server("failing"), "echo", {"text": "少了參數"})
    assert output.failed
    assert output.text == "少了參數"


def test_content_we_cannot_show_leaves_a_line_saying_so():
    """圖片那種區塊不能安靜地消失，否則使用者以為工具壞了。"""
    text = flatten_content(
        [
            {"type": "text", "text": "前面這段看得到"},
            {"type": "image", "data": "…"},
            {"type": "resource", "resource": {"text": "附件裡的字"}},
        ]
    )
    assert "前面這段看得到" in text
    assert "image" in text
    assert "附件裡的字" in text


def test_content_that_is_not_a_list_is_just_empty():
    assert flatten_content(None) == ""
    assert flatten_content("一段字") == ""


# ------------------------------------------------------------- 一整批伺服器


def test_the_ones_that_could_not_be_reached_come_back_too(fake_server):
    """壞掉的那幾個一定要交出來。

    只回成功的話，使用者會以為他設定的兩個工具都在，然後納悶模型為什麼從來
    不用其中一個——而真正的原因（那支程式根本沒啟動起來）完全看不到。
    """
    good = fake_server(name="good")
    bad = McpServer(name="bad", command="這個指令不存在_zzz")

    tools, failures = list_tools([good, bad])

    assert [tool.qualified for tool in tools] == ["good.echo", "good.quiet"]
    assert [name for name, _why in failures] == ["bad"]


def test_a_disabled_server_is_never_started(fake_server, monkeypatch):
    """停用就是完全不啟動它，不是啟動了再忽略。"""
    import subprocess

    def explode(*_args, **_kwargs):
        raise AssertionError("停用的伺服器不該被啟動")

    monkeypatch.setattr(subprocess, "Popen", explode)
    off = McpServer(name="off", command=sys.executable, args=("-c", "pass"), enabled=False)

    tools, failures = list_tools([off])

    assert not tools
    assert not failures


# ----------------------------------------------------------------- 環境變數


def test_a_secret_reference_is_read_from_the_vault(monkeypatch):
    """``${secret:名稱}`` 要去保管庫拿。

    有這個寫法是因為 ``user_settings.yaml`` 是明碼的純文字檔。少了它，使用者
    要接一個需要金鑰的工具就只能把金鑰打在那裡——這個專案「金鑰只放在作業
    系統的憑證保管庫」那一條就等於被這個功能開了一個洞。
    """
    import ai.mcp as mcp

    monkeypatch.setattr(mcp, "get_secret", lambda name: "真的金鑰" if name == "k" else "")

    assert resolve_env({"API_KEY": "${secret:k}"}) == {"API_KEY": "真的金鑰"}


def test_a_missing_secret_becomes_empty_not_an_error(monkeypatch):
    """查不到就留空，讓伺服器自己說「沒有授權」——那個訊息比這裡猜的準。"""
    import ai.mcp as mcp

    monkeypatch.setattr(mcp, "get_secret", lambda _name: "")

    assert resolve_env({"API_KEY": "${secret:沒有這個}"}) == {"API_KEY": ""}


def test_a_plain_value_is_left_alone():
    assert resolve_env({"MODE": "fast"}) == {"MODE": "fast"}


# ------------------------------------------------------------------- 顯示


def test_describing_a_server_never_prints_the_environment():
    """畫面上那一行不能含環境變數的值——金鑰常常在裡面，而設定頁很常被截圖。"""
    server = McpServer(
        name="x", command="npx", args=("-y", "some-server"), env={"KEY": "sk-祕密"}
    )
    assert "sk-祕密" not in server.describe()
    assert server.describe() == "npx -y some-server"


def test_a_tool_name_is_prefixed_by_its_server():
    """兩個伺服器都有 ``search`` 的時候要分得出來是哪一個。"""
    assert McpTool(server="a", name="search").qualified == "a.search"


def test_the_request_we_send_is_valid_json_rpc(fake_server, monkeypatch):
    """握手送出去的東西要照規格，不然對方會直接掛掉。"""
    sent: list[dict] = []
    server = fake_server()
    with connect(server) as client:
        original = client._send

        def spy(message):
            sent.append(message)
            return original(message)

        monkeypatch.setattr(client, "_send", spy)
        client.call("echo", {"text": "hi"})

    call = sent[-1]
    assert call["jsonrpc"] == "2.0"
    assert call["method"] == "tools/call"
    assert call["params"] == {"name": "echo", "arguments": {"text": "hi"}}
    assert json.loads(json.dumps(call))  # 送得出去的東西必須是純 JSON


def test_the_helper_script_is_what_we_think_it_is():
    """假伺服器本身壞掉時，上面每一條都會用錯的理由失敗。"""
    body = textwrap.dedent(_SERVER.format(behaviour="plain"))
    compile(body, "fake_server.py", "exec")
