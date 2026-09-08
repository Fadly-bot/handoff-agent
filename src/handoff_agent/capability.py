"""Capability & Agent Contract (Phase 12).

Defines the model for agent identity, adapter identity, capability
discovery, capability declaration, capability negotiation, permission
boundaries, and security constraints.

This module is deliberately provider-agnostic. It does not reference any
specific AI provider, CLI, or persistence backend. It provides the contract
that any agent (CLI, MCP, Skill, or custom adapter) must satisfy before
performing read/write/validation operations on a Handoff checkpoint.

Key invariants:

  - Deterministic output (sorted capability lists, stable hashing).
  - Permission boundaries are enforced atomically.
  - Unsupported capabilities produce safe errors (never expose internals).
  - Security constraints are declarative and cannot be bypassed by an agent.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class CapabilityError(Exception):
    """Base error for capability-related failures."""


class UnsupportedCapabilityError(CapabilityError):
    """Raised when an agent requests a capability that does not exist."""

    def __init__(self, capability: str) -> None:
        super().__init__(f"Unsupported capability: {capability!r}")
        self.capability = capability


class CapabilityDeniedError(CapabilityError):
    """Raised when an agent is not permitted to perform a capability."""

    def __init__(self, agent: str, capability: str, reason: str = "") -> None:
        msg = f"Capability denied for agent {agent!r}: {capability!r}"
        if reason:
            msg += f" — {reason}"
        super().__init__(msg)
        self.agent = agent
        self.capability = capability
        self.reason = reason


# ---------------------------------------------------------------------------
# Well-known capabilities
# ---------------------------------------------------------------------------

# Each capability is a dotted string: "category.action".
# The dot-separated format allows grouping and deterministic comparison.

CAP_PROJECT_INSPECTION = "project.inspection"
CAP_GIT_INSPECTION = "git.inspection"
CAP_CHECKPOINT_READ = "checkpoint.read"
CAP_CHECKPOINT_CREATE = "checkpoint.create"
CAP_CHECKPOINT_UPDATE = "checkpoint.update"
CAP_CHANGELOG_READ = "changelog.read"
CAP_VALIDATION = "validation"

READ_CAPABILITIES: frozenset[str] = frozenset({
    CAP_PROJECT_INSPECTION,
    CAP_GIT_INSPECTION,
    CAP_CHECKPOINT_READ,
    CAP_CHANGELOG_READ,
})

WRITE_CAPABILITIES: frozenset[str] = frozenset({
    CAP_CHECKPOINT_CREATE,
    CAP_CHECKPOINT_UPDATE,
})

ALL_CAPABILITIES: frozenset[str] = READ_CAPABILITIES | WRITE_CAPABILITIES | {
    CAP_VALIDATION,
}


def discover_capabilities() -> tuple[str, ...]:
    """Return all well-known capabilities (sorted for determinism)."""
    return tuple(sorted(ALL_CAPABILITIES))


def capabilities_for_category(category: str) -> tuple[str, ...]:
    """Return capabilities matching a category prefix (e.g. ``"checkpoint"``).

    Deterministic: sorted output.
    """
    prefix = category + "."
    return tuple(sorted(c for c in ALL_CAPABILITIES if c.startswith(prefix)))


def is_known_capability(capability: str) -> bool:
    """Return True if *capability* is a recognized capability string."""
    return capability in ALL_CAPABILITIES


# ---------------------------------------------------------------------------
# Agent identity
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AgentIdentity:
    """Identifies an agent that interacts with the Handoff system.

    Agents are identified by name + version. The ``kind`` field classifies
    the agent type (``"ai"``, ``"tool"``, ``"human"``) and is informational.
    """

    name: str
    version: str = "0.0.1"
    kind: str = "ai"  # "ai" | "tool" | "human"

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "version": self.version, "kind": self.kind}

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> AgentIdentity:
        return cls(
            name=data.get("name", "unknown"),
            version=data.get("version", "0.0.1"),
            kind=data.get("kind", "ai"),
        )


# ---------------------------------------------------------------------------
# Adapter identity
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AdapterIdentity:
    """Identifies a provider adapter (e.g. Claude, OpenAI, Qwen, etc.)."""

    name: str
    version: str = "0.0.1"

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "version": self.version}

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> AdapterIdentity:
        return cls(
            name=data.get("name", "unknown"),
            version=data.get("version", "0.0.1"),
        )


# ---------------------------------------------------------------------------
# Capability declaration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CapabilityDeclaration:
    """A formal declaration of capabilities by an agent.

    The declaration includes the agent identity, a set of capabilities, and a
    timestamp. The declaration does not imply the capabilities are *granted*;
    use ``negotiate_capabilities`` to determine which are actually allowed.
    """

    agent: AgentIdentity
    capabilities: frozenset[str] = field(default_factory=frozenset)
    adapter: AdapterIdentity | None = None
    declared_at: str = ""

    def __post_init__(self) -> None:
        # Validate all declared capabilities are known
        unknown = self.capabilities - ALL_CAPABILITIES
        if unknown:
            raise CapabilityError(
                f"Declaration contains unknown capabilities: "
                f"{', '.join(sorted(unknown))}"
            )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "agent": self.agent.to_dict(),
            "capabilities": sorted(self.capabilities),
            "declared_at": self.declared_at or _now_iso(),
        }
        if self.adapter:
            d["adapter"] = self.adapter.to_dict()
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CapabilityDeclaration:
        agent = AgentIdentity.from_dict(data.get("agent", {}))
        caps = frozenset(data.get("capabilities", []))
        adapter_data = data.get("adapter")
        adapter = AdapterIdentity.from_dict(adapter_data) if adapter_data else None
        return cls(
            agent=agent,
            capabilities=caps,
            adapter=adapter,
            declared_at=data.get("declared_at", ""),
        )


# ---------------------------------------------------------------------------
# Capability negotiation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NegotiationResult:
    """Result of negotiating capabilities between agent and system."""

    granted: frozenset[str] = field(default_factory=frozenset)
    denied: frozenset[str] = field(default_factory=frozenset)
    unknown: frozenset[str] = field(default_factory=frozenset)

    @property
    def ok(self) -> bool:
        """True if all requested capabilities were granted."""
        return not self.denied and not self.unknown

    def to_dict(self) -> dict[str, Any]:
        return {
            "granted": sorted(self.granted),
            "denied": sorted(self.denied),
            "unknown": sorted(self.unknown),
        }


def negotiate_capabilities(
    declared: frozenset[str] | Sequence[str],
    allowed: frozenset[str] | Sequence[str] = frozenset(ALL_CAPABILITIES),
) -> NegotiationResult:
    """Negotiate capabilities between a declaration and allowed set.

    Each declared capability is checked:
    - If the capability is unknown → ``unknown``.
    - If the capability is not in ``allowed`` → ``denied``.
    - Otherwise → ``granted``.

    The result is deterministic (sorted frozensets).
    """
    declared_set = frozenset(declared)
    allowed_set = frozenset(allowed)

    unknown = declared_set - ALL_CAPABILITIES
    known = declared_set - unknown
    granted = known & allowed_set
    denied = known - allowed_set

    return NegotiationResult(
        granted=granted,
        denied=denied,
        unknown=unknown,
    )


# ---------------------------------------------------------------------------
# Permission boundaries
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PermissionBoundary:
    """Declares the permission envelope for an agent.

    A boundary specifies which capabilities are *allowed*. If ``deny_all`` is
    True, all capabilities are denied (read-only mode). Capability checks
    compare against the boundary; capabilities not in ``allowed`` are denied.
    """

    allowed: frozenset[str] = field(default_factory=lambda: frozenset(ALL_CAPABILITIES))
    deny_all: bool = False
    name: str = "default"

    def is_allowed(self, capability: str) -> bool:
        """Return True if *capability* is allowed by this boundary."""
        if self.deny_all:
            return False
        return capability in self.allowed

    def check(self, capability: str) -> None:
        """Raise ``CapabilityDeniedError`` if *capability* is not allowed.

        ``*capability*`` must be a known capability string.
        """
        if not self.is_allowed(capability):
            raise CapabilityDeniedError(
                agent=self.name,
                capability=capability,
                reason="not in permission boundary",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "allowed": sorted(self.allowed),
            "deny_all": self.deny_all,
        }


# ---------------------------------------------------------------------------
# Security constraints (immutable policy)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SecurityConstraints:
    """Immutable security constraints that apply to all agents.

    These cannot be negotiated or overridden by an agent. They enforce the
    fundamental security guarantees of the Handoff system.
    """

    max_writes_per_session: int = 0  # 0 = unlimited
    require_confirmation: bool = False
    audit_log: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_writes_per_session": self.max_writes_per_session,
            "require_confirmation": self.require_confirmation,
            "audit_log": self.audit_log,
        }


# ---------------------------------------------------------------------------
# Agent contract (combined)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AgentContract:
    """A fully negotiated contract between the Handoff system and an agent.

    Combines the agent identity, granted capabilities, permission boundary,
    and security constraints into a single validated object.
    """

    agent: AgentIdentity
    granted: frozenset[str]
    boundary: PermissionBoundary
    security: SecurityConstraints = field(default_factory=SecurityConstraints)
    adapter: AdapterIdentity | None = None

    def can(self, capability: str) -> bool:
        """Return True if the agent is permitted to perform *capability*."""
        return self.boundary.is_allowed(capability) and capability in self.granted

    def assert_can(self, capability: str) -> None:
        """Raise ``CapabilityDeniedError`` if the agent cannot perform *capability*."""
        if not self.can(capability):
            if not is_known_capability(capability):
                raise UnsupportedCapabilityError(capability)
            raise CapabilityDeniedError(
                agent=self.agent.name,
                capability=capability,
                reason="not granted by contract",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent.to_dict(),
            "adapter": self.adapter.to_dict() if self.adapter else None,
            "granted": sorted(self.granted),
            "boundary": self.boundary.to_dict(),
            "security": self.security.to_dict(),
        }


def build_contract(
    agent: AgentIdentity,
    requested: frozenset[str] | Sequence[str],
    *,
    boundary: PermissionBoundary | None = None,
    security: SecurityConstraints | None = None,
    adapter: AdapterIdentity | None = None,
) -> AgentContract:
    """Build an ``AgentContract`` from a declaration.

    Negotiates *requested* against the boundary, raises on any unknown
    capability, and returns a validated contract.
    """
    bnd = boundary or PermissionBoundary(name=agent.name)
    sec = security or SecurityConstraints()
    negotiation = negotiate_capabilities(requested, bnd.allowed)
    if negotiation.unknown:
        raise CapabilityError(
            f"Requested unknown capabilities: "
            f"{', '.join(sorted(negotiation.unknown))}"
        )
    return AgentContract(
        agent=agent,
        granted=negotiation.granted,
        boundary=bnd,
        security=sec,
        adapter=adapter,
    )


# ---------------------------------------------------------------------------
# Deterministic capability output
# ---------------------------------------------------------------------------

def capability_catalog() -> dict[str, dict[str, str]]:
    """Return the full capability catalog as a sorted dictionary.

    Each entry maps a capability string to a description dict. The output is
    deterministic and safe to serialize.
    """
    descriptions = {
        CAP_PROJECT_INSPECTION: "Read project metadata and type detection.",
        CAP_GIT_INSPECTION: "Read Git repository state (branch, head, status).",
        CAP_CHECKPOINT_READ: "Read the current Handoff checkpoint.",
        CAP_CHECKPOINT_CREATE: "Create a new Handoff checkpoint.",
        CAP_CHECKPOINT_UPDATE: "Update an existing Handoff checkpoint.",
        CAP_CHANGELOG_READ: "Read the Handoff changelog history.",
        CAP_VALIDATION: "Validate a Handoff checkpoint against the protocol.",
    }
    return {cap: {"description": descriptions.get(cap, "")} for cap in sorted(ALL_CAPABILITIES)}


def identity_hash(agent: AgentIdentity) -> str:
    """Return a deterministic SHA-256 identity hash for an agent.

    The hash is derived from name + version + kind, sorted and canonical.
    """
    canonical = json.dumps(
        {"name": agent.name, "version": agent.version, "kind": agent.kind},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
