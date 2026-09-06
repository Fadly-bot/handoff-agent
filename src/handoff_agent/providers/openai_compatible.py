"""Shared HTTP implementation for OpenAI-compatible chat providers.

Used by the OpenAI, Qwen (DashScope compatible-mode), and DeepSeek providers.
Only providers are permitted to perform network requests. It uses only the
Python standard library (urllib) and never hardcodes an API key.

Security rules enforced here:
  - API key is read only from the configured environment variable at request
    time; it is never hardcoded, stored in config, printed, logged, or placed
    inside exceptions.
  - No secret, API key, headers, or request payload is ever logged.
  - Endpoints must be HTTPS; non-HTTPS endpoints are refused before any
    network traffic.
  - No filesystem access, no Git commands, no environment enumeration.
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
    ProviderHttpError,
    ProviderMissingKeyError,
    ProviderNetworkError,
    ProviderResponseError,
)

if TYPE_CHECKING:
    from handoff_agent.context_builder import FullContext

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 60


class OpenAICompatibleProvider(ProviderAdapter):
    """Base adapter for OpenAI-compatible ``/chat/completions`` APIs.

    Subclasses provide their default endpoint, API-key environment variable,
    and default model. Everything may be overridden through ``config``:
    ``model``, ``api_key_env``, and ``endpoint``.
    """

    DEFAULT_ENDPOINT = ""
    DEFAULT_API_KEY_ENV = ""
    DEFAULT_MODEL = ""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self.model = str(config.get("model") or "").strip()
        if not self.model:
            self.model = self.__class__.DEFAULT_MODEL
        self.api_key_env = str(config.get("api_key_env") or "").strip()
        if not self.api_key_env:
            self.api_key_env = self.__class__.DEFAULT_API_KEY_ENV
        self.endpoint = str(config.get("endpoint") or "").strip()
        if not self.endpoint:
            self.endpoint = self.__class__.DEFAULT_ENDPOINT

    def name(self) -> str:
        raise NotImplementedError

    def validate_config(self, config: dict[str, Any]) -> bool:
        """Validate configuration for structural safety."""
        api_key_env = config.get("api_key_env")
        if api_key_env is not None and (
            not isinstance(api_key_env, str) or not api_key_env.strip()
        ):
            return False
        endpoint = config.get("endpoint")
        if endpoint is not None and not (
            isinstance(endpoint, str) and endpoint.strip().startswith("https://")
        ):
            return False
        return True

    def is_configured(self) -> bool:
        """Return True if the required API key env var is present in env."""
        return bool(self._read_api_key())

    def _read_api_key(self) -> str | None:
        """Read the API key from the environment (never logs it)."""
        key = os.environ.get(self.api_key_env)
        return key if key else None

    def _require_api_key(self) -> str:
        key = self._read_api_key()
        if not key:
            raise ProviderMissingKeyError(
                f"{self.api_key_env} is not configured"
            )
        return key

    def _assert_safe_endpoint(self) -> None:
        """Refuse anything other than an HTTPS endpoint."""
        if not self.endpoint.startswith("https://"):
            raise ProviderConfigError(
                f"Provider '{self.name()}' requires an https endpoint."
            )

    def generate(self, context: "FullContext", prompt: str) -> str:
        """Send the prompt to the provider and return the assistant text.

        The context is only used for payload composition; it is never logged.
        """
        self._assert_safe_endpoint()
        api_key = self._require_api_key()
        payload = self._build_request(prompt)
        response_text = self._post(api_key=api_key, payload=payload)
        return self._extract_text(response_text)

    def _build_request(self, prompt: str) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
        }

    def _post(self, api_key: str, payload: dict[str, Any]) -> str:
        """Perform the HTTPS POST. Never logs secrets or the payload."""
        self._assert_safe_endpoint()
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=data,
            method="POST",
        )
        request.add_header("content-type", "application/json")
        request.add_header("authorization", f"Bearer {api_key}")

        try:
            with urllib.request.urlopen(
                request, timeout=_REQUEST_TIMEOUT_SECONDS
            ) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            _safe_read_body(exc)
            raise ProviderHttpError(
                f"{self.name()} API returned HTTP {exc.code}"
            ) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ProviderNetworkError(
                f"{self.name()} API request failed (network)"
            ) from exc

    def _extract_text(self, response_text: str) -> str:
        """Parse an OpenAI-compatible chat response and return the text."""
        try:
            data = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise ProviderResponseError(
                f"{self.name()} API returned an unreadable response"
            ) from exc

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderResponseError(
                f"{self.name()} API returned an unexpected response"
            )

        first = choices[0]
        if not isinstance(first, dict):
            raise ProviderResponseError(
                f"{self.name()} API returned an unexpected response"
            )
        message = first.get("message")
        if not isinstance(message, dict):
            raise ProviderResponseError(
                f"{self.name()} API returned an unexpected response"
            )
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            raise ProviderResponseError(
                f"{self.name()} API returned no text content"
            )
        return text


def _safe_read_body(exc: urllib.error.HTTPError) -> str:
    """Read an error body without letting non-ASCII crash the parse."""
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:
        return ""