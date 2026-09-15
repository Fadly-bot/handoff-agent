"""Universal AI-to-AI Workflow (Phase 20).

A provider-independent state machine for multi-agent handoffs on top of the
universal handoff protocol. Two AIs (producer → consumer) coordinate through
explicit, versioned, verifiable checkpoint handoffs:

  - agents are registered identities with declared capabilities;
  - every transition is validated against an explicit state machine;
  - a handoff is an explicit request / acknowledgement pair whose acceptance
    sits behind a human-approval boundary;
  - continuity (task, context, constraints, decisions, validation, artifacts,
    Git) is verified before a consumer is allowed to continue;
  - failed, abandoned, and interrupted workflows recover non-destructively;
  - every operation is recorded in an append-only audit trail.

Nothing here shells out to Git and nothing is destructively mutated. All
persistence goes through the same adapter/checkpoint machinery used by the
rest of the protocol.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from handoff_agent.capability import AgentIdentity, identity_hash
from handoff_agent.interop import (
    ConflictReport,
    detect_conflict,
    is_stale,
    snapshot_from_adapter,
)
from handoff_agent.protocol import (
    PROTOCOL_VERSION,
    ProtocolIdentity,
    build_checkpoint,
    diff_checkpoints,
    parse_handoff_document,
    render_state_block,
    validate_checkpoint,
    verify_identity,
)
from handoff_agent.telemetry import (
    TelemetryCollector,
    TelemetryDomain,
    TelemetryStatus,
    emit_event,
)

#: Continuity fields that a successor must preserve across a handoff.
CONTINUITY_FIELDS: tuple[str, ...] = (
    "task",
    "context",
    "constraints",
    "decisions",
    "validation",
    "artifacts",
    "git",
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class WorkflowError(Exception):
    """Base error for workflow operations."""


class WorkflowStateError(WorkflowError):
    """Raised for invalid/duplicate state transitions."""


class UnknownAgentError(WorkflowError):
    """Raised when an unregistered identity is used."""


class UnknownWorkflowError(WorkflowError):
    """Raised when a workflow id is not tracked."""


class OwnershipError(WorkflowError):
    """Raised when an actor performs an action they do not own."""


class StaleCheckpointError(WorkflowError):
    """Raised when a continuation base does not match the current checkpoint."""


class HumanApprovalRequiredError(WorkflowError):
    """Raised when a handoff acceptance requires human approval."""


class WorkflowConflictError(WorkflowError):
    """Raised when divergent work is detected (two heads, no base)."""


# ---------------------------------------------------------------------------
# Workflow state machine
# ---------------------------------------------------------------------------

class WorkflowState(str, Enum):
    IDLE = "idle"
    WORKING = "working"
    CHECKPOINTED = "checkpointed"
    HANDOFF_REQUESTED = "handoff_requested"
    HANDOFF_ACCEPTED = "handoff_accepted"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    FAILED = "failed"


#: Allowed transitions. A transition is legal from -> {next states}.
_MACHINE: dict[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.IDLE: frozenset({WorkflowState.WORKING, WorkflowState.ABANDONED}),
    WorkflowState.WORKING: frozenset(
        {
            WorkflowState.CHECKPOINTED,
            WorkflowState.ABANDONED,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.CHECKPOINTED: frozenset(
        {
            WorkflowState.WORKING,  # continue work
            WorkflowState.HANDOFF_REQUESTED,
            WorkflowState.COMPLETED,  # consumer finishes a verified, approved state
            WorkflowState.ABANDONED,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.HANDOFF_REQUESTED: frozenset(
        {
            WorkflowState.HANDOFF_ACCEPTED,
            WorkflowState.ABANDONED,
            WorkflowState.FAILED,
            WorkflowState.WORKING,  # producer revokes / amends
        }
    ),
    WorkflowState.HANDOFF_ACCEPTED: frozenset(
        {
            WorkflowState.COMPLETED,
            WorkflowState.WORKING,  # consumer continues after acceptance
            WorkflowState.ABANDONED,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.COMPLETED: frozenset(),
    WorkflowState.ABANDONED: frozenset({WorkflowState.WORKING}),
    WorkflowState.FAILED: frozenset({WorkflowState.WORKING}),
}

TERMINAL_STATES: frozenset[WorkflowState] = frozenset(
    {WorkflowState.COMPLETED, WorkflowState.ABANDONED, WorkflowState.FAILED}
)


class WorkflowRole(str, Enum):
    PRODUCER = "producer"
    CONSUMER = "consumer"


# ---------------------------------------------------------------------------
# Identities & audit
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionIdentity:
    """Identity of one AI session participating in a workflow."""

    session_id: str
    agent: AgentIdentity

    def to_dict(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "agent": self.agent.to_dict()}


@dataclass(frozen=True)
class ProjectIdentity:
    """Stable identity of the project a workflow operates on."""

    project_root: str

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            str(self.project_root).encode("utf-8")
        ).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {"project_root": self.project_root, "fingerprint": self.fingerprint}


@dataclass(frozen=True)
class AuditEntry:
    timestamp: str
    actor: str
    action: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "timestamp": self.timestamp,
            "actor": self.actor,
            "action": self.action,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Continuity
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ContinuityReport:
    consistent: bool
    preserved: dict[str, bool]
    gaps: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "consistent": self.consistent,
            "preserved": dict(self.preserved),
            "gaps": list(self.gaps),
            "notes": list(self.notes),
        }


def _continuity_fingerprint(cp: Any) -> dict[str, str]:
    """Map a checkpoint's continuity fields to stable fingerprints."""
    state = cp.state
    metadata = dict(cp.metadata or {})
    git_head = (cp.git.head or "") if getattr(cp, "git", None) else ""
    git_obj = getattr(cp, "git", None)
    git_clean = str(git_obj.clean) if git_obj is not None and git_obj.clean is not None else ""
    return {
        "task": metadata.get("task") or state.objective,
        "context": metadata.get("context") or "",
        "constraints": "|".join(state.constraints),
        "decisions": "|".join(state.decisions),
        "validation": cp.validation.status if getattr(cp, "validation", None) else "",
        "artifacts": "|".join(getattr(cp, "artifacts", ()) or ()),
        "git": f"{git_head}:{git_clean}",
    }


