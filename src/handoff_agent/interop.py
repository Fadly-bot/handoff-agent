"""Cross-AI interoperability helpers (Phase 17).

Provides state comparison, stale-checkpoint detection, conflict detection,
checkpoint snapshots, and state-consistency validation on top of the
universal adapter interface. Provider-independent: every helper operates on
a ``BaseAdapter`` (file / CLI / API) or plain protocol data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from handoff_agent.protocol import (
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    Checkpoint,
    diff_checkpoints,
    parse_handoff_document,
    validate_checkpoint,
    verify_identity,
)


# ---------------------------------------------------------------------------
# Checkpoint snapshots
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CheckpointSnapshot:
    """A stable, comparable view of a checkpoint (timestamps excluded)."""

    identity: str
    sequence: int
    objective: str
    protocol: str
    protocol_version: int
    state: dict[str, Any]

    @classmethod
    def from_checkpoint(cls, cp: Checkpoint) -> "CheckpointSnapshot":
        metadata = dict(cp.metadata or {})
        objective = metadata.get("objective") or cp.state.objective
        return cls(
            identity=cp.identity.id,
            sequence=cp.identity.sequence,
            objective=objective,
            protocol=PROTOCOL_NAME,
            protocol_version=PROTOCOL_VERSION,
            state=cp.state.to_dict(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "sequence": self.sequence,
            "objective": self.objective,
            "protocol": self.protocol,
            "protocol_version": self.protocol_version,
            "state": self.state,
        }


def snapshot_from_text(text: str | None) -> CheckpointSnapshot | None:
    """Parse raw markdown text into a snapshot (None if absent/invalid)."""
    if text is None:
        return None
    cp = parse_handoff_document(text)
    if cp is None:
        return None
    return CheckpointSnapshot.from_checkpoint(cp)


def snapshot_from_adapter(adapter) -> CheckpointSnapshot | None:
    """Read the current checkpoint through an adapter into a snapshot."""
    content = adapter.read_handoff()
    return snapshot_from_text(content)


# ---------------------------------------------------------------------------
# Staleness & conflict detection
# ---------------------------------------------------------------------------

def is_stale(expected_identity: str | None, current_identity: str) -> bool:
    """Return True if a writer's observed identity is older than current."""
    return expected_identity is not None and expected_identity != current_identity


@dataclass(frozen=True)
class ConflictReport:
    """A structured conflict description between two checkpoints."""

    base_identity: str | None
    ours: CheckpointSnapshot | None
    theirs: CheckpointSnapshot | None
    conflicting: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_identity": self.base_identity,
            "ours": self.ours.to_dict() if self.ours else None,
            "theirs": self.theirs.to_dict() if self.theirs else None,
            "conflicting": self.conflicting,
            "reason": self.reason,
        }


def detect_conflict(
    base_identity: str | None,
    ours_text: str | None,
    theirs_text: str | None,
) -> ConflictReport:
    """Compare two independent writers against a common base.

    - If both writers started from the same base identity and produced the
      same current identity → no conflict (one already applied).
    - If their current checkpoints have different identities but any of them
      matches the base → a fast-forward is still safe (no conflict).
    - If the base has been superseded by both sides differently → conflict.
    """
    ours = snapshot_from_text(ours_text)
    theirs = snapshot_from_text(theirs_text)
    if ours is None and theirs is None:
        return ConflictReport(base_identity, ours, theirs, conflicting=False)
    if ours is None or theirs is None:
        return ConflictReport(base_identity, ours, theirs, conflicting=False)
    if ours.identity == theirs.identity:
        return ConflictReport(base_identity, ours, theirs, conflicting=False)
    if ours.identity == base_identity or theirs.identity == base_identity:
        return ConflictReport(base_identity, ours, theirs, conflicting=False)
    return ConflictReport(
        base_identity,
        ours,
        theirs,
        conflicting=True,
        reason=(
            "Base has been superseded on both sides with different "
            "checkpoints; manual merge required."
        ),
    )


# ---------------------------------------------------------------------------
# State comparison
# ---------------------------------------------------------------------------

def compare_states(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Return a structured diff between two state dicts (shallow, sorted)."""
    keys = sorted(set(a) | set(b))
    changed: dict[str, dict[str, Any]] = {}
    only_in_a: dict[str, Any] = {}
    only_in_b: dict[str, Any] = {}
    for key in keys:
        va, vb = a.get(key), b.get(key)
        if key not in b:
            only_in_a[key] = va
        elif key not in a:
            only_in_b[key] = vb
        elif va != vb:
            changed[key] = {"before": va, "after": vb}
    return {
        "equal": not changed and not only_in_a and not only_in_b,
        "changed": changed,
        "only_in_first": only_in_a,
        "only_in_second": only_in_b,
    }


def state_consistency(adapter) -> dict[str, Any]:
    """Verify the adapter's checkpoint against validation + identity rules."""
    result = adapter.validate_checkpoint()
    if not result.get("valid"):
        return {
            "consistent": False,
            "valid": False,
            "errors": list(result.get("errors", [])),
        }
    return {
        "consistent": bool(result.get("identity_verified")),
        "valid": True,
        "identity_verified": bool(result.get("identity_verified")),
        "identity": result.get("identity"),
    }


# ---------------------------------------------------------------------------
# Cross-adapter compatibility
# ---------------------------------------------------------------------------

def cross_adapter_round_trip(from_adapter, to_adapter) -> dict[str, Any]:
    """Write a checkpoint through one adapter, then read it through another.

    Verifies the content survives intact and produces the same identity
    (Markdown + machine-readable schema compatibility across adapters).
    """
    content = from_adapter.read_handoff()
    if content is None:
        return {"round_trip": False, "reason": "no checkpoint on source"}
    result = to_adapter.write_checkpoint(content)
    reread = to_adapter.read_handoff()
    same = reread is not None and reread == content
    return {
        "round_trip": same,
        "identity": result.identity,
        "created": result.created,
        "modified": result.modified,
        "unchanged": result.unchanged,
        "history_recorded": result.history_recorded,
    }


def checkpoint_binary_compat(first: str, second: str) -> bool:
    """Return True if two adapters expose identical checkpoint bytes."""
    if first is None or second is None:
        return first is None and second is None
    return first == second


# ---------------------------------------------------------------------------
# High-level interoperability summary
# ---------------------------------------------------------------------------

def interop_summary(adapter) -> dict[str, Any]:
    """One-call summary: snapshot + staleness + consistency for an adapter."""
    snapshot = snapshot_from_adapter(adapter)
    consistency = state_consistency(adapter)
    return {
        "protocol": {"name": PROTOCOL_NAME, "version": PROTOCOL_VERSION},
        "snapshot": snapshot.to_dict() if snapshot else None,
        "consistency": consistency,
        "adapter": adapter.adapter_name,
    }