"""Phase 27 — Advanced Multi-Agent Workflow Engine.

A universal workflow definition engine with templates, triggers, branching,
decision/approval/agent/task nodes, parallel/sequential execution, loop
control, dependency scheduling, capability-based routing, recovery, and
security enforcement. Integrates with Handoff, CHANGELOG, Agent Registry,
Delegation, and Messaging.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from handoff_agent.capability import is_known_capability
from handoff_agent.constants import HANDOFF_HOME
from handoff_agent.persistence import _atomic_write_file, _contains_secret_like_content

WORKFLOW_PROTOCOL_VERSION = "1"
_DEADLOCK_DEPTH = 20


class WorkflowEngineError(Exception):
    """Base error for the workflow engine."""


class WorkflowDefinitionError(WorkflowEngineError):
    """A workflow definition failed validation."""


class UnknownWorkflowError(WorkflowEngineError):
    """No workflow matches the requested id."""


class DuplicateWorkflowError(WorkflowEngineError):
    """A workflow with this id already exists."""


class UnknownExecutionError(WorkflowEngineError):
    """No execution matches the requested id."""


class WorkflowStateError(WorkflowEngineError):
    """An illegal workflow state transition was attempted."""


class WorkflowPausedError(WorkflowEngineError):
    """The workflow is paused and cannot accept this operation."""


class DependencyError(WorkflowEngineError):
    """A dependency is missing, circular, or unsorted."""


class CircularDependencyError(DependencyError):
    """A circular dependency was detected."""


class NoNodeAvailableError(WorkflowEngineError):
    """No node can be scheduled for execution."""


class NodeUnavailableError(WorkflowEngineError):
    """The target node does not exist or is in an invalid state."""


class ApprovalRequiredError(WorkflowEngineError):
    """The workflow requires a human approval before continuing."""


class TimeoutError(WorkflowEngineError):
    """The workflow or node exceeded its configured timeout."""


class DeadlockDetectedError(WorkflowEngineError):
    """The workflow graph has a deadlock (non-schedulable cycle)."""


class DuplicateExecutionError(WorkflowEngineError):
    """A workflow execution already exists for this definition."""


class CorruptionError(WorkflowEngineError):
    """Persisted workflow state is unreadable or invalid."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _make_workflow_id(name: str, version: str, timestamp: str) -> str:
    raw = f"{name}:{version}:{timestamp}"
    return "wf-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _make_execution_id(workflow_id: str, timestamp: str) -> str:
    raw = f"{workflow_id}:{timestamp}"
    return "exec-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class WorkflowStatus(Enum):
    DRAFT = "draft"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    RECOVERING = "recovering"


class WorkflowTrigger(Enum):
    MANUAL = "manual"
    EVENT = "event"
    TASK = "task"
    SCHEDULED = "scheduled"


class NodeType(Enum):
    START = "start"
    END = "end"
    DECISION = "decision"
    CONDITIONAL = "conditional"
    APPROVAL = "approval"
    AGENT = "agent"
    TASK = "task"
    PARALLEL = "parallel"
    SEQUENTIAL = "sequential"
    MERGE = "merge"
    LOOP = "loop"
    RETRY = "retry"
    TIMEOUT = "timeout"
    FAILURE = "failure"
    RECOVERY = "recovery"
    HUMAN_APPROVAL = "human_approval"


class NodeStatus(Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    WAITING = "waiting"


# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NodeDefinition:
    """A single node in a workflow graph."""

    node_id: str
    node_type: str
    name: str = ""
    description: str = ""
    next_nodes: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    agent_id: str = ""
    capability: str = ""
    task_id: str = ""
    condition: str = ""
    decision_branches: tuple[str, ...] = ()
    timeout_seconds: int = 0
    max_retries: int = 0
    parallel_count: int = 1
    loop_condition: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def with_(self, **changes: Any) -> "NodeDefinition":
        data = self.to_dict()
        data.update(changes)
        return NodeDefinition.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "name": self.name,
            "description": self.description,
            "next_nodes": list(self.next_nodes),
            "dependencies": list(self.dependencies),
            "agent_id": self.agent_id,
            "capability": self.capability,
            "task_id": self.task_id,
            "condition": self.condition,
            "decision_branches": list(self.decision_branches),
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "parallel_count": self.parallel_count,
            "loop_condition": self.loop_condition,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "NodeDefinition") -> "NodeDefinition":
        if isinstance(data, cls):
            return data
        raw = dict(data)
        return cls(
            node_id=str(raw.get("node_id", "")),
            node_type=str(raw.get("node_type", "task")),
            name=str(raw.get("name", "")),
            description=str(raw.get("description", "")),
            next_nodes=tuple(raw.get("next_nodes", ())),
            dependencies=tuple(raw.get("dependencies", ())),
            agent_id=str(raw.get("agent_id", "")),
            capability=str(raw.get("capability", "")),
            task_id=str(raw.get("task_id", "")),
            condition=str(raw.get("condition", "")),
            decision_branches=tuple(raw.get("decision_branches", ())),
            timeout_seconds=int(raw.get("timeout_seconds", 0)),
            max_retries=int(raw.get("max_retries", 0)),
            parallel_count=int(raw.get("parallel_count", 1)),
            loop_condition=str(raw.get("loop_condition", "")),
            payload=dict(raw.get("payload", {})),
        )


@dataclass(frozen=True)
class WorkflowDefinition:
    """Universal workflow definition."""

    workflow_id: str
    name: str
    version: str = "1"
    description: str = ""
    trigger: str = WorkflowTrigger.MANUAL.value
    schedule: str = ""
    event_type: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)
    nodes: tuple[NodeDefinition, ...] = field(default_factory=tuple)
    start_node: str = ""
    end_nodes: tuple[str, ...] = ()
    timeout_seconds: int = 0
    max_concurrency: int = 4
    created_at: str = ""
    updated_at: str = ""
    template: str = ""

    def with_(self, **changes: Any) -> "WorkflowDefinition":
        data = self.to_dict()
        data.update(changes)
        return WorkflowDefinition.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "trigger": self.trigger,
            "schedule": self.schedule,
            "event_type": self.event_type,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "variables": dict(self.variables),
            "nodes": [n.to_dict() for n in self.nodes],
            "start_node": self.start_node,
            "end_nodes": list(self.end_nodes),
            "timeout_seconds": self.timeout_seconds,
            "max_concurrency": self.max_concurrency,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "template": self.template,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "WorkflowDefinition") -> "WorkflowDefinition":
        if isinstance(data, cls):
            return data
        raw = dict(data)
        nodes = tuple(NodeDefinition.from_dict(n) for n in raw.get("nodes", ()))
        return cls(
            workflow_id=str(raw.get("workflow_id", "")),
            name=str(raw.get("name", "")),
            version=str(raw.get("version", "1")),
            description=str(raw.get("description", "")),
            trigger=str(raw.get("trigger", WorkflowTrigger.MANUAL.value)),
            schedule=str(raw.get("schedule", "")),
            event_type=str(raw.get("event_type", "")),
            input_schema=dict(raw.get("input_schema", {})),
            output_schema=dict(raw.get("output_schema", {})),
            variables=dict(raw.get("variables", {})),
            nodes=nodes,
            start_node=str(raw.get("start_node", "")),
            end_nodes=tuple(raw.get("end_nodes", ())),
            timeout_seconds=int(raw.get("timeout_seconds", 0)),
            max_concurrency=int(raw.get("max_concurrency", 4)),
            created_at=str(raw.get("created_at", "")),
            updated_at=str(raw.get("updated_at", "")),
            template=str(raw.get("template", "")),
        )

    def validate(self) -> tuple[bool, list[str]]:
        errors: list[str] = []
        if not self.workflow_id:
            errors.append("workflow_id is required")
        if not self.name:
            errors.append("name is required")
        try:
            WorkflowTrigger(self.trigger)
        except ValueError:
            errors.append(f"unknown trigger: {self.trigger!r}")
        if not self.nodes:
            errors.append("workflow must have at least one node")
        if not self.start_node:
            errors.append("start_node is required")
        node_ids = {n.node_id for n in self.nodes}
        if self.start_node not in node_ids:
            errors.append(f"start_node {self.start_node!r} does not exist")
        for n in self.nodes:
            try:
                NodeType(n.node_type)
            except ValueError:
                errors.append(f"node {n.node_id!r} has unknown type {n.node_type!r}")
            for nxt in n.next_nodes:
                if nxt not in node_ids:
                    errors.append(f"node {n.node_id!r} next_node {nxt!r} does not exist")
            for dep in n.dependencies:
                if dep not in node_ids:
                    errors.append(f"node {n.node_id!r} dependency {dep!r} does not exist")
            if n.capability and not is_known_capability(n.capability):
                errors.append(f"node {n.node_id!r} has unknown capability {n.capability!r}")
            if n.timeout_seconds < 0:
                errors.append(f"node {n.node_id!r} timeout must be non-negative")
            if n.max_retries < 0:
                errors.append(f"node {n.node_id!r} max_retries must be non-negative")
            if n.parallel_count < 1:
                errors.append(f"node {n.node_id!r} parallel_count must be >= 1")
        if self.timeout_seconds < 0:
            errors.append("workflow timeout must be non-negative")
        if self.max_concurrency < 1:
            errors.append("max_concurrency must be >= 1")
        secret_scan = _canonical(self.to_dict())
        if _contains_secret_like_content(secret_scan):
            errors.append("workflow contains secret-like values")
        return (not errors, errors)


