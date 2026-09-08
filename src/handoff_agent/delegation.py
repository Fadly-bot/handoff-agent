"""Phase 25 — Task Delegation & Agent Routing.

A universal task model plus capability-based, deterministic routing that hands
tasks to registry agents, propagates context/handoffs/correlation ids, and
enforces the security perimeter (no approval bypass, no secret propagation, no
unrestricted Git, no arbitrary agent execution).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from handoff_agent.capability import (
    PermissionBoundary,
    is_known_capability,
)
from handoff_agent.constants import HANDOFF_HOME
from handoff_agent.persistence import _atomic_write_file, _contains_secret_like_content
from handoff_agent.registry import (
    AgentRecord,
    AgentRegistry,
    AvailabilityStatus,
    UnknownAgentError,
)

_DEFAULT_PERMISSIONS = ("checkpoint.create", "checkpoint.update")
_CAP_WRITE = frozenset(_DEFAULT_PERMISSIONS)


class DelegationError(Exception):
    """Base error for delegation and routing."""


class TaskValidationError(DelegationError):
    """A task definition failed validation."""


class UnknownTaskError(DelegationError):
    """No task matches the requested id."""


class DuplicateTaskError(DelegationError):
    """A task with this id already exists."""


class InvalidDependencyError(DelegationError):
    """A dependency references a missing or future task."""


class NoAgentAvailableError(DelegationError):
    """No registry agent matched the routing constraints."""


class AssignmentError(DelegationError):
    """An assignment was rejected by availability, scope, or state."""


class ApprovalRequiredError(DelegationError):
    """Routing/progress requires a human approval."""


class StaleResultError(DelegationError):
    """A submitted result refers to a superseded execution."""


class DuplicateResultError(DelegationError):
    """A result was already accepted for this execution."""


class PermissionDeniedError(DelegationError):
    """The operation violates the permission boundary."""


class CorruptionError(DelegationError):
    """Persisted delegation state is unreadable or invalid."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class TaskLifecycle(Enum):
    CREATED = "created"
    PENDING = "pending"
    ROUTED = "routed"
    ASSIGNED = "assigned"
    CONFIRMED = "confirmed"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    BLOCKED = "blocked"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REFUSED = "refused"
    ESCALATED = "escalated"


_TERMINAL = frozenset(
    {
        TaskLifecycle.SUCCESS,
        TaskLifecycle.FAILED,
        TaskLifecycle.CANCELLED,
    }
)


