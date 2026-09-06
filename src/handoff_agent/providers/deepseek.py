"""DeepSeek provider — DeepSeek chat completions via standard library."""

from __future__ import annotations

from typing import Any

from handoff_agent.providers.openai_compatible import OpenAICompatibleProvider


class DeepSeekProvider(OpenAICompatibleProvider):
    """Provider adapter for DeepSeek chat completions."""

    DEFAULT_ENDPOINT = "https://api.deepseek.com/chat/completions"
    DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"
    DEFAULT_MODEL = "deepseek-chat"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)

    def name(self) -> str:
        return "deepseek"