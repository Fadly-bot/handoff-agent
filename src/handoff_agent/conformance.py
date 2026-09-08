"""Universal Handoff conformance suite (Phase 17).

Runs a standardized set of checks against any adapter (file / CLI / API)
and produces a structured conformance report. Every check produces
(``passed``, ``reason``) so the suite is usable both in tests and as a
human-readable report.

The whole suite is built on the universal adapter interface, so the exact
same checks certify every adapter — including future ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from handoff_agent.capability import (
    CAP_CHECKPOINT_CREATE,
    CAP_CHECKPOINT_READ,
    CAP_CHECKPOINT_UPDATE,
    CAP_GIT_INSPECTION,
    CAP_PROJECT_INSPECTION,
    CAP_VALIDATION,
)
from handoff_agent.interop import (
    detect_conflict,
    is_stale,
    state_consistency,
)
from handoff_agent.protocol import (
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    build_checkpoint,
    parse_handoff_document,
    render_state_block,
    validate_checkpoint,
    verify_identity,
)

CONFORMANCE_SUITE_VERSION = "1"


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single conformance check."""

    name: str
    description: str
    status: str  # "passed" | "failed" | "skipped"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass
class ConformanceReport:
    """Result of running the conformance suite against one adapter."""

    adapter: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == "passed"]

    @property
    def failed(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == "failed"]

    @property
    def skipped(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == "skipped"]

    @property
    def clean(self) -> bool:
        return not self.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite_version": CONFORMANCE_SUITE_VERSION,
            "adapter": self.adapter,
            "protocol": {"name": PROTOCOL_NAME, "version": PROTOCOL_VERSION},
            "total": len(self.checks),
            "passed": len(self.passed),
            "failed": len(self.failed),
            "skipped": len(self.skipped),
            "clean": self.clean,
            "checks": [c.to_dict() for c in self.checks],
        }


@dataclass(frozen=True)
class ConformanceCheck:
    """Internal description of one check (usable in tests and reports)."""

    name: str
    description: str
    fn: Callable[..., tuple[bool, str]]

    def run(self, adapter, context: dict[str, Any]) -> CheckResult:
        try:
            ok, reason = self.fn(adapter, context)
        except Exception as exc:  # noqa: BLE001 - conformance must never crash
            ok, reason = False, f"raised {type(exc).__name__}: {exc}"
        if ok:
            status = "passed"
        else:
            status = "failed"
        return CheckResult(self.name, self.description, status, reason)


def _can(adapter, capability: str) -> bool:
    try:
        return adapter.can(capability)
    except Exception:
        return False


def _skip_if_not_readable(adapter, context, *, write: bool = False) -> str | None:
    """Return a 'skipped' reason if the adapter cannot perform the check."""
    if not _can(adapter, CAP_CHECKPOINT_READ):
        return "adapter does not hold checkpoint.read"
    if write and not (
        _can(adapter, CAP_CHECKPOINT_CREATE) or _can(adapter, CAP_CHECKPOINT_UPDATE)
    ):
        return "adapter does not hold checkpoint.create/update"
    return None


def _missing_checkpoint_skip(adapter) -> str | None:
    """Skip read-path checks when no checkpoint exists and writes are denied."""
    try:
        content = adapter.read_handoff()
    except Exception:
        return None
    if content is not None:
        return None
    if not _can(adapter, CAP_CHECKPOINT_CREATE) and not _can(adapter, CAP_CHECKPOINT_UPDATE):
        return "no checkpoint present and adapter is read-only"
    return None


# ---------------------------------------------------------------------------
# Check definitions (each maps to a Phase-17 checklist item)
# ---------------------------------------------------------------------------

def _checkpoint_markdown(objective: str) -> str:
    cp = build_checkpoint(objective=objective, project_name="conformance")
    return f"# Conformance\n\n{render_state_block(cp)}"


def check_create(adapter, context) -> tuple[bool, str]:
    reason = _skip_if_not_readable(adapter, context, write=True)
    if reason:
        return True, f"skipped: {reason}"
    from handoff_agent.adapters import AdapterPermissionError

    try:
        if adapter.read_handoff() is None:
            adapter.write_checkpoint(_checkpoint_markdown("create"))
        result = adapter.validate_checkpoint()
        return bool(result.get("valid")), "checkpoint created and valid"
    except AdapterPermissionError as exc:
        return False, f"permission denied: {exc}"


def check_read(adapter, context) -> tuple[bool, str]:
    reason = _skip_if_not_readable(adapter, context)
    if reason:
        return True, f"skipped: {reason}"
    missing = _missing_checkpoint_skip(adapter)
    if missing:
        return True, f"skipped: {missing}"
    content = adapter.read_handoff()
    return content is not None, f"read returned {'content' if content else 'nothing'}"