# ---------------------------------------------------------------------------
# Universal task model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskDefinition:
    """Normalized, validated task model (Phase 25)."""

    task_id: str
    description: str = ""
    task_type: str = "generic"
    parent_task_id: str = ""
    workflow_id: str = ""
    project_id: str = ""
    requester_agent_id: str = ""
    assigned_agent_id: str = ""
    context: str = ""
    requirements: dict[str, str] = field(default_factory=dict)
    constraints: tuple[str, ...] = ()
    priority: int = 5
    deadline: str = ""
    dependencies: tuple[str, ...] = ()
    required_capabilities: frozenset[str] = field(default_factory=frozenset)
    preferred_capabilities: frozenset[str] = field(default_factory=frozenset)
    required_platform: str = ""
    preferred_platform: str = ""
    required_provider: str = ""
    preferred_provider: str = ""
    required_model: str = ""
    preferred_model: str = ""
    permission_scope: frozenset[str] = field(default_factory=frozenset)
    trust_min: int = 1
    approval_required: bool = False
    correlation_id: str = ""
    request_id: str = ""
    created_at: str = ""
    updated_at: str = ""

    def validate(self) -> tuple[bool, list[str]]:
        errors: list[str] = []
        if not self.task_id:
            errors.append("task_id is required")
        if not self.description and not self.context:
            errors.append("description or context is required")
        unknown = sorted(
            c for c in self.required_capabilities | self.preferred_capabilities
            if not is_known_capability(c)
        )
        if unknown:
            errors.append("unknown capability ids: " + ", ".join(unknown))
        if self.task_id in self.dependencies:
            errors.append("a task may not depend on itself")
        if self.deadline and _canonical_value(self.deadline) is None:
            errors.append("deadline must be a valid ISO-8601 timestamp")
        return (not errors, errors)

    def normalized(self) -> "TaskDefinition":
        """Deterministic normalization: sorted sets and trimmed strings."""
        return TaskDefinition(
            task_id=self.task_id.strip() or self.task_id,
            description=self.description.strip(),
            task_type=(self.task_type or "generic").strip(),
            parent_task_id=self.parent_task_id.strip(),
            workflow_id=self.workflow_id.strip(),
            project_id=self.project_id.strip(),
            requester_agent_id=self.requester_agent_id.strip(),
            assigned_agent_id=self.assigned_agent_id.strip(),
            context=self.context,
            requirements={k.strip(): v for k, v in self.requirements.items() if k.strip()},
            constraints=tuple(sorted({c for c in self.constraints if c.strip()})),
            priority=int(self.priority) if self.priority is not None else 5,
            deadline=self.deadline.strip() if isinstance(self.deadline, str) else self.deadline,
            dependencies=tuple(sorted(set(self.dependencies))),
            required_capabilities=frozenset(self.required_capabilities),
            preferred_capabilities=frozenset(
                c for c in self.preferred_capabilities
                if c not in self.required_capabilities
            ),
            required_platform=self.required_platform.strip(),
            preferred_platform=self.preferred_platform.strip(),
            required_provider=self.required_provider.strip(),
            preferred_provider=self.preferred_provider.strip(),
            required_model=self.required_model.strip(),
            preferred_model=self.preferred_model.strip(),
            permission_scope=frozenset(self.permission_scope),
            trust_min=int(self.trust_min) if self.trust_min is not None else 1,
            approval_required=bool(self.approval_required),
            correlation_id=self.correlation_id.strip(),
            request_id=self.request_id.strip(),
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    def with_(self, **changes: Any) -> "TaskDefinition":
        data = self.to_dict()
        data.update(changes)
        return TaskDefinition.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "description": self.description,
            "task_type": self.task_type,
            "parent_task_id": self.parent_task_id,
            "workflow_id": self.workflow_id,
            "project_id": self.project_id,
            "requester_agent_id": self.requester_agent_id,
            "assigned_agent_id": self.assigned_agent_id,
            "context": self.context,
            "requirements": self.requirements,
            "constraints": list(self.constraints),
            "priority": self.priority,
            "deadline": self.deadline,
            "dependencies": list(self.dependencies),
            "required_capabilities": sorted(self.required_capabilities),
            "preferred_capabilities": sorted(self.preferred_capabilities),
            "required_platform": self.required_platform,
            "preferred_platform": self.preferred_platform,
            "required_provider": self.required_provider,
            "preferred_provider": self.preferred_provider,
            "required_model": self.required_model,
            "preferred_model": self.preferred_model,
            "permission_scope": sorted(self.permission_scope),
            "trust_min": self.trust_min,
            "approval_required": self.approval_required,
            "correlation_id": self.correlation_id,
            "request_id": self.request_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskDefinition":
        raw = dict(data)
        return cls(
            task_id=str(raw.get("task_id", "")),
            description=str(raw.get("description", "")),
            task_type=str(raw.get("task_type", "generic")),
            parent_task_id=str(raw.get("parent_task_id", "")),
            workflow_id=str(raw.get("workflow_id", "")),
            project_id=str(raw.get("project_id", "")),
            requester_agent_id=str(raw.get("requester_agent_id", "")),
            assigned_agent_id=str(raw.get("assigned_agent_id", "")),
            context=str(raw.get("context", "")),
            requirements=dict(raw.get("requirements", {})),
            constraints=tuple(raw.get("constraints", ())),
            priority=int(raw.get("priority", 5)),
            deadline=str(raw.get("deadline", "")),
            dependencies=tuple(raw.get("dependencies", ())),
            required_capabilities=frozenset(raw.get("required_capabilities", ())),
            preferred_capabilities=frozenset(raw.get("preferred_capabilities", ())),
            required_platform=str(raw.get("required_platform", "")),
            preferred_platform=str(raw.get("preferred_platform", "")),
            required_provider=str(raw.get("required_provider", "")),
            preferred_provider=str(raw.get("preferred_provider", "")),
            required_model=str(raw.get("required_model", "")),
            preferred_model=str(raw.get("preferred_model", "")),
            permission_scope=frozenset(raw.get("permission_scope", ())),
            trust_min=int(raw.get("trust_min", 1)),
            approval_required=bool(raw.get("approval_required", False)),
            correlation_id=str(raw.get("correlation_id", "")),
            request_id=str(raw.get("request_id", "")),
            created_at=str(raw.get("created_at", "")),
            updated_at=str(raw.get("updated_at", "")),
        )


def _canonical_value(value: str) -> Any:
    try:
        datetime.fromisoformat(value)
        return value
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Routing results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutingDecision:
    """Deterministic, explainable routing outcome."""

    task_id: str
    agent_id: str
    reason: str
    ranked: tuple[str, ...]
    matched: tuple[str, ...] = ()
    at: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "reason": self.reason,
            "ranked": ",".join(self.ranked),
            "at": self.at,
        }


