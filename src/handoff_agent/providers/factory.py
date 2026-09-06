"""Provider registry / factory for Handoff Agent.

Maps a provider name to its adapter, validates configuration, and
instantiates providers. Unknown providers produce a clear error.

Selection precedence (no automatic fallback):

    --provider X
        ↓
    explicit provider
    otherwise
        ↓
    config.default_provider
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from handoff_agent.providers.base import (
    ProviderAdapter,
    ProviderConfigError,
    ProviderError,
)
from handoff_agent.providers.claude import ClaudeProvider
from handoff_agent.providers.deepseek import DeepSeekProvider
from handoff_agent.providers.openai import OpenAIProvider
from handoff_agent.providers.qwen import QwenProvider

if TYPE_CHECKING:
    from handoff_agent.providers.base import (
        ProviderAdapter as _ProviderAdapter,  # noqa: F401
    )


class UnknownProviderError(ProviderError):
    """Raised when the requested provider is not registered."""


class ProviderNotFoundError(ProviderError):
    """Raised when a valid provider name cannot be resolved."""


_PROVIDERS: dict[str, type[ProviderAdapter]] = {
    "claude": ClaudeProvider,
    "openai": OpenAIProvider,
    "qwen": QwenProvider,
    "deepseek": DeepSeekProvider,
}


def available_providers() -> tuple[str, ...]:
    """Return the names of all registered providers."""
    return tuple(sorted(_PROVIDERS.keys()))


def register_provider(
    name: str,
    adapter_cls: type[ProviderAdapter],
    *,
    overwrite: bool = False,
) -> None:
    """Register a provider adapter class under *name*.

    Raises ``ProviderError`` if *name* is already registered and *overwrite*
    is False.
    """
    if not name or not isinstance(name, str):
        raise ProviderError("Provider name must be a non-empty string.")
    if not (isinstance(adapter_cls, type) and issubclass(adapter_cls, ProviderAdapter)):
        raise ProviderError(
            f"Provider '{name}' must be a ProviderAdapter subclass."
        )
    if name in _PROVIDERS and not overwrite:
        raise ProviderError(
            f"Provider '{name}' is already registered. Use overwrite=True to replace."
        )
    _PROVIDERS[name] = adapter_cls


def create_provider(name: str, config: dict) -> ProviderAdapter:
    """Instantiate a provider adapter for *name* with *config*.

    Raises ``UnknownProviderError`` for unregistered names.
    """
    if name not in _PROVIDERS:
        raise UnknownProviderError(
            f"Unknown provider: '{name}'. Available providers: "
            f"{', '.join(available_providers()) or '(none)'}."
        )
    adapter_cls = _PROVIDERS[name]
    return adapter_cls(config=config)


def resolve_provider_name(
    explicit: str | None,
    config_default: str | None,
) -> str:
    """Resolve which provider name to use, with no automatic fallback.

    Priority: explicit argument, then config default.
    Raises ``ProviderNotFoundError`` if no provider is determined.
    """
    if explicit:
        return explicit
    if config_default:
        return config_default
    raise ProviderNotFoundError(
        "No provider specified and no default provider configured."
    )


def is_known_provider(name: str) -> bool:
    """Return True if *name* is a registered provider."""
    return name in _PROVIDERS