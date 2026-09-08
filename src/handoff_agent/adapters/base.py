"""Generic Adapter & Interoperability Layer (Phase 15).

Defines the universal adapter contract: a provider-independent, capability-aware
interface that any adapter (file, CLI, or HTTP/API) must satisfy to interact
with the Handoff system.

Core invariants:

  - One contract per adapter (``AgentContract``). Every operation is checked
    against the contract before it runs; a denied operation raises a safe
    ``AdapterPermissionError``.
  - Adapter isolation: an adapter configuration / contract never leaks to
    another adapter. Capabilities are negotiated per adapter.
  - Provider independence: this module references no AI provider directly.
  - Safe errors: adapters surface ``AdapterError`` subclasses rather than raw
    internals, and never echo secrets.
  - No secret propagation: values are read from the environment at request
    time only and never enter ``status()`` / config output.
  - No arbitrary filesystem access and no unrestricted Git operations: the
    concrete adapters only use the existing read-only inspection and the
    narrowly-scoped persistence layer.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from handoff_agent.capability import (
    ALL_CAPABILITIES,
    CAP_CHANGELOG_READ,
    CAP_CHECKPOINT_CREATE,
    CAP_CHECKPOINT_READ,
    CAP_CHECKPOINT_UPDATE,
    CAP_GIT_INSPECTION,
    CAP_PROJECT_INSPECTION,
    CAP_VALIDATION,
    AgentContract,
    AgentIdentity,
    AdapterIdentity,
    CapabilityDeniedError,
    NegotiationResult,
    PermissionBoundary,
    UnsupportedCapabilityError,
    build_contract,
    negotiate_capabilities,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class AdapterError(Exception):
    """Base error for all adapter failures."""


class AdapterConfigError(AdapterError):
    """Raised when adapter configuration is invalid or unsafe."""


class AdapterPermissionError(AdapterError):
    """Raised when an operation is denied by the adapter's contract."""


class AdapterUnsupportedError(AdapterError):
    """Raised when an adapter does not support an operation."""


class AdapterStateError(AdapterError):
    """Raised when an adapter is used before ``start()`` has been called."""


class AdapterContentError(AdapterError):
    """Raised when content looks like it embeds secret values."""


# ---------------------------------------------------------------------------
# Secret-like content scanning (mirrors persistence / MCP adapter patterns).
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"""(?i)(?:api[_\-]?key|apikey)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-]{16,}"""),
    re.compile(r"""(?i)(?:secret[_\-]?key|secretkey)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-]{16,}"""),
    re.compile(r"""(?i)(?:access[_\-]?token|accesstoken)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-\.]{16,}"""),
    re.compile(r"""(?i)(?:private[_\-]?key)\s*['"]?\s*[:=]"""),
    re.compile(r"""-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----"""),
]


def contains_secret_like(content: str) -> bool:
    """Return True if *content* looks like it embeds secret values."""
    for pattern in _SECRET_PATTERNS:
        if pattern.search(content):
            return True
    return False


def resolve_within_root(root: Path, rel: str) -> Path:
    """Resolve ``rel`` inside ``root``, rejecting escapes and symlinks.

    These rules match the persistence layer: relative paths only, no ``..``
    traversal, and any symlink that resolves outside ``root`` is rejected.
    """
    root_resolved = root.resolve()
    rel_path = Path(rel)
    if rel_path.is_absolute():
        raise AdapterConfigError(
            f"Path must be project-relative, got absolute: {rel}"
        )
    for part in rel_path.parts:
        if part == "..":
            raise AdapterConfigError(
                "Path must not contain '..' traversal."
            )
    candidate = root_resolved / rel_path
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise AdapterConfigError(
            "Resolved path escapes the project root (via symlink or traversal)."
        ) from exc
    return resolved


# ---------------------------------------------------------------------------
# Agent identity mapping
# ---------------------------------------------------------------------------

