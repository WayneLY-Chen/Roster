"""可抽換的語言模型供應商。

## 三種來源，取捨完全不同

======================  ========  ==================================================
provider                要金鑰？  說明
======================  ========  ==================================================
``ollama``              否        在你自己的機器上跑。內容不出門，但要自己先裝。
``openrouter``          是        一把金鑰通到幾百個模型（含 Claude、GPT、Gemini）。
``anthropic``           是        Anthropic 官方 API。**與 Claude 訂閱分開計費**。
======================  ========  ==================================================

設定寫 ``auto``（預設）時照上表由上而下挑第一個可用的，都沒有就是「還沒設
定」。**本機優先是刻意的**——這支程式的資料庫欄位是加密的、密碼是鎖在系統保管
庫的，那樣的專案不該預設把使用者蒐集來的資料送去別人的伺服器。

## 「我有 Claude/ChatGPT 訂閱，可以直接登入用嗎？」

不行，而且沒有合法的做法。詳見 :class:`AnthropicProvider` 的說明——訂閱買的是
在對方自家 App 裡使用的權利，不含 API 額度，也沒有讓第三方程式代登入的授權
流程。要用官方 API 就是另外申請金鑰、另外計費。

## 隱私：這件事使用者必須知道

用 OpenRouter 時，送出去的東西包含網頁原始內容，之後也會包含資料庫裡的公司
資料。那是**傳給第三方**。設定畫面上必須講清楚，不能只寫「填金鑰」。
Ollama 沒有這個問題，代價是要自己裝、而且吃自己的記憶體與顯示卡。

## 這個模組不做的事

它不抓網頁。一次都不會。模型看得到的網頁內容一律由 :mod:`crawler.fetcher`
事先抓好——那一層才有 robots.txt 檢查與請求間隔。理由見 :mod:`ai.prompts`。
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import httpx

from core.config import AppConfig, get_config
from core.constants import LogCategory
from core.credentials import get_secret
from core.errors import AIError, AINotConfigured
from core.logging_setup import get_logger

log = get_logger(LogCategory.GUI)

#: 憑證保管庫裡的金鑰名稱。
OPENROUTER_KEY_SECRET = "openrouter_key"

OPENROUTER_BASE = "https://openrouter.ai/api/v1"

ANTHROPIC_KEY_SECRET = "anthropic_key"
ANTHROPIC_BASE = "https://api.anthropic.com/v1"
#: Anthropic 要求每個請求都帶版本，這個字串是他們定的常數不是我們的設定。
ANTHROPIC_VERSION = "2023-06-01"

#: OpenRouter 建議每個呼叫端表明自己是誰，會顯示在他們的統計頁上。
#:
#: 這跟這支程式在爬取時表明身分是同一件事：讓對方知道是誰在用、出問題找得到
#: 人。不是偽裝成別的東西——那正好是這個專案不做的事。
OPENROUTER_REFERER = "https://github.com/WayneLY-Chen/Roster"
OPENROUTER_TITLE = "Roster"

#: 列模型清單用的短逾時。這一步只是拿一份列表，卡住就該早點放棄。
LIST_TIMEOUT = 15.0

#: 「這個供應商現在可用嗎」的探測結果要快取幾秒。
#:
#: Ollama 沒有金鑰可以查，判斷它在不在只能真的連一次線。而畫面上問這件事的
#: 地方不只一個——「現在會用哪一個」「要不要顯示隱私警告」「送出鈕要不要
#: 啟用」全都要知道答案。沒有快取的話一次刷新就是三四次連線，實測讓「AI 助
#: 手」頁的 refresh() 花了 7 秒、設定頁的 build() 花了 9 秒，整個介面卡住。
#:
#: 十秒足夠讓一次畫面刷新裡的所有查詢共用同一個答案，又短到使用者剛把 Ollama
#: 開起來時不會等太久。設定改變時呼叫 :func:`forget_probes` 立刻失效。
PROBE_TTL = 10.0

_probe_cache: dict[str, tuple[float, bool]] = {}


def forget_probes() -> None:
    """丟掉快取的探測結果，下一次查詢重新連線。

    使用者剛裝好 Ollama、剛存了金鑰、或按了「重新整理」時要呼叫——那幾個時
    間點他就是在說「我改了東西，再看一次」。
    """
    _probe_cache.clear()


def _cached_probe(key: str, probe: Callable[[], bool]) -> bool:
    now = time.monotonic()
    hit = _probe_cache.get(key)
    if hit is not None and now - hit[0] < PROBE_TTL:
        return hit[1]
    value = probe()
    _probe_cache[key] = (now, value)
    return value



@dataclass(frozen=True, slots=True)
class ChatMessage:
    """對話裡的一則訊息。"""

    role: str  # "system" | "user" | "assistant"
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class Model:
    """一個可以選的模型。"""

    id: str
    label: str
    provider: str
    #: OpenRouter 上標價為 0 的模型。本機模型一律視為免費。
    free: bool = False

    def display(self) -> str:
        return f"{self.label}（免費）" if self.free else self.label


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """給設定畫面顯示用，永遠不包含金鑰本身。"""

    name: str
    label: str
    configured: bool
    detail: str = ""


class BaseProvider(ABC):
    """一個語言模型來源。"""

    name: str = ""
    label: str = ""
    #: 送出去的內容會不會離開這台機器。設定畫面靠這個決定要不要示警。
    sends_data_off_device: bool = True

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()

    @abstractmethod
    def is_configured(self) -> bool:
        """現在就能用嗎？這個方法會被畫面頻繁呼叫，不可以慢。"""

    @abstractmethod
    def list_models(self) -> list[Model]:
        """跟對方要一份可用模型清單。會連網。"""

    @abstractmethod
    def chat(
        self,
        messages: Sequence[ChatMessage],
        model: str,
        *,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        """送一輪對話，回傳助手的完整回覆。

        給了 ``on_chunk`` 就用串流，每收到一段就呼叫一次——聊天畫面靠它做到
        「字一個一個浮出來」，而不是轉圈轉三十秒然後整段跳出來。
        """

    # ------------------------------------------------------------- 共用工具

    @property
    def _timeout(self) -> httpx.Timeout:
        """連線快、讀取慢。

        分開設是因為兩者的失敗意義不同：連不上應該幾秒內就知道，但模型「正在
        想」是正常的，本機的大模型第一次載入權重可能要一分鐘以上。用單一個
        逾時值的話，不是連不上時傻等，就是本機模型永遠跑不完。
        """
        seconds = float(self.config.ai.timeout_seconds)
        return httpx.Timeout(connect=10.0, read=seconds, write=30.0, pool=10.0)


class OpenRouterProvider(BaseProvider):
    """OpenRouter：一把金鑰通到幾百個模型，介面與 OpenAI 相容。"""

    name = "openrouter"
    label = "OpenRouter（需要金鑰）"
    sends_data_off_device = True

    def _key(self) -> str:
        key = get_secret(OPENROUTER_KEY_SECRET).strip()
        if not key:
            raise AINotConfigured(
                "還沒有設定 OpenRouter 金鑰。到「設定」頁的「AI 模型」填進去，"
                "或改用本機的 Ollama。"
            )
        return key

    def is_configured(self) -> bool:
        return bool(get_secret(OPENROUTER_KEY_SECRET).strip())

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key()}",
            "HTTP-Referer": OPENROUTER_REFERER,
            "X-Title": OPENROUTER_TITLE,
            "Content-Type": "application/json",
        }

    def list_models(self) -> list[Model]:
        headers = self._headers()  # 沒金鑰時給「去設定」而不是「連線失敗」
        try:
            response = httpx.get(
                f"{OPENROUTER_BASE}/models", headers=headers, timeout=LIST_TIMEOUT
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise AIError(_http_message("OpenRouter", exc)) from exc
        except httpx.HTTPError as exc:
            raise AIError(f"連不上 OpenRouter：{exc}") from exc
        except ValueError as exc:
            raise AIError("OpenRouter 回了看不懂的內容。") from exc

        models: list[Model] = []
        for item in payload.get("data", []):
            model_id = str(item.get("id") or "").strip()
            if not model_id:
                continue
            models.append(
                Model(
                    id=model_id,
                    label=str(item.get("name") or model_id),
                    provider=self.name,
                    free=_is_free(item.get("pricing")),
                )
            )
        models.sort(key=lambda m: (not m.free, m.label.lower()))
        return models

    def chat(
        self,
        messages: Sequence[ChatMessage],
        model: str,
        *,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        headers = self._headers()
        settings = self.config.ai
        body = {
            "model": model,
            "messages": [m.as_dict() for m in messages],
            "temperature": settings.temperature,
            "max_tokens": settings.max_output_tokens,
        }
        url = f"{OPENROUTER_BASE}/chat/completions"
        if on_chunk is None:
            return _post_json_chat(
                url, headers, body, self._timeout, "OpenRouter", _openrouter_content
            )
        body["stream"] = True
        return _stream_sse(url, headers, body, self._timeout, "OpenRouter", on_chunk)


class AnthropicProvider(BaseProvider):
    """Anthropic 官方 API。

    ## 這跟 Claude 的訂閱方案不是同一個東西

    使用者最常問的就是這個：「我已經付 Claude Pro 了，可以直接登入來用嗎？」
    **不行，而且沒有合法的做法。**

    Claude Pro／Max 那類訂閱買的是「你在 claude.ai 與官方 App 裡使用」的權利，
    不含 API 額度，也沒有提供任何讓第三方程式登入代用的授權流程。網路上流傳
    的做法是去抓 claude.ai 的登入 cookie 冒充瀏覽器——那違反對方的使用條款、
    隨時會失效，而且可能害使用者的帳號被停權。那正是這個專案從第一天起就
    不做的那一類事（見 :mod:`crawler.robots`）。

    合法的路是這個類別走的：到 console.anthropic.com 開一個 API 金鑰，那是
    **另外計費**的產品，跟訂閱各付各的。同樣的道理適用於 ChatGPT Plus 與
    Gemini Advanced。

    ## 介面形狀跟另外兩家不一樣

    system prompt 是**頂層參數**不是一則訊息，回覆是 content blocks 陣列不是
    單一字串。抽象層存在的意義就在這裡——上層完全不需要知道這些差別。
    """

    name = "anthropic"
    label = "Anthropic API（需要金鑰，與 Claude 訂閱分開計費）"
    sends_data_off_device = True

    def _key(self) -> str:
        key = get_secret(ANTHROPIC_KEY_SECRET).strip()
        if not key:
            raise AINotConfigured(
                "還沒有設定 Anthropic 金鑰。到 console.anthropic.com 產生一把，"
                "填進「設定」頁的「AI 模型」。\n\n"
                "注意這跟 Claude Pro／Max 訂閱是兩回事，帳單分開——訂閱不含 "
                "API 額度，也沒有辦法用訂閱帳號登入第三方程式。"
            )
        return key

    def is_configured(self) -> bool:
        return bool(get_secret(ANTHROPIC_KEY_SECRET).strip())

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._key(),
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    def list_models(self) -> list[Model]:
        headers = self._headers()
        try:
            response = httpx.get(
                f"{ANTHROPIC_BASE}/models", headers=headers, timeout=LIST_TIMEOUT
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise AIError(_http_message("Anthropic", exc)) from exc
        except httpx.HTTPError as exc:
            raise AIError(f"連不上 Anthropic：{exc}") from exc
        except ValueError as exc:
            raise AIError("Anthropic 回了看不懂的內容。") from exc

        models = [
            Model(
                id=str(item.get("id") or "").strip(),
                label=str(item.get("display_name") or item.get("id") or "").strip(),
                provider=self.name,
            )
            for item in payload.get("data", [])
            if str(item.get("id") or "").strip()
        ]
        models.sort(key=lambda m: m.label.lower())
        return models

    @staticmethod
    def _split_system(messages: Sequence[ChatMessage]) -> tuple[str, list[dict[str, str]]]:
        """把 system 訊息抽成頂層參數。

        Anthropic 的 ``/v1/messages`` 不吃 ``role: "system"``——留在陣列裡會被
        當成錯誤退回。多則 system 會合併成一段。
        """
        system_parts = [m.content for m in messages if m.role == "system"]
        rest = [m.as_dict() for m in messages if m.role != "system"]
        return "\n\n".join(system_parts), rest

    def chat(
        self,
        messages: Sequence[ChatMessage],
        model: str,
        *,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        headers = self._headers()
        settings = self.config.ai
        system, conversation = self._split_system(messages)
        body: dict = {
            "model": model,
            "messages": conversation,
            "max_tokens": settings.max_output_tokens,
            "temperature": settings.temperature,
        }
        if system:
            body["system"] = system

        url = f"{ANTHROPIC_BASE}/messages"
        if on_chunk is None:
            return _post_json_chat(
                url, headers, body, self._timeout, "Anthropic", _anthropic_content
            )
        body["stream"] = True
        return _stream_anthropic(url, headers, body, self._timeout, on_chunk)


class OllamaProvider(BaseProvider):
    """Ollama：模型跑在使用者自己的機器上，內容不出門。"""

    name = "ollama"
    label = "Ollama（本機執行，不需金鑰）"
    sends_data_off_device = False

    @property
    def base_url(self) -> str:
        return str(self.config.ai.ollama_url).rstrip("/")

    def is_configured(self) -> bool:
        """Ollama 沒有金鑰可以檢查，只能問它在不在。

        所以這裡真的會連一次線——但只連本機、逾時兩秒，而且結果會快取
        :data:`PROBE_TTL` 秒（見那裡的說明：沒有快取時畫面會卡好幾秒）。
        跟連外的供應商不同，「有沒有裝」這件事沒有別的判斷依據。

        **這個方法仍然可能花上兩秒**，所以呼叫端不該在畫面執行緒上直接叫它——
        第一次探測一定會付那個代價。用 BackgroundTask 包起來。
        """

        def probe() -> bool:
            try:
                response = httpx.get(f"{self.base_url}/api/tags", timeout=2.0)
                return response.status_code == 200
            except httpx.HTTPError:
                return False

        return _cached_probe(f"ollama:{self.base_url}", probe)

    def list_models(self) -> list[Model]:
        try:
            response = httpx.get(f"{self.base_url}/api/tags", timeout=LIST_TIMEOUT)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise AINotConfigured(
                f"連不上本機的 Ollama（{self.base_url}）。\n\n"
                "確認 Ollama 已經安裝而且正在執行：終端機執行 ollama list "
                "看得到東西就是在跑。還沒安裝的話到 https://ollama.com 下載，"
                "裝好之後至少要先拉一個模型下來，例如 ollama pull gemma3。"
            ) from exc
        except ValueError as exc:
            raise AIError("Ollama 回了看不懂的內容。") from exc

        models = [
            Model(
                id=str(item.get("name") or "").strip(),
                label=str(item.get("name") or "").strip(),
                provider=self.name,
                free=True,
            )
            for item in payload.get("models", [])
            if str(item.get("name") or "").strip()
        ]
        models.sort(key=lambda m: m.label.lower())
        return models

    def chat(
        self,
        messages: Sequence[ChatMessage],
        model: str,
        *,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        headers = {"Content-Type": "application/json"}
        settings = self.config.ai
        body = {
            "model": model,
            "messages": [m.as_dict() for m in messages],
            "options": {
                "temperature": settings.temperature,
                "num_predict": settings.max_output_tokens,
            },
            "stream": on_chunk is not None,
        }
        url = f"{self.base_url}/api/chat"
        if on_chunk is None:
            return _post_json_chat(
                url, headers, body, self._timeout, "Ollama", _ollama_content
            )
        return _stream_ndjson(url, headers, body, self._timeout, on_chunk)


#: 名稱 -> 類別。加新的供應商只要在這裡多一行。
PROVIDERS: dict[str, type[BaseProvider]] = {
    "openrouter": OpenRouterProvider,
    "anthropic": AnthropicProvider,
    "ollama": OllamaProvider,
}

#: ``auto`` 的優先序：本機優先。理由見模組說明。
AUTO_ORDER: tuple[str, ...] = ("ollama", "openrouter", "anthropic")


def available_providers() -> dict[str, str]:
    """給下拉選單用的 名稱 -> 顯示文字。"""
    options = {"auto": "自動（本機優先）"}
    options.update({name: cls.label for name, cls in PROVIDERS.items()})
    return options


def configured_providers(config: AppConfig | None = None) -> list[str]:
    """現在就能用的供應商名稱，順序即優先序。"""
    config = config or get_config()
    ready = []
    for name in AUTO_ORDER:
        try:
            if PROVIDERS[name](config).is_configured():
                ready.append(name)
        except Exception as exc:  # 供應商壞掉不該讓整個設定頁開不起來
            log.debug("檢查供應商 {} 時失敗：{}", name, exc)
    return ready


def provider_status(config: AppConfig | None = None) -> list[ProviderStatus]:
    """設定畫面用的逐項狀態。"""
    config = config or get_config()
    statuses = []
    for name, cls in PROVIDERS.items():
        provider = cls(config)
        try:
            configured = provider.is_configured()
        except Exception:
            configured = False
        if name == "ollama":
            detail = provider.base_url if configured else "沒有偵測到執行中的 Ollama"
        else:
            detail = "金鑰已設定" if configured else "尚未設定金鑰"
        statuses.append(ProviderStatus(name, cls.label, configured, detail))
    return statuses


def get_provider(
    name: str | None = None, config: AppConfig | None = None
) -> BaseProvider:
    """取得要用的供應商。``auto``／``None`` 走自動選擇。"""
    config = config or get_config()
    name = (name or config.ai.provider or "auto").strip().lower()

    if name == "auto":
        ready = configured_providers(config)
        if not ready:
            raise AINotConfigured(
                "還沒有可用的 AI 模型。兩種選擇：\n\n"
                "1. 本機執行（資料不出門）：到 https://ollama.com 安裝 Ollama，"
                "然後執行 ollama pull gemma3。\n"
                "2. 線上服務：到 https://openrouter.ai 申請金鑰，填進「設定」頁。"
            )
        name = ready[0]

    if name not in PROVIDERS:
        raise AIError(f"不認得的 AI 供應商：{name}")
    return PROVIDERS[name](config)


# ------------------------------------------------------------------ 內部工具


def _is_free(pricing: object) -> bool:
    """OpenRouter 的 pricing 是字串形式的數字，全是 0 就是免費。"""
    if not isinstance(pricing, dict):
        return False
    try:
        return all(
            float(pricing.get(key, 1) or 0) == 0 for key in ("prompt", "completion")
        )
    except (TypeError, ValueError):
        return False


def _http_message(who: str, exc: httpx.HTTPStatusError) -> str:
    """把 HTTP 狀態碼翻成使用者看得懂的話。

    401/402/429 各自對應一個很具體、而且使用者有辦法處理的情況；直接把
    「HTTP 402」丟出去的話沒有人知道那是要儲值。
    """
    code = exc.response.status_code
    if code == 401:
        return f"{who} 說金鑰不對。到「設定」頁確認一次，或重新產生一把。"
    if code == 402:
        return f"{who} 說額度不足。這把金鑰的餘額用完了，需要儲值或改用免費模型。"
    if code == 429:
        return f"{who} 說太頻繁了，稍等一下再試。"
    if code == 404:
        return f"{who} 找不到這個模型，可能是名稱改了或已經下架。換一個試試。"
    try:
        payload = exc.response.json()
        detail = str(payload.get("error", {}).get("message") or "")
    except Exception:
        detail = (exc.response.text or "")[:200]
    return f"{who} 回了錯誤（HTTP {code}）{'：' + detail if detail else '。'}"


def _openrouter_content(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    return str(choices[0].get("message", {}).get("content") or "")


def _ollama_content(payload: dict) -> str:
    return str(payload.get("message", {}).get("content") or "")


def _post_json_chat(
    url: str,
    headers: dict[str, str],
    body: dict,
    timeout: httpx.Timeout,
    who: str,
    extract: Callable[[dict], str],
) -> str:
    try:
        response = httpx.post(url, headers=headers, json=body, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise AIError(_http_message(who, exc)) from exc
    except httpx.ReadTimeout as exc:
        raise AIError(
            f"{who} 太久沒有回應。模型太大或機器太忙時會這樣——"
            "可以在設定裡把逾時調長，或換一個小一點的模型。"
        ) from exc
    except httpx.HTTPError as exc:
        raise AIError(f"連不上 {who}：{exc}") from exc
    except ValueError as exc:
        raise AIError(f"{who} 回了看不懂的內容。") from exc

    content = extract(payload)
    if not content:
        raise AIError(f"{who} 回了空的內容。")
    return content


def _stream_sse(
    url: str,
    headers: dict[str, str],
    body: dict,
    timeout: httpx.Timeout,
    who: str,
    on_chunk: Callable[[str], None],
) -> str:
    """OpenAI 相容的 SSE 串流：每行 ``data: {...}``，結尾是 ``data: [DONE]``。"""
    parts: list[str] = []
    try:
        with httpx.stream(
            "POST", url, headers=headers, json=body, timeout=timeout
        ) as response:
            if response.status_code >= 400:
                response.read()
                response.raise_for_status()
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    payload = json.loads(data)
                except ValueError:
                    continue  # 心跳或註解行，跳過就好
                for choice in payload.get("choices") or []:
                    piece = str(choice.get("delta", {}).get("content") or "")
                    if piece:
                        parts.append(piece)
                        on_chunk(piece)
    except httpx.HTTPStatusError as exc:
        raise AIError(_http_message(who, exc)) from exc
    except httpx.HTTPError as exc:
        raise AIError(f"連不上 {who}：{exc}") from exc
    return "".join(parts)


def _stream_ndjson(
    url: str,
    headers: dict[str, str],
    body: dict,
    timeout: httpx.Timeout,
    on_chunk: Callable[[str], None],
) -> str:
    """Ollama 的串流：一行一個完整的 JSON 物件，不是 SSE。"""
    parts: list[str] = []
    try:
        with httpx.stream(
            "POST", url, headers=headers, json=body, timeout=timeout
        ) as response:
            if response.status_code >= 400:
                response.read()
                response.raise_for_status()
            for line in response.iter_lines():
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                piece = str(payload.get("message", {}).get("content") or "")
                if piece:
                    parts.append(piece)
                    on_chunk(piece)
                if payload.get("done"):
                    break
    except httpx.HTTPStatusError as exc:
        raise AIError(_http_message("Ollama", exc)) from exc
    except httpx.HTTPError as exc:
        raise AIError(f"連不上 Ollama：{exc}") from exc
    return "".join(parts)


def _anthropic_content(payload: dict) -> str:
    """Anthropic 的回覆是 content blocks，文字可能被拆成好幾塊。"""
    blocks = payload.get("content") or []
    return "".join(
        str(block.get("text") or "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _stream_anthropic(
    url: str,
    headers: dict[str, str],
    body: dict,
    timeout: httpx.Timeout,
    on_chunk: Callable[[str], None],
) -> str:
    """Anthropic 的 SSE：跟 OpenAI 相容格式不同，文字在 ``content_block_delta``。

    這裡只認 ``text_delta``。其他事件（``message_start``、``ping``、
    ``content_block_start`` …）照規格會出現，但沒有文字，忽略即可。
    """
    parts: list[str] = []
    try:
        with httpx.stream(
            "POST", url, headers=headers, json=body, timeout=timeout
        ) as response:
            if response.status_code >= 400:
                response.read()
                response.raise_for_status()
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                try:
                    payload = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if payload.get("type") != "content_block_delta":
                    continue
                delta = payload.get("delta") or {}
                if delta.get("type") != "text_delta":
                    continue
                piece = str(delta.get("text") or "")
                if piece:
                    parts.append(piece)
                    on_chunk(piece)
    except httpx.HTTPStatusError as exc:
        raise AIError(_http_message("Anthropic", exc)) from exc
    except httpx.HTTPError as exc:
        raise AIError(f"連不上 Anthropic：{exc}") from exc
    return "".join(parts)