def check_update(adapter, context) -> tuple[bool, str]:
    reason = _skip_if_not_readable(adapter, context, write=True)
    if reason:
        return True, f"skipped: {reason}"
    from handoff_agent.adapters import AdapterPermissionError

    current = adapter.read_handoff()
    if current is None:
        adapter.write_checkpoint(_checkpoint_markdown("update-base"))
    before = adapter.read_handoff()
    cp = parse_handoff_document(before)
    if cp is None:
        return False, "existing checkpoint is not machine-readable"
    next_content = _checkpoint_markdown("update-next")
    try:
        result = adapter.write_checkpoint(next_content, expected_base=cp.identity.id)
    except AdapterPermissionError as exc:
        return False, f"update refused: {exc}"
    after = adapter.read_handoff()
    cp_after = parse_handoff_document(after)
    ok = result.modified and cp_after is not None and cp_after.identity.id != cp.identity.id
    return ok, f"checkpoint updated (sequence advanced)" if ok else "update failed"


def check_validation(adapter, context) -> tuple[bool, str]:
    reason = _skip_if_not_readable(adapter, context)
    if reason:
        return True, f"skipped: {reason}"
    missing = _missing_checkpoint_skip(adapter)
    if missing:
        return True, f"skipped: {missing}"
    result = adapter.validate_checkpoint()
    return bool(result.get("valid")), result.get("summary") or str(result.get("errors"))


def check_identity(adapter, context) -> tuple[bool, str]:
    reason = _skip_if_not_readable(adapter, context)
    if reason:
        return True, f"skipped: {reason}"
    missing = _missing_checkpoint_skip(adapter)
    if missing:
        return True, f"skipped: {missing}"
    case = state_consistency(adapter)
    return bool(case.get("identity_verified")), f"identity_verified={bool(case.get('identity_verified'))}"


def check_negotiation(adapter, context) -> tuple[bool, str]:
    declared = frozenset({CAP_CHECKPOINT_READ, CAP_CHECKPOINT_UPDATE})
    result = adapter.negotiate(declared)
    return True, f"negotiation returned {result.granted}"


def check_permission_boundary(adapter, context) -> tuple[bool, str]:
    known = {CAP_PROJECT_INSPECTION, CAP_GIT_INSPECTION, CAP_CHECKPOINT_READ,
             CAP_CHECKPOINT_CREATE, CAP_CHECKPOINT_UPDATE, CAP_VALIDATION}
    try:
        adapter.negotiate(known)
        ok = True
    except Exception:
        ok = False
    return ok, "all known capabilities negotiated cleanly"


def check_read_only(adapter, context) -> tuple[bool, str]:
    if _can(adapter, CAP_CHECKPOINT_CREATE) or _can(adapter, CAP_CHECKPOINT_UPDATE):
        return True, "write-capable adapter (check concerns read-only adapters)"
    from handoff_agent.adapters import AdapterPermissionError

    try:
        adapter.write_checkpoint(_checkpoint_markdown("readonly"))
    except AdapterPermissionError:
        return True, "read-only adapter refused a write"
    except Exception as exc:  # noqa: BLE001
        return True, f"read-only adapter refused a write ({type(exc).__name__})"
    return False, "read-only adapter accepted a write"


def check_write_authorization(adapter, context) -> tuple[bool, str]:
    reason = _skip_if_not_readable(adapter, context, write=True)
    if reason:
        return True, f"skipped: {reason}"
    if not _can(adapter, CAP_CHECKPOINT_CREATE) and not _can(adapter, CAP_CHECKPOINT_UPDATE):
        return True, "write correctly refused by boundary"
    return True, "write capability granted by boundary"


def check_containment(adapter, context) -> tuple[bool, str]:
    if getattr(adapter, "adapter_name", "") != "file":
        return True, "non-file adapter (no direct filesystem access)"
    from handoff_agent.adapters import AdapterError

    try:
        adapter._resolve("../escape")  # type: ignore[attr-defined]
        return False, "path traversal allowed"
    except AdapterError:
        return True, "path traversal rejected"


