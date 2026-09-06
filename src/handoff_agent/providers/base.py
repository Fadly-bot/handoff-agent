"""Abstract base class and shared error types for AI providers.

Providers only receive a structured ``FullContext`` and a prepared prompt.
They MUST NOT access the filesystem, execute project/git commands, or inspect
the environment beyond reading their configured API key.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from handoff_agent.context_builder import FullContext


class ProviderError(Exception):
    """Base error for all provider failures."""


class ProviderRequestError(ProviderError):
    """Base error for failures during an AI provider request."""


class ProviderMissingKeyError(ProviderRequestError):
    """Raised when the configured API key environment variable is not set."""


class ProviderHttpError(ProviderRequestError):
    """Raised when a provider API returns an HTTP error."""


class ProviderNetworkError(ProviderRequestError):
    """Raised on network / transport failures."""


class ProviderResponseError(ProviderRequestError):
    """Raised when a provider response cannot be parsed."""


class ProviderConfigError(ProviderError):
    """Raised when provider configuration is invalid or unsafe."""


class ProviderAdapter(ABC):
    """Abstract interface for AI providers.

    Providers only receive a structured ``FullContext`` and a prepared prompt.
    They MUST NOT access the filesystem, execute project/git commands, or
    inspect the environment beyond reading their configured API key.
    """

    @abstractmethod
    def name(self) -> str:
        """Return the provider name."""
        ...

    @abstractmethod
    def generate(self, context: "FullContext", prompt: str) -> str:
        """Generate handoff content from a full context and prepared prompt."""
        ...

    @abstractmethod
    def validate_config(self, config: dict[str, Any]) -> bool:
        """Validate provider configuration. Return True if valid."""
        ...