@dataclass(frozen=True)
class ResultAcceptance:
    """Outcome of a submitted task result."""

    task_id: str
    accepted: bool
    status: str
    reason: str
    result: dict[str, Any] = field(default_factory=dict)
    execution_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "accepted": self.accepted,
            "status": self.status,
            "reason": self.reason,
            "execution_id": self.execution_id,
        }


# ---------------------------------------------------------------------------
# Delegator
# ---------------------------------------------------------------------------


class Delegator:
    """Routes and watches delegated tasks against a registry (Phase 25)."""

    def __init__(
        self,
        registry: AgentRegistry,
        *,
        state_dir: str | Path | None = None,
        require_human_approval: bool = False,
        boundary: PermissionBoundary | None = None,
    ) -> None:
        self.registry = registry
        self.state_dir = Path(state_dir or HANDOFF_HOME / "delegation")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.require_human_approval = require_human_approval
        self.boundary = boundary or PermissionBoundary(
            name="delegator", allowed=_CAP_WRITE
        )
        self._tasks: dict[str, TaskDefinition] = {}
        self._status: dict[str, str] = {}
        self._execution: dict[str, str] = {}
        self._results: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, str]] = []
        self._load()

    # -- persistence --------------------------------------------------------

    @property
    def path(self) -> Path:
        return self.state_dir / "delegations.json"

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise CorruptionError("delegation state is not an object")
            for task_id, raw in data.get("tasks", {}).items():
                definition = TaskDefinition.from_dict(raw)
                self._tasks[task_id] = definition
            self._status = dict(data.get("status", {}))
            self._execution = dict(data.get("execution", {}))
            self._results = {k: dict(v) for k, v in data.get("results", {}).items()}
            self._events = [dict(e) for e in data.get("events", [])]
        except (OSError, ValueError, TypeError, KeyError, CorruptionError) as exc:
            if isinstance(exc, CorruptionError):
                raise
            raise CorruptionError(f"could not load delegation state: {exc}") from exc

    def _save(self) -> None:
        payload = {
            "tasks": {t.task_id: t.to_dict() for t in self._tasks.values()},
            "status": dict(self._status),
            "execution": dict(self._execution),
            "results": self._results,
            "events": list(self._events),
        }
        _atomic_write_file(self.path, _canonical(payload))

    def _log(self, action: str, actor: str, task_id: str, detail: str = "") -> None:
        event = {
            "timestamp": _now_iso(),
            "actor": actor or "system",
            "action": action,
            "task_id": task_id,
            "detail": detail,
        }
        secret_scan = _canonical(event)
        if _contains_secret_like_content(secret_scan):
            event = {**event, "detail": "[redacted]"}
        self._events.append(event)
        self._save()

    # -- task lifecycle -----------------------------------------------------

    def _check(self, capability: str) -> None:
        self.boundary.check(capability)

    def create_task(self, definition: TaskDefinition | None = None, *, actor: str = "requester", **fields: Any) -> TaskDefinition:
        """Validate, normalize, and persist a task."""
        self._check("checkpoint.create")
        definition = definition or TaskDefinition(**fields)
        definition = definition.normalized()
        if not definition.created_at:
            definition = definition.with_(created_at=_now_iso(), updated_at=_now_iso())
        else:
            definition = definition.with_(updated_at=_now_iso())
        ok, errors = definition.validate()
        if not ok:
            raise TaskValidationError("; ".join(errors))
        if definition.task_id in self._tasks:
            raise DuplicateTaskError(f"Task {definition.task_id!r} already exists.")
        for dep in definition.dependencies:
            if dep not in self._tasks:
                raise InvalidDependencyError(
                    f"Dependency {dep!r} does not exist for {definition.task_id!r}."
                )
        secret_scan = _canonical(definition.to_dict())
        if _contains_secret_like_content(secret_scan):
            raise TaskValidationError("task definition contains secret-like values.")
        self._tasks[definition.task_id] = definition
        self._status[definition.task_id] = TaskLifecycle.CREATED.value
        self._execution[definition.task_id] = ""
        self._results[definition.task_id] = {}
        self._log("task.created", actor, definition.task_id)
        return definition

    def get_task(self, task_id: str) -> TaskDefinition:
        if task_id not in self._tasks:
            raise UnknownTaskError(f"Task {task_id!r} is unknown.")
        return self._tasks[task_id]

    def list_tasks(self) -> list[TaskDefinition]:
        return sorted(self._tasks.values(), key=lambda t: (t.priority, t.task_id))

    def status_of(self, task_id: str) -> str:
        return self._status.get(task_id, TaskLifecycle.CREATED.value)

    # -- decomposition ------------------------------------------------------

    def decompose(
        self, parent_task_id: str, subtasks: Iterable[TaskDefinition], *, actor: str = "orchestrator"
    ) -> list[TaskDefinition]:
        """Split a task into subtasks with a linear dependency chain."""
        parent = self.get_task(parent_task_id)
        created: list[TaskDefinition] = []
        prior = ""
        for index, subtask in enumerate(subtasks, start=1):
            subtask = subtask.normalized().with_(
                parent_task_id=parent.task_id,
                workflow_id=parent.workflow_id,
                project_id=parent.project_id,
                requester_agent_id=parent.requester_agent_id,
                correlation_id=parent.correlation_id or parent.task_id,
                request_id=parent.request_id or parent.task_id,
                dependencies=((prior,)) if prior else (),
                task_id=subtask.task_id or f"{parent.task_id}-{index}",
            )
            created.append(self.create_task(subtask, actor=actor))
            prior = subtask.task_id
        self._log("task.decomposed", actor, parent_task_id, f"count={len(created)}")
        return created

    # -- routing ------------------------------------------------------------

    def route(self, task_id: str, *, actor: str = "orchestrator") -> RoutingDecision:
        """Capability-based, deterministic routing to the best available agent."""
        task = self.get_task(task_id)
        if task.approval_required and not task.assigned_agent_id:
            if self.require_human_approval:
                self._status[task_id] = TaskLifecycle.WAITING_APPROVAL.value
                self._log("task.approval_gate", actor, task_id)
                self._save()
                raise ApprovalRequiredError(
                    f"Task {task_id!r} requires human approval before routing."
                )
        self.registry.detect_stale()
        required_caps = task.required_capabilities or {c for c in task.permission_scope}
        ranked = self.registry.filter(
            capabilities=required_caps,
            permission=task.permission_scope or None,
            provider=task.required_provider or None,
            platform=task.required_platform or None,
            model=task.required_model or None,
            trust_min=task.trust_min or None,
            task_type=task.task_type,
            project_id=task.project_id or None,
            available_only=True,
        )
        if task.preferred_provider or task.preferred_platform or task.preferred_model or task.preferred_capabilities:
            preferred = self.registry.filter(
                capabilities=task.required_capabilities,
                provider=task.preferred_provider or None,
                platform=task.preferred_platform or None,
                model=task.preferred_model or None,
                available_only=True,
            )
            pool = _dedupe_by_id(
                self.registry.rank(preferred) + self.registry.rank(ranked)
            )
        else:
            pool = self.registry.rank(ranked)
        if not pool:
            raise NoAgentAvailableError(
                f"No available agent satisfies routing for task {task_id!r}."
            )
        chosen = pool[0]
        decision = RoutingDecision(
            task_id=task_id,
            agent_id=chosen.agent_id,
            reason=_routing_reason(task, chosen),
            ranked=tuple(a.agent_id for a in pool),
            at=_now_iso(),
        )
        self._status[task_id] = TaskLifecycle.ROUTED.value
        self._log(
            "task.routed", actor, task_id,
            detail=f"agent={chosen.agent_id} via {decision.reason}",
        )
        self._save()
        return decision

    # -- assignment ---------------------------------------------------------

    def assign(
        self,
        task_id: str,
        agent_id: str,
        *,
        actor: str = "orchestrator",
        require_confirmation: bool = True,
    ) -> TaskDefinition:
        """Assign a routed task to a specific agent (scope/permission checked)."""
        task = self.get_task(task_id)
        try:
            record = self.registry.get(agent_id)
        except UnknownAgentError as exc:
            raise AssignmentError(f"Agent {agent_id!r} is not registered.") from exc
        health = self.registry.health_check(agent_id)
        if not health["ok"]:
            raise AssignmentError(
                f"Agent {agent_id!r} is not healthy: {health['status']}."
            )
        _ensure_scope(task, record)
        required_caps = task.required_capabilities or {c for c in task.permission_scope} or {"checkpoint.read"}
        if not required_caps.issubset(record.capabilities):
            raise AssignmentError(
                f"Agent {agent_id!r} lacks required capabilities {sorted(required_caps)}."
            )
        updated = task.with_(
            assigned_agent_id=agent_id,
            updated_at=_now_iso(),
        )
        self._tasks[task_id] = updated
        self._status[task_id] = (
            TaskLifecycle.ASSIGNED.value
            if require_confirmation
            else TaskLifecycle.CONFIRMED.value
        )
        self._log("task.assigned", actor, task_id, detail=agent_id)
        self._save()
        return updated

    def confirm_assignment(self, task_id: str, agent_id: str, *, actor: str | None = None) -> TaskDefinition:
        task = self.get_task(task_id)
        if task.assigned_agent_id != agent_id:
            raise AssignmentError(
                f"Task {task_id!r} is not assigned to {agent_id!r}."
            )
        if self._status[task_id] != TaskLifecycle.ASSIGNED.value:
            raise AssignmentError(
                f"Task {task_id!r} is not awaiting confirmation."
            )
        self._status[task_id] = TaskLifecycle.CONFIRMED.value
        self._log("task.assigned_confirmed", actor or agent_id, task_id, detail=agent_id)
        self._save()
        return task

    def reject_assignment(self, task_id: str, agent_id: str, *, actor: str | None = None, reason: str = "") -> TaskDefinition:
        task = self.get_task(task_id)
        if task.assigned_agent_id != agent_id:
            raise AssignmentError(
                f"Task {task_id!r} is not assigned to {agent_id!r}."
            )
        self._status[task_id] = TaskLifecycle.PENDING.value
        self._tasks[task_id] = task.with_(assigned_agent_id="", updated_at=_now_iso())
        self._log("task.assigned_rejected", actor or agent_id, task_id, detail=reason)
        self._save()
        return self._tasks[task_id]

    def accept_task(self, task_id: str, *, agent_id: str, execution_id: str = "") -> TaskDefinition:
        self._check("checkpoint.update")
        task = self.get_task(task_id)
        if task.assigned_agent_id and task.assigned_agent_id != agent_id:
            raise AssignmentError(
                f"Agent {agent_id!r} is not the assigned agent."
            )
        if self._status[task_id] not in (
            TaskLifecycle.CONFIRMED.value,
            TaskLifecycle.ROUTED.value,
            TaskLifecycle.PENDING.value,
            TaskLifecycle.ASSIGNED.value,
        ):
            raise AssignmentError(
                f"Task {task_id!r} cannot move to running from {self._status[task_id]!r}."
            )
        if not execution_id:
            execution_id = _new_execution_id(agent_id)
        self._execution[task_id] = execution_id
        self._status[task_id] = TaskLifecycle.RUNNING.value
        self._log("task.running", agent_id, task_id, detail=execution_id)
        self._save()
        return task

    def refuse_task(self, task_id: str, *, agent_id: str, reason: str = "") -> TaskDefinition:
        task = self.get_task(task_id)
        if task.assigned_agent_id and task.assigned_agent_id != agent_id:
            raise AssignmentError(f"Agent {agent_id!r} cannot refuse this task.")
        self._status[task_id] = TaskLifecycle.REFUSED.value
        self._log("task.refused", agent_id, task_id, detail=reason)
        self._save()
        return task

    # -- approval / blocked -------------------------------------------------

    def require_approval(self, task_id: str, *, actor: str = "orchestrator") -> TaskDefinition:
        task = self.get_task(task_id)
        if not task.approval_required:
            task = task.with_(approval_required=True, updated_at=_now_iso())
            self._tasks[task_id] = task
        self._status[task_id] = TaskLifecycle.WAITING_APPROVAL.value
        self._log("task.approval_required", actor, task_id)
        self._save()
        return task

    def approve(self, task_id: str, *, human: str) -> TaskDefinition:
        task = self.get_task(task_id)
        if not human:
            raise ApprovalRequiredError("Human approval requires a named human.")
        if self._status[task_id] != TaskLifecycle.WAITING_APPROVAL.value:
            if not task.approval_required:
                raise ApprovalRequiredError(f"Task {task_id!r} does not require approval.")
        self._status[task_id] = (
            TaskLifecycle.PENDING.value
            if not task.assigned_agent_id
            else TaskLifecycle.CONFIRMED.value
        )
        self._log("task.approved", human, task_id)
        self._save()
        return task

    def reject_approval(self, task_id: str, *, human: str, reason: str = "") -> TaskDefinition:
        task = self.get_task(task_id)
        if not human:
            raise ApprovalRequiredError("Rejection requires a named human.")
        self._status[task_id] = TaskLifecycle.CANCELLED.value
        self._log("task.approval_rejected", human, task_id, detail=reason)
        self._save()
        return task

    def block(self, task_id: str, *, actor: str = "orchestrator", reason: str = "") -> TaskDefinition:
        task = self.get_task(task_id)
        if self._status[task_id] in _TERMINAL_VALUE:
            raise AssignmentError(f"Task {task_id!r} is terminal.")
        self._status[task_id] = TaskLifecycle.BLOCKED.value
        self._log("task.blocked", actor, task_id, detail=reason)
        self._save()
        return task

    def unblock(self, task_id: str, *, actor: str = "orchestrator") -> TaskDefinition:
        task = self.get_task(task_id)
        if self._status[task_id] != TaskLifecycle.BLOCKED.value:
            raise AssignmentError(f"Task {task_id!r} is not blocked.")
        self._status[task_id] = (
            TaskLifecycle.WAITING_APPROVAL.value
            if task.approval_required
            else TaskLifecycle.PENDING.value
        )
        self._log("task.unblocked", actor, task_id)
        self._save()
        return task

    # -- results ------------------------------------------------------------

    def submission_in_progress_ok(self, task_id: str) -> bool:
        return self._status.get(task_id) in (
            TaskLifecycle.RUNNING.value,
            TaskLifecycle.BLOCKED.value,
            TaskLifecycle.WAITING_APPROVAL.value,
        )

    def submit_result(
        self,
        task_id: str,
        *,
        agent_id: str,
        result: Mapping[str, Any],
        execution_id: str,
        previous: Mapping[str, Any] | None = None,
        partial: bool = False,
    ) -> ResultAcceptance:
        """Validate, dedupe, and accept a structured task result."""
        self._check("checkpoint.update")
        task = self.get_task(task_id)
        if task.assigned_agent_id and task.assigned_agent_id != agent_id:
            return ResultAcceptance(
                task_id, False, self._status[task_id],
                f"agent {agent_id!r} is not the assigned agent", {},
            )
        current_execution = self._execution.get(task_id, "")
        if current_execution != execution_id:
            raise StaleResultError(
                f"Result for {task_id!r} references execution {execution_id!r}; "
                f"current is {current_execution!r}."
            )
        payload = dict(result)
        secret_scan = _canonical(payload)
        if _contains_secret_like_content(secret_scan):
            raise DelegationError(
                f"Result for {task_id!r} contains secret-like values."
            )
        status = self._status[task_id]
        if status == TaskLifecycle.SUCCESS.value:
            existing = self._results.get(task_id, {})
            if existing == payload:
                return ResultAcceptance(
                    task_id, True, TaskLifecycle.SUCCESS.value,
                    "duplicate result (idempotent)", payload, execution_id,
                )
            raise DuplicateResultError(
                f"Conflicting duplicate-submission result for {task_id!r}."
            )
        if status != TaskLifecycle.RUNNING.value:
            raise AssignmentError(
                f"Task {task_id!r} cannot accept results in state {status!r}."
            )
        if partial:
            merged = {
                **(self._results.get(task_id) or {}),
                **(dict(previous or {})),
                **payload,
            }
            self._results[task_id] = merged
            self._log("task.partial", agent_id, task_id, detail=f"fields={len(payload)}")
            self._save()
            return ResultAcceptance(
                task_id, True, TaskLifecycle.RUNNING.value,
                "partial result accepted", merged, execution_id,
            )
        self._results[task_id] = payload
        self._status[task_id] = TaskLifecycle.SUCCESS.value
        self._log("task.result_accepted", agent_id, task_id, detail=execution_id)
        self._propagate_success(task_id, agent_id)
        self._save()
        return ResultAcceptance(
            task_id, True, TaskLifecycle.SUCCESS.value, "accepted", payload, execution_id,
        )

    def reject_result(self, task_id: str, reason: str, *, actor: str = "orchestrator") -> TaskDefinition:
        task = self.get_task(task_id)
        self._results[task_id] = {}
        self._status[task_id] = TaskLifecycle.FAILED.value
        self._log("task.result_rejected", actor, task_id, detail=reason)
        self._save()
        return task

    # -- failure handling ---------------------------------------------------

    def fail_task(self, task_id: str, reason: str, *, actor: str = "system") -> TaskDefinition:
        task = self.get_task(task_id)
        self._results[task_id] = {}
        self._status[task_id] = TaskLifecycle.FAILED.value
        self._log("task.failed", actor, task_id, detail=reason)
        self._propagate_failure(task_id, reason, actor)
        self._save()
        return task

    def retry(self, task_id: str, *, actor: str = "orchestrator") -> TaskDefinition:
        task = self.get_task(task_id)
        allowed = {
            TaskLifecycle.FAILED.value,
            TaskLifecycle.CANCELLED.value,
            TaskLifecycle.REFUSED.value,
            TaskLifecycle.BLOCKED.value,
        }
        if self._status[task_id] not in allowed:
            raise AssignmentError(
                f"Task {task_id!r} cannot be retried from {self._status[task_id]!r}."
            )
        self._execution[task_id] = ""
        self._results[task_id] = {}
        self._status[task_id] = (
            TaskLifecycle.WAITING_APPROVAL.value
            if task.approval_required and self.require_human_approval
            else TaskLifecycle.PENDING.value
        )
        self._log("task.retry", actor, task_id)
        self._save()
        return task

    def reassign(
        self, task_id: str, agent_id: str, *, actor: str = "orchestrator"
    ) -> TaskDefinition:
        """Reassign a failed/cancelled task to a different available agent."""
        task = self.get_task(task_id)
        if self._status[task_id] not in (
            TaskLifecycle.FAILED.value,
            TaskLifecycle.CANCELLED.value,
            TaskLifecycle.REFUSED.value,
        ):
            raise AssignmentError(
                f"Task {task_id!r} is not eligible for reassignment."
            )
        updated = task.with_(assigned_agent_id=agent_id, updated_at=_now_iso())
        self._tasks[task_id] = updated
        self._execution[task_id] = ""
        self._results[task_id] = {}
        self._status[task_id] = TaskLifecycle.ASSIGNED.value
        self._log("task.reassigned", actor, task_id, detail=agent_id)
        self._save()
        return updated

    def reassign_failed_agent(self, task_id: str, *, failed_agent: str, actor: str = "orchestrator") -> TaskDefinition:
        task = self.get_task(task_id)
        if task.assigned_agent_id and task.assigned_agent_id != failed_agent:
            raise AssignmentError(
                f"Task {task_id!r} was assigned to {task.assigned_agent_id!r}, "
                f"not failed agent {failed_agent!r}."
            )
        candidates = self.registry.filter(
            capabilities=task.required_capabilities or {c for c in task.permission_scope} or {"checkpoint.read"},
            provider=task.required_provider or None,
            available_only=True,
        )
        alternatives = [a for a in candidates if a.agent_id != failed_agent]
        if not alternatives:
            self.escalate(task_id, human=True, reason=f"no alternative to {failed_agent}")
            raise NoAgentAvailableError(
                f"No alternative to failed agent {failed_agent!r}; task escalated."
            )
        return self.reassign(task_id, alternatives[0].agent_id, actor=actor)

    def reassign_unavailable_agent(self, task_id: str, *, unavailable_agent: str, actor: str = "orchestrator") -> TaskDefinition:
        return self.reassign_failed_agent(
            task_id, failed_agent=unavailable_agent, actor=actor
        )

    def escalate(self, task_id: str, *, human: bool = False, reason: str = "") -> TaskDefinition:
        task = self.get_task(task_id)
        self._status[task_id] = TaskLifecycle.ESCALATED.value
        detail = f"human={human} {reason}".strip()
        self._log("task.escalated", "system", task_id, detail=detail)
        self._save()
        return task

    def timeout(self, task_id: str, *, actor: str = "system") -> TaskDefinition:
        task = self.get_task(task_id)
        self._status[task_id] = TaskLifecycle.FAILED.value
        self._log("task.timeout", actor, task_id)
        self._propagate_failure(task_id, "timeout", actor)
        self._save()
        return task

    def cancel(self, task_id: str, *, actor: str = "system", reason: str = "") -> TaskDefinition:
        task = self.get_task(task_id)
        if self._status[task_id] in _TERMINAL_VALUE:
            raise AssignmentError(f"Task {task_id!r} is already terminal.")
        self._status[task_id] = TaskLifecycle.CANCELLED.value
        self._log("task.cancelled", actor, task_id, detail=reason)
        self._save()
        return task

    # -- propagation / handoff ---------------------------------------------

    def _propagate_success(self, task_id: str, agent_id: str) -> None:
        for candidate_id, candidate in self._tasks.items():
            if task_id not in candidate.dependencies:
                continue
            deps = candidate.dependencies
            if all(self._status.get(d) == TaskLifecycle.SUCCESS.value for d in deps):
                self._status[candidate_id] = (
                    TaskLifecycle.WAITING_APPROVAL.value
                    if candidate.approval_required
                    else TaskLifecycle.PENDING.value
                )
                self._log("task.unblocked", agent_id, candidate_id, detail="dependencies satisfied")

    def _propagate_failure(self, task_id: str, reason: str, actor: str) -> None:
        for candidate_id, candidate in self._tasks.items():
            if task_id in candidate.dependencies:
                self._status[candidate_id] = TaskLifecycle.FAILED.value
                self._log("task.failed", actor, candidate_id, detail=f"dependency {task_id} failed")

    def checkpoint_propagation(self, task_id: str) -> dict[str, Any]:
        """Assemble the context/handoff checkpoint fragment for a task."""
        task = self.get_task(task_id)
        children = sorted(
            t.task_id for t in self._tasks.values() if t.parent_task_id == task_id
        )
        return {
            "task_id": task.task_id,
            "task_type": task.task_type,
            "parent_task_id": task.parent_task_id,
            "workflow_id": task.workflow_id,
            "project_id": task.project_id,
            "context": task.context,
            "requirements": task.requirements,
            "constraints": task.constraints,
            "correlation_id": task.correlation_id,
            "request_id": task.request_id,
            "assigned_agent_id": task.assigned_agent_id,
            "subtasks": children,
            "status": self._status[task_id],
        }

    def execution_handoff(self, task_id: str) -> dict[str, Any]:
        self._check("checkpoint.create")
        from handoff_agent.protocol import build_checkpoint, render_state_block
        task = self.get_task(task_id)
        checkpoint = build_checkpoint(
            objective=task.description or "delegated task",
            completed=(
                tuple(
                    t.task_id for t in self._tasks.values()
                    if self._status.get(t.task_id) == TaskLifecycle.SUCCESS.value
                )
            ),
            in_progress=(task_id,),
            next_actions=(
                tuple(
                    t.task_id for t in self._tasks.values()
                    if self._status.get(t.task_id) == TaskLifecycle.PENDING.value
                )
            ),
            decisions=(f"assigned_agent={task.assigned_agent_id or ''}",),
            constraints=task.constraints,
            project_name=task.project_id or "unknown",
            project_type=task.task_type,
            agents=(task.assigned_agent_id,) if task.assigned_agent_id else (),
            validation_status="passed",
            sequence=1,
        )
        return {
            "handoff": render_state_block(checkpoint),
            "correlation_id": task.correlation_id,
            "request_id": task.request_id,
            "workflow_id": task.workflow_id,
        }

    # -- reporting / health -------------------------------------------------

    def report(self, task_id: str | None = None) -> dict[str, Any]:
        target = [task_id] if task_id else list(self._tasks)
        by_status: dict[str, int] = {}
        for tid in target:
            status = self._status.get(tid, "created")
            by_status[status] = by_status.get(status, 0) + 1
        return {
            "task_id": task_id or "ALL",
            "count": len(target),
            "by_status": by_status,
            "audit_events": len(self._events),
        }

    def diagnostics(self) -> dict[str, Any]:
        failures = [
            tid for tid, status in self._status.items()
            if status == TaskLifecycle.FAILED.value
        ]
        return {
            "path": str(self.path),
            "tasks": len(self._tasks),
            "running": sum(
                1 for s in self._status.values() if s == TaskLifecycle.RUNNING.value
            ),
            "failed": failures,
            "escalated": [
                tid for tid, status in self._status.items()
                if status == TaskLifecycle.ESCALATED.value
            ],
        }

    def health_check(self) -> dict[str, Any]:
        unhealthy = [
            tid for tid, status in self._status.items()
            if status in (TaskLifecycle.FAILED.value, TaskLifecycle.CANCELLED.value)
        ]
        return {
            "ok": not unhealthy,
            "tasks": len(self._tasks),
            "failed": len(unhealthy),
            "registry_alive": self.registry.health_report()["ok"],
        }

    def events(self) -> tuple[dict[str, str], ...]:
        return tuple(dict(e) for e in self._events)


