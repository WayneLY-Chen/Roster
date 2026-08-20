"""語言模型的接入層。

這個套件只負責「跟模型講話」，不負責抓網頁。抓網頁永遠是 :mod:`crawler`
的事，理由見 :mod:`ai.prompts` 的說明。
"""

from ai.prompts import BASE_SYSTEM_PROMPT, EXTRACT_SYSTEM_PROMPT, build_system_prompt
from ai.provider import (
    ChatMessage,
    Model,
    available_providers,
    configured_providers,
    get_provider,
    provider_status,
)

__all__ = [
    "BASE_SYSTEM_PROMPT",
    "EXTRACT_SYSTEM_PROMPT",
    "ChatMessage",
    "Model",
    "available_providers",
    "build_system_prompt",
    "configured_providers",
    "get_provider",
    "provider_status",
]