# ---------------------------------------------------------------------------
# Workflow templates
# ---------------------------------------------------------------------------

def _node(node_id: str, node_type: str, **kwargs: Any) -> NodeDefinition:
    return NodeDefinition(node_id=node_id, node_type=node_type, **kwargs)


def build_workflow_template(template: str, *, workflow_id: str = "", **variables: Any) -> WorkflowDefinition:
    """Build a workflow from a named template."""
    if template == "linear":
        return _template_linear(workflow_id, **variables)
    if template == "approval_gate":
        return _template_approval_gate(workflow_id, **variables)
    if template == "parallel":
        return _template_parallel(workflow_id, **variables)
    if template == "conditional":
        return _template_conditional(workflow_id, **variables)
    if template == "retry_loop":
        return _template_retry_loop(workflow_id, **variables)
    raise WorkflowDefinitionError(f"unknown template: {template!r}")


def _template_linear(workflow_id: str, **variables: Any) -> WorkflowDefinition:
    nodes = (
        _node("start", NodeType.START.value),
        _node("step1", NodeType.TASK.value, next_nodes=("step2",), description="First task"),
        _node("step2", NodeType.TASK.value, next_nodes=("step3",), dependencies=("step1",), description="Second task"),
        _node("step3", NodeType.TASK.value, next_nodes=("end",), dependencies=("step2",), description="Third task"),
        _node("end", NodeType.END.value, dependencies=("step3",)),
    )
    return _workflow_def(workflow_id, "linear", nodes, **variables)


def _template_approval_gate(workflow_id: str, **variables: Any) -> WorkflowDefinition:
    nodes = (
        _node("start", NodeType.START.value),
        _node("task1", NodeType.TASK.value, next_nodes=("approval",), description="Action requiring approval"),
        _node("approval", NodeType.HUMAN_APPROVAL.value, next_nodes=("task2",), dependencies=("task1",), description="Human review"),
        _node("task2", NodeType.TASK.value, next_nodes=("end",), dependencies=("approval",), description="Approved action"),
        _node("end", NodeType.END.value, dependencies=("task2",)),
    )
    return _workflow_def(workflow_id, "approval_gate", nodes, **variables)


def _template_parallel(workflow_id: str, **variables: Any) -> WorkflowDefinition:
    nodes = (
        _node("start", NodeType.START.value),
        _node("fanout", NodeType.PARALLEL.value, next_nodes=("branch_a", "branch_b", "branch_c"), description="Parallel fan-out"),
        _node("branch_a", NodeType.TASK.value, next_nodes=("merge",), dependencies=("fanout",), description="Branch A"),
        _node("branch_b", NodeType.TASK.value, next_nodes=("merge",), dependencies=("fanout",), description="Branch B"),
        _node("branch_c", NodeType.TASK.value, next_nodes=("merge",), dependencies=("fanout",), description="Branch C"),
        _node("merge", NodeType.MERGE.value, dependencies=("branch_a", "branch_b", "branch_c"), next_nodes=("end",), description="Join branches"),
        _node("end", NodeType.END.value, dependencies=("merge",)),
    )
    return _workflow_def(workflow_id, "parallel", nodes, **variables)


def _template_conditional(workflow_id: str, **variables: Any) -> WorkflowDefinition:
    nodes = (
        _node("start", NodeType.START.value),
        _node("check", NodeType.DECISION.value, next_nodes=("yes", "no"), decision_branches=("yes", "no"), condition="condition", description="Evaluate condition"),
        _node("yes", NodeType.TASK.value, next_nodes=("end",), dependencies=("check",), description="Condition true path"),
        _node("no", NodeType.TASK.value, next_nodes=("end",), dependencies=("check",), description="Condition false path"),
        _node("end", NodeType.END.value, dependencies=("yes", "no")),
    )
    return _workflow_def(workflow_id, "conditional", nodes, **variables)


def _template_retry_loop(workflow_id: str, **variables: Any) -> WorkflowDefinition:
    nodes = (
        _node("start", NodeType.START.value),
        _node("attempt", NodeType.RETRY.value, next_nodes=("try", "recover", "end"), max_retries=2, description="Task with retries"),
        _node("try", NodeType.TASK.value, next_nodes=("attempt", "recover", "end"), description="Retryable task"),
        _node("recover", NodeType.RECOVERY.value, next_nodes=("end",), description="Failure recovery"),
        _node("end", NodeType.END.value, dependencies=("attempt", "recover")),
    )
    return _workflow_def(workflow_id, "retry_loop", nodes, **variables)


def _workflow_def(
    workflow_id: str,
    template: str,
    nodes: tuple[NodeDefinition, ...],
    **variables: Any,
) -> WorkflowDefinition:
    ts = _now_iso()
    start_nodes = [n.node_id for n in nodes if n.node_type == NodeType.START.value]
    end_nodes = tuple(n.node_id for n in nodes if n.node_type == NodeType.END.value)
    return WorkflowDefinition(
        workflow_id=workflow_id or _make_workflow_id(template, "1", ts),
        name=template,
        version=str(variables.pop("version", "1")),
        trigger=str(variables.pop("trigger", WorkflowTrigger.MANUAL.value)),
        template=template,
        nodes=nodes,
        start_node=start_nodes[0] if start_nodes else "",
        end_nodes=end_nodes,
        variables=dict(variables),
        max_concurrency=int(variables.pop("max_concurrency", 4)),
        timeout_seconds=int(variables.pop("timeout_seconds", 0)),
        created_at=ts,
        updated_at=ts,
    )


# ---------------------------------------------------------------------------
# Workflow execution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NodeExecution:
    """Runtime state of a single node execution."""

    execution_id: str
    workflow_id: str
    node_id: str
    status: str = NodeStatus.PENDING.value
    started_at: str = ""
    completed_at: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    error_detail: str = ""
    agent_id: str = ""
    retry_count: int = 0
    parent_execution_id: str = ""

    def with_(self, **changes: Any) -> "NodeExecution":
        data = self.to_dict()
        data.update(changes)
        return NodeExecution.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "workflow_id": self.workflow_id,
            "node_id": self.node_id,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "result": dict(self.result),
            "error_detail": self.error_detail,
            "agent_id": self.agent_id,
            "retry_count": self.retry_count,
            "parent_execution_id": self.parent_execution_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "NodeExecution") -> "NodeExecution":
        if isinstance(data, cls):
            return data
        raw = dict(data)
        return cls(
            execution_id=str(raw.get("execution_id", "")),
            workflow_id=str(raw.get("workflow_id", "")),
            node_id=str(raw.get("node_id", "")),
            status=str(raw.get("status", NodeStatus.PENDING.value)),
            started_at=str(raw.get("started_at", "")),
            completed_at=str(raw.get("completed_at", "")),
            result=dict(raw.get("result", {})),
            error_detail=str(raw.get("error_detail", "")),
            agent_id=str(raw.get("agent_id", "")),
            retry_count=int(raw.get("retry_count", 0)),
            parent_execution_id=str(raw.get("parent_execution_id", "")),
        )