def continuity_report(predecessor: Any, successor: Any) -> ContinuityReport:
    """Compare continuity between a predecessor and successor checkpoint."""
    before = _continuity_fingerprint(predecessor)
    after = _continuity_fingerprint(successor)
    preserved: dict[str, bool] = {}
    gaps: list[str] = []
    for key in CONTINUITY_FIELDS:
        same = before.get(key) == after.get(key)
        preserved[key] = same
        if not same:
            gaps.append(key)
    return ContinuityReport(
        consistent=not gaps,
        preserved=preserved,
        gaps=tuple(gaps),
        notes=(),
    )


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class HandoffRequest:
    token: str
    producer: str
    consumer: str
    message: str
    produced_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "producer": self.producer,
            "consumer": self.consumer,
            "message": self.message,
            "produced_at": self.produced_at,
        }


@dataclass
class WorkflowRecord:
    workflow_id: str
    project: ProjectIdentity
    producer: str
    consumer: str
    state: WorkflowState = WorkflowState.IDLE
    sequence: int = 0
    current_identity: str | None = None
    checkpoint_text: str | None = None
    request: HandoffRequest | None = None
    human_approved: bool = False
    audit: list[AuditEntry] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "project": self.project.to_dict(),
            "producer": self.producer,
            "consumer": self.consumer,
            "state": self.state.value,
            "sequence": self.sequence,
            "current_identity": self.current_identity,
            "request": self.request.to_dict() if self.request else None,
            "human_approved": self.human_approved,
            "audit": [e.to_dict() for e in self.audit],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ---------------------------------------------------------------------------
# The workflow manager
# ---------------------------------------------------------------------------