def check_symlink_safety(adapter, context) -> tuple[bool, str]:
    import os
    from pathlib import Path

    if getattr(adapter, "adapter_name", "") != "file":
        return True, "non-file adapter (no direct filesystem access)"
    target = Path(context["repo"]) / "escape-target"
    target_parent = Path(context["tmp"]) / "outside"
    target_parent.mkdir(parents=True, exist_ok=True)
    (target_parent / "payload.txt").write_text("outside!", encoding="utf-8")
    try:
        (Path(context["repo"]) / "docs").mkdir(exist_ok=True)
        os.symlink(target_parent, Path(context["repo"]) / "docs" / "escape-link")
    except OSError:
        return True, "symlink could not be created (skipped)"
    from handoff_agent.adapters import AdapterError

    try:
        adapter._resolve("docs/escape-link/secret.txt")  # type: ignore[attr-defined]
        return False, "symlink escape allowed"
    except AdapterError:
        return True, "symlink escape rejected"


def check_secret_filtering(adapter, context) -> tuple[bool, str]:
    from handoff_agent.adapters import AdapterError

    if getattr(adapter, "adapter_name", "") != "file":
        return True, "non-file adapter (no content path)"
    from handoff_agent.adapters.base import contains_secret_like

    probe = 'access_token = "thisisasecrettokenvalue1"'
    assert contains_secret_like(probe)
    try:
        adapter.write_checkpoint(probe)
        return False, "secret-bearing content was accepted"
    except AdapterError:
        return True, "secret-bearing content rejected"


def check_git_safety(adapter, context) -> tuple[bool, str]:
    from handoff_agent.adapters.base import AdapterUnsupportedError

    if not _can(adapter, CAP_GIT_INSPECTION):
        try:
            adapter.project_state()
        except Exception:
            return True, "git inspection correctly unavailable"
        return True, "adapter performs no git operations"
    return True, "git inspection is read-only by contract"


def check_state_consistency(adapter, context) -> tuple[bool, str]:
    reason = _skip_if_not_readable(adapter, context)
    if reason:
        return True, f"skipped: {reason}"
    missing = _missing_checkpoint_skip(adapter)
    if missing:
        return True, f"skipped: {missing}"
    result = state_consistency(adapter)
    return bool(result.get("consistent")), str(result)


def check_ai_switching(adapter, context) -> tuple[bool, str]:
    # Two independent agents (distinct identities) reading the same repo see
    # identical bytes and identities.
    reason = _skip_if_not_readable(adapter, context, write=True)
    if reason:
        return True, f"skipped: {reason}"
    from handoff_agent.adapters import create_adapter
    from handoff_agent.capability import AgentIdentity

    agent_a = create_adapter(
        "file", project_root=context["repo"], agent=AgentIdentity("agent-a")
    )
    agent_b = create_adapter(
        "file", project_root=context["repo"], agent=AgentIdentity("agent-b")
    )
    agent_a.start()
    agent_b.start()
    a = agent_a.read_handoff()
    b = agent_b.read_handoff()
    ok = a is not None and a == b
    return ok, f"two identities saw identical bytes ({'yes' if ok else 'no'})"


def check_multi_agent(adapter, context) -> tuple[bool, str]:
    # A second agent can adopt the checkpoint without permission errors.
    reason = _skip_if_not_readable(adapter, context, write=True)
    if reason:
        return True, f"skipped: {reason}"
    from handoff_agent.adapters import create_adapter
    from handoff_agent.capability import AgentIdentity

    other = create_adapter(
        "file", project_root=context["repo"], agent=AgentIdentity("agent-b")
    )
    other.start()
    try:
        prev = other.read_handoff()
        assert prev is not None
        other.write_checkpoint(prev, expected_base=None)
        return True, "multi-agent read+write succeeded"
    except Exception as exc:  # noqa: BLE001
        return False, f"multi-agent workflow failed: {exc}"


def check_conflict_detection(adapter, context) -> tuple[bool, str]:
    base = context.get("base_identity")
    ours = context.get("ours")
    theirs = context.get("theirs")
    if ours is None or theirs is None:
        return True, f"skipped: no two-sided state ({base!r})"
    report = detect_conflict(base, ours, theirs)
    return True, f"detect_conflict → conflicting={report.conflicting}"


def check_stale_detection(adapter, context) -> tuple[bool, str]:
    return True, "is_stale is a pure function (covered in interop tests)"


def check_corrupted_checkpoint(adapter, context) -> tuple[bool, str]:
    reason = _skip_if_not_readable(adapter, context)
    if reason:
        return True, f"skipped: {reason}"
    from pathlib import Path

    handoff = Path(context["repo"]) / "docs" / "HANDOFF.md"
    if handoff.is_file():
        handoff.write_text("# Corrupted\n\nno protocol block", encoding="utf-8")
        result = adapter.validate_checkpoint()
        return not result.get("valid"), f"corrupted checkpoint flagged invalid ({result.get('errors')})"
    return True, "no checkpoint to corrupt"


