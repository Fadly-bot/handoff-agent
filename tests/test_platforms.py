"""Tests for Phase 16 — AI Platform Adapters.

Covers the platform adapter architecture, all registered platforms (Claude,
ChatGPT/OpenAI, Gemini, Perplexity, Grok, DeepSeek, Qwen, Kimi, GLM, Manus,
OpenCode, Cline), capability mapping, interface support, instruction mapping,
skill/MCP/CLI integration, configuration/auth boundaries, API-key isolation,
platform-specific limitations, unsupported-feature handling, deterministic
output, and adapter conformance.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from conftest import init_repo

from handoff_agent.capability import ALL_CAPABILITIES, WRITE_CAPABILITIES
from handoff_agent.adapters import (
    AdapterUnsupportedError,
    UnknownPlatformError,
    create_platform_adapter,
    discover_platform_adapters,
    get_platform_spec,
    integration_instructions,
    is_platform,
    list_platforms,
    platform_instructions,
)
from handoff_agent.adapters.platforms import (
    FALLBACK_PLATFORM,
    PlatformAdapter,
    PlatformError,
    PlatformSpec,
)

EXPECTED_PLATFORMS = {
    "claude",
    "chatgpt",
    "gemini",
    "perplexity",
    "grok",
    "deepseek",
    "qwen",
    "kimi",
    "glm",
    "manus",
    "opencode",
    "cline",
}


class TestRegistry:
    def test_all_platforms_present(self) -> None:
        assert EXPECTED_PLATFORMS <= set(list_platforms())

    def test_discovery_deterministic(self) -> None:
        d1 = discover_platform_adapters()
        d2 = discover_platform_adapters()
        assert d1 == d2
        assert set(d1) == {"chatgpt", "claude", "cline", "deepseek", "gemini",
                           "glm", "grok", "kimi", "manus", "opencode", "perplexity", "qwen"}

    def test_is_platform(self) -> None:
        assert is_platform("claude")
        assert not is_platform("nope")

    def test_unknown_platform_no_automatic_fallback(self) -> None:
        with pytest.raises(UnknownPlatformError):
            create_platform_adapter("does-not-exist")

    def test_fallback_requires_explicit_flag(self) -> None:
        with pytest.raises(UnknownPlatformError):
            create_platform_adapter("generic")
        fb = create_platform_adapter("generic", fallback=True)
        assert fb.name == "generic"
        assert fb.spec.read_only is True

    def test_every_known_platform_constructible(self) -> None:
        for name in list_platforms():
            pa = create_platform_adapter(name)
            assert pa.name == name


class TestSpecWellFormed:
    def test_capabilities_are_known(self) -> None:
        for name in list_platforms():
            spec = get_platform_spec(name)
            assert spec.capability_grant <= ALL_CAPABILITIES, name

    def test_read_only_platforms_never_grant_write(self) -> None:
        read_only = {"perplexity", "deepseek", "qwen", "kimi", "glm"}
        for name in read_only:
            spec = get_platform_spec(name)
            assert spec.read_only is True
            assert not (spec.capability_grant & WRITE_CAPABILITIES)

    def test_auth_boundary_is_env_name_only(self) -> None:
        for name in list_platforms():
            pa = create_platform_adapter(name)
            boundary = pa.authentication_boundary()
            env = boundary["env_var"]
            if env is not None:
                # Must be an environment variable NAME, never a value.
                assert re.fullmatch(r"[A-Z][A-Z0-9_]*", env), (name, env)
            # The boundary never carries an actual key.
            assert "sk-" not in json.dumps(boundary).lower()

    def test_instruction_map_deterministic(self) -> None:
        a = create_platform_adapter("claude").instruction_map()
        b = create_platform_adapter("claude").instruction_map()
        assert a == b
        assert list(a.keys()) == sorted(a.keys())

    def test_capability_map_is_deterministic(self) -> None:
        a = create_platform_adapter("opencode").capability_map()
        b = create_platform_adapter("opencode").capability_map()
        assert a == b
        assert all(cap in a for cap in ALL_CAPABILITIES)


class TestInterfaceSupport:
    @pytest.mark.parametrize(
        "name,iface,expect",
        [
            ("claude", "skill", True),
            ("claude", "mcp", True),
            ("claude", "cli", True),
            ("chatgpt", "mcp", False),
            ("chatgpt", "cli", False),
            ("gemini", "mcp", True),
            ("perplexity", "skill", False),
            ("perplexity", "mcp", False),
            ("deepseek", "mcp", False),
            ("opencode", "skill", True),
            ("opencode", "mcp", True),
            ("opencode", "cli", True),
            ("cline", "mcp", True),
        ],
    )
    def test_interface_matrix(self, name: str, iface: str, expect: bool) -> None:
        pa = create_platform_adapter(name)
        assert pa.spec.supports(iface) is expect, (name, iface)

    def test_concrete_adapter_for_supported_interface(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        pa = create_platform_adapter("opencode")
        ad = pa.concrete_adapter("file", project_root=str(repo))
        assert ad.adapter_name == "file"
        assert ad.started is True

    def test_concrete_adapter_rejects_unsupported(self, tmp_path: Path) -> None:
        pa = create_platform_adapter("perplexity")
        with pytest.raises(AdapterUnsupportedError):
            pa.concrete_adapter("file", project_root=str(tmp_path))

    def test_integration_instructions_unsupported_interface(self) -> None:
        with pytest.raises(AdapterUnsupportedError):
            integration_instructions("perplexity", "mcp")


class TestInstructions:
    def test_protocol_instructions_are_provider_independent(self) -> None:
        for name in list_platforms():
            text = platform_instructions(get_platform_spec(name))
            assert "Universal Handoff Protocol" in text
            # Provider-agnostic: no dependency on a specific vendor API.
            assert "handoff-protocol" in text.lower() or "checkpoint" in text.lower()

    def test_instructions_have_no_secrets(self) -> None:
        for name in list_platforms():
            pa = create_platform_adapter(name)
            for iface, text in pa.instruction_map().items():
                assert "sk-" not in text.lower()
                assert "api_key" not in text.lower()

    def test_skill_integration_mentions_skill(self) -> None:
        text = integration_instructions("claude", "skill")
        assert "skills/universal-handoff" in text
        assert "read-before-continue" in text

    def test_mcp_integration_mentions_server(self) -> None:
        text = integration_instructions("claude", "mcp")
        assert "MCP server" in text
        assert "docs/MCP.md" in text

    def test_cli_integration_mentions_cli(self) -> None:
        text = integration_instructions("opencode", "cli")
        assert "handoff inspect" in text

    def test_read_only_flagged_in_instructions(self) -> None:
        # Perplexity supports no machine interface -> any integration plan
        # request must be refused.
        with pytest.raises(AdapterUnsupportedError):
            integration_instructions("perplexity", "file")


class TestPlatformAdapterConformance:
    def test_concrete_file_adapter_passes_basic_ops(self, tmp_path: Path) -> None:
        from handoff_agent.adapters.base import AdapterWriteResult
        from handoff_agent.adapters.filesystem import FileAdapter
        from handoff_agent.protocol import build_checkpoint, render_state_block

        repo = tmp_path / "repo"
        init_repo(repo)
        pa = create_platform_adapter("claude")
        ad = pa.concrete_adapter("file", project_root=str(repo))
        assert isinstance(ad, FileAdapter)
        cp = build_checkpoint(objective="platform", project_name="repo")
        result = ad.write_checkpoint("# Platform\n\n" + render_state_block(cp))
        assert result.created is True
        assert ad.validate_checkpoint()["valid"] is True
        assert ad.read_handoff() is not None

    def test_read_only_platform_file_adapter_denies_write(self, tmp_path: Path) -> None:
        from handoff_agent.adapters import AdapterPermissionError

        repo = tmp_path / "repo"
        init_repo(repo)
        pa = create_platform_adapter("deepseek")  # read-only, supports file
        ad = pa.concrete_adapter("file", project_root=str(repo))
        assert ad.can("checkpoint.update") is False
        assert ad.status()["read_only"] is True
        with pytest.raises(AdapterPermissionError):
            ad.write_checkpoint("x")

    def test_platform_spec_rejects_unknown_capabilities(self) -> None:
        with pytest.raises(PlatformError):
            PlatformSpec(
                name="bad",
                display_name="Bad",
                capability_grant=frozenset({"nonsense.cap"}),
            )


class TestNoHardcodedCredentials:
    def test_platform_module_has_no_secret_values(self) -> None:
        import inspect
        from handoff_agent.adapters import platforms as mod
        source = inspect.getsource(mod)
        assert "sk-" not in source.lower()
        assert "api_key=" not in source.lower().replace(" ", "")
        # No real key values anywhere in the platform table.
        for env_name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
            # Only the name may appear — never an assignment to a value.
            assert re.search(rf"{env_name}\s*=\s*['\"]", source) is None

    def test_fallback_spec_is_read_only(self) -> None:
        assert FALLBACK_PLATFORM.read_only is True
        assert (FALLBACK_PLATFORM.capability_grant & WRITE_CAPABILITIES) == set()

    def test_no_automatic_fallback_in_factory(self) -> None:
        with pytest.raises(UnknownPlatformError):
            create_platform_adapter("megacorp-ai")