def map_agent_identity(
    name: str,
    *,
    version: str = "0.0.1",
    kind: str = "tool",
) -> AgentIdentity:
    """Map an adapter / platform name to a stable ``AgentIdentity``."""
    return AgentIdentity(name=name, version=version, kind=kind)


# ---------------------------------------------------------------------------
# Operation result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AdapterWriteResult:
    """Result of persisting a checkpoint through an adapter."""

    rel_path: str
    created: bool = False
    modified: bool = False
    unchanged: bool = False
    history_recorded: bool = False
    identity: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rel_path": self.rel_path,
            "created": self.created,
            "modified": self.modified,
            "unchanged": self.unchanged,
            "history_recorded": self.history_recorded,
            "identity": self.identity,
        }


# ---------------------------------------------------------------------------
# Capability mapping helpers
# ---------------------------------------------------------------------------

def _capability_for_op(op: str) -> str:
    return {
        "read_handoff": CAP_CHECKPOINT_READ,
        "write_checkpoint": CAP_CHECKPOINT_CREATE,
        "project_state": CAP_PROJECT_INSPECTION,
        "validate_checkpoint": CAP_VALIDATION,
        "read_changelog": CAP_CHANGELOG_READ,
    }[op]


def _boundary_for(
    default_capabilities: frozenset[str],
    *,
    read_only: bool,
    name: str,
) -> PermissionBoundary:
    if read_only:
        write_caps = frozenset(
            {CAP_CHECKPOINT_CREATE, CAP_CHECKPOINT_UPDATE}
        ) & default_capabilities
        return PermissionBoundary(
            allowed=default_capabilities - write_caps,
            name=f"{name}-read-only",
        )
    return PermissionBoundary(allowed=default_capabilities, name=name)


# ---------------------------------------------------------------------------
# Universal adapter interface
# ---------------------------------------------------------------------------

