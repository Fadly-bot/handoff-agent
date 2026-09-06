"""Tests for provider base interface, registry, and factory."""

import pytest

from handoff_agent.providers.base import ProviderAdapter
from handoff_agent.providers.factory import (
    ProviderError,
    ProviderNotFoundError,
    UnknownProviderError,
    available_providers,
    create_provider,
    is_known_provider,
    register_provider,
    resolve_provider_name,
)
from handoff_agent.providers.claude import ClaudeProvider


class DummyProvider(ProviderAdapter):
    def __init__(self, config=None):
        pass

    def name(self) -> str:
        return "dummy"

    def generate(self, context: dict, prompt: str) -> str:
        return "generated"

    def validate_config(self, config: dict) -> bool:
        return True


class TestProviderAdapter:
    def test_abstract_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            ProviderAdapter()  # type: ignore[abstract]

    def test_concrete_implementation(self) -> None:
        provider = DummyProvider()
        assert provider.name() == "dummy"

    def test_generate_returns_string(self) -> None:
        provider = DummyProvider()
        result = provider.generate({}, "test prompt")
        assert isinstance(result, str)

    def test_validate_config(self) -> None:
        provider = DummyProvider()
        assert provider.validate_config({}) is True


# ===================================================================
# Registry / Factory
# ===================================================================

class TestRegistry:
    def test_known_provider_lookup(self) -> None:
        assert is_known_provider("claude") is True

    def test_unknown_provider_lookup(self) -> None:
        assert is_known_provider("nonexistent") is False

    def test_create_known_provider(self) -> None:
        provider = create_provider("claude", {"model": "claude-sonnet"})
        assert isinstance(provider, ClaudeProvider)

    def test_create_unknown_provider_raises(self) -> None:
        with pytest.raises(UnknownProviderError):
            create_provider("bogus_provider", {})

    def test_unknown_provider_error_message_clear(self) -> None:
        with pytest.raises(UnknownProviderError, match="Unknown provider"):
            create_provider("nope", {})

    def test_register_simple_provider(self) -> None:
        register_provider("dummy", DummyProvider)
        try:
            assert is_known_provider("dummy") is True
            assert create_provider("dummy", {}).name() == "dummy"
        finally:
            from handoff_agent.providers import factory
            factory._PROVIDERS.pop("dummy", None)

    def test_register_rejects_overwrite_by_default(self) -> None:
        register_provider("dummy", DummyProvider)
        try:
            with pytest.raises(ProviderError, match="already registered"):
                register_provider("dummy", DummyProvider)
        finally:
            from handoff_agent.providers import factory
            factory._PROVIDERS.pop("dummy", None)

    def test_register_allows_overwrite_with_flag(self) -> None:
        class Dummy2(DummyProvider):
            def name(self) -> str:
                return "dummy2"
        register_provider("dummy", DummyProvider)
        register_provider("dummy", Dummy2, overwrite=True)
        try:
            assert create_provider("dummy", {}).name() == "dummy2"
        finally:
            from handoff_agent.providers import factory
            factory._PROVIDERS.pop("dummy", None)

    def test_register_rejects_non_adapter(self) -> None:
        with pytest.raises(ProviderError, match="ProviderAdapter"):
            register_provider("bad_type", dict)

    def test_register_rejects_empty_name(self) -> None:
        with pytest.raises(ProviderError, match="non-empty"):
            register_provider("", DummyProvider)

    def test_register_rejects_non_string_name(self) -> None:
        with pytest.raises(ProviderError, match="non-empty"):
            register_provider(123, DummyProvider)  # type: ignore[arg-type]


class TestFactorySelection:
    def test_resolve_explicit_provider(self) -> None:
        assert resolve_provider_name("claude", None) == "claude"

    def test_resolve_default_provider(self) -> None:
        assert resolve_provider_name(None, "claude") == "claude"

    def test_resolve_explicit_over_default(self) -> None:
        assert resolve_provider_name("openai", "claude") == "openai"

    def test_resolve_no_provider_raises(self) -> None:
        with pytest.raises(ProviderNotFoundError, match="No provider"):
            resolve_provider_name(None, None)

    def test_resolve_empty_string_treated_as_none(self) -> None:
        assert resolve_provider_name("", "claude") == "claude"

    def test_no_automatic_fallback_when_unknown(self) -> None:
        """Unknown default provider must not silently fall back to claude."""
        with pytest.raises(UnknownProviderError):
            create_provider("missing_default", {})


class TestAvailableProviders:
    def test_available_includes_claude(self) -> None:
        assert "claude" in available_providers()

    def test_available_is_sorted(self) -> None:
        providers = available_providers()
        assert providers == tuple(sorted(providers))

    def test_available_is_tuple(self) -> None:
        assert isinstance(available_providers(), tuple)


# ===================================================================
# Provider failure isolation
# ===================================================================

class TestProviderIsolation:
    def test_generate_failure_does_not_trigger_another_provider(self) -> None:
        """When generate() fails, no other provider should be instantiated."""
        from unittest.mock import MagicMock

        call_log = []

        class FailingProvider(ProviderAdapter):
            def __init__(self, **kwargs):
                call_log.append(("failing", "init"))
            def name(self) -> str:
                return "failing"
            def generate(self, context, prompt):
                call_log.append(("failing", "generate"))
                raise RuntimeError("I always fail")
            def validate_config(self, config):
                return True

        class TrackingProvider(ProviderAdapter):
            def __init__(self, **kwargs):
                call_log.append(("tracking", "init"))
            def name(self) -> str:
                return "tracking"
            def generate(self, context, prompt):
                call_log.append(("tracking", "generate"))
                return "ok"
            def validate_config(self, config):
                return True

        register_provider("failing", FailingProvider, overwrite=True)
        register_provider("tracking", TrackingProvider, overwrite=True)
        try:
            p = create_provider("failing", {})
            with pytest.raises(RuntimeError, match="I always fail"):
                p.generate({}, "test")
            # Only failing provider should have been touched
            assert ("failing", "init") in call_log
            assert ("failing", "generate") in call_log
            assert ("tracking", "init") not in call_log
            assert ("tracking", "generate") not in call_log
        finally:
            from handoff_agent.providers import factory
            factory._PROVIDERS.pop("failing", None)
            factory._PROVIDERS.pop("tracking", None)
