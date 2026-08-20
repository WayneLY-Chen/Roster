"""AI 相關畫面與 :mod:`ai` 之間的那一層。

跟其他 controller 一樣：畫面不直接 import :mod:`ai.provider`，也不自己組
訊息串。這裡負責把「一段對話」翻成供應商要的東西，並且**保證 system prompt
一定在最前面**——那件事不能交給畫面去記得做。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ai.prompts import build_system_prompt
from ai.provider import (
    ChatMessage,
    Model,
    ProviderStatus,
    available_providers,
    configured_providers,
    get_provider,
    provider_status,
)
from core.config import AppConfig, get_config, save_user_settings
from core.errors import AIError

#: 一次帶幾則歷史訊息給模型。
#:
#: 不是無上限往回帶：每一則都要付 token（OpenRouter 是錢，本機是時間），而且
#: 超過模型的上下文長度時對方會直接回錯誤，而不是自己截斷。20 則大約是十輪
#: 來回，聊天情境下夠用；真的需要更長的脈絡是另一個功能，不是把這個數字調大。
MAX_HISTORY_MESSAGES = 20


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """畫面保存的一則對話。與 :class:`~ai.provider.ChatMessage` 分開是因為
    畫面還要記「這則是不是還在串流中」之類的東西。"""

    role: str
    content: str


class AIController:
    """聊天與模型清單。"""

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()

    # ------------------------------------------------------------- 設定狀態

    @staticmethod
    def provider_options() -> dict[str, str]:
        return available_providers()

    def statuses(self) -> list[ProviderStatus]:
        return provider_status(self.config)

    def ready(self) -> bool:
        """有沒有任何一個供應商現在就能用。"""
        return bool(configured_providers(self.config))

    def active_provider_label(self) -> str:
        """現在實際會用哪一個，講人話。設定頁與聊天頁都要顯示這個。"""
        try:
            provider = get_provider(self.config.ai.provider, self.config)
        except AIError as exc:
            return str(exc).splitlines()[0]
        return provider.label

    def sends_data_off_device(self) -> bool:
        """現在選的供應商會不會把內容送出這台機器。

        畫面要靠這個決定要不要顯示隱私提醒。查不出來時回 ``True``——不確定的
        時候要往「會外送」猜，猜錯的代價是多顯示一行提醒，反過來猜錯的代價是
        使用者以為資料留在本機。
        """
        try:
            return get_provider(self.config.ai.provider, self.config).sends_data_off_device
        except AIError:
            return True

    # ------------------------------------------------------------- 模型清單

    def models(
        self,
        provider_name: str | None = None,
        *,
        report: Callable[[object], None] | None = None,
        cancel_event: object | None = None,
    ) -> list[Model]:
        """跟供應商要一份可用模型清單。會連網。

        ``report``／``cancel_event`` 收下就丟掉：這個方法會被
        :class:`~gui_qt.tasks.BackgroundTask` 當成 worker 呼叫，而它一律用
        關鍵字傳這兩個參數進來（見 gui_qt/tasks.py 的說明）。這裡沒有進度可
        回報、也沒有中途可取消的點，但簽名必須收得下，否則畫面一按就
        TypeError。
        """
        provider = get_provider(provider_name or self.config.ai.provider, self.config)
        return provider.list_models()

    def remember_choice(self, provider_name: str, model_id: str) -> None:
        """把使用者選的供應商與模型存進 ``user_settings.yaml``。

        存在那裡而不是 ``config.yaml``：後者會進 git，而且更新程式時會被
        `git pull` 碰到。使用者的選擇是他自己的東西，不該兩邊打架。
        """
        save_user_settings("ai", {"provider": provider_name, "model": model_id})
        self.config = get_config()

    def remember_prompt(self, prompt: str) -> None:
        save_user_settings("ai", {"system_prompt": prompt})
        self.config = get_config()

    # ------------------------------------------------------------------ 聊天

    def system_prompt(self) -> str:
        """實際會送出去的那一段，含使用者的補充指示。"""
        return build_system_prompt(self.config.ai.system_prompt)

    def chat(
        self,
        history: Sequence[ChatTurn],
        *,
        model: str | None = None,
        provider_name: str | None = None,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        """送一輪對話出去。

        ``history`` 是畫面上看得到的東西，不含 system prompt——那一段由這裡
        補上，畫面沒有辦法把它拿掉或改掉。
        """
        provider = get_provider(provider_name or self.config.ai.provider, self.config)
        chosen = (model or self.config.ai.model or "").strip()
        if not chosen:
            raise AIError("還沒有選模型。到「設定」頁的「AI 模型」選一個。")

        recent = list(history)[-MAX_HISTORY_MESSAGES:]
        messages = [ChatMessage("system", self.system_prompt())]
        messages.extend(ChatMessage(turn.role, turn.content) for turn in recent)
        return provider.chat(messages, chosen, on_chunk=on_chunk)
