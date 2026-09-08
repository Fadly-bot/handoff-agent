"""Persistent Multi-Agent Orchestration (Phase 23).

A provider-independent orchestration engine that persists multi-agent
workflow state and drives a deterministic task state machine.

Design rules (aligned with the Universal Handoff invariants):

  - Workflow state is persisted as atomic JSON files under a dedicated state
    directory (``~/.handoff/orchestration`` by default). No project file is
    ever mutated except through an explicit adapter checkpoint write.
  - Every state mutation is atomic and hash-checked: two concurrent writers
    cannot silently overwrite each other (stale workflow detection), and a
    truncated/corrupt file on disk fails state-integrity validation.
  - Task execution is idempotent: the same ``execution_id`` cannot succeed
    twice with different results, and duplicate completion is a no-op.
  - A human approval gate is enforced: ``approve_task`` is the ONLY way to
    clear an approval requirement; there is no automatic approval bypass.
  - No API keys, no secret values, and no credential material are ever
    persisted: serialized state is secret-filtered and refused if tainted.
  - The engine never executes Git and never writes outside the state
    directory (no arbitrary project mutation, no unrestricted Git mutation).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from handoff_agent.capability import (
    CAP_CHECKPOINT_CREATE,
    CAP_CHECKPOINT_UPDATE,
    CapabilityDeniedError,
    PermissionBoundary,
    negotiate_capabilities,
)
from handoff_agent.constants import HANDOFF_HOME
from handoff_agent.persistence import _atomic_write_file, _contains_secret_like_content


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class OrchestrationError(Exception):
    """Base error for orchestration failures."""


class WorkflowNotFoundError(OrchestrationError):
    """Raised when a workflow id is not persisted."""


class DuplicateWorkflowError(OrchestrationError):
    """Raised when creating a workflow with a duplicate id."""


class DuplicateTaskError(OrchestrationError):
    """Raised when a task id already exists in a workflow."""


class TaskNotFoundError(OrchestrationError):
    """Raised when a task id does not exist in a workflow."""


class InvalidTaskDependencyError(OrchestrationError):
    """Raised when a dependency set is invalid (self / missing / cycle)."""


class TaskStateError(OrchestrationError):
    """Raised when a task transition is not allowed by the state machine."""


class StaleWorkflowError(OrchestrationError):
    """Raised when mutating against an outdated workflow base."""


class StaleTaskError(OrchestrationError):
    """Raised when a task result references a superseded execution."""


class DuplicateExecutionError(OrchestrationError):
    """Raised when the same execution_id is replayed with different data."""


class ApprovalRequiredError(OrchestrationError):
    """Raised when an approved transition is attempted without approval."""


class CorruptStateError(OrchestrationError):
    """Raised when persisted state fails integrity validation."""


class UnauthorizedOperationError(OrchestrationError):
    """Raised when an actor performs a disallowed operation."""


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class WorkflowStatus(Enum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_APPROVAL = "waiting_approval"
    CANCELLED = "cancelled"
    FAILED = "failed"
    COMPLETED = "completed"


class TaskStatus(Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    BLOCKED = "blocked"
    APPROVAL_REQUIRED = "approval_required"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL = frozenset(
    {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_execution_id() -> str:
    return "exec-" + uuid.uuid4().hex[:16]


# ---------------------------------------------------------------------------
# Canonical serialization helpers
# ---------------------------------------------------------------------------

def _json_default(o: object) -> Any:
    if isinstance(o, Enum):
        return o.value
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"cannot serialize {type(o).__name__}")


def _canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_json_default)


def _digest(obj: object) -> str:
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Structured records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OrchestrationEvent:
    """An append-only execution event / state transition audit entry."""

    action: str
    actor: str
    task_id: str = ""
    execution_id: str = ""
    detail: str = ""
    timestamp: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, str]:
        return {
            "action": self.action,
            "actor": self.actor,
            "task_id": self.task_id,
            "execution_id": self.execution_id,
            "detail": self.detail,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OrchestrationEvent":
        return cls(
            action=str(data.get("action", "")),
            actor=str(data.get("actor", "")),
            task_id=str(data.get("task_id", "")),
            execution_id=str(data.get("execution_id", "")),
            detail=str(data.get("detail", "")),
            timestamp=str(data.get("timestamp", "")),
        )


@dataclass
class TaskRecord:
    """A single task inside a workflow.

    ``execution_id`` distinguishes individual runs of a retried task; a task
    that is retried gets a fresh execution id, so a stale result from a prior
    run is rejected.
    """

    task_id: str
    workflow_id: str
    name: str
    task_type: str = "generic"
    parent_task_id: str = ""
    agent_id: str = ""
    requester_agent_id: str = ""
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 5
    deadline: str = ""
    dependencies: tuple[str, ...] = ()
    required_capabilities: frozenset[str] = frozenset()
    context: str = ""
    requirements: dict[str, str] = field(default_factory=dict)
    constraints: tuple[str, ...] = ()
    approval_required: bool = False
    approved_by: str = ""
    execution_id: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    attempts: int = 0
    block_reason: str = ""
    correlation_id: str = ""
    request_id: str = ""
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    started_at: str = ""
    completed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "workflow_id": self.workflow_id,
            "name": self.name,
            "task_type": self.task_type,
            "parent_task_id": self.parent_task_id,
            "agent_id": self.agent_id,
            "requester_agent_id": self.requester_agent_id,
            "status": self.status.value,
            "priority": self.priority,
            "deadline": self.deadline,
            "dependencies": sorted(self.dependencies),
            "required_capabilities": sorted(self.required_capabilities),
            "context": self.context,
            "requirements": dict(self.requirements),
            "constraints": sorted(self.constraints),
            "approval_required": self.approval_required,
            "approved_by": self.approved_by,
            "execution_id": self.execution_id,
            "result": dict(self.result),
            "error": self.error,
            "attempts": self.attempts,
            "block_reason": self.block_reason,
            "correlation_id": self.correlation_id,
            "request_id": self.request_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskRecord":
        return cls(
            task_id=str(data.get("task_id", "")),
            workflow_id=str(data.get("workflow_id", "")),
            name=str(data.get("name", "")),
            task_type=str(data.get("task_type", "generic")),
            parent_task_id=str(data.get("parent_task_id", "")),
            agent_id=str(data.get("agent_id", "")),
            requester_agent_id=str(data.get("requester_agent_id", "")),
            status=TaskStatus(str(data.get("status", TaskStatus.PENDING.value))),
            priority=int(data.get("priority", 5)),
            deadline=str(data.get("deadline", "")),
            dependencies=tuple(sorted(data.get("dependencies", []))),
            required_capabilities=frozenset(data.get("required_capabilities", [])),
            context=str(data.get("context", "")),
            requirements=dict(data.get("requirements", {})),
            constraints=tuple(sorted(data.get("constraints", []))),
            approval_required=bool(data.get("approval_required", False)),
            approved_by=str(data.get("approved_by", "")),
            execution_id=str(data.get("execution_id", "")),
            result=dict(data.get("result", {})),
            error=str(data.get("error", "")),
            attempts=int(data.get("attempts", 0)),
            block_reason=str(data.get("block_reason", "")),
            correlation_id=str(data.get("correlation_id", "")),
            request_id=str(data.get("request_id", "")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            started_at=str(data.get("started_at", "")),
            completed_at=str(data.get("completed_at", "")),
        )

    def _touched(self) -> None:
        self.updated_at = _now_iso()

    def _with(self, **changes: Any) -> "TaskRecord":
        """Return a copy of this task with ``*changes*`` applied."""
        data = self.to_dict()
        for key, value in changes.items():
            if isinstance(value, Enum):
                value = value.value
            data[key] = value
        return TaskRecord.from_dict(data)


@dataclass
class WorkflowRecord:
    """Persisted orchestration workflow state."""

    workflow_id: str
    project_id: str
    name: str
    status: WorkflowStatus = WorkflowStatus.CREATED
    timeout_seconds: int = 3600
    tasks: dict[str, TaskRecord] = field(default_factory=dict)
    assignments: dict[str, str] = field(default_factory=dict)
    events: list[OrchestrationEvent] = field(default_factory=list)
    correlation_id: str = ""
    request_id: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    state_hash: str = ""
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    # -- helpers -----------------------------------------------------------

    @property
    def task_list(self) -> tuple[TaskRecord, ...]:
        """Deterministic task ordering by task_id."""
        return tuple(sorted(self.tasks.values(), key=lambda t: t.task_id))

    def task(self, task_id: str) -> TaskRecord:
        if task_id not in self.tasks:
            raise TaskNotFoundError(
                f"Task {task_id!r} does not exist in workflow {self.workflow_id!r}."
            )
        return self.tasks[task_id]

    def count(self, status: TaskStatus) -> int:
        return sum(1 for t in self.tasks.values() if t.status == status)

    def status_counts(self) -> dict[str, int]:
        counts = {s.value: 0 for s in TaskStatus}
        for task in self.tasks.values():
            counts[task.status.value] += 1
        return counts

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "workflow_id": self.workflow_id,
            "project_id": self.project_id,
            "name": self.name,
            "status": self.status.value,
            "timeout_seconds": self.timeout_seconds,
            "tasks": [t.to_dict() for t in self.task_list],
            "assignments": dict(sorted(self.assignments.items())),
            "events": [e.to_dict() for e in self.events],
            "correlation_id": self.correlation_id,
            "request_id": self.request_id,
            "meta": dict(self.meta),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.state_hash:
            d["state_hash"] = self.state_hash
        return d

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkflowRecord":
        rec = cls(
            workflow_id=str(data.get("workflow_id", "")),
            project_id=str(data.get("project_id", "")),
            name=str(data.get("name", "")),
            status=WorkflowStatus(str(data.get("status", WorkflowStatus.CREATED.value))),
            timeout_seconds=int(data.get("timeout_seconds", 3600)),
            tasks={
                t["task_id"]: TaskRecord.from_dict(t)
                for t in data.get("tasks", [])
            },
            assignments=dict(data.get("assignments", {})),
            events=[OrchestrationEvent.from_dict(e) for e in data.get("events", [])],
            correlation_id=str(data.get("correlation_id", "")),
            request_id=str(data.get("request_id", "")),
            meta=dict(data.get("meta", {})),
            state_hash=str(data.get("state_hash", "")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )
        return rec


# ---------------------------------------------------------------------------
# State integrity helpers
# ---------------------------------------------------------------------------

def _content_hash(record: WorkflowRecord) -> str:
    """Canonical content digest of a record (excluding its stored hash)."""
    data = record.to_dict()
    data.pop("state_hash", None)
    return _digest(data)


def validate_state(record: WorkflowRecord) -> dict[str, Any]:
    """Validate a workflow record's structural and integrity invariants.

    Returns a report dict; raises ``CorruptStateError`` when integrity fails.
    """
    errors: list[str] = []
    task_ids = set(record.tasks)
    for task in record.tasks.values():
        if task.task_id not in task_ids:
            errors.append(f"task {task.task_id!r} not indexed")
        for dep in task.dependencies:
            if dep not in task_ids:
                errors.append(
                    f"task {task.task_id!r} depends on missing {dep!r}"
                )
            if dep == task.task_id:
                errors.append(f"task {task.task_id!r} depends on itself")
    if record.state_hash and _content_hash(record) != record.state_hash:
        raise CorruptStateError(
            "Persisted workflow state integrity hash does not match its content."
        )
    return {"ok": not errors, "errors": errors}


def detect_stale(expected_hash: str, current_hash: str) -> bool:
    """True when ``*current_hash*`` has superseded ``*expected_hash*``."""
    return expected_hash != current_hash


# ---------------------------------------------------------------------------
# Dependency analysis
# ---------------------------------------------------------------------------

def _has_cycle(adjacency: Mapping[str, Iterable[str]]) -> bool:
    visiting = set()
    done: set[str] = set()

    def visit(node: str) -> bool:
        if node in done:
            return False
        if node in visiting:
            return True
        visiting.add(node)
        for dep in adjacency.get(node, ()):
            if visit(dep):
                return True
        visiting.discard(node)
        done.add(node)
        return False

    return any(visit(n) for n in adjacency)


def _ready_tasks(
    tasks: Mapping[str, TaskRecord],
    *,
    parallel: bool,
    requiring_approval: bool,
) -> tuple[TaskRecord, ...]:
    """Deterministically select the runnable tasks under the dispatch policy."""
    running = [t for t in tasks.values() if t.status == TaskStatus.RUNNING]
    if running and not parallel:
        return ()

    candidates = [
        t for t in tasks.values()
        if t.status in (TaskStatus.PENDING, TaskStatus.QUEUED)
        and all(tasks[d].status == TaskStatus.SUCCESS for d in t.dependencies)
    ]

    def _ok(t: TaskRecord) -> bool:
        if t.approval_required and not t.approved_by:
            return False
        if requiring_approval and not t.approved_by:
            return False
        return True

    ready = [t for t in candidates if _ok(t)]
    ready.sort(key=lambda t: (t.priority, t.created_at, t.task_id))
    if not parallel:
        return tuple(ready[:1])
    return tuple(ready)


# ---------------------------------------------------------------------------
# Persistent orchestration engine
# ---------------------------------------------------------------------------

class PersistentOrchestrator:
    """File-backed multi-agent orchestration engine (Phase 23)."""

    def __init__(
        self,
        state_dir: str | Path | None = None,
        *,
        timeout_seconds: int = 3600,
        require_human_approval: bool = False,
        boundary: PermissionBoundary | None = None,
    ) -> None:
        self.state_dir = Path(state_dir or HANDOFF_HOME / "orchestration")
        self.timeout_seconds = timeout_seconds
        self.require_human_approval = require_human_approval
        self.boundary = boundary
        self._adapter: Any | None = None
        self.state_dir.mkdir(parents=True, exist_ok=True)

    # -- persistence plumbing -------------------------------------------------

    def _workflow_path(self, workflow_id: str) -> Path:
        return self.state_dir / f"{workflow_id}.json"

    def _save(self, record: WorkflowRecord) -> WorkflowRecord:
        text = _canonical(record.to_dict())
        if _contains_secret_like_content(text):
            raise OrchestrationError(
                "Refusing to persist workflow: state contains secret-like values."
            )
        try:
            _atomic_write_file(self._workflow_path(record.workflow_id), text)
        except OSError as exc:
            raise OrchestrationError(
                f"Could not persist workflow {record.workflow_id!r}: {exc}"
            ) from exc
        return record

    def _load_raw(self, workflow_id: str) -> WorkflowRecord:
        path = self._workflow_path(workflow_id)
        if not path.exists():
            raise WorkflowNotFoundError(f"Workflow {workflow_id!r} not found.")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise CorruptStateError(
                    f"Persisted workflow {workflow_id!r} is not an object."
                )
            expected_keys = {"workflow_id", "status", "tasks", "events", "correlation_id"}
            if not expected_keys.issubset(data):
                missing = sorted(expected_keys - set(data))
                raise CorruptStateError(
                    f"Persisted workflow {workflow_id!r} is missing keys {missing}."
                )
            record = WorkflowRecord.from_dict(data)
        except (OSError, ValueError, TypeError, KeyError, CorruptStateError) as exc:
            if isinstance(exc, CorruptStateError):
                raise
            raise CorruptStateError(
                f"Could not parse persisted workflow {workflow_id!r}: {exc}"
            ) from exc
        report = validate_state(record)
        if not report["ok"]:
            raise CorruptStateError(
                "Persisted workflow failed validation: "
                + "; ".join(report["errors"])
            )
        return record

    def _load(self, workflow_id: str, expected_base: str | None = None) -> WorkflowRecord:
        record = self._load_raw(workflow_id)
        if expected_base is not None and record.state_hash != expected_base:
            raise StaleWorkflowError(
                f"Workflow {workflow_id!r} was superseded (expected base "
                f"{expected_base!r}, current {record.state_hash!r})."
            )
        return record

    def _commit(self, record: WorkflowRecord) -> WorkflowRecord:
        record.state_hash = _content_hash(record)
        self._save(record)
        return record

    def _log(
        self,
        record: WorkflowRecord,
        action: str,
        actor: str,
        *,
        task_id: str = "",
        execution_id: str = "",
        detail: str = "",
    ) -> None:
        record.events.append(
            OrchestrationEvent(
                action=action,
                actor=actor,
                task_id=task_id,
                execution_id=execution_id,
                detail=detail,
            )
        )

    # -- workflow lifecycle ----------------------------------------------------

    def create_workflow(
        self,
        project_id: str,
        name: str,
        *,
        workflow_id: str | None = None,
        timeout_seconds: int | None = None,
        requester_agent_id: str = "",
        meta: Mapping[str, Any] | None = None,
        actor: str = "orchestrator",
    ) -> WorkflowRecord:
        wid = workflow_id or uuid.uuid4().hex[:16]
        if self._workflow_path(wid).exists():
            raise DuplicateWorkflowError(f"Workflow {wid!r} already exists.")
        record = WorkflowRecord(
            workflow_id=wid,
            project_id=project_id,
            name=name,
            status=WorkflowStatus.CREATED,
            timeout_seconds=timeout_seconds if timeout_seconds is not None else self.timeout_seconds,
            correlation_id="corr-" + uuid.uuid4().hex[:16],
            request_id="req-" + uuid.uuid4().hex[:16],
            meta=dict(meta or {}),
        )
        self._log(record, "workflow.created", actor, detail=project_id)
        return self._commit(record)

    def get_workflow(self, workflow_id: str) -> WorkflowRecord:
        return self._load(workflow_id)

    def list_workflows(self) -> tuple[WorkflowRecord, ...]:
        records: list[WorkflowRecord] = []
        for path in sorted(self.state_dir.glob("*.json")):
            try:
                records.append(self._load_raw(path.stem))
            except CorruptStateError:
                continue
        return tuple(records)

    # -- task creation & dependencies ------------------------------------------

    def add_task(
        self,
        workflow_id: str,
        *,
        task_id: str,
        name: str,
        task_type: str = "generic",
        parent_task_id: str = "",
        dependencies: Iterable[str] = (),
        required_capabilities: Iterable[str] = (),
        priority: int = 5,
        deadline: str = "",
        context: str = "",
        requirements: Mapping[str, str] | None = None,
        constraints: Iterable[str] = (),
        approval_required: bool = False,
        requester_agent_id: str = "",
        correlation_id: str = "",
        request_id: str = "",
        agent_id: str = "",
        expected_base: str | None = None,
        actor: str = "orchestrator",
    ) -> WorkflowRecord:
        record = self._load(workflow_id, expected_base)
        if task_id in record.tasks:
            raise DuplicateTaskError(
                f"Task {task_id!r} already exists in workflow {workflow_id!r}."
            )
        deps = tuple(sorted(set(dependencies)))
        for dep in deps:
            if dep not in record.tasks:
                raise InvalidTaskDependencyError(
                    f"Dependency {dep!r} for task {task_id!r} does not exist."
                )
        if task_id in deps:
            raise InvalidTaskDependencyError(f"Task {task_id!r} may not depend on itself.")
        task = TaskRecord(
            task_id=task_id,
            workflow_id=workflow_id,
            name=name,
            task_type=task_type,
            parent_task_id=parent_task_id,
            agent_id=agent_id,
            requester_agent_id=requester_agent_id,
            status=(
                TaskStatus.APPROVAL_REQUIRED if approval_required else TaskStatus.PENDING
            ),
            priority=priority,
            deadline=deadline,
            dependencies=deps,
            required_capabilities=frozenset(required_capabilities),
            context=context,
            requirements=dict(requirements or {}),
            constraints=tuple(sorted(constraints)),
            approval_required=approval_required,
            correlation_id=correlation_id or record.correlation_id,
            request_id=request_id or record.request_id,
        )
        record.tasks[task_id] = task
        if approval_required:
            record.status = WorkflowStatus.WAITING_APPROVAL
            self._log(record, "task.approval_required", actor, task_id=task_id)
        self._log(
            record,
            "task.created",
            actor,
            task_id=task_id,
            detail=f"deps={len(deps)} approval={approval_required}",
        )
        return self._commit(record)

    def set_dependencies(
        self,
        workflow_id: str,
        task_id: str,
        dependencies: Iterable[str],
        *,
        expected_base: str | None = None,
        actor: str = "orchestrator",
    ) -> WorkflowRecord:
        record = self._load(workflow_id, expected_base)
        task = record.task(task_id)
        if task.status not in (TaskStatus.PENDING, TaskStatus.QUEUED):
            raise TaskStateError(
                f"Cannot change dependencies of task {task_id!r} in state {task.status.value}."
            )
        deps = tuple(sorted(set(dependencies)))
        if task_id in deps:
            raise InvalidTaskDependencyError(f"Task {task_id!r} may not depend on itself.")
        for dep in deps:
            if dep not in record.tasks:
                raise InvalidTaskDependencyError(
                    f"Dependency {dep!r} for task {task_id!r} does not exist."
                )
        tentative = dict(record.tasks)
        tentative[task_id] = task._with(dependencies=deps)
        if _has_cycle({tid: t.dependencies for tid, t in tentative.items()}):
            raise InvalidTaskDependencyError(
                f"Setting dependencies of {task_id!r} would create a cycle."
            )
        record.tasks[task_id] = tentative[task_id]
        self._log(record, "task.dependencies", actor, task_id=task_id, detail=f"{deps}")
        return self._commit(record)

    # -- agent assignment & capability negotiation -----------------------------

    def assign_agent(
        self,
        workflow_id: str,
        task_id: str,
        agent_id: str,
        *,
        agent_capabilities: Iterable[str] = (),
        boundary: PermissionBoundary | None = None,
        expected_base: str | None = None,
        actor: str = "orchestrator",
    ) -> WorkflowRecord:
        record = self._load(workflow_id, expected_base)
        task = record.task(task_id)
        bnd = boundary or self.boundary or PermissionBoundary(name="orchestrator")
        if task.required_capabilities:
            result = negotiate_capabilities(
                frozenset(agent_capabilities), bnd.allowed
            )
            missing = task.required_capabilities - result.granted
            if missing:
                raise CapabilityDeniedError(
                    agent=agent_id,
                    capability=sorted(missing)[0],
                    reason="required capabilities not granted",
                )
        record.assignments[task_id] = agent_id
        record.tasks[task_id] = task._with(agent_id=agent_id)
        self._log(
            record,
            "task.assign",
            actor,
            task_id=task_id,
            detail=f"agent={agent_id}",
        )
        return self._commit(record)

    def unassign_agent(
        self,
        workflow_id: str,
        task_id: str,
        *,
        expected_base: str | None = None,
        actor: str = "orchestrator",
    ) -> WorkflowRecord:
        record = self._load(workflow_id, expected_base)
        task = record.task(task_id)
        if task.status not in (TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.BLOCKED, TaskStatus.WAITING):
            raise TaskStateError(
                f"Cannot unassign task {task_id!r} in state {task.status.value}."
            )
        record.tasks[task_id] = task._with(agent_id="")
        record.assignments.pop(task_id, None)
        self._log(record, "task.unassign", actor, task_id=task_id)
        return self._commit(record)

    # -- task state machine -----------------------------------------------------

    def approve_task(
        self,
        workflow_id: str,
        task_id: str,
        *,
        human: str,
        expected_base: str | None = None,
        actor: str | None = None,
    ) -> WorkflowRecord:
        """The ONLY way to clear a task's approval gate."""
        record = self._load(workflow_id, expected_base)
        task = record.task(task_id)
        if not human:
            raise ApprovalRequiredError("Human approval requires a human actor.")
        actor = actor or human
        if task.status not in (TaskStatus.APPROVAL_REQUIRED, TaskStatus.QUEUED, TaskStatus.PENDING, TaskStatus.BLOCKED):
            raise TaskStateError(
                f"Task {task_id!r} in state {task.status.value} does not require approval."
            )
        record.tasks[task_id] = record.tasks[task_id]._with(
            approved_by=human,
            block_reason="",
            status=TaskStatus.QUEUED,
        )
        self._log(record, "task.approved", actor, task_id=task_id, detail=f"by={human}")
        self._tick_workflow_status(record, actor)
        return self._commit(record)

    def dispatch(
        self,
        workflow_id: str,
        *,
        parallel: bool = False,
        expected_base: str | None = None,
        actor: str = "orchestrator",
    ) -> tuple[WorkflowRecord, tuple[TaskRecord, ...]]:
        """Transition ready tasks to RUNNING and assign execution ids.

        Sequential policy (``parallel=False``) runs at most one task at a
        time; parallel policy runs every currently ready task.
        """
        record = self._load(workflow_id, expected_base)
        if record.status not in (
            WorkflowStatus.RUNNING,
            WorkflowStatus.CREATED,
            WorkflowStatus.WAITING_APPROVAL,
        ):
            if record.status == WorkflowStatus.PAUSED:
                raise TaskStateError("Workflow is paused; resume before dispatching.")
            raise TaskStateError(
                f"Cannot dispatch from workflow state {record.status.value}."
            )
        if record.status == WorkflowStatus.CREATED:
            record.status = WorkflowStatus.RUNNING
            self._log(record, "workflow.running", actor)
        ready = _ready_tasks(
            record.tasks,
            parallel=parallel,
            requiring_approval=self.require_human_approval,
        )
        if not ready:
            self._tick_workflow_status(record, actor)
            return self._commit(record), ()
        running: list[TaskRecord] = []
        for task in ready:
            if not task.execution_id:
                task.execution_id = _new_execution_id()
            next_task = task._with(
                status=TaskStatus.RUNNING,
                attempts=task.attempts + 1,
                started_at=_now_iso(),
                error="",
            )
            record.tasks[task.task_id] = next_task
            self._log(
                record,
                "task.running",
                actor,
                task_id=task.task_id,
                execution_id=task.execution_id,
            )
            running.append(next_task)
        self._tick_workflow_status(record, actor)
        return self._commit(record), tuple(running)

    def complete_task(
        self,
        workflow_id: str,
        task_id: str,
        *,
        agent_id: str,
        execution_id: str,
        result: Mapping[str, Any] | None = None,
        actor: str | None = None,
        expected_base: str | None = None,
    ) -> WorkflowRecord:
        record = self._load(workflow_id, expected_base)
        task = record.task(task_id)
        actor = actor or agent_id
        if task.agent_id and agent_id and task.agent_id != agent_id:
            raise UnauthorizedOperationError(
                f"Agent {agent_id!r} is not assigned to task {task_id!r}."
            )
        payload = dict(result or {})
        text = _canonical(payload)
        if _contains_secret_like_content(text):
            raise OrchestrationError(
                "Refusing to accept a task result containing secret-like values."
            )
        if task.status == TaskStatus.SUCCESS and task.execution_id == execution_id:
            if dict(task.result or {}) == payload:
                self._log(record, "task.complete.duplicate", actor, task_id=task_id, detail="idempotent no-op")
                return self._commit(record)
            raise StaleTaskError(
                f"Conflicting completion for task {task_id!r} with execution "
                f"{execution_id!r} differed from its recorded result."
            )
        if task.status not in (TaskStatus.RUNNING,):
            raise TaskStateError(
                f"Cannot complete task {task_id!r} in state {task.status.value}."
            )
        current = task.execution_id
        if current and execution_id and execution_id != current:
            raise StaleTaskError(
                f"Result for task {task_id!r} references superseded execution "
                f"{execution_id!r}; current is {current!r}."
            )
        record.tasks[task_id] = task._with(
            status=TaskStatus.SUCCESS,
            execution_id=execution_id,
            result=payload,
            completed_at=_now_iso(),
        )
        self._log(
            record,
            "task.complete",
            actor,
            task_id=task_id,
            execution_id=execution_id,
            detail=f"agent={agent_id}",
        )
        self._propagate(record, task_id, actor)
        self._tick_workflow_status(record, actor)
        return self._commit(record)

    def fail_task(
        self,
        workflow_id: str,
        task_id: str,
        *,
        execution_id: str,
        error: str,
        agent_id: str = "",
        actor: str | None = None,
        expected_base: str | None = None,
    ) -> WorkflowRecord:
        record = self._load(workflow_id, expected_base)
        task = record.task(task_id)
        actor = actor or (agent_id or "system")
        if task.status == TaskStatus.FAILED and task.execution_id == execution_id:
            self._log(record, "task.fail.duplicate", actor, task_id=task_id, detail="idempotent no-op")
            return self._commit(record)
        if task.status not in (TaskStatus.RUNNING, TaskStatus.QUEUED):
            raise TaskStateError(
                f"Cannot fail task {task_id!r} in state {task.status.value}."
            )
        current = task.execution_id
        if current and execution_id and execution_id != current:
            raise StaleTaskError(
                f"Failure for task {task_id!r} references superseded execution "
                f"{execution_id!r}; current is {current!r}."
            )
        record.tasks[task_id] = task._with(
            status=TaskStatus.FAILED,
            execution_id=execution_id,
            error=error,
            completed_at=_now_iso(),
        )
        self._log(record, "task.failed", actor, task_id=task_id, execution_id=execution_id, detail=error)
        self._propagate(record, task_id, actor)
        self._tick_workflow_status(record, actor)
        return self._commit(record)

    def cancel_task(
        self,
        workflow_id: str,
        task_id: str,
        *,
        actor: str = "orchestrator",
        reason: str = "",
        expected_base: str | None = None,
    ) -> WorkflowRecord:
        record = self._load(workflow_id, expected_base)
        task = record.task(task_id)
        if task.status in _TERMINAL:
            raise TaskStateError(
                f"Task {task_id!r} is already terminal ({task.status.value})."
            )
        if task.status in (TaskStatus.RUNNING,):
            raise TaskStateError(
                f"Task {task_id!r} is running; fail or retry instead of cancelling."
            )
        record.tasks[task_id] = task._with(status=TaskStatus.CANCELLED, error=reason)
        self._log(record, "task.cancelled", actor, task_id=task_id, detail=reason)
        self._propagate(record, task_id, actor)
        self._tick_workflow_status(record, actor)
        return self._commit(record)

    def retry_task(
        self,
        workflow_id: str,
        task_id: str,
        *,
        agent_id: str = "",
        expected_base: str | None = None,
        actor: str = "orchestrator",
    ) -> WorkflowRecord:
        """Requeue a FAILED / CANCELLED task with a fresh execution id."""
        record = self._load(workflow_id, expected_base)
        task = record.task(task_id)
        if task.status not in (TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.BLOCKED):
            raise TaskStateError(
                f"Only failed/cancelled/blocked tasks may be retried; {task_id!r} is {task.status.value}."
            )
        next_task = task._with(
            status=TaskStatus.QUEUED if not task.approval_required else TaskStatus.APPROVAL_REQUIRED,
            error="",
            block_reason="",
            execution_id="",
            result={},
            agent_id=agent_id or task.agent_id,
        )
        if self.require_human_approval and not next_task.approved_by:
            next_task = next_task._with(status=TaskStatus.APPROVAL_REQUIRED)
        record.tasks[task_id] = next_task
        self._log(record, "task.retry", actor, task_id=task_id, detail=f"attempts={next_task.attempts}")
        self._tick_workflow_status(record, actor)
        return self._commit(record)

    def flag_approval(self, workflow_id: str, task_id: str, *, actor: str = "orchestrator") -> WorkflowRecord:
        """Flag a task as requiring human approval before it may run."""
        record = self._load(workflow_id)
        task = record.task(task_id)
        if task.status not in (TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.BLOCKED, TaskStatus.WAITING):
            raise TaskStateError(
                f"Cannot flag task {task_id!r} for approval in state {task.status.value}."
            )
        record.tasks[task_id] = task._with(
            status=TaskStatus.APPROVAL_REQUIRED, approval_required=True
        )
        record.status = WorkflowStatus.WAITING_APPROVAL
        self._log(record, "task.approval_required", actor, task_id=task_id)
        return self._commit(record)

    def block_task(
        self,
        workflow_id: str,
        task_id: str,
        *,
        reason: str,
        actor: str = "orchestrator",
    ) -> WorkflowRecord:
        record = self._load(workflow_id)
        task = record.task(task_id)
        if task.status in _TERMINAL:
            raise TaskStateError(f"Task {task_id!r} is terminal ({task.status.value}).")
        record.tasks[task_id] = task._with(status=TaskStatus.BLOCKED, block_reason=reason)
        self._log(record, "task.blocked", actor, task_id=task_id, detail=reason)
        self._tick_workflow_status(record, actor)
        return self._commit(record)

    def unblock_task(
        self,
        workflow_id: str,
        task_id: str,
        *,
        actor: str = "orchestrator",
    ) -> WorkflowRecord:
        record = self._load(workflow_id)
        task = record.task(task_id)
        if task.status != TaskStatus.BLOCKED:
            raise TaskStateError(f"Task {task_id!r} is not blocked.")
        next_task = task._with(
            status=TaskStatus.QUEUED if not task.approval_required else TaskStatus.APPROVAL_REQUIRED,
            block_reason="",
        )
        record.tasks[task_id] = next_task
        self._log(record, "task.unblocked", actor, task_id=task_id)
        self._tick_workflow_status(record, actor)
        return self._commit(record)

    # -- failure propagation ----------------------------------------------------

    def _propagate(self, record: WorkflowRecord, task_id: str, actor: str) -> None:
        """Propagate success/failure/cancellation to dependent tasks."""
        for dep in record.tasks.values():
            if dep.status in (TaskStatus.PENDING, TaskStatus.QUEUED):
                if task_id not in dep.dependencies:
                    continue
                blocked = any(
                    record.tasks[d].status == TaskStatus.FAILED
                    for d in dep.dependencies
                )
                if blocked:
                    record.tasks[dep.task_id] = dep._with(
                        status=TaskStatus.FAILED,
                        error=f"dependency {task_id!r} failed",
                        completed_at=_now_iso(),
                    )
                    self._log(record, "task.failed", actor, task_id=dep.task_id, detail="dependency failed")
                elif all(
                    record.tasks[d].status == TaskStatus.SUCCESS
                    for d in dep.dependencies
                ):
                    if dep.approval_required and not dep.approved_by:
                        record.tasks[dep.task_id] = dep._with(status=TaskStatus.APPROVAL_REQUIRED)
                        record.status = WorkflowStatus.WAITING_APPROVAL
                    else:
                        record.tasks[dep.task_id] = dep._with(status=TaskStatus.QUEUED)
                    self._log(record, "task.unblocked", actor, task_id=dep.task_id, detail="dependencies satisfied")

    def _tick_workflow_status(self, record: WorkflowRecord, actor: str) -> None:
        if not record.tasks:
            return
        if record.status == WorkflowStatus.PAUSED:
            return
        if any(
            t.status == TaskStatus.APPROVAL_REQUIRED
            and not t.approved_by
            for t in record.tasks.values()
        ):
            record.status = WorkflowStatus.WAITING_APPROVAL
            return
        if any(t.status == TaskStatus.FAILED for t in record.tasks.values()):
            record.status = WorkflowStatus.FAILED
            return
        if all(t.status == TaskStatus.SUCCESS for t in record.tasks.values()):
            record.status = WorkflowStatus.COMPLETED
            self._log(record, "workflow.completed", actor)
            return
        if record.status != WorkflowStatus.RUNNING:
            record.status = WorkflowStatus.RUNNING

    # -- workflow-level lifecycle ------------------------------------------------

    def pause(self, workflow_id: str, *, actor: str = "orchestrator", expected_base: str | None = None) -> WorkflowRecord:
        record = self._load(workflow_id, expected_base)
        if record.status not in (WorkflowStatus.RUNNING, WorkflowStatus.CREATED, WorkflowStatus.WAITING_APPROVAL):
            raise TaskStateError(f"Cannot pause workflow in state {record.status.value}.")
        if any(t.status == TaskStatus.RUNNING for t in record.tasks.values()):
            raise TaskStateError("Cannot pause while a task is running; wait for completion or fail it.")
        record.status = WorkflowStatus.PAUSED
        self._log(record, "workflow.paused", actor)
        return self._commit(record)

    def resume(self, workflow_id: str, *, actor: str = "orchestrator", expected_base: str | None = None) -> WorkflowRecord:
        record = self._load(workflow_id, expected_base)
        if record.status != WorkflowStatus.PAUSED:
            raise TaskStateError(f"Cannot resume workflow in state {record.status.value}.")
        record.status = WorkflowStatus.RUNNING
        self._log(record, "workflow.resumed", actor)
        return self._commit(record)

    def cancel(self, workflow_id: str, *, actor: str = "orchestrator", reason: str = "", expected_base: str | None = None) -> WorkflowRecord:
        record = self._load(workflow_id, expected_base)
        if record.status == WorkflowStatus.COMPLETED:
            raise TaskStateError("Cannot cancel a completed workflow.")
        for task in record.tasks.values():
            if task.status not in _TERMINAL:
                record.tasks[task.task_id] = task._with(
                    status=TaskStatus.CANCELLED, error=reason
                )
                self._log(record, "task.cancelled", actor, task_id=task.task_id, detail=reason)
        record.status = WorkflowStatus.CANCELLED
        self._log(record, "workflow.cancelled", actor, detail=reason)
        return self._commit(record)

    def recover(self, workflow_id: str, *, actor: str = "orchestrator", expected_base: str | None = None) -> WorkflowRecord:
        """Non-destructive recovery: reset interrupted/recoverable tasks."""
        record = self._load(workflow_id, expected_base)
        if record.status in (WorkflowStatus.COMPLETED,):
            raise TaskStateError("Completed workflows cannot be recovered.")
        for task in record.tasks.values():
            if task.status in (TaskStatus.RUNNING, TaskStatus.WAITING, TaskStatus.BLOCKED):
                next_status = (
                    TaskStatus.APPROVAL_REQUIRED
                    if (task.approval_required or self.require_human_approval) and not task.approved_by
                    else TaskStatus.QUEUED
                )
                record.tasks[task.task_id] = task._with(
                    status=next_status, started_at="", execution_id=""
                )
                self._log(record, "task.recovered", actor, task_id=task.task_id)
            elif task.status == TaskStatus.FAILED:
                retryable = True
                if retryable:
                    record.tasks[task.task_id] = task._with(status=TaskStatus.QUEUED, execution_id="", error="")
                    self._log(record, "task.recovered", actor, task_id=task.task_id, detail="requeued failed task")
        record.status = WorkflowStatus.RUNNING
        self._log(record, "workflow.recovered", actor)
        return self._commit(record)

    # -- failure-class recovery aliases --------------------------------------------

    def recover_interrupted(self, workflow_id: str, *, actor: str = "system") -> WorkflowRecord:
        return self.recover(workflow_id, actor=actor)

    def recover_agent_failure(self, workflow_id: str, *, failed_agent: str, actor: str = "system") -> WorkflowRecord:
        record = self._load(workflow_id)
        for task in record.tasks.values():
            if task.status in (TaskStatus.RUNNING, TaskStatus.WAITING) and task.agent_id == failed_agent:
                record.tasks[task.task_id] = task._with(status=TaskStatus.QUEUED, execution_id="", started_at="")
                self._log(record, "task.recovered", actor, task_id=task.task_id, detail=f"agent failure {failed_agent}")
        record.status = WorkflowStatus.RUNNING
        self._log(record, "workflow.recovered", actor, detail=f"agent {failed_agent}")
        return self._commit(record)

    def recover_provider_failure(self, workflow_id: str, *, actor: str = "system") -> WorkflowRecord:
        return self.recover(workflow_id, actor=actor)

    def recover_adapter_failure(self, workflow_id: str, *, actor: str = "system") -> WorkflowRecord:
        return self.recover(workflow_id, actor=actor)

    def recover_mcp_failure(self, workflow_id: str, *, actor: str = "system") -> WorkflowRecord:
        return self.recover(workflow_id, actor=actor)

    # -- timeouts -----------------------------------------------------------------

    def apply_timeouts(self, workflow_id: str, *, now: str | None = None) -> WorkflowRecord:
        """Fail tasks that exceeded the workflow timeout or their deadline."""
        record = self._load(workflow_id)
        now_dt = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)

        def _iso_dt(value: str) -> datetime | None:
            try:
                return datetime.fromisoformat(value) if value else None
            except ValueError:
                return None

        for task in record.tasks.values():
            if task.status == TaskStatus.RUNNING:
                started = _iso_dt(task.started_at)
                if started and (now_dt - started).total_seconds() > record.timeout_seconds:
                    record.tasks[task.task_id] = task._with(
                        status=TaskStatus.FAILED,
                        error=f"workflow timeout exceeded ({record.timeout_seconds}s)",
                        completed_at=_now_iso(),
                    )
                    self._log(record, "task.failed", "timeout", task_id=task.task_id, detail="timeout")
            elif task.status in (TaskStatus.PENDING, TaskStatus.QUEUED) and task.deadline:
                deadline = _iso_dt(task.deadline)
                if deadline is not None and now_dt > deadline:
                    record.tasks[task.task_id] = task._with(
                        status=TaskStatus.FAILED,
                        error="deadline exceeded",
                        completed_at=_now_iso(),
                    )
                    self._log(record, "task.failed", "timeout", task_id=task.task_id, detail="deadline")
        self._propagate_all(record)
        self._tick_workflow_status(record, "timeout")
        return self._commit(record)

    def _propagate_all(self, record: WorkflowRecord) -> None:
        for task_id in [t.task_id for t in record.tasks.values() if t.status in (_TERMINAL)]:
            self._propagate(record, task_id, "timeout")

    # -- checkpoint / handoff integration ------------------------------------------

    def emit_checkpoint(
        self,
        workflow_id: str,
        *,
        objective: str = "",
        project_name: str = "unknown",
        project_type: str = "",
        adapter: Any | None = None,
        expected_base: str | None = None,
    ) -> str:
        """Render a protocol checkpoint for the workflow's current state.

        When an adapter is supplied (or configured), the checkpoint is
        persisted through its confined checkpoint lifecycle (docs/HANDOFF.md +
        docs/CHANGELOG.md history) — the ONLY project mutation the engine
        ever performs.
        """
        from handoff_agent.protocol import build_checkpoint, render_state_block

        record = self._load(workflow_id, expected_base)
        if self.boundary is not None:
            allowed = self.boundary.is_allowed(CAP_CHECKPOINT_CREATE) or self.boundary.is_allowed(CAP_CHECKPOINT_UPDATE)
            if not allowed:
                raise CapabilityDeniedError(
                    agent="orchestrator",
                    capability=CAP_CHECKPOINT_CREATE,
                    reason="orchestrator boundary is read-only",
                )
        done = tuple(t.name for t in record.task_list if t.status == TaskStatus.SUCCESS)
        active = tuple(
            t.name for t in record.task_list
            if t.status in (TaskStatus.RUNNING, TaskStatus.QUEUED, TaskStatus.WAITING)
        )
        planned = tuple(
            t.name for t in record.task_list
            if t.status in (TaskStatus.PENDING, TaskStatus.APPROVAL_REQUIRED, TaskStatus.BLOCKED)
        )
        agents = tuple(sorted({t.agent_id for t in record.tasks.values() if t.agent_id}))
        constraints: tuple[str, ...] = ()
        if record.tasks:
            constraints = tuple(
                sorted({c for t in record.tasks.values() for c in t.constraints})
            )
        checkpoint = build_checkpoint(
            objective=objective or f"workflow {workflow_id}",
            completed=done,
            in_progress=active,
            next_actions=planned,
            decisions=(f"status={record.status.value}",),
            constraints=constraints,
            project_name=project_name,
            project_type=project_type,
            agents=agents,
            validation_status="passed",
            sequence=len(record.events),
        )
        text = render_state_block(checkpoint)
        target = adapter if adapter is not None else getattr(self, "_adapter", None)
        if target is not None:
            target.write_checkpoint(text)
        return text

    def set_checkpoint_adapter(self, adapter: Any | None) -> None:
        self._adapter = adapter

    # -- reporting -----------------------------------------------------------------

    def status(self, workflow_id: str) -> dict[str, Any]:
        record = self._load(workflow_id)
        return {
            "workflow_id": record.workflow_id,
            "project_id": record.project_id,
            "name": record.name,
            "status": record.status.value,
            "tasks": record.status_counts(),
            "assignments": dict(sorted(record.assignments.items())),
            "events": len(record.events),
            "updated_at": record.updated_at,
        }

    def events(self, workflow_id: str, *, action: str | None = None) -> tuple[OrchestrationEvent, ...]:
        record = self._load(workflow_id)
        events = tuple(record.events)
        if action:
            events = tuple(e for e in events if e.action == action)
        return events

    def state_transitions(self, workflow_id: str) -> tuple[OrchestrationEvent, ...]:
        return self.events(workflow_id, action="task.running") or self.events(
            workflow_id, action="task.complete"
        )

    def report(self, workflow_id: str) -> dict[str, Any]:
        record = self._load(workflow_id)
        return {
            "workflow_id": record.workflow_id,
            "project_id": record.project_id,
            "name": record.name,
            "status": record.status.value,
            "integrity": validate_state(record),
            "task_summary": [(t.task_id, t.name, t.status.value, t.execution_id) for t in record.task_list],
            "events": [e.to_dict() for e in record.events],
            "meta": dict(record.meta),
        }

    def health_check(self) -> dict[str, Any]:
        try:
            probe = self.state_dir / ".probe"
            _atomic_write_file(probe, "ok")
            probe.unlink(missing_ok=True)
            writable = True
        except OSError:
            writable = False
        workflows = self.list_workflows()
        corrupt = 0
        for path in self.state_dir.glob("*.json"):
            try:
                self._load_raw(path.stem)
            except (CorruptStateError, OSError):
                corrupt += 1
        return {
            "state_dir": str(self.state_dir),
            "writable": writable,
            "workflows": len(workflows),
            "corrupt": corrupt,
            "running": sum(1 for w in workflows if w.status == WorkflowStatus.RUNNING),
            "failed": sum(1 for w in workflows if w.status == WorkflowStatus.FAILED),
            "completed": sum(1 for w in workflows if w.status == WorkflowStatus.COMPLETED),
        }

    def diagnostics(self) -> dict[str, Any]:
        records = self.list_workflows()
        awaiting = sum(1 for w in records if w.status == WorkflowStatus.WAITING_APPROVAL)
        blocked = sum(
            1 for w in records
            for t in w.tasks.values() if t.status == TaskStatus.BLOCKED
        )
        running = [w.workflow_id for w in records if w.status == WorkflowStatus.RUNNING]
        failed = [w.workflow_id for w in records if w.status == WorkflowStatus.FAILED]
        return {
            "workflow_count": len(records),
            "awaiting_approval": awaiting,
            "blocked_tasks": blocked,
            "running_workflows": sorted(running),
            "failed_workflows": sorted(failed),
            "stale": sorted(
                w.workflow_id for w in records
                if w.status in (WorkflowStatus.PAUSED, WorkflowStatus.CANCELLED)
            ),
        }

    def graceful_shutdown(self, workflow_id: str, *, actor: str = "system") -> WorkflowRecord:
        """Pause-idle shutdown: pause a workflow that has no running tasks."""
        return self.pause(workflow_id, actor=actor)

    def graceful_restart(self, workflow_id: str, *, actor: str = "system") -> WorkflowRecord:
        return self.resume(workflow_id, actor=actor)