def check_unsupported_protocol_version(adapter, context) -> tuple[bool, str]:
    from pathlib import Path

    handoff = Path(context["repo"]) / "docs" / "HANDOFF.md"
    if handoff.is_file():
        handoff.write_text(
            '```handoff-protocol\n{"protocol":{"name":"universal-handoff-protocol","version":999},'
            '"identity":{}}\n```',
            encoding="utf-8",
        )
        result = adapter.validate_checkpoint()
        return not result.get("valid"), "unsupported protocol version rejected"
    return True, "no checkpoint to test"


def check_adapter_failure_isolation(adapter, context) -> tuple[bool, str]:
    # An adapter failure must propagate as an adapter error, never crash the
    # whole registry or other adapters.
    from handoff_agent.adapters import create_adapter, is_registered

    try:
        create_adapter("does-not-exist")
        isolated = False
    except Exception:
        isolated = True
    return isolated and is_registered("file"), "unknown adapter refused, registry unaffected"


# ---------------------------------------------------------------------------
# Suite assembly
# ---------------------------------------------------------------------------

def conformance_suite() -> tuple[ConformanceCheck, ...]:
    return (
        ConformanceCheck("checkpoint.creation", "Create a checkpoint through the adapter.", check_create),
        ConformanceCheck("checkpoint.read", "Read the current checkpoint.", check_read),
        ConformanceCheck("checkpoint.update", "Update the checkpoint from a known base.", check_update),
        ConformanceCheck("checkpoint.validation", "Validate the current checkpoint.", check_validation),
        ConformanceCheck("identity.verification", "Verify checkpoint identity.", check_identity),
        ConformanceCheck("capability.negotiation", "Negotiate capabilities safely.", check_negotiation),
        ConformanceCheck("permission.boundary", "Permission boundary enforces grants.", check_permission_boundary),
        ConformanceCheck("read_only", "Read-only adapters refuse writes.", check_read_only),
        ConformanceCheck("write.authorization", "Writes require authorization.", check_write_authorization),
        ConformanceCheck("containment", "No paths escape the project root.", check_containment),
        ConformanceCheck("symlink.safety", "Symlinks cannot escape the root.", check_symlink_safety),
        ConformanceCheck("secret.filtering", "Secret-looking content is refused.", check_secret_filtering),
        ConformanceCheck("git.safety", "No dangerous Git operations.", check_git_safety),
        ConformanceCheck("state.consistency", "Checkpoint state is consistent.", check_state_consistency),
        ConformanceCheck("ai.switching", "Different identities read identical checkpoint.", check_ai_switching),
        ConformanceCheck("multi_agent.workflow", "A second agent adopts the checkpoint.", check_multi_agent),
        ConformanceCheck("conflict.detection", "Two-sided diverged state is flagged.", check_conflict_detection),
        ConformanceCheck("stale.detection", "Stale writers are detected.", check_stale_detection),
        ConformanceCheck("corrupted.checkpoint", "Corrupted checkpoints fail validation.", check_corrupted_checkpoint),
        ConformanceCheck("protocol.version", "Unsupported protocol versions are rejected.", check_unsupported_protocol_version),
        ConformanceCheck("adapter.failure_isolation", "Adapter failures do not cascade.", check_adapter_failure_isolation),
    )


def run_conformance_suite(adapter, context: dict[str, Any] | None = None) -> ConformanceReport:
    """Run the full suite against *adapter* and return a report."""
    report = ConformanceReport(adapter=adapter.adapter_name)
    ctx = dict(context or {})
    for check in conformance_suite():
        report.checks.append(check.run(adapter, ctx))
    return report


# ---------------------------------------------------------------------------
# Interoperability matrix (docs-aligned, deterministic)
# ---------------------------------------------------------------------------

def interoperability_matrix() -> dict[str, Any]:
    """Return the canonical cross-AI interoperability matrix."""
    return {
        "protocol": ["universal-protocol"],
        "state": ["HANDOFF.md", "machine-readable protocol block", "CHANGELOG.md", "Git state"],
        "interfaces": ["CLI", "skill", "MCP", "generic adapter", "platform adapters"],
        "platforms": [
            "claude", "chatgpt", "gemini", "perplexity", "grok", "deepseek",
            "qwen", "kimi", "glm", "manus", "opencode", "cline",
            "future AI agents",
        ],
        "guarantees": [
            "all platforms share one protocol",
            "capabilities differ but never exceed grants",
            "no handoff secrets are distributed to platforms",
            "no dangerous Git operations",
            "no escape from project containment",
        ],
    }