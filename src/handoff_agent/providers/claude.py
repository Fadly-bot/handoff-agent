"""Claude provider — sends prepared prompts to the Anthropic API.

Only this provider is permitted to perform network requests. It uses only the
Python standard library (urllib) and never hardcodes an API key or model ID.

Security rules enforced here:
  - API key is read only from the configured environment variable at request
    time; it is never hardcoded, stored in config, printed, logged, or placed
    inside exceptions.
  - No secret, API key, headers, or request payload is ever logged.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

from handoff_agent.providers.base import (
    ProviderAdapter,
    ProviderConfigError,
    ProviderError,
    ProviderHttpError,
    ProviderMissingKeyError,
    ProviderNetworkError,
    ProviderResponseError,
)

if TYPE_CHECKING:
    from handoff_agent.context_builder import FullContext

logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "https://api.anthropic.com/v1/messages"
_DEFAULT_MODEL = "claude-sonnet-4-5"
_DEFAULT_API_KEY_ENV = "ANTHROPIC_API_KEY"
_MAX_RESPONSE_TOKENS = 4096


class ClaudeProviderError(ProviderError):
    """Base error for Claude provider failures."""


class ClaudeMissingApiKeyError(ProviderMissingKeyError, ClaudeProviderError):
    """Raised when the configured API key environment variable is not set."""


class ClaudeHttpError(ProviderHttpError, ClaudeProviderError):
    """Raised when the Anthropic API returns an HTTP error."""


class ClaudeNetworkError(ProviderNetworkError, ClaudeProviderError):
    """Raised on network / transport failures."""


class ClaudeBadResponseError(ProviderResponseError, ClaudeProviderError):
    """Raised when the response cannot be parsed."""


class ClaudeProvider(ProviderAdapter):
    """Provider adapter for Anthropic/Claude."""

    DEFAULT_MODEL = _DEFAULT_MODEL

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self.model = str(config.get("model") or "").strip()
        if not self.model:
            self.model = self.__class__.DEFAULT_MODEL
        self.api_key_env = str(
            config.get("api_key_env") or _DEFAULT_API_KEY_ENV
        ).strip()
        self.endpoint = str(
            config.get("endpoint") or _DEFAULT_ENDPOINT
        ).strip()

    def name(self) -> str:
        return "claude"

    def validate_config(self, config: dict[str, Any]) -> bool:
        """Validate configuration. A model must be configured."""
        api_key_env = config.get("api_key_env")
        if api_key_env is not None and (
            not isinstance(api_key_env, str) or not api_key_env.strip()
        ):
            return False
        endpoint = config.get("endpoint")
        if endpoint is not None and (
            not isinstance(endpoint, str) or not endpoint.strip().startswith("https://")
        ):
            return False
        return True

    def is_configured(self) -> bool:
        """Return True if a required API key env var name is present in env."""
        return bool(self._read_api_key())

    def _read_api_key(self) -> str | None:
        """Read the API key from the environment (never logs it)."""
        key = os.environ.get(self.api_key_env)
        return key if key else None

    def generate(self, context: "FullContext", prompt: str) -> str:
        """Send the prompt to Anthropic and return the assistant text.

        The context is only used for payload composition; it is never logged.
        """
        api_key = self._require_api_key()
        payload = self._build_request(prompt)
        response_text = self._post(api_key=api_key, payload=payload)
        return self._extract_text(response_text)

    def _require_api_key(self) -> str:
        key = self._read_api_key()
        if not key:
            raise ClaudeMissingApiKeyError(
                f"{self.api_key_env} is not configured"
            )
        return key

    def _assert_safe_endpoint(self) -> None:
        """Refuse anything other than an HTTPS endpoint."""
        if not self.endpoint.startswith("https://"):
            raise ProviderConfigError(
                f"Provider '{self.name()}' requires an https endpoint."
            )

    def _build_request(self, prompt: str) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": _MAX_RESPONSE_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }

    def _post(self, api_key: str, payload: dict[str, Any]) -> str:
        """Perform the HTTP POST. Never logs secrets or the payload."""
        self._assert_safe_endpoint()
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=data,
            method="POST",
        )
        request.add_header("content-type", "application/json")
        request.add_header("x-api-key", api_key)
        request.add_header("anthropic-version", "2023-06-01")

        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = _safe_read_body(exc)
            raise ClaudeHttpError(
                f"Claude API returned HTTP {exc.code}"
            ) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ClaudeNetworkError("Claude API request failed (network)") from exc

    def _extract_text(self, response_text: str) -> str:
        """Parse the Anthropic messages response and return the text."""
        try:
            data = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise ClaudeBadResponseError(
                "Claude API returned an unreadable response"
            ) from exc

        content = data.get("content")
        if not isinstance(content, list):
            raise ClaudeBadResponseError(
                "Claude API returned an unexpected response"
            )

        parts: list[str] = []
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                parts.append(block["text"])

        if not parts:
            raise ClaudeBadResponseError(
                "Claude API returned no text content"
            )
        return "\n".join(parts)


def _safe_read_body(exc: urllib.error.HTTPError) -> str:
    """Read an error body without letting non-ASCII crash the parse."""
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:
        return ""