class BaseAdapter(ABC):
    """Universal adapter interface (protocol adapter contract).

    Every adapter implements five operations:

      - ``read_handoff()``             → current ``docs/HANDOFF.md`` content
      - ``write_checkpoint(content)``  → persist a new current checkpoint
      - ``project_state()``            → project + Git snapshot (read-only)
      - ``validate_checkpoint()``      → validate the current checkpoint
      - ``read_changelog()``           → checkpoint history

    Capability awareness: each operation is gated on the negotiated
    ``AgentContract``. Permission enforcement happens in the public method,
    so a subclass can never bypass it accidentally.
    """

    adapter_name: str = "generic"
    adapter_version: str = "0.4.0"
    default_capabilities: frozenset[str] = frozenset(ALL_CAPABILITIES)

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        agent: AgentIdentity | None = None,
        read_only: bool = False,
        contract: AgentContract | None = None,
    ) -> None:
        self.config = dict(config or {})
        self._agent = agent or map_agent_identity(self.adapter_name)
        self._started = False
        if contract is not None:
            self._contract = contract
        else:
            boundary = _boundary_for(
                self.default_capabilities,
                read_only=read_only,
                name=self.adapter_name,
            )
            self._contract = build_contract(
                self._agent,
                boundary.allowed,
                boundary=boundary,
                adapter=AdapterIdentity(self.adapter_name, self.adapter_version),
            )

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Activate the adapter (idempotent)."""
        self._started = True

    def stop(self) -> None:
        """Deactivate the adapter (idempotent)."""
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    def status(self) -> dict[str, Any]:
        """Return operational + contract status (never exposes secrets)."""
        return {
            "adapter": self.adapter_name,
            "version": self.adapter_version,
            "agent": self._agent.to_dict(),
            "started": self._started,
            "read_only": not self._contract.boundary.is_allowed(CAP_CHECKPOINT_CREATE)
            and not self._contract.boundary.is_allowed(CAP_CHECKPOINT_UPDATE),
            "granted": sorted(self._contract.granted),
            "capabilities": sorted(self.default_capabilities),
        }

    # -- capability negotiation ------------------------------------------

    @property
    def contract(self) -> AgentContract:
        return self._contract

    def negotiate(self, declared: frozenset[str] | Sequence[str]) -> NegotiationResult:
        """Negotiate *declared* capabilities against this adapter's boundary."""
        return negotiate_capabilities(declared, self._contract.boundary.allowed)

    def granted_capabilities(self) -> tuple[str, ...]:
        return tuple(sorted(self._contract.granted))

    def can(self, capability: str) -> bool:
        return self._contract.can(capability)

    def assert_can(self, capability: str) -> None:
        """Raise a safe ``AdapterPermissionError`` if *capability* is denied."""
        try:
            self._contract.assert_can(capability)
        except UnsupportedCapabilityError as exc:
            raise AdapterError(str(exc)) from exc
        except CapabilityDeniedError as exc:
            raise AdapterPermissionError(str(exc)) from exc

    def _require_started(self) -> None:
        if not self._started:
            raise AdapterStateError(
                f"Adapter {self.adapter_name!r} is not started. Call start() first."
            )

    # -- operations (capability-checked) ----------------------------------

    def read_handoff(self) -> str | None:
        """Read the current checkpoint. Returns the markdown or None."""
        self._require_started()
        self.assert_can(CAP_CHECKPOINT_READ)
        return self._do_read_handoff()

    def write_checkpoint(
        self,
        content: str,
        *,
        expected_base: str | None = None,
    ) -> AdapterWriteResult:
        """Persist ``content`` as the current checkpoint.

        ``expected_base`` enables optimistic-concurrency updates: if it is not
        None and the on-disk checkpoint identity differs, the write is refused.
        """
        self._require_started()
        try:
            current = self._do_current_identity()
        except AdapterError:
            current = None
        if current:
            self.assert_can(CAP_CHECKPOINT_UPDATE)
        else:
            self.assert_can(CAP_CHECKPOINT_CREATE)
        if expected_base is not None and current != expected_base:
            raise AdapterPermissionError(
                "Checkpoint changed since it was last observed "
                f"(expected identity {expected_base!r}, found {current!r}). "
                "Re-read the current checkpoint before writing."
            )
        return self._do_write_checkpoint(content)

    def project_state(self) -> dict[str, Any]:
        """Return the project + Git snapshot (read-only)."""
        self._require_started()
        self.assert_can(CAP_PROJECT_INSPECTION)
        return self._do_project_state()

    def validate_checkpoint(self) -> dict[str, Any]:
        """Validate the current checkpoint against the protocol schema."""
        self._require_started()
        self.assert_can(CAP_VALIDATION)
        return self._do_validate_checkpoint()

    def read_changelog(self) -> str | None:
        """Read the checkpoint history. Returns the markdown or None."""
        self._require_started()
        self.assert_can(CAP_CHANGELOG_READ)
        return self._do_read_changelog()

    # -- subclass hooks ----------------------------------------------------

    def _do_current_identity(self) -> str | None:
        """Return the canonical identity of the current checkpoint, if any."""
        content = self._do_read_handoff()
        if content is None:
            return None
        from handoff_agent.protocol import parse_handoff_document

        cp = parse_handoff_document(content)
        return cp.identity.id if cp is not None else None

    @abstractmethod
    def _do_read_handoff(self) -> str | None: ...

    @abstractmethod
    def _do_write_checkpoint(self, content: str) -> AdapterWriteResult: ...

    @abstractmethod
    def _do_project_state(self) -> dict[str, Any]: ...

    @abstractmethod
    def _do_validate_checkpoint(self) -> dict[str, Any]: ...

    @abstractmethod
    def _do_read_changelog(self) -> str | None: ...

    def to_dict(self) -> dict[str, Any]:
        """Deterministic adapter descriptor (safe to serialize)."""
        return self.status()