@dataclass(frozen=True)
class WorkflowExecution:
    """Runtime state of a workflow execution."""

    execution_id: str
    workflow_id: str
    status: str = WorkflowStatus.DRAFT.value
    input: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    node_executions: tuple[NodeExecution, ...] = field(default_factory=tuple)
    started_at: str = ""
    completed_at: str = ""
    error_detail: str = ""
    correlation_id: str = ""
    request_id: str = ""
    trigger: str = ""
    checkpoint_id: str = ""

    def with_(self, **changes: Any) -> "WorkflowExecution":
        data = self.to_dict()
        data.update(changes)
        return WorkflowExecution.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "workflow_id": self.workflow_id,
            "status": self.status,
            "input": dict(self.input),
            "output": dict(self.output),
            "variables": dict(self.variables),
            "context": dict(self.context),
            "node_executions": [n.to_dict() for n in self.node_executions],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error_detail": self.error_detail,
            "correlation_id": self.correlation_id,
            "request_id": self.request_id,
            "trigger": self.trigger,
            "checkpoint_id": self.checkpoint_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "WorkflowExecution") -> "WorkflowExecution":
        if isinstance(data, cls):
            return data
        raw = dict(data)
        node_execs = tuple(NodeExecution.from_dict(n) for n in raw.get("node_executions", ()))
        return cls(
            execution_id=str(raw.get("execution_id", "")),
            workflow_id=str(raw.get("workflow_id", "")),
            status=str(raw.get("status", WorkflowStatus.DRAFT.value)),
            input=dict(raw.get("input", {})),
            output=dict(raw.get("output", {})),
            variables=dict(raw.get("variables", {})),
            context=dict(raw.get("context", {})),
            node_executions=node_execs,
            started_at=str(raw.get("started_at", "")),
            completed_at=str(raw.get("completed_at", "")),
            error_detail=str(raw.get("error_detail", "")),
            correlation_id=str(raw.get("correlation_id", "")),
            request_id=str(raw.get("request_id", "")),
            trigger=str(raw.get("trigger", "")),
            checkpoint_id=str(raw.get("checkpoint_id", "")),
        )


# ---------------------------------------------------------------------------
# Workflow engine
# ---------------------------------------------------------------------------

