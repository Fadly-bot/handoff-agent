"""OpenAI provider — OpenAI-compatible chat completions via standard library."""

from __future__ import annotations

from typing import Any

from handoff_agent.providers.openai_compatible import OpenAICompatibleProvider


class OpenAIProvider(OpenAICompatibleProvider):
    """Provider adapter for OpenAI-compatible chat completions."""

    DEFAULT_ENDPOINT = "https://api.openai.com/v1/chat/completions"
    DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"
    DEFAULT_MODEL = "gpt-4o"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)

    def name(self) -> str:
        return "openai"