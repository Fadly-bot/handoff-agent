"""Generic Adapter registry, discovery, and factory (Phase 15).

Handles adapter configuration, registration, discovery, and safe creation.
Adapter creation never falls back to a different adapter silently; an unknown
name raises ``AdapterConfigError``.
"""

from __future__ import annotations

from typing import Any, Callable

from handoff_agent.adapters.base import (
    AdapterConfigError,
    AdapterError,
    AdapterPermissionError,
    AdapterStateError,
    AdapterUnsupportedError,
    AdapterWriteResult,
    BaseAdapter,
    contains_secret_like,
    map_agent_identity,
    resolve_within_root,
)
from handoff_agent.adapters.cli import CliAdapter
from handoff_agent.adapters.filesystem import FileAdapter
from handoff_agent.adapters.api import ApiAdapter

# Re-export the AI platform adapters (Phase 16).
from handoff_agent.adapters.platforms import (  # noqa: E402  # isort: skip
    FALLBACK_PLATFORM,
    PlatformAdapter,
    PlatformError,
    PlatformInterface,
    PlatformSpec,
    UnknownPlatformError,
    create_platform_adapter,
    discover_platform_adapters,
    get_platform_spec,
    integration_instructions,
    is_platform,
    list_platforms,
    platform_instructions,
)

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, type[BaseAdapter]] = {}


def register_adapter(name: str, cls: type[BaseAdapter]) -> None:
    """Register an adapter class under *name*."""
    if not _REGISTRY:
        _register_builtins()
    if name in _REGISTRY:
        raise AdapterConfigError(f"Adapter {name!r} is already registered.")
    _REGISTRY[name] = cls


def register_adapters(mapping: dict[str, type[BaseAdapter]]) -> None:
    """Register several adapters at once."""
    for name, cls in mapping.items():
        register_adapter(name, cls)


def unregister_adapter(name: str) -> None:
    """Unregister an adapter (used in tests)."""
    _REGISTRY.pop(name, None)


def _register_builtins() -> None:
    from handoff_agent.adapters.cli import CliAdapter
    from handoff_agent.adapters.filesystem import FileAdapter
    from handoff_agent.adapters.api import ApiAdapter

    _REGISTRY.update(
        {
            "file": FileAdapter,
            "cli": CliAdapter,
            "api": ApiAdapter,
        }
    )


def is_registered(name: str) -> bool:
    return name in _REGISTRY


def list_adapters() -> tuple[str, ...]:
    """Return all registered adapter names (deterministic)."""
    if not _REGISTRY:
        _register_builtins()
    return tuple(sorted(_REGISTRY))


def discover_adapters() -> dict[str, dict[str, Any]]:
    """Return the adapter registry as deterministic descriptors."""
    if not _REGISTRY:
        _register_builtins()
    return {
        name: {
            "class": cls.adapter_name,
            "version": getattr(cls, "adapter_version", "0.0.0"),
        }
        for name, cls in sorted(_REGISTRY.items())
    }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_adapter(
    name: str,
    config: dict[str, Any] | None = None,
    **kwargs: Any,
) -> BaseAdapter:
    """Create an adapter instance by registered name.

    Unknown names raise ``AdapterConfigError`` — there is never an automatic
    fallback to a different adapter type.
    """
    if not _REGISTRY:
        _register_builtins()
    cls = _REGISTRY.get(name)
    if cls is None:
        raise AdapterConfigError(
            f"Unknown adapter {name!r}. Available: {', '.join(list_adapters())}"
        )
    return cls(config=config, **kwargs)


__all__ = [
    "AdapterConfigError",
    "AdapterError",
    "AdapterPermissionError",
    "AdapterStateError",
    "AdapterUnsupportedError",
    "AdapterWriteResult",
    "ApiAdapter",
    "BaseAdapter",
    "CliAdapter",
    "FALLBACK_PLATFORM",
    "FileAdapter",
    "PlatformAdapter",
    "PlatformError",
    "PlatformInterface",
    "PlatformSpec",
    "UnknownPlatformError",
    "contains_secret_like",
    "create_adapter",
    "create_platform_adapter",
    "discover_adapters",
    "discover_platform_adapters",
    "get_platform_spec",
    "integration_instructions",
    "is_platform",
    "is_registered",
    "list_adapters",
    "list_platforms",
    "map_agent_identity",
    "platform_instructions",
    "register_adapter",
    "register_adapters",
    "resolve_within_root",
    "unregister_adapter",
]