_TERMINAL_VALUE = {s.value for s in _TERMINAL}


def _dedupe_by_id(records: Iterable[AgentRecord]) -> list[AgentRecord]:
    seen: set[str] = set()
    unique: list[AgentRecord] = []
    for record in records:
        if record.agent_id in seen:
            continue
        seen.add(record.agent_id)
        unique.append(record)
    return unique


def _new_execution_id(agent_id: str) -> str:
    import hashlib
    nonce = f"{agent_id}{_now_iso()}"
    return "exec-" + hashlib.sha256(nonce.encode("utf-8")).hexdigest()[:16]


def _routing_reason(task: TaskDefinition, agent: AgentRecord) -> str:
    parts = ["required_capabilities"]
    if task.preferred_capabilities:
        parts.append("preferred_capabilities")
    if task.required_provider or task.preferred_provider:
        parts.append("provider")
    if task.trust_min > 1:
        parts.append("trust")
    return "matched: " + ", ".join(dict.fromkeys(parts))


def _ensure_scope(task: TaskDefinition, record: AgentRecord) -> None:
    if record.project_scope and task.project_id and task.project_id not in record.project_scope:
        raise AssignmentError(
            f"Agent {record.agent_id!r} is not scoped to project {task.project_id!r}."
        )
    if record.task_scope and task.task_type not in record.task_scope:
        raise AssignmentError(
            f"Agent {record.agent_id!r} cannot handle tasks of type {task.task_type!r}."
        )