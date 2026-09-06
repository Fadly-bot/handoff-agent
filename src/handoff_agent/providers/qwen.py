"""Qwen provider — DashScope compatible-mode chat completions."""

from __future__ import annotations

from typing import Any

from handoff_agent.providers.openai_compatible import OpenAICompatibleProvider


class QwenProvider(OpenAICompatibleProvider):
    """Provider adapter for Alibaba Qwen via DashScope compatible-mode."""

    DEFAULT_ENDPOINT = (
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    )
    DEFAULT_API_KEY_ENV = "DASHSCOPE_API_KEY"
    DEFAULT_MODEL = "qwen-plus"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)

    def name(self) -> str:
        return "qwen"