class WorkflowEngine:
    """Persistent, security-enforced multi-agent workflow engine."""

    def __init__(
        self,
        state_dir: str | Path | None = None,
        *,
        require_human_approval: bool = False,
        max_concurrency: int = 4,
    ) -> None:
        self.state_dir = Path(state_dir or HANDOFF_HOME / "workflow_engine")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.require_human_approval = require_human_approval
        self.max_concurrency = max_concurrency
        self._workflows: dict[str, WorkflowDefinition] = {}
        self._executions: dict[str, WorkflowExecution] = {}
        self._events: list[dict[str, str]] = []
        self._load()

    # -- persistence --------------------------------------------------------

    @property
    def workflow_path(self) -> Path:
        return self.state_dir / "workflows.json"

    @property
    def execution_path(self) -> Path:
        return self.state_dir / "executions.json"

    def _load(self) -> None:
        self._load_workflows()
        self._load_executions()

    def _load_workflows(self) -> None:
        if not self.workflow_path.exists():
            return
        try:
            data = json.loads(self.workflow_path.read_text(encoding="utf-8"))
            for wf_id, raw in data.items():
                definition = WorkflowDefinition.from_dict(raw)
                ok, errors = definition.validate()
                if not ok:
                    raise CorruptionError(f"Persisted workflow {wf_id!r} failed validation: {'; '.join(errors)}")
                self._workflows[wf_id] = definition
        except (OSError, ValueError, TypeError, KeyError, CorruptionError) as exc:
            if isinstance(exc, CorruptionError):
                raise
            raise CorruptionError(f"could not load workflows: {exc}") from exc

    def _load_executions(self) -> None:
        if not self.execution_path.exists():
            return
        try:
            data = json.loads(self.execution_path.read_text(encoding="utf-8"))
            for ex_id, raw in data.items():
                self._executions[ex_id] = WorkflowExecution.from_dict(raw)
        except (OSError, ValueError, TypeError, KeyError, CorruptionError) as exc:
            if isinstance(exc, CorruptionError):
                raise
            raise CorruptionError(f"could not load executions: {exc}") from exc

    def _save_workflows(self) -> None:
        payload = {wid: w.to_dict() for wid, w in self._workflows.items()}
        _atomic_write_file(self.workflow_path, _canonical(payload))

    def _save_executions(self) -> None:
        payload = {eid: e.to_dict() for eid, e in self._executions.items()}
        _atomic_write_file(self.execution_path, _canonical(payload))

    def _log(self, action: str, actor: str, workflow_id: str, detail: str = "") -> None:
        event = {
            "timestamp": _now_iso(),
            "actor": actor or "system",
            "action": action,
            "workflow_id": workflow_id,
            "detail": detail,
        }
        secret_scan = _canonical(event)
        if _contains_secret_like_content(secret_scan):
            event = {**event, "detail": "[redacted]"}
        self._events.append(event)

    # -- workflow definitions ----------------------------------------------

    def create_workflow(
        self,
        definition: WorkflowDefinition,
        *,
        actor: str = "orchestrator",
    ) -> WorkflowDefinition:
        """Validate, normalize, and persist a workflow definition."""
        definition = definition.with_(
            updated_at=_now_iso(),
            created_at=definition.created_at or _now_iso(),
        )
        ok, errors = definition.validate()
        if not ok:
            raise WorkflowDefinitionError("; ".join(errors))
        if definition.workflow_id in self._workflows:
            raise DuplicateWorkflowError(
                f"Workflow {definition.workflow_id!r} already exists."
            )
        self._workflows[definition.workflow_id] = definition
        self._log("workflow.created", actor, definition.workflow_id)
        self._save_workflows()
        return definition

    def get_workflow(self, workflow_id: str) -> WorkflowDefinition:
        if workflow_id not in self._workflows:
            raise UnknownWorkflowError(f"Workflow {workflow_id!r} is unknown.")
        return self._workflows[workflow_id]

    def list_workflows(self) -> list[WorkflowDefinition]:
        return sorted(self._workflows.values(), key=lambda w: (w.name, w.version, w.workflow_id))

    def update_workflow(
        self,
        workflow_id: str,
        *,
        actor: str = "orchestrator",
        **changes: Any,
    ) -> WorkflowDefinition:
        definition = self.get_workflow(workflow_id)
        updated = definition.with_(**changes)
        ok, errors = updated.validate()
        if not ok:
            raise WorkflowDefinitionError("; ".join(errors))
        updated = updated.with_(updated_at=_now_iso())
        self._workflows[workflow_id] = updated
        self._log("workflow.updated", actor, workflow_id)
        self._save_workflows()
        return updated

    def version_workflow(
        self,
        workflow_id: str,
        new_version: str,
        *,
        actor: str = "orchestrator",
    ) -> WorkflowDefinition:
        definition = self.get_workflow(workflow_id)
        ts = _now_iso()
        new_id = _make_workflow_id(definition.name, new_version, ts)
        new_def = definition.with_(
            workflow_id=new_id,
            version=new_version,
            created_at=ts,
            updated_at=ts,
        )
        self._workflows[new_id] = new_def
        self._log("workflow.versioned", actor, workflow_id, detail=f"new={new_id}")
        self._save_workflows()
        return new_def

    # -- triggers -----------------------------------------------------------

    def trigger_workflow(
        self,
        workflow_id: str,
        *,
        trigger: str = WorkflowTrigger.MANUAL.value,
        input_data: Mapping[str, Any] | None = None,
        correlation_id: str = "",
        request_id: str = "",
        actor: str = "orchestrator",
        require_approval: bool = False,
    ) -> WorkflowExecution:
        """Trigger a workflow execution."""
        definition = self.get_workflow(workflow_id)
        try:
            WorkflowTrigger(trigger)
        except ValueError:
            raise WorkflowDefinitionError(f"unknown trigger: {trigger!r}")
        ts = _now_iso()
        exec_id = _make_execution_id(workflow_id, ts)
        if require_approval:
            status = WorkflowStatus.WAITING_APPROVAL.value
        else:
            status = WorkflowStatus.READY.value
        exec_obj = WorkflowExecution(
            execution_id=exec_id,
            workflow_id=workflow_id,
            status=status,
            input=dict(input_data or {}),
            variables=dict(definition.variables),
            context={},
            started_at=ts,
            correlation_id=correlation_id or exec_id,
            request_id=request_id or exec_id,
            trigger=trigger,
        )
        self._executions[exec_id] = exec_obj
        self._log("workflow.triggered", actor, workflow_id, detail=f"trigger={trigger}")
        self._save_executions()
        return exec_obj

    def start_execution(
        self,
        execution_id: str,
        *,
        actor: str = "orchestrator",
    ) -> WorkflowExecution:
        """Begin executing a workflow (moves READY/WAITING_APPROVAL -> RUNNING)."""
        execution = self.get_execution(execution_id)
        if execution.status == WorkflowStatus.WAITING_APPROVAL.value:
            raise ApprovalRequiredError(
                f"Execution {execution_id!r} requires human approval."
            )
        if execution.status != WorkflowStatus.READY.value:
            raise WorkflowStateError(
                f"Execution {execution_id!r} cannot start from {execution.status!r}."
            )
        definition = self.get_workflow(execution.workflow_id)
        node_execs = []
        for node in definition.nodes:
            node_execs.append(
                NodeExecution(
                    execution_id=_make_execution_id(execution.execution_id + ":" + node.node_id, _now_iso()),
                    workflow_id=definition.workflow_id,
                    node_id=node.node_id,
                    status=NodeStatus.PENDING.value,
                    agent_id=node.agent_id,
                )
            )
        exec_obj = execution.with_(
            status=WorkflowStatus.RUNNING.value,
            node_executions=tuple(node_execs),
        )
        self._executions[execution_id] = exec_obj
        self._log("workflow.started", actor, execution.workflow_id, detail=execution_id)
        self._save_executions()
        return exec_obj

    def get_execution(self, execution_id: str) -> WorkflowExecution:
        if execution_id not in self._executions:
            raise UnknownExecutionError(f"Execution {execution_id!r} is unknown.")
        return self._executions[execution_id]

    def list_executions(self, workflow_id: str | None = None) -> list[WorkflowExecution]:
        results = [
            e for e in self._executions.values()
            if (not workflow_id or e.workflow_id == workflow_id)
        ]
        return sorted(results, key=lambda e: (e.started_at, e.execution_id))

    def execution_status(self, execution_id: str) -> str:
        return self.get_execution(execution_id).status

    # -- pause / resume / cancel -------------------------------------------

    def pause_execution(self, execution_id: str, *, actor: str = "orchestrator") -> WorkflowExecution:
        exec_obj = self.get_execution(execution_id)
        if exec_obj.status != WorkflowStatus.RUNNING.value:
            raise WorkflowStateError(f"Execution {execution_id!r} is not running.")
        exec_obj = exec_obj.with_(status=WorkflowStatus.PAUSED.value)
        self._executions[execution_id] = exec_obj
        self._log("workflow.paused", actor, exec_obj.workflow_id, detail=execution_id)
        self._save_executions()
        return exec_obj

    def resume_execution(self, execution_id: str, *, actor: str = "orchestrator") -> WorkflowExecution:
        exec_obj = self.get_execution(execution_id)
        if exec_obj.status != WorkflowStatus.PAUSED.value:
            raise WorkflowStateError(f"Execution {execution_id!r} is not paused.")
        exec_obj = exec_obj.with_(status=WorkflowStatus.RUNNING.value)
        self._executions[execution_id] = exec_obj
        self._log("workflow.resumed", actor, exec_obj.workflow_id, detail=execution_id)
        self._save_executions()
        return exec_obj

    def cancel_execution(self, execution_id: str, *, actor: str = "system", reason: str = "") -> WorkflowExecution:
        exec_obj = self.get_execution(execution_id)
        if exec_obj.status in (
            WorkflowStatus.COMPLETED.value,
            WorkflowStatus.FAILED.value,
            WorkflowStatus.CANCELLED.value,
        ):
            raise WorkflowStateError(f"Execution {execution_id!r} is already terminal.")
        exec_obj = exec_obj.with_(
            status=WorkflowStatus.CANCELLED.value,
            error_detail=reason,
            completed_at=_now_iso(),
        )
        self._executions[execution_id] = exec_obj
        self._log("workflow.cancelled", actor, exec_obj.workflow_id, detail=execution_id)
        self._save_executions()
        return exec_obj

    # -- retry / recovery ----------------------------------------------------

    def retry_execution(
        self,
        execution_id: str,
        *,
        actor: str = "orchestrator",
        require_approval: bool = False,
    ) -> WorkflowExecution:
        exec_obj = self.get_execution(execution_id)
        if exec_obj.status not in (
            WorkflowStatus.FAILED.value,
            WorkflowStatus.CANCELLED.value,
            WorkflowStatus.TIMEOUT.value,
        ):
            raise WorkflowStateError(
                f"Execution {execution_id!r} cannot be retried from {exec_obj.status!r}."
            )
        ts = _now_iso()
        new_exec_id = _make_execution_id(exec_obj.workflow_id, ts)
        new_exec = WorkflowExecution(
            execution_id=new_exec_id,
            workflow_id=exec_obj.workflow_id,
            status=(
                WorkflowStatus.WAITING_APPROVAL.value
                if require_approval
                else WorkflowStatus.READY.value
            ),
            input=dict(exec_obj.input),
            variables=dict(exec_obj.variables),
            context=dict(exec_obj.context),
            started_at=ts,
            correlation_id=exec_obj.correlation_id,
            request_id=exec_obj.request_id,
            trigger=exec_obj.trigger,
            checkpoint_id=exec_obj.checkpoint_id,
        )
        self._executions[new_exec_id] = new_exec
        self._log("workflow.retried", actor, exec_obj.workflow_id, detail=execution_id)
        self._save_executions()
        return new_exec

    def recover_execution(
        self,
        execution_id: str,
        *,
        actor: str = "system",
    ) -> WorkflowExecution:
        exec_obj = self.get_execution(execution_id)
        if exec_obj.status not in (
            WorkflowStatus.FAILED.value,
            WorkflowStatus.TIMEOUT.value,
            WorkflowStatus.RECOVERING.value,
        ):
            raise WorkflowStateError(
                f"Execution {execution_id!r} cannot be recovered from {exec_obj.status!r}."
            )
        exec_obj = exec_obj.with_(
            status=WorkflowStatus.RUNNING.value,
            error_detail="",
        )
        self._executions[execution_id] = exec_obj
        self._log("workflow.recovered", actor, exec_obj.workflow_id, detail=execution_id)
        self._save_executions()
        return exec_obj

    def timeout_execution(
        self,
        execution_id: str,
        *,
        timeout_seconds: int,
        actor: str = "system",
    ) -> WorkflowExecution:
        exec_obj = self.get_execution(execution_id)
        if exec_obj.status != WorkflowStatus.RUNNING.value:
            raise WorkflowStateError(
                f"Execution {execution_id!r} is not running."
            )
        if not timeout_seconds:
            raise TimeoutError("timeout_seconds must be positive")
        started = datetime.fromisoformat(exec_obj.started_at)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        if elapsed <= timeout_seconds:
            raise TimeoutError(
                f"Execution {execution_id!r} has not exceeded its timeout yet ({elapsed:.1f}s / {timeout_seconds}s)."
            )
        exec_obj = exec_obj.with_(
            status=WorkflowStatus.TIMEOUT.value,
            error_detail=f"exceeded timeout of {timeout_seconds}s",
            completed_at=_now_iso(),
        )
        self._executions[execution_id] = exec_obj
        self._log("workflow.timeout", actor, exec_obj.workflow_id, detail=execution_id)
        self._save_executions()
        return exec_obj

    # -- branching / conditional --------------------------------------------

    def evaluate_condition(self, condition: str, variables: Mapping[str, Any]) -> bool:
        """Deterministic, safe evaluation of a workflow condition."""
        expr = condition.strip()
        if not expr:
            return True
        operator = "=="
        for op in ("!=", ">=", "<=", ">", "<", "=="):
            if op in expr:
                operator = op
                break
        if operator not in expr:
            return _truthy(expr, variables)
        left, right = expr.split(operator, 1)
        left_val = _resolve_variable(left.strip(), variables)
        right_val = _resolve_variable(right.strip(), variables)
        if operator == "==":
            return str(left_val) == str(right_val)
        if operator == "!=":
            return str(left_val) != str(right_val)
        try:
            lf = float(left_val)
            rf = float(right_val)
        except (TypeError, ValueError):
            return False
        if operator == ">":
            return lf > rf
        if operator == "<":
            return lf < rf
        if operator == ">=":
            return lf >= rf
        if operator == "<=":
            return lf <= rf
        return False

    def choose_branch(self, node: NodeDefinition, variables: Mapping[str, Any]) -> str | None:
        """Choose a branch for a DECISION/CONDITIONAL node."""
        if not node.decision_branches:
            return node.next_nodes[0] if node.next_nodes else None
        condition = node.condition or "condition"
        result = _resolve_variable(condition, variables)
        result_str = str(result)
        if result_str in node.decision_branches:
            return result_str
        for branch in node.decision_branches:
            if branch == "default":
                return branch
        return node.decision_branches[0]

    # -- scheduling ----------------------------------------------------------

    def schedule_ready_nodes(
        self,
        execution_id: str,
        *,
        actor: str = "orchestrator",
    ) -> list[NodeExecution]:
        """Identify and return nodes ready for execution."""
        exec_obj = self.get_execution(execution_id)
        if exec_obj.status != WorkflowStatus.RUNNING.value:
            raise WorkflowStateError(
                f"Execution {execution_id!r} is not running."
            )
        definition = self.get_workflow(exec_obj.workflow_id)
        node_map = {n.node_id: n for n in definition.nodes}
        node_exec_map = {ne.node_id: ne for ne in exec_obj.node_executions}
        ready: list[NodeExecution] = []
        for node in definition.nodes:
            if node.node_type == NodeType.END.value:
                continue
            ne = node_exec_map.get(node.node_id)
            if not ne or ne.status != NodeStatus.PENDING.value:
                continue
            deps_satisfied = True
            for dep in node.dependencies:
                dep_exec = node_exec_map.get(dep)
                if not dep_exec or dep_exec.status != NodeStatus.COMPLETED.value:
                    deps_satisfied = False
                    break
            if node.node_id == definition.start_node and not node.dependencies:
                deps_satisfied = True
            if deps_satisfied:
                ready.append(ne)
        return ready

    def execute_node(
        self,
        execution_id: str,
        node_id: str,
        *,
        agent_id: str = "",
        actor: str = "agent",
        result: Mapping[str, Any] | None = None,
    ) -> NodeExecution:
        """Mark a node as running, then complete it with a result."""
        exec_obj = self.get_execution(execution_id)
        if exec_obj.status != WorkflowStatus.RUNNING.value:
            raise WorkflowStateError(
                f"Execution {execution_id!r} is not running."
            )
        definition = self.get_workflow(exec_obj.workflow_id)
        node_execs = list(exec_obj.node_executions)
        target = next((ne for ne in node_execs if ne.node_id == node_id), None)
        if not target:
            raise NodeUnavailableError(f"Node {node_id!r} does not exist.")
        if target.status not in (NodeStatus.PENDING.value, NodeStatus.READY.value):
            raise WorkflowStateError(
                f"Node {node_id!r} is in state {target.status!r}."
            )
        updated = target.with_(
            status=NodeStatus.RUNNING.value,
            started_at=_now_iso(),
            agent_id=agent_id or target.agent_id,
        )
        for i, ne in enumerate(node_execs):
            if ne.node_id == node_id:
                node_execs[i] = updated
        exec_obj = exec_obj.with_(node_executions=tuple(node_execs))
        self._executions[execution_id] = exec_obj
        self._log("workflow.node_running", actor, exec_obj.workflow_id, detail=f"{execution_id}:{node_id}")
        self._save_executions()
        return updated

    def complete_node(
        self,
        execution_id: str,
        node_id: str,
        *,
        result: Mapping[str, Any] | None = None,
        actor: str = "agent",
    ) -> WorkflowExecution:
        """Complete a node and propagate results to dependents."""
        exec_obj = self.get_execution(execution_id)
        node_execs = list(exec_obj.node_executions)
        target_idx = next((i for i, ne in enumerate(node_execs) if ne.node_id == node_id), None)
        if target_idx is None:
            raise NodeUnavailableError(f"Node {node_id!r} does not exist.")
        target = node_execs[target_idx]
        if target.status != NodeStatus.RUNNING.value:
            raise WorkflowStateError(
                f"Node {node_id!r} is not running."
            )
        updated = target.with_(
            status=NodeStatus.COMPLETED.value,
            completed_at=_now_iso(),
            result=dict(result or {}),
        )
        node_execs[target_idx] = updated
        exec_obj = exec_obj.with_(node_executions=tuple(node_execs))
        exec_obj = self._propagate_results(exec_obj, node_id, dict(result or {}))
        self._executions[execution_id] = exec_obj
        self._log("workflow.node_completed", actor, exec_obj.workflow_id, detail=f"{execution_id}:{node_id}")
        if self._is_complete(exec_obj):
            exec_obj = exec_obj.with_(
                status=WorkflowStatus.COMPLETED.value,
                completed_at=_now_iso(),
            )
            self._executions[execution_id] = exec_obj
            self._log("workflow.completed", actor, exec_obj.workflow_id, detail=execution_id)
        self._save_executions()
        return exec_obj

    def fail_node(
        self,
        execution_id: str,
        node_id: str,
        *,
        reason: str,
        actor: str = "system",
    ) -> WorkflowExecution:
        """Fail a node, propagate failure to dependents."""
        exec_obj = self.get_execution(execution_id)
        node_execs = list(exec_obj.node_executions)
        target_idx = next((i for i, ne in enumerate(node_execs) if ne.node_id == node_id), None)
        if target_idx is None:
            raise NodeUnavailableError(f"Node {node_id!r} does not exist.")
        target = node_execs[target_idx]
        updated = target.with_(
            status=NodeStatus.FAILED.value,
            completed_at=_now_iso(),
            error_detail=reason,
        )
        node_execs[target_idx] = updated
        exec_obj = exec_obj.with_(node_executions=tuple(node_execs))
        exec_obj = self._propagate_failure(exec_obj, node_id, reason)
        self._executions[execution_id] = exec_obj
        self._log("workflow.node_failed", actor, exec_obj.workflow_id, detail=f"{execution_id}:{node_id} {reason}")
        self._save_executions()
        return exec_obj

    # -- human approval -----------------------------------------------------

    def require_approval(
        self,
        execution_id: str,
        node_id: str,
        *,
        actor: str = "orchestrator",
    ) -> WorkflowExecution:
        """Mark a workflow as waiting for human approval."""
        exec_obj = self.get_execution(execution_id)
        node_execs = list(exec_obj.node_executions)
        target_idx = next((i for i, ne in enumerate(node_execs) if ne.node_id == node_id), None)
        if target_idx is not None:
            target = node_execs[target_idx]
            node_execs[target_idx] = target.with_(status=NodeStatus.WAITING_APPROVAL.value)
            exec_obj = exec_obj.with_(node_executions=tuple(node_execs))
        exec_obj = exec_obj.with_(status=WorkflowStatus.WAITING_APPROVAL.value)
        self._executions[execution_id] = exec_obj
        self._log("workflow.approval_required", actor, exec_obj.workflow_id, detail=execution_id)
        self._save_executions()
        return exec_obj

    def approve(
        self,
        execution_id: str,
        *,
        human: str,
        node_id: str = "",
    ) -> WorkflowExecution:
        """Approve a workflow, allowing it to continue."""
        if not human:
            raise ApprovalRequiredError("Human approval requires a named human.")
        exec_obj = self.get_execution(execution_id)
        if exec_obj.status != WorkflowStatus.WAITING_APPROVAL.value:
            raise ApprovalRequiredError(
                f"Execution {execution_id!r} is not awaiting approval."
            )
        if node_id:
            node_execs = list(exec_obj.node_executions)
            target_idx = next((i for i, ne in enumerate(node_execs) if ne.node_id == node_id), None)
            if target_idx is not None:
                target = node_execs[target_idx]
                node_execs[target_idx] = target.with_(status=NodeStatus.READY.value)
                exec_obj = exec_obj.with_(node_executions=tuple(node_execs))
        exec_obj = exec_obj.with_(status=WorkflowStatus.RUNNING.value)
        self._executions[execution_id] = exec_obj
        self._log("workflow.approved", human, exec_obj.workflow_id, detail=execution_id)
        self._save_executions()
        return exec_obj

    def reject_approval(
        self,
        execution_id: str,
        *,
        human: str,
        reason: str = "",
    ) -> WorkflowExecution:
        """Reject a workflow approval."""
        if not human:
            raise ApprovalRequiredError("Approval rejection requires a named human.")
        exec_obj = self.get_execution(execution_id)
        if exec_obj.status != WorkflowStatus.WAITING_APPROVAL.value:
            raise ApprovalRequiredError(
                f"Execution {execution_id!r} is not awaiting approval."
            )
        exec_obj = exec_obj.with_(
            status=WorkflowStatus.CANCELLED.value,
            error_detail=reason or "approval rejected",
            completed_at=_now_iso(),
        )
        self._executions[execution_id] = exec_obj
        self._log("workflow.approval_rejected", human, exec_obj.workflow_id, detail=execution_id)
        self._save_executions()
        return exec_obj

    # -- dependency / deadlock detection -----------------------------------

    def detect_circular_dependency(self, workflow_id: str) -> list[list[str]]:
        """Detect cycles in the workflow dependency graph."""
        definition = self.get_workflow(workflow_id)
        adj: dict[str, list[str]] = {n.node_id: list(n.dependencies) for n in definition.nodes}
        cycles: list[list[str]] = []
        visited: set[str] = set()
        path: list[str] = []

        def _dfs(node: str) -> None:
            if node in path:
                idx = path.index(node)
                cycle = path[idx:] + [node]
                if cycle not in cycles:
                    cycles.append(cycle)
                return
            if node in visited:
                return
            path.append(node)
            for dep in adj.get(node, []):
                _dfs(dep)
            path.pop()
            visited.add(node)

        for node in definition.nodes:
            visited = set()
            path.clear()
            _dfs(node.node_id)
        return cycles

    def detect_deadlock(self, workflow_id: str) -> list[list[str]]:
        """Detect a deadlock: a non-terminal cycle blocking all forward progress."""
        cycles = self.detect_circular_dependency(workflow_id)
        return [c for c in cycles if cycles]

    def detect_stuck_workflows(self, *, max_idle_minutes: int = 30) -> list[str]:
        """Detect workflows stuck in RUNNING without node progress."""
        stuck: list[str] = []
        for eid, exec_obj in self._executions.items():
            if exec_obj.status != WorkflowStatus.RUNNING.value:
                continue
            node_execs = [ne for ne in exec_obj.node_executions if ne.status != NodeStatus.COMPLETED.value]
            if not node_execs:
                continue
            running_nodes = [ne for ne in node_execs if ne.status == NodeStatus.RUNNING.value]
            if not running_nodes:
                stuck.append(eid)
        return stuck

    def detect_orphan_tasks(self, workflow_id: str) -> list[str]:
        """Detect nodes not reachable from the start node."""
        definition = self.get_workflow(workflow_id)
        reachable: set[str] = set()
        stack = [definition.start_node]
        while stack:
            current = stack.pop()
            if current in reachable:
                continue
            reachable.add(current)
            node = next((n for n in definition.nodes if n.node_id == current), None)
            if node:
                stack.extend(node.next_nodes)
        orphans = [n.node_id for n in definition.nodes if n.node_id not in reachable]
        return orphans

    def detect_stale_executions(self, *, max_age_seconds: int = 3600) -> list[str]:
        """Detect executions that have exceeded their duration."""
        stale: list[str] = []
        for eid, exec_obj in self._executions.items():
            if exec_obj.status not in (
                WorkflowStatus.RUNNING.value,
                WorkflowStatus.WAITING_APPROVAL.value,
            ):
                continue
            try:
                started = datetime.fromisoformat(exec_obj.started_at)
                elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            except ValueError:
                continue
            if elapsed > max_age_seconds:
                stale.append(eid)
        return stale

    def prevent_duplicate_execution(
        self,
        workflow_id: str,
        *,
        correlation_id: str,
    ) -> bool:
        """Prevent duplicate executions sharing a correlation id."""
        for exec_obj in self._executions.values():
            if (
                exec_obj.workflow_id == workflow_id
                and exec_obj.correlation_id == correlation_id
                and exec_obj.status in (
                    WorkflowStatus.RUNNING.value,
                    WorkflowStatus.READY.value,
                    WorkflowStatus.WAITING_APPROVAL.value,
                )
            ):
                return False
        return True

    def atomic_state_transition(
        self,
        execution_id: str,
        current: str,
        target: str,
    ) -> bool:
        """Atomic (single-step) workflow state transition with validation."""
        exec_obj = self.get_execution(execution_id)
        if exec_obj.status != current:
            return False
        try:
            _valid_from = {
                WorkflowStatus.DRAFT.value: {WorkflowStatus.READY.value},
                WorkflowStatus.READY.value: {WorkflowStatus.RUNNING.value, WorkflowStatus.CANCELLED.value},
                WorkflowStatus.RUNNING.value: {
                    WorkflowStatus.PAUSED.value,
                    WorkflowStatus.COMPLETED.value,
                    WorkflowStatus.FAILED.value,
                    WorkflowStatus.WAITING_APPROVAL.value,
                },
                WorkflowStatus.PAUSED.value: {WorkflowStatus.RUNNING.value, WorkflowStatus.CANCELLED.value},
                WorkflowStatus.WAITING_APPROVAL.value: {WorkflowStatus.RUNNING.value, WorkflowStatus.CANCELLED.value, WorkflowStatus.FAILED.value},
                WorkflowStatus.COMPLETED.value: set(),
                WorkflowStatus.FAILED.value: {WorkflowStatus.READY.value, WorkflowStatus.RECOVERING.value},
                WorkflowStatus.CANCELLED.value: {WorkflowStatus.READY.value},
                WorkflowStatus.TIMEOUT.value: {WorkflowStatus.READY.value},
                WorkflowStatus.RECOVERING.value: {WorkflowStatus.RUNNING.value, WorkflowStatus.FAILED.value},
            }
            allowed = _valid_from.get(current, set())
            if target not in allowed:
                return False
        except Exception:
            return False
        self._executions[execution_id] = exec_obj.with_(status=target)
        self._save_executions()
        return True

    # -- crash recovery / failure recovery -----------------------------------

    def crash_recovery(self, *, actor: str = "system") -> dict[str, Any]:
        """Detect and recover executions stuck mid-transition."""
        recovered: list[str] = []
        for eid, exec_obj in list(self._executions.items()):
            if exec_obj.status == WorkflowStatus.RUNNING.value:
                running_nodes = [
                    ne for ne in exec_obj.node_executions
                    if ne.status == NodeStatus.RUNNING.value
                ]
                if running_nodes:
                    node_execs = []
                    for ne in exec_obj.node_executions:
                        if ne.status == NodeStatus.RUNNING.value:
                            node_execs.append(
                                ne.with_(
                                    status=NodeStatus.FAILED.value,
                                    error_detail="crash recovery",
                                )
                            )
                        else:
                            node_execs.append(ne)
                    self._executions[eid] = exec_obj.with_(
                        node_executions=tuple(node_execs),
                        status=WorkflowStatus.RECOVERING.value,
                    )
                    recovered.append(eid)
        if recovered:
            self._save_executions()
        return {"ok": not recovered, "recovered": recovered}

    def provider_failure_recovery(
        self,
        execution_id: str,
        *,
        node_id: str,
        actor: str = "system",
    ) -> WorkflowExecution:
        """Recover from a provider failure by marking the node failed."""
        exec_obj = self.get_execution(execution_id)
        node_execs = list(exec_obj.node_executions)
        target_idx = next((i for i, ne in enumerate(node_execs) if ne.node_id == node_id), None)
        if target_idx is None:
            raise NodeUnavailableError(f"Node {node_id!r} does not exist.")
        target = node_execs[target_idx]
        node_execs[target_idx] = target.with_(
            status=NodeStatus.FAILED.value,
            error_detail="provider failure",
        )
        exec_obj = exec_obj.with_(node_executions=tuple(node_execs))
        self._executions[execution_id] = exec_obj
        self._log("workflow.provider_failure", actor, exec_obj.workflow_id, detail=f"{execution_id}:{node_id}")
        self._save_executions()
        return exec_obj

    # -- internal result/failure propagation --------------------------------

    def _propagate_results(
        self,
        exec_obj: WorkflowExecution,
        node_id: str,
        result: dict[str, Any],
    ) -> WorkflowExecution:
        """Merge node results into the workflow variables/output context."""
        merged_vars = dict(exec_obj.variables)
        merged_vars[f"node.{node_id}"] = result.get("value", result)
        merged_output = dict(exec_obj.output)
        merged_output[node_id] = result
        return exec_obj.with_(variables=merged_vars, output=merged_output)

    def _propagate_failure(
        self,
        exec_obj: WorkflowExecution,
        node_id: str,
        reason: str,
    ) -> WorkflowExecution:
        """Mark dependent nodes as blocked due to a failed dependency."""
        definition = self.get_workflow(exec_obj.workflow_id)
        node_map = {n.node_id: n for n in definition.nodes}
        node_execs = list(exec_obj.node_executions)
        for ne in node_execs:
            node_def = node_map.get(ne.node_id)
            if node_def and node_id in node_def.dependencies:
                if ne.status == NodeStatus.PENDING.value:
                    node_execs[node_execs.index(ne)] = ne.with_(
                        status=NodeStatus.BLOCKED.value,
                        error_detail=f"dependency {node_id} failed: {reason}",
                    )
        return exec_obj.with_(node_executions=tuple(node_execs))

    def _is_complete(self, exec_obj: WorkflowExecution) -> bool:
        """Check if all non-END nodes have completed."""
        definition = self.get_workflow(exec_obj.workflow_id)
        node_map = {n.node_id: n for n in definition.nodes}
        for ne in exec_obj.node_executions:
            node_def = node_map.get(ne.node_id)
            if not node_def or node_def.node_type in (
                NodeType.END.value,
                NodeType.START.value,
            ):
                continue
            if ne.status not in (
                NodeStatus.COMPLETED.value,
                NodeStatus.SKIPPED.value,
                NodeStatus.CANCELLED.value,
            ):
                return False
        return True

    # -- checkpoint creation/restoration ------------------------------------

    def create_checkpoint(self, execution_id: str, *, actor: str = "system") -> dict[str, Any]:
        """Create a checkpoint snapshot of execution state."""
        exec_obj = self.get_execution(execution_id)
        return {
            "execution_id": execution_id,
            "workflow_id": exec_obj.workflow_id,
            "status": exec_obj.status,
            "node_executions": [ne.to_dict() for ne in exec_obj.node_executions],
            "variables": dict(exec_obj.variables),
            "context": dict(exec_obj.context),
            "input": dict(exec_obj.input),
            "output": dict(exec_obj.output),
            "correlation_id": exec_obj.correlation_id,
            "request_id": exec_obj.request_id,
            "created_at": _now_iso(),
        }

    def restore_checkpoint(self, checkpoint: Mapping[str, Any], *, actor: str = "system") -> str:
        """Restore an execution from a checkpoint snapshot."""
        raw = dict(checkpoint)
        execution_id = str(raw.get("execution_id", ""))
        if not execution_id:
            raise WorkflowDefinitionError("checkpoint has no execution_id")
        node_execs = []
        for ne_raw in raw.get("node_executions", []):
            node_execs.append(NodeExecution.from_dict(ne_raw))
        exec_obj = WorkflowExecution(
            execution_id=execution_id,
            workflow_id=str(raw.get("workflow_id", "")),
            status=str(raw.get("status", WorkflowStatus.RECOVERING.value)),
            input=dict(raw.get("input", {})),
            output=dict(raw.get("output", {})),
            variables=dict(raw.get("variables", {})),
            context=dict(raw.get("context", {})),
            node_executions=tuple(node_execs),
            correlation_id=str(raw.get("correlation_id", "")),
            request_id=str(raw.get("request_id", "")),
        )
        self._executions[execution_id] = exec_obj
        self._log("workflow.checkpoint_restored", actor, exec_obj.workflow_id, detail=execution_id)
        self._save_executions()
        return execution_id

    # -- handoff / changelog / registry / delegation / messaging integration

    def handoff_integration(self, execution_id: str, objective: str = "") -> dict[str, Any]:
        """Produce a handoff document fragment for this execution."""
        exec_obj = self.get_execution(execution_id)
        completed_nodes = [
            ne.node_id for ne in exec_obj.node_executions
            if ne.status == NodeStatus.COMPLETED.value
        ]
        running_nodes = [
            ne.node_id for ne in exec_obj.node_executions
            if ne.status in (NodeStatus.RUNNING.value, NodeStatus.READY.value)
        ]
        pending_nodes = [
            ne.node_id for ne in exec_obj.node_executions
            if ne.status == NodeStatus.PENDING.value
        ]
        return {
            "execution_id": execution_id,
            "workflow_id": exec_obj.workflow_id,
            "objective": objective or exec_obj.workflow_id,
            "completed": completed_nodes,
            "in_progress": running_nodes,
            "next_actions": pending_nodes,
            "output": dict(exec_obj.output),
            "correlation_id": exec_obj.correlation_id,
            "request_id": exec_obj.request_id,
        }

    def changelog_entry(self, execution_id: str) -> dict[str, Any]:
        """Produce a CHANGELOG-ready entry for this execution."""
        exec_obj = self.get_execution(execution_id)
        return {
            "timestamp": _now_iso(),
            "execution_id": execution_id,
            "workflow_id": exec_obj.workflow_id,
            "status": exec_obj.status,
            "trigger": exec_obj.trigger,
            "output": dict(exec_obj.output),
        }

    def registry_integration(self, workflow_id: str, agent_ids: Iterable[str]) -> list[str]:
        """Declare the agents required by a workflow for registration discovery."""
        definition = self.get_workflow(workflow_id)
        required = set(agent_ids)
        for node in definition.nodes:
            if node.agent_id:
                required.add(node.agent_id)
        return sorted(required)

    def delegation_integration(self, execution_id: str, task_id: str) -> dict[str, Any]:
        """Assemble a delegation context for a workflow task."""
        exec_obj = self.get_execution(execution_id)
        definition = self.get_workflow(exec_obj.workflow_id)
        return {
            "task_id": task_id,
            "workflow_id": exec_obj.workflow_id,
            "execution_id": execution_id,
            "project_id": definition.variables.get("project_id", ""),
            "requester_agent_id": definition.variables.get("requester_agent_id", ""),
            "correlation_id": exec_obj.correlation_id,
            "request_id": exec_obj.request_id,
        }

    def messaging_integration(self, execution_id: str) -> dict[str, Any]:
        """Assemble messaging context for workflow execution events."""
        exec_obj = self.get_execution(execution_id)
        return {
            "workflow_id": exec_obj.workflow_id,
            "execution_id": execution_id,
            "correlation_id": exec_obj.correlation_id,
            "request_id": exec_obj.request_id,
            "status": exec_obj.status,
        }

    # -- dynamic task creation / agent assignment ----------------------------

    def dynamic_task_creation(
        self,
        execution_id: str,
        *,
        node_id: str,
        new_task_ids: Iterable[str],
        actor: str = "orchestrator",
    ) -> WorkflowExecution:
        """Dynamically create tasks from a node result."""
        exec_obj = self.get_execution(execution_id)
        definition = self.get_workflow(exec_obj.workflow_id)
        new_tasks = [str(t) for t in new_task_ids]
        node_execs = list(exec_obj.node_executions)
        for task in new_tasks:
            new_node = NodeDefinition(
                node_id=f"{node_id}.task.{task}",
                node_type=NodeType.TASK.value,
                task_id=task,
                dependencies=(node_id,),
                next_nodes=(),
            )
            if new_node.node_id not in {n.node_id for n in definition.nodes}:
                definition = definition.with_(
                    nodes=definition.nodes + (new_node,)
                )
                node_execs.append(
                    NodeExecution(
                        execution_id=_make_execution_id(execution_id + ":" + new_node.node_id, _now_iso()),
                        workflow_id=definition.workflow_id,
                        node_id=new_node.node_id,
                        status=NodeStatus.PENDING.value,
                    )
                )
        self._workflows[definition.workflow_id] = definition
        exec_obj = exec_obj.with_(node_executions=tuple(node_execs))
        self._executions[execution_id] = exec_obj
        self._log("workflow.dynamic_tasks", actor, exec_obj.workflow_id, detail=f"nodes={len(new_tasks)}")
        self._save_workflows()
        self._save_executions()
        return exec_obj

    def dynamic_agent_assignment(
        self,
        execution_id: str,
        node_id: str,
        agent_id: str,
        *,
        actor: str = "orchestrator",
    ) -> WorkflowExecution:
        """Dynamically assign an agent to a node."""
        exec_obj = self.get_execution(execution_id)
        node_execs = list(exec_obj.node_executions)
        target_idx = next((i for i, ne in enumerate(node_execs) if ne.node_id == node_id), None)
        if target_idx is None:
            raise NodeUnavailableError(f"Node {node_id!r} does not exist.")
        updated = node_execs[target_idx].with_(agent_id=agent_id)
        node_execs[target_idx] = updated
        exec_obj = exec_obj.with_(node_executions=tuple(node_execs))
        self._executions[execution_id] = exec_obj
        self._save_executions()
        return exec_obj

    def capability_routing(
        self,
        workflow_id: str,
        *,
        capability: str,
        available_agents: Iterable[str] | None = None,
    ) -> list[str]:
        """Determine agents capable of handling the given capability."""
        definition = self.get_workflow(workflow_id)
        requested = capability or ""
        if not requested:
            return []
        node_ids = [
            node.node_id for node in definition.nodes
            if node.capability == requested or node.capability == "" and node.node_type == NodeType.AGENT.value
        ]
        if available_agents is not None:
            pool = set(available_agents)
        else:
            pool = set()
            for node in definition.nodes:
                if node.agent_id:
                    pool.add(node.agent_id)
        if requested:
            result = sorted(node_ids)
            if available_agents is not None:
                result = [a for a in result if a in pool]
            return result
        return sorted(pool)

    def agent_load_awareness(self) -> dict[str, int]:
        """Count running nodes per agent to assess load."""
        load: dict[str, int] = {}
        for exec_obj in self._executions.values():
            if exec_obj.status != WorkflowStatus.RUNNING.value:
                continue
            for ne in exec_obj.node_executions:
                if ne.status == NodeStatus.RUNNING.value and ne.agent_id:
                    load[ne.agent_id] = load.get(ne.agent_id, 0) + 1
        return load

    def agent_availability_awareness(self) -> dict[str, list[str]]:
        """Map agent availability to their assigned executions."""
        by_agent: dict[str, list[str]] = {}
        for exec_obj in self._executions.values():
            for ne in exec_obj.node_executions:
                if ne.agent_id and ne.status in (
                    NodeStatus.RUNNING.value,
                    NodeStatus.READY.value,
                ):
                    by_agent.setdefault(ne.agent_id, []).append(exec_obj.execution_id)
        return by_agent

    def priority_scheduling(self, execution_ids: list[str], *, priority_map: Mapping[str, int] | None = None) -> list[str]:
        """Schedule executions by priority (lower value = higher priority)."""
        if priority_map is None:
            priority_map = {eid: 5 for eid in execution_ids}
        return sorted(execution_ids, key=lambda eid: (priority_map.get(eid, 5), eid))

    def bounded_concurrency(self, execution_ids: list[str], *, max_concurrency: int | None = None) -> list[str]:
        """Return only the first N executions given bounded concurrency."""
        limit = max_concurrency or self.max_concurrency
        return list(execution_ids)[:limit]

    def execution_isolation(self, execution_id: str) -> bool:
        """Check that an execution has isolated state."""
        exec_obj = self.get_execution(execution_id)
        node_ids = {ne.node_id for ne in exec_obj.node_executions}
        return len(node_ids) == len(exec_obj.node_executions)

    # -- security -----------------------------------------------------------

    def enforce_permissions(self, workflow_id: str) -> dict[str, list[str]]:
        """Enforce that workflows only use known capabilities."""
        definition = self.get_workflow(workflow_id)
        problems: dict[str, list[str]] = {}
        for node in definition.nodes:
            if node.capability and not is_known_capability(node.capability):
                problems.setdefault("unknown_capabilities", []).append(
                    f"{node.node_id}:{node.capability}"
                )
        return problems

    def no_destructive_bypass(self, workflow_id: str) -> bool:
        """Verify no nodes attempt destructive operations."""
        definition = self.get_workflow(workflow_id)
        destructive = {
            "rm", "drop", "destroy", "delete_all", "wipe", "truncate",
            "unlink", "rmdir", "force_push",
        }
        for node in definition.nodes:
            joined = _canonical(node.to_dict()).lower()
            for term in destructive:
                if f'"{term}"' in joined or f"'{term}'" in joined:
                    return False
        return True

    def secret_filtering(self, workflow_id: str) -> bool:
        """Confirm no secrets in workflow definitions."""
        definition = self.get_workflow(workflow_id)
        return not _contains_secret_like_content(_canonical(definition.to_dict()))

    def credential_isolation(self, workflow_id: str) -> bool:
        """Confirm workflow definitions hold no credentials."""
        definition = self.get_workflow(workflow_id)
        text = _canonical(definition.to_dict())
        return "api_key" not in text.lower() and "password" not in text.lower()

    # -- reporting / diagnostics -------------------------------------------

    def execution_report(self, execution_id: str | None = None) -> dict[str, Any]:
        if execution_id:
            exec_obj = self.get_execution(execution_id)
            return {
                "execution_id": execution_id,
                "workflow_id": exec_obj.workflow_id,
                "status": exec_obj.status,
                "nodes": len(exec_obj.node_executions),
                "completed_nodes": sum(
                    1 for ne in exec_obj.node_executions if ne.status == NodeStatus.COMPLETED.value
                ),
                "trigger": exec_obj.trigger,
            }
        by_status: dict[str, int] = {}
        for exec_obj in self._executions.values():
            by_status[exec_obj.status] = by_status.get(exec_obj.status, 0) + 1
        return {
            "total_executions": len(self._executions),
            "total_workflows": len(self._workflows),
            "by_execution_status": by_status,
            "audit_events": len(self._events),
        }

    def audit_trail(self) -> tuple[dict[str, str], ...]:
        return tuple(dict(e) for e in self._events)

    def events(self) -> tuple[dict[str, str], ...]:
        return self.audit_trail()

    def diagnostics(self) -> dict[str, Any]:
        failed_execs = [
            eid for eid, e in self._executions.items()
            if e.status == WorkflowStatus.FAILED.value
        ]
        return {
            "workflow_path": str(self.workflow_path),
            "execution_path": str(self.execution_path),
            "workflows": len(self._workflows),
            "executions": len(self._executions),
            "failed": failed_execs,
            "event_count": len(self._events),
            "integrity": {
                "writable": self.state_dir.is_dir(),
                "events_append_only": True,
            },
        }

    def health_check(self) -> dict[str, Any]:
        failed = sum(
            1 for e in self._executions.values()
            if e.status == WorkflowStatus.FAILED.value
        )
        return {
            "ok": failed == 0,
            "workflows": len(self._workflows),
            "executions": len(self._executions),
            "failed": failed,
        }

    # -- CLI / API / MCP interfaces -----------------------------------------

    def cli_payload(self) -> dict[str, Any]:
        return {
            "workflows": [w.to_dict() for w in self.list_workflows()],
            "executions": [e.to_dict() for e in self.list_executions()],
            "health": self.health_check(),
        }

    def api_payload(self, workflow_id: str | None = None, execution_id: str | None = None) -> dict[str, Any]:
        if workflow_id:
            return {"workflow": self.get_workflow(workflow_id).to_dict()}
        if execution_id:
            return {"execution": self.get_execution(execution_id).to_dict()}
        return {
            "workflows": [w.to_dict() for w in self.list_workflows()],
            "executions": [e.to_dict() for e in self.list_executions()],
            "health": self.health_check(),
        }

    def mcp_payload(self) -> dict[str, Any]:
        view = []
        for w in self.list_workflows():
            view.append({
                "workflow_id": w.workflow_id,
                "name": w.name,
                "version": w.version,
                "trigger": w.trigger,
                "nodes": len(w.nodes),
            })
        return {"workflows": view, "count": len(view), "health": self.health_check()}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_variable(name: str, variables: Mapping[str, Any]) -> Any:
    """Resolve a variable name from the variables mapping."""
    name = name.strip()
    if name in variables:
        return variables[name]
    if name.startswith("variables."):
        key = name[len("variables."):]
        return variables.get(key)
    if name.startswith("var."):
        key = name[len("var."):]
        return variables.get(key)
    allowed = ("true", "false", "yes", "no", "on", "off", "null", "none")
    lowered = name.lower()
    if lowered == "true" or lowered == "yes" or lowered == "on":
        return True
    if lowered == "false" or lowered == "no" or lowered == "off":
        return False
    if lowered in ("null", "none"):
        return None
    try:
        return int(name)
    except ValueError:
        pass
    try:
        return float(name)
    except ValueError:
        pass
    return name


def _truthy(value: str, variables: Mapping[str, Any]) -> bool:
    """Evaluate a bare identifier as a truthy check."""
    resolved = _resolve_variable(value, variables)
    if isinstance(resolved, bool):
        return resolved
    if resolved is None:
        return False
    if isinstance(resolved, (int, float)):
        return resolved != 0
    text = str(resolved).strip().lower()
    if text in ("true", "yes", "on", "1"):
        return True
    if text in ("false", "no", "off", "0", ""):
        return False
    return bool(resolved)