class WorkflowManager:
    """Coordinates multi-agent handoff workflows for one process."""

    def __init__(
        self,
        adapter: Any | None = None,
        *,
        telemetry: TelemetryCollector | None = None,
    ) -> None:
        self.adapter = adapter
        self._agents: dict[str, AgentIdentity] = {}
        self._workflows: dict[str, WorkflowRecord] = {}
        self._telemetry = telemetry

    # -- registration --------------------------------------------------------

    def register_agent(self, agent: AgentIdentity) -> str:
        """Register an agent identity; returns its canonical id (idempotent)."""
        key = identity_hash(agent)
        self._agents[key] = agent
        return key

    def is_registered(self, agent_id: str) -> bool:
        return agent_id in self._agents

    def known_agents(self) -> tuple[str, ...]:
        return tuple(sorted(self._agents))

    def _require_agent(self, agent_id: str) -> AgentIdentity:
        try:
            return self._agents[agent_id]
        except KeyError:
            raise UnknownAgentError(
                f"Unknown agent {agent_id!r}; register_agent() first."
            ) from None

    def new_session(self, agent: AgentIdentity) -> SessionIdentity:
        return SessionIdentity(session_id=uuid.uuid4().hex, agent=agent)

    def _touch(self, record: WorkflowRecord) -> None:
        record.updated_at = _now()

    def _log(self, record: WorkflowRecord, actor: str, action: str, detail: str = "") -> None:
        record.audit.append(AuditEntry(_now(), actor, action, detail))

    def _transition(self, record: WorkflowRecord, target: WorkflowState, actor: str, detail: str = "") -> None:
        allowed = _MACHINE[record.state]
        if target not in allowed:
            raise WorkflowStateError(
                f"Invalid transition {record.state.value} -> {target.value} "
                f"for workflow {record.workflow_id!r}."
            )
        record.state = target
        self._touch(record)
        self._log(record, actor, f"transition:{target.value}", detail)

    # -- lifecycle -----------------------------------------------------------

    def begin(
        self,
        producer: AgentIdentity,
        consumer: AgentIdentity,
        project_root: str,
        *,
        producer_session: SessionIdentity | None = None,
        consumer_session: SessionIdentity | None = None,
    ) -> WorkflowRecord:
        """Open a new workflow (IDLE → WORKING). Idempotent per producer."""
        producer_id = self.register_agent(producer)
        consumer_id = self.register_agent(consumer)
        producer_session = producer_session or self.new_session(producer)
        consumer_session = consumer_session or self.new_session(consumer)
        probe = uuid.uuid5(uuid.NAMESPACE_URL, f"{producer_session.session_id}:{consumer_session.session_id}")
        workflow_id = f"wf-{probe.hex[:12]}"
        existing = self._workflows.get(workflow_id)
        if existing is not None:
            if existing.state in (WorkflowState.COMPLETED,):
                raise WorkflowStateError(
                    f"Duplicate workflow for session pair {workflow_id!r} already completed."
                )
            return existing

        record = WorkflowRecord(
            workflow_id=workflow_id,
            project=ProjectIdentity(str(project_root)),
            producer=producer_id,
            consumer=consumer_id,
            state=WorkflowState.IDLE,
            created_at=_now(),
            updated_at=_now(),
        )
        self._workflows[workflow_id] = record
        self._log(
            record,
            producer_id,
            "workflow.begin",
            f"producer={producer.name} consumer={consumer.name} project={record.project.fingerprint}",
        )
        record.human_approved = False
        self._transition(record, WorkflowState.WORKING, producer_id, "session opened")
        return record

    def get(self, workflow_id: str) -> WorkflowRecord:
        try:
            return self._workflows[workflow_id]
        except KeyError:
            raise UnknownWorkflowError(f"Unknown workflow id {workflow_id!r}.") from None

    def list_workflows(self) -> tuple[str, ...]:
        return tuple(sorted(self._workflows))

    # -- checkpointing --------------------------------------------------------

    def checkpoint(
        self,
        record: WorkflowRecord,
        *,
        objective: str,
        completed: tuple[str, ...] = (),
        in_progress: tuple[str, ...] = (),
        next_actions: tuple[str, ...] = (),
        decisions: tuple[str, ...] = (),
        constraints: tuple[str, ...] = (),
        artifacts: tuple[str, ...] = (),
        context: str = "",
        validation_status: str = "pending",
        validation_checks: tuple[str, ...] = (),
        actor: str | None = None,
        expected_base: str | None = None,
    ) -> WorkflowRecord:
        """Create/refresh the current checkpoint (idempotent on duplicate).

        A duplicate checkpoint (same content as current) is a no-op that does
        not advance the sequence and cannot race the handoff request.
        """
        actor = actor or record.producer
        if record.state == WorkflowState.HANDOFF_REQUESTED:
            if actor != record.producer:
                raise OwnershipError(
                    f"Only the producer {record.producer!r} may revoke a pending handoff."
                )
            self._transition(record, WorkflowState.WORKING, actor, "handoff revoked; work resumed")
        elif record.state == WorkflowState.HANDOFF_ACCEPTED:
            if actor != record.consumer:
                raise OwnershipError(
                    f"Only the consumer {record.consumer!r} may continue after acceptance."
                )
            self._transition(record, WorkflowState.WORKING, actor, "continuation; work resumed")
        elif record.state not in (WorkflowState.WORKING, WorkflowState.CHECKPOINTED):
            raise WorkflowStateError(
                f"Cannot checkpoint workflow in state {record.state.value}."
            )
        if actor != record.producer and actor != record.consumer:
            raise OwnershipError(f"Actor {actor!r} does not own a checkpoint in {record.workflow_id}.")

        cp = build_checkpoint(
            objective=objective,
            completed=completed,
            in_progress=in_progress,
            next_actions=next_actions,
            decisions=decisions,
            constraints=constraints,
            project_name=record.project.fingerprint,
            agents=[record.producer, record.consumer],
            validation_status=validation_status,
            validation_checks=validation_checks,
            artifacts=artifacts,
            sequence=record.sequence + 1,
        )
        cp.metadata["task"] = objective
        cp.metadata["context"] = context
        text = render_state_block(cp)

        identity = cp.identity.id
        if self.adapter is None:
            if expected_base is not None and expected_base != record.current_identity:
                raise StaleCheckpointError(
                    f"Stale checkpoint: expected base {expected_base!r}, "
                    f"current identity is {record.current_identity!r}."
                )
            if identity == record.current_identity and text == record.checkpoint_text:
                self._log(record, actor, "checkpoint.duplicate", identity)
                return record
        else:
            existing = snapshot_from_adapter(self.adapter)
            if expected_base is not None:
                if existing is None or is_stale(expected_base, existing.identity):
                    raise StaleCheckpointError(
                        f"Stale checkpoint: expected base {expected_base!r}, "
                        f"adapter current is {existing.identity if existing else 'absent'}."
                    )
            if existing is not None and existing.identity == identity:
                # Same machine state = same protocol identity; a re-checkpoint of
                # identical state is a no-op regardless of timestamps.
                self._log(record, actor, "checkpoint.duplicate", identity)
                return record
            write = self.adapter.write_checkpoint(text, expected_base=expected_base)
            if write.unchanged:
                self._log(record, actor, "checkpoint.duplicate", identity)
                return record
            if record.current_identity is not None and identity == record.current_identity:
                self._log(record, actor, "checkpoint.same_identity", identity)

        record.sequence += 1
        record.current_identity = identity
        record.checkpoint_text = text
        self._touch(record)
        self._log(
            record,
            actor,
            f"checkpoint.{'create' if record.state == WorkflowState.WORKING else 'update'} seq={record.sequence}",
            identity,
        )
        if record.state == WorkflowState.WORKING:
            record.state = WorkflowState.CHECKPOINTED
            self._log(record, actor, "transition:checkpointed", identity[:12])
        return record

    def verify_checkpoint(self, record: WorkflowRecord) -> dict[str, Any]:
        """Validate the current on-disk checkpoint + identity."""
        if self.adapter is not None:
            snapshot = snapshot_from_adapter(self.adapter)
            if snapshot is None:
                return {"ok": False, "reason": "no checkpoint on adapter"}
            current_id = snapshot.identity
            text = self.adapter.read_handoff()
        else:
            current_id = record.current_identity
            text = record.checkpoint_text
        if current_id is None:
            return {"ok": False, "reason": "no checkpoint recorded"}
        if record.current_identity not in (None, current_id):
            self._log(record, "system", "verify.checkpoint_mismatch", current_id or "absent")
            return {
                "ok": False,
                "reason": f"record identity {record.current_identity!r} != current {current_id!r}",
            }
        cp = parse_handoff_document(text) if text else None
        if cp is None:
            return {"ok": False, "reason": "checkpoint unparseable"}
        errors = validate_checkpoint(cp)
        return {
            "ok": not errors and verify_identity(cp),
            "identity": cp.identity.id,
            "errors": errors,
        }

    # -- continuity -----------------------------------------------------------

    def continuity(self, record: WorkflowRecord) -> ContinuityReport:
        """Continuity of the latest recorded checkpoint vs the previous one."""
        if record.sequence <= 1 or record.checkpoint_text is None:
            return ContinuityReport(consistent=True, preserved={k: True for k in CONTINUITY_FIELDS})
        holder = _ContinuityHolder(self, record)
        return holder.report()

    def verify_before_continue(
        self, record: WorkflowRecord, *, expected_base: str | None = None
    ) -> ContinuityReport:
        """Gate a consumer's continuation on a valid, non-stale, conflict-free
        checkpoint. Raises on stale bases or divergent heads."""
        if record.state not in (WorkflowState.CHECKPOINTED, WorkflowState.HANDOFF_ACCEPTED):
            raise WorkflowStateError(
                f"Cannot continue from state {record.state.value}."
            )
        snapshot = snapshot_from_adapter(self.adapter) if self.adapter else None
        if snapshot is not None:
            if snapshot.identity != record.current_identity:
                raise StaleCheckpointError(
                    f"Checkpoint superseded by another writer: record has "
                    f"{record.current_identity!r}, adapter current is {snapshot.identity!r}."
                )
            if expected_base is not None and expected_base != snapshot.identity:
                raise StaleCheckpointError(
                    f"Stale base {expected_base!r}; current is {snapshot.identity}."
                )
        result = self.verify_checkpoint(record)
        if not result["ok"]:
            raise WorkflowError(f"Checkpoint verification failed: {result.get('reason', result.get('errors'))}")
        conflict = self._detect_divergence(record)
        if conflict is not None and conflict.conflicting:
            raise WorkflowConflictError(conflict.reason)
        return self.continuity(record)

    def _detect_divergence(self, record: WorkflowRecord) -> ConflictReport | None:
        """Compare the record's checkpoint against the adapter's current.

        A single writer side observed means fast-forward is safe; two
        different heads from the same base means conflict.
        """
        theirs_text = None
        if self.adapter is not None:
            theirs_snap = snapshot_from_adapter(self.adapter)
            theirs_text = self.adapter.read_handoff() if theirs_snap else None
        report = detect_conflict(
            record.current_identity, record.checkpoint_text, theirs_text
        )
        return report

    # -- handoff lifecycle ----------------------------------------------------

    def request_handoff(
        self, record: WorkflowRecord, *, consumer: str, message: str = "", actor: str | None = None
    ) -> HandoffRequest:
        """Producer requests handoff to ``consumer`` (idempotent)."""
        actor = actor or record.producer
        if actor != record.producer:
            raise OwnershipError(f"Only the producer {record.producer!r} may request handoff.")
        self._require_agent(consumer)
        if record.state not in (WorkflowState.CHECKPOINTED, WorkflowState.HANDOFF_REQUESTED):
            raise WorkflowStateError(
                f"Cannot request handoff from state {record.state.value}."
            )
        if record.state == WorkflowState.HANDOFF_REQUESTED:
            # idempotent: identical request → same token
            if record.request and record.request.consumer == consumer:
                self._log(record, actor, "handoff.request.duplicate", record.request.token)
                return record.request
            raise WorkflowStateError(
                f"Handoff already requested for consumer {record.request.consumer if record.request else '?'}."
            )
        verification = self.verify_checkpoint(record)
        if not verification["ok"]:
            raise WorkflowError(
                f"Cannot hand off an unverifiable checkpoint: {verification.get('reason', verification.get('errors'))}"
            )
        request = HandoffRequest(
            token=f"ho-{uuid.uuid4().hex[:12]}",
            producer=record.producer,
            consumer=consumer,
            message=message,
            produced_at=_now(),
        )
        record.request = request
        self._transition(
            record, WorkflowState.HANDOFF_REQUESTED, actor,
            f"to consumer={consumer} token={request.token} objective={message[:60] or '(none)'}",
        )
        emit_event(
            self._telemetry,
            domain=TelemetryDomain.HANDOFF.value,
            operation="request",
            status=TelemetryStatus.OK.value,
            resource=record.workflow_id,
            actor=actor,
            metadata={"producer": record.producer, "consumer": consumer},
        )
        return request

    def accept_handoff(
        self,
        record: WorkflowRecord,
        *,
        consumer: str,
        token: str,
        human_approved: bool = False,
        actor: str | None = None,
    ) -> WorkflowRecord:
        """Consumer acknowledges the handoff (CHECKPOINTED → ACCEPTED).

        The human-approval boundary is explicit: unless ``human_approved``
        is set, acceptance is rejected.
        """
        actor = actor or consumer
        if actor != record.consumer or consumer != record.consumer:
            raise OwnershipError(f"Only consumer {record.consumer!r} may accept.")
        if record.state != WorkflowState.HANDOFF_REQUESTED:
            raise WorkflowStateError(
                f"No pending handoff to accept (state {record.state.value})."
            )
        if record.request is None or record.request.token != token:
            raise WorkflowStateError("Handoff token mismatch; acceptance rejected.")
        if record.request.consumer != record.consumer:
            raise WorkflowStateError("Handoff is not addressed to this consumer.")
        if not human_approved:
            raise HumanApprovalRequiredError(
                "Handoff acceptance requires explicit human approval "
                "(human_approved=True)."
            )
        record.human_approved = True
        self._transition(
            record, WorkflowState.HANDOFF_ACCEPTED, actor,
            f"accepted token={token}",
        )
        emit_event(
            self._telemetry,
            domain=TelemetryDomain.HANDOFF.value,
            operation="accept",
            status=TelemetryStatus.OK.value,
            resource=record.workflow_id,
            actor=actor,
            metadata={"producer": record.producer, "consumer": record.consumer},
        )
        return record

    def complete(self, record: WorkflowRecord, *, actor: str | None = None) -> WorkflowRecord:
        """Consumer marks the shared objective complete.

        Completion is only reachable after an accepted handoff (the record is
        approved) and while the checkpoint still verifies.
        """
        actor = actor or record.consumer
        if actor != record.consumer:
            raise OwnershipError(f"Only consumer {record.consumer!r} may complete.")
        if record.state not in (WorkflowState.HANDOFF_ACCEPTED, WorkflowState.CHECKPOINTED):
            raise WorkflowStateError(
                f"Cannot complete from state {record.state.value}."
            )
        if not record.human_approved:
            raise HumanApprovalRequiredError(
                "Completion requires a handoff accepted with human approval."
            )
        verifying = self.verify_checkpoint(record)
        if not verifying["ok"]:
            raise WorkflowError("Refusing completion: current checkpoint fails verification.")
        self._transition(record, WorkflowState.COMPLETED, actor, "objective complete")
        emit_event(
            self._telemetry,
            domain=TelemetryDomain.HANDOFF.value,
            operation="complete",
            status=TelemetryStatus.OK.value,
            resource=record.workflow_id,
            actor=actor,
            metadata={"producer": record.producer, "consumer": record.consumer},
        )
        return record

    def abandon(self, record: WorkflowRecord, *, reason: str = "", actor: str | None = None) -> WorkflowRecord:
        """Abandon the workflow (goes TERMINAL, recoverable)."""
        actor = actor or record.producer
        if record.state in TERMINAL_STATES:
            raise WorkflowStateError(f"Workflow already in terminal state {record.state.value}.")
        self._transition(record, WorkflowState.ABANDONED, actor, reason or "abandoned")
        return record

    def fail(self, record: WorkflowRecord, *, reason: str = "", actor: str = "system") -> WorkflowRecord:
        """Mark the workflow failed (recoverable via ``recover``)."""
        if record.state in TERMINAL_STATES:
            raise WorkflowStateError(f"Workflow already in terminal state {record.state.value}.")
        self._transition(record, WorkflowState.FAILED, actor, reason or "failed")
        emit_event(
            self._telemetry,
            domain=TelemetryDomain.HANDOFF.value,
            operation="fail",
            status=TelemetryStatus.ERROR.value,
            resource=record.workflow_id,
            actor=actor,
            error_type="handoff",
            error_reason=reason or "failed",
        )
        return record

    def recover(
        self, record: WorkflowRecord, *, artifact: str = "checkpoint", actor: str = "system"
    ) -> WorkflowRecord:
        """Roll a failed/abandoned workflow back to a usable state.

        Recovery is non-destructive: it never rewrites project files, it only
        moves the workflow's logical state to WORKING (reactivating the
        producer). The last valid checkpoint remains on disk untouched.
        """
        if record.state not in TERMINAL_STATES:
            raise WorkflowStateError(
                f"Recovery only applies to terminal states (state {record.state.value})."
            )
        result = self.verify_checkpoint(record)
        recovered = bool(result["ok"] and record.current_identity is not None)
        self._transition(
            record,
            WorkflowState.WORKING,
            actor,
            f"recovered via {artifact} checkpoint_intact={recovered}",
        )
        return record

    # -- audit & visualization ------------------------------------------------

    def export_audit(self, record: WorkflowRecord) -> list[dict[str, str]]:
        return [e.to_dict() for e in record.audit]

    def global_audit(self) -> list[dict[str, str]]:
        return [
            e.to_dict()
            for record in self._workflows.values()
            for e in record.audit
        ]

    def render_state_diagram(self) -> str:
        """ASCII state machine for the workflow lifecycle."""
        lines = [
            "Universal AI-to-AI Workflow state machine",
            "",
            "  idle ──begin──▶ working",
            "  working ──checkpoint──▶ checkpointed",
            "  checkpointed ──continue──▶ working",
            "  checkpointed ──request_handoff──▶ handoff_requested",
            "  handoff_requested ──ack (human+token)──▶ handoff_accepted",
            "  handoff_accepted ──verify──▶ working (consumer continues)",
            "  handoff_accepted ──complete──▶ completed",
            "  any ──abandon──▶ abandoned ──recover──▶ working",
            "  any ──fail──▶ failed ──recover──▶ working",
        ]
        return "\n".join(lines)

    def render_flow(self, record: WorkflowRecord) -> str:
        """ASCII trace of a single workflow's journey."""
        steps = [e.action for e in record.audit]
        path: list[str] = []
        for step in steps:
            if step.startswith("transition"):
                path.append(step.split(":", 1)[1])
        if not path and record.state in TERMINAL_STATES:
            return f"workflow {record.workflow_id} ({record.state.value}) no transitions recorded"
        return " ──▶ ".join(["idle", *path])


# ---------------------------------------------------------------------------
# Continuity comparison across the record's checkpoint history (via adapter
# changelog when available)
# ---------------------------------------------------------------------------

class _ContinuityHolder:
    """Compares the current checkpoint against the immediately previous one."""

    def __init__(self, manager: WorkflowManager, record: WorkflowRecord) -> None:
        self._manager = manager
        self._record = record

    def report(self) -> ContinuityReport:
        current = parse_handoff_document(self._record.checkpoint_text)  # type: ignore[arg-type]
        if current is None:
            return ContinuityReport(consistent=False, preserved={}, gaps=("checkpoint",))
        previous = self._previous()
        if previous is None:
            return ContinuityReport(consistent=True, preserved={k: True for k in CONTINUITY_FIELDS})
        return continuity_report(previous, current)

    def _previous(self) -> Any | None:
        if self._manager.adapter is None:
            return None
        changelog = None
        try:
            changelog = self._manager.adapter.read_changelog()
        except Exception:  # noqa: BLE001
            changelog = None
        if not changelog:
            return None
        block_index = changelog.rfind("```handoff-protocol")
        if block_index < 0:
            return None
        tail = changelog[block_index:]
        return parse_handoff_document(tail)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")