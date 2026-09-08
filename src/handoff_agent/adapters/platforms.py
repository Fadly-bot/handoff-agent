"""AI Platform Adapters (Phase 16).

Maps each AI platform (Claude, ChatGPT/OpenAI, Gemini, Perplexity, Grok,
DeepSeek, Qwen, Kimi, GLM, Manus, OpenCode, Cline, and future agents) onto the
Handoff interfaces: capability mapping, interface support (skill / MCP / CLI /
file / API), platform-specific instruction mapping, authentication boundary,
model configuration, limitations, and unsupported-feature handling.

Design rules:

  - Provider-independent core: platform data is declarative; no platform
    import is executed here.
  - No automatic provider fallback: ``create_platform_adapter`` only returns a
    generic fallback adapter when ``fallback=True`` is explicitly passed.
  - No hardcoded credentials: only environment-variable *names* are stored.
  - API-key isolation: values are read from the environment at request time;
    they never appear in any output of this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from handoff_agent.adapters.base import AdapterUnsupportedError, BaseAdapter
from handoff_agent.capability import (
    ALL_CAPABILITIES,
    READ_CAPABILITIES,
    WRITE_CAPABILITIES,
    AgentIdentity,
)


# ---------------------------------------------------------------------------
# Interface identifiers
# ---------------------------------------------------------------------------

IFACE_SKILL = "skill"
IFACE_MCP = "mcp"
IFACE_CLI = "cli"
IFACE_FILE = "file"
IFACE_API = "api"

_INTERFACES: tuple[str, ...] = (IFACE_SKILL, IFACE_MCP, IFACE_CLI, IFACE_FILE, IFACE_API)

#: Version string emitted by conformance / registry tooling for platforms.
PLATFORM_SCHEMA_VERSION = "1"


class PlatformError(Exception):
    """Base error for platform adapter failures."""


class UnknownPlatformError(PlatformError):
    """Raised when a platform name is not registered."""


@dataclass(frozen=True)
class PlatformInterface:
    """Declares whether a platform supports an interface and how."""

    skill: bool = False
    mcp: bool = False
    cli: bool = False
    file: bool = False
    api: bool = False

    def supports(self, interface: str) -> bool:
        return getattr(self, interface, False)

    def as_names(self) -> tuple[str, ...]:
        return tuple(i for i in _INTERFACES if getattr(self, i, False))

    def to_dict(self) -> dict[str, bool]:
        return {i: getattr(self, i, False) for i in _INTERFACES}


@dataclass(frozen=True)
class PlatformSpec:
    """Declarative, provider-independent description of one AI platform."""

    name: str
    display_name: str
    interfaces: PlatformInterface = field(default_factory=PlatformInterface)
    capability_grant: frozenset[str] = field(default_factory=lambda: frozenset())
    auth_env: str | None = None
    default_model: str = ""
    models: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        unknown = self.capability_grant - ALL_CAPABILITIES
        if unknown:
            raise PlatformError(
                f"Platform {self.name!r} declares unknown capabilities: "
                f"{', '.join(sorted(unknown))}"
            )

    @property
    def read_only(self) -> bool:
        return not bool(self.capability_grant & WRITE_CAPABILITIES)

    def supports(self, interface: str) -> bool:
        return self.interfaces.supports(interface)

    def can_read(self) -> bool:
        return bool(self.capability_grant & READ_CAPABILITIES)

    def can_write(self) -> bool:
        return bool(self.capability_grant & WRITE_CAPABILITIES)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "interfaces": self.interfaces.to_dict(),
            "capabilities": sorted(self.capability_grant),
            "read_only": self.read_only,
            "auth_env": self.auth_env,
            "default_model": self.default_model,
            "models": list(self.models),
            "limitations": list(self.limitations),
            "unsupported": list(self.unsupported),
        }


# ---------------------------------------------------------------------------
# Platform registry
# ---------------------------------------------------------------------------

# Capability grants. Read-only platforms only hold read + inspection
# capabilities; they can never create/update a checkpoint.

PLATFORM_SPECS: dict[str, PlatformSpec] = {
    "claude": PlatformSpec(
        name="claude",
        display_name="Claude (Claude Code)",
        interfaces=PlatformInterface(skill=True, mcp=True, cli=True, file=True),
        capability_grant=frozenset(ALL_CAPABILITIES),
        auth_env="ANTHROPIC_API_KEY",
        default_model="claude-sonnet-4-5",
        models=("claude-sonnet-4-5", "claude-opus-4-1", "claude-haiku-4-5"),
        limitations=("requires API key for cloud usage",),
        unsupported=(),
    ),
    "chatgpt": PlatformSpec(
        name="chatgpt",
        display_name="ChatGPT / OpenAI",
        interfaces=PlatformInterface(skill=True, file=True),
        capability_grant=frozenset(ALL_CAPABILITIES),
        auth_env="OPENAI_API_KEY",
        default_model="gpt-4o",
        models=("gpt-4o", "gpt-4.1", "o3"),
        limitations=(
            "write operations require explicit approval (custom actions/files)",
            "no sandboxed MCP endpoint",
        ),
        unsupported=(IFACE_MCP, IFACE_CLI),
    ),
    "gemini": PlatformSpec(
        name="gemini",
        display_name="Gemini (Google AI Studio)",
        interfaces=PlatformInterface(skill=True, mcp=True),
        capability_grant=frozenset(ALL_CAPABILITIES),
        auth_env="GOOGLE_API_KEY",
        default_model="gemini-2.5-pro",
        models=("gemini-2.5-pro", "gemini-2.5-flash"),
        limitations=("MCP support varies by client",),
        unsupported=(IFACE_CLI, IFACE_FILE),
    ),
    "perplexity": PlatformSpec(
        name="perplexity",
        display_name="Perplexity",
        interfaces=PlatformInterface(),
        capability_grant=frozenset(READ_CAPABILITIES),
        auth_env="PERPLEXITY_API_KEY",
        default_model="sonar-pro",
        models=("sonar-pro", "sonar"),
        limitations=("research assistant; read-only access",),
        unsupported=(IFACE_SKILL, IFACE_MCP, IFACE_CLI, IFACE_FILE, IFACE_API),
    ),
    "grok": PlatformSpec(
        name="grok",
        display_name="Grok",
        interfaces=PlatformInterface(mcp=True, skill=True),
        capability_grant=frozenset(ALL_CAPABILITIES),
        auth_env="XAI_API_KEY",
        default_model="grok-4",
        models=("grok-4", "grok-4-fast"),
        limitations=("MCP connectors available in Grok Code",),
        unsupported=(IFACE_CLI, IFACE_FILE),
    ),
    "deepseek": PlatformSpec(
        name="deepseek",
        display_name="DeepSeek",
        interfaces=PlatformInterface(file=True),
        capability_grant=frozenset(READ_CAPABILITIES),
        auth_env="DEEPSEEK_API_KEY",
        default_model="deepseek-chat",
        models=("deepseek-chat", "deepseek-reasoner"),
        limitations=("no MCP/skill hosting; read-only by default",),
        unsupported=(IFACE_SKILL, IFACE_MCP, IFACE_CLI, IFACE_API),
    ),
    "qwen": PlatformSpec(
        name="qwen",
        display_name="Qwen",
        interfaces=PlatformInterface(file=True),
        capability_grant=frozenset(READ_CAPABILITIES),
        auth_env="DASHSCOPE_API_KEY",
        default_model="qwen-plus",
        models=("qwen-plus", "qwen-max"),
        limitations=("read-only by default; no MCP/skill hosting",),
        unsupported=(IFACE_SKILL, IFACE_MCP, IFACE_CLI, IFACE_API),
    ),
    "kimi": PlatformSpec(
        name="kimi",
        display_name="Kimi (Moonshot)",
        interfaces=PlatformInterface(file=True),
        capability_grant=frozenset(READ_CAPABILITIES),
        auth_env="MOONSHOT_API_KEY",
        default_model="moonshot-v1-8k",
        models=("moonshot-v1-8k", "moonshot-v1-32k"),
        limitations=("read-only by default",),
        unsupported=(IFACE_SKILL, IFACE_MCP, IFACE_CLI, IFACE_API),
    ),
    "glm": PlatformSpec(
        name="glm",
        display_name="GLM (Zhipu AI)",
        interfaces=PlatformInterface(file=True),
        capability_grant=frozenset(READ_CAPABILITIES),
        auth_env="ZHIPU_API_KEY",
        default_model="glm-4-plus",
        models=("glm-4-plus", "glm-4-air"),
        limitations=("read-only by default",),
        unsupported=(IFACE_SKILL, IFACE_MCP, IFACE_CLI, IFACE_API),
    ),
    "manus": PlatformSpec(
        name="manus",
        display_name="Manus",
        interfaces=PlatformInterface(skill=True, mcp=True, file=True),
        capability_grant=frozenset(ALL_CAPABILITIES),
        auth_env="MANUS_API_KEY",
        default_model="",
        models=(),
        limitations=("supports MCP servers and file access",),
        unsupported=(IFACE_CLI,),
    ),
    "opencode": PlatformSpec(
        name="opencode",
        display_name="OpenCode",
        interfaces=PlatformInterface(skill=True, mcp=True, cli=True, file=True),
        capability_grant=frozenset(ALL_CAPABILITIES),
        auth_env=None,
        default_model="",
        models=(),
        limitations=("local agent; authentication is handled by the client",),
        unsupported=(IFACE_API,),
    ),
    "cline": PlatformSpec(
        name="cline",
        display_name="Cline",
        interfaces=PlatformInterface(mcp=True, cli=True, file=True),
        capability_grant=frozenset(ALL_CAPABILITIES),
        auth_env=None,
        default_model="",
        models=(),
        limitations=("local VS Code agent; MCP server support",),
        unsupported=(IFACE_SKILL, IFACE_API),
    ),
}

#: The explicit generic fallback adapter for future / unknown platforms.
FALLBACK_PLATFORM = PlatformSpec(
    name="generic",
    display_name="Generic / Future AI Agent",
    interfaces=PlatformInterface(file=True),
    capability_grant=frozenset(READ_CAPABILITIES),
    auth_env=None,
    default_model="",
    models=(),
    limitations=(
        "future-compatible default: read + validate only; write requires "
        "explicit configuration",
    ),
    unsupported=(IFACE_SKILL, IFACE_MCP, IFACE_CLI, IFACE_API),
)


def list_platforms() -> tuple[str, ...]:
    """Return all registered platform names (deterministic)."""
    return tuple(sorted(PLATFORM_SPECS))


def is_platform(name: str) -> bool:
    return name in PLATFORM_SPECS


def get_platform_spec(name: str) -> PlatformSpec:
    """Return the spec for a registered platform (no fallback)."""
    spec = PLATFORM_SPECS.get(name)
    if spec is None:
        raise UnknownPlatformError(
            f"Unknown platform {name!r}. Available: {', '.join(list_platforms())}"
        )
    return spec


# ---------------------------------------------------------------------------
# Agent identity mapping
# ---------------------------------------------------------------------------

def map_platform_identity(
    name: str,
    display_name: str = "",
    version: str = "1",
) -> AgentIdentity:
    """Map an AI platform / agent name onto a protocol ``AgentIdentity``.

    Deterministic and provider-independent: no platform SDK is consulted.
    """
    if not name:
        raise PlatformError("Platform/agent name must not be empty.")
    return AgentIdentity(name=name, version=version, kind="ai")


# ---------------------------------------------------------------------------
# Instruction mapping
# ---------------------------------------------------------------------------

_PROTOCOL_INSTRUCTION = (
    "You operate under the Universal Handoff Protocol. Read and verify the "
    "machine-readable checkpoint in docs/HANDOFF.md before continuing, and "
    "record checkpoints using the handoff-protocol block."
)


def platform_instructions(spec: PlatformSpec) -> str:
    """Return a provider-independent operating instruction for a platform."""
    lines = [
        f"# Handoff integration instructions — {spec.display_name}",
        "",
        _PROTOCOL_INSTRUCTION,
        "",
        "## Interfaces",
        "",
    ]
    if spec.supports(IFACE_SKILL):
        lines += ["- Skill: install the universal-handoff skill package for full lifecycle guidance."]
    if spec.supports(IFACE_MCP):
        lines += ["- MCP: connect to the handoff MCP server (see docs/MCP.md)."]
    if spec.supports(IFACE_CLI):
        lines += ["- CLI: use `handoff --dry-run`, `handoff inspect`, and `handoff mcp`."]
    if spec.supports(IFACE_FILE):
        lines += ["- File: read/write docs/HANDOFF.md directly through the file adapter."]
    if spec.supports(IFACE_API):
        lines += ["- API: integrate through the handoff JSON service (see docs/ADAPTERS.md)."]
    if not any(
        spec.supports(i) for i in (IFACE_SKILL, IFACE_MCP, IFACE_CLI, IFACE_FILE, IFACE_API)
    ):
        lines += ["- None of the machine interfaces are available; this platform is read-only."]
    lines += [
        "",
        "## Security",
        "",
        "- Never persist API keys or secrets into the checkpoint.",
        "- Stay inside the project root; never follow paths that escape it.",
        "- Only create/update a checkpoint if you hold the corresponding capability.",
        "",
    ]
    return "\n".join(lines)


def integration_instructions(platform: str, interface: str) -> str:
    """Return interface-specific integration instructions for *platform*."""
    spec = get_platform_spec(platform)
    if not spec.supports(interface):
        raise AdapterUnsupportedError(
            f"Platform {platform!r} does not support interface {interface!r}."
        )
    if interface == IFACE_SKILL:
        body = (
            "Install the portable universal-handoff skill package "
            "(skills/universal-handoff). It teaches read-before-continue, "
            "verify-before-trust, and checkpoint lifecycle workflows."
        )
    elif interface == IFACE_MCP:
        body = (
            "Add the handoff MCP server as an MCP server. It exposes "
            "get_current_handoff, get_project_state, get_changelog, "
            "validate_checkpoint, create_checkpoint, and get_capabilities "
            "over JSON-RPC/stdio (see docs/MCP.md)."
        )
    elif interface == IFACE_CLI:
        body = (
            "Expose the handoff command line tool. Use `handoff inspect` for "
            "project+git state and `handoff mcp` for the MCP server."
        )
    elif interface == IFACE_FILE:
        body = (
            "Grant direct file access to docs/HANDOFF.md and docs/CHANGELOG.md "
            "inside the project root via the file adapter."
        )
    elif interface == IFACE_API:
        body = (
            "Integrate the handoff JSON service through the API adapter "
            "(docs/ADAPTERS.md)."
        )
    else:
        raise AdapterUnsupportedError(f"Unknown interface {interface!r}.")
    return (
        f"# {spec.display_name} — {interface} integration\n\n{body}\n\n"
        f"Protocol: universal-handoff-protocol\n"
        f"Read-only: {spec.read_only}\n"
    )


# ---------------------------------------------------------------------------
# Platform adapter object
# ---------------------------------------------------------------------------

class PlatformAdapter:
    """Runtime wrapper around a :class:`PlatformSpec`.

    This is the "AI platform adapter": it maps platform capabilities onto
    Handoff capabilities and can provision a concrete adapter for a supported
    interface.
    """

    schema_version = PLATFORM_SCHEMA_VERSION

    def __init__(self, spec: PlatformSpec) -> None:
        self.spec = spec

    @property
    def name(self) -> str:
        return self.spec.name

    # -- capability mapping ------------------------------------------------

    def capability_map(self) -> dict[str, dict[str, str]]:
        """Return the platform's Handoff capability map (deterministic)."""
        return {
            cap: {"granted": cap in self.spec.capability_grant}
            for cap in sorted(ALL_CAPABILITIES)
        }

    # -- integration plans --------------------------------------------------

    def instruction_map(self) -> dict[str, str]:
        """Return per-interface integration instructions (sorted keys)."""
        return {
            iface: integration_instructions(self.name, iface)
            for iface in sorted(i for i in _INTERFACES if self.spec.supports(i))
        }

    def authentication_boundary(self) -> dict[str, Any]:
        """Describe the authentication boundary (env var name only, no value)."""
        return {
            "required": self.spec.auth_env is not None,
            "env_var": self.spec.auth_env,
            "note": "API keys are read from the environment at request time only "
            "and are never stored or logged.",
        }

    def model_configuration(self) -> dict[str, Any]:
        return {
            "default_model": self.spec.default_model,
            "models": list(self.spec.models),
        }

    def limitations_report(self) -> dict[str, Any]:
        return {
            "limitations": list(self.spec.limitations),
            "unsupported": list(self.spec.unsupported),
            "interfaces": self.spec.interfaces.to_dict(),
        }

    # -- concrete operation adapter -----------------------------------------

    def concrete_adapter(
        self,
        interface: str,
        *,
        project_root: str | Path | None = None,
        **kwargs: Any,
    ) -> BaseAdapter:
        """Provision a concrete operation adapter for a supported interface."""
        if not self.spec.supports(interface):
            raise AdapterUnsupportedError(
                f"Platform {self.name!r} does not support interface {interface!r}."
            )
        mapping = {
            IFACE_FILE: "file",
            IFACE_API: "api",
            IFACE_CLI: "cli",
        }
        name = mapping.get(interface)
        if name is None:
            raise AdapterUnsupportedError(
                f"There is no concrete operation adapter for interface {interface!r}."
            )
        kwargs.pop("agent", None)
        if "project_root" not in kwargs:
            kwargs["project_root"] = project_root
        from handoff_agent.adapters import create_adapter
        from handoff_agent.capability import (
            AdapterIdentity,
            PermissionBoundary,
            build_contract,
        )

        agent_name = kwargs.pop("agent_name", None)
        if agent_name is None:
            agent_name = f"platform:{self.name}"
        agent = map_platform_identity(agent_name, self.spec.display_name, "1")

        contract = build_contract(
            agent,
            self.spec.capability_grant,
            boundary=PermissionBoundary(
                allowed=self.spec.capability_grant,
                name=f"platform:{self.name}",
            ),
            adapter=AdapterIdentity(f"platform:{self.name}", "1"),
        )
        kwargs["contract"] = contract
        ad = create_adapter(name, **kwargs)
        ad.start()
        return ad

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.spec.to_dict(),
            "schema_version": self.schema_version,
            "instructions": self.instruction_map(),
        }


# ---------------------------------------------------------------------------
# Factory (no automatic fallback)
# ---------------------------------------------------------------------------

def create_platform_adapter(
    name: str,
    *,
    fallback: bool = False,
) -> PlatformAdapter:
    """Create a :class:`PlatformAdapter` for a registered platform.

    ``fallback`` must be explicit to obtain the generic fallback adapter;
    unknown names never fall back automatically.
    """
    if name == "generic" or name in ("fallback", "unknown", "future"):
        if fallback:
            return PlatformAdapter(FALLBACK_PLATFORM)
        raise UnknownPlatformError(
            f"Generic fallback adapter requested without fallback=True for {name!r}."
        )
    spec = get_platform_spec(name)
    return PlatformAdapter(spec)


def discover_platform_adapters() -> dict[str, dict[str, Any]]:
    """Return all platform adapters as deterministic descriptors."""
    return {name: PlatformAdapter(spec).to_dict() for name, spec in sorted(PLATFORM_SPECS.items())}