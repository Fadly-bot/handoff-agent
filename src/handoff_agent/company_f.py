"""Development Company F Coordination Contract (revised roadmap Phase 30).

Formal coordination layer between the AI Council, Planning Council, Coding
Agents, Handoff Agent, Quality Guardian, Deployment Check, and humans.

The contract guarantees:

- only the AI Council (or a human approver) makes GO / NO-GO decisions;
- the Planning Council may only create a Work Order after a GO decision;
- a Coding Agent only ever receives a Work Order that matches its declared
  capabilities (capability verification — never assumed, never a silent
  fallback to another agent);
- the Handoff Agent rejects stale checkpoints or checkpoints without valid
  ownership;
- the Quality Guardian may issue PASS / FAIL / REQUIRE_FIX;
- the Deployment Check refuses to release a Work Order without a Quality PASS;
- mandatory human approval can never be bypassed;
- conflicting decisions produce a safe CONFLICT state that requires review;
- an agent that fails or stops never loses a Work Order (in-memory store plus
  optional durable JSON persistence);
- every transition is recorded as immutable audit evidence.

The runtime uses only the Python standard library. This module is the basis
for the observability (31), policy (32), adapter/tool/sandbox (33),
reliability (34), and operations (35) layers.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from handoff_agent.persistence import _contains_secret_like_content
from handoff_agent.telemetry import (
    TelemetryDomain,
    TelemetryStatus,
    emit_event,
    make_trace_id,
    start_trace_span,
)


def new_id(prefix: str) -> str:
    """Deterministic-ish, collision-resistant id with a readable prefix."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _now() -> int:
    return int(time.time() * 1000)


def _iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Roles & identities
# ---------------------------------------------------------------------------


class CompanyRole(str, Enum):
    """Roles defined by the Development Company F organization."""

    AI_COUNCIL = "ai_council"
    PLANNING_COUNCIL = "planning_council"
    CODING_AGENT = "coding_agent"
    HANDOFF_AGENT = "handoff_agent"
    QUALITY_GUARDIAN = "quality_guardian"
    DEPLOYMENT_CHECK = "deployment_check"
    HUMAN = "human"


MIN_TRUST_BY_ACTION: dict[str, int] = {
    "decide": 3,          # AI Council / human only
    "plan": 2,            # Planning Council
    "execute": 2,         # Coding Agent
    "checkpoint": 2,      # Handoff Agent / Coding Agent
    "quality_gate": 3,    # Quality Guardian
    "deployment_gate": 3, # Deployment Check
    "approve": 5,         # human approval boundary
}


@dataclass(frozen=True)
class RoleIdentity:
    """A role instance: who it is, what it can do, how much it is trusted."""

    role: CompanyRole
    agent_id: str
    name: str = ""
    capabilities: frozenset[str] = frozenset()
    trust_level: int = 1

    def requires(self, action: str) -> bool:
        return self.trust_level >= MIN_TRUST_BY_ACTION[action]

    def can_do(self, capability: str) -> bool:
        return capability in self.capabilities


# ---------------------------------------------------------------------------
# Coordination artifacts
# ---------------------------------------------------------------------------


class DecisionStatus(str, Enum):
    GO = "go"
    NO_GO = "no_go"
    PENDING = "pending"


@dataclass(frozen=True)
class Decision:
    decision_id: str
    project_id: str
    decision_status: DecisionStatus
    made_by: str  # agent_id of the AI Council member / human
    rationale: str
    timestamp_ms: int
    work_orders_created: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WorkOrderStatus(str, Enum):
    PENDING = "pending"
    PLANNED = "planned"
    EXECUTING = "executing"
    CHECKPOINTED = "checkpointed"
    QUALITY_GATED = "quality_gated"
    DEPLOYMENT_GATED = "deployment_gated"
    RELEASED = "released"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CONFLICT = "conflict"


class QualityVerdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    REQUIRE_FIX = "require_fix"


class DeploymentVerdict(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class CheckpointState(str, Enum):
    VALID = "valid"
    STALE = "stale"
    NO_OWNERSHIP = "no_ownership"
    CONFLICT = "conflict"


class HandoffVerdict(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"


@dataclass
class ProjectIdentity:
    project_id: str
    name: str
    root: str = ""


@dataclass
class ApprovalRequirement:
    required: bool
    approver_role: CompanyRole = CompanyRole.HUMAN
    note: str = ""
    approved_by: str | None = None
    approved_at_ms: int | None = None
    satisfied: bool = False


@dataclass
class Risk:
    risk_id: str
    description: str
    severity: str  # low | medium | high | critical
    mitigation: str = ""


@dataclass
class Plan:
    work_order_id: str
    strategy: str
    risks: list[Risk] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkOrder:
    work_order_id: str
    project_id: str
    decision_id: str
    title: str
    description: str
    required_capabilities: frozenset[str] = frozenset()
    status: WorkOrderStatus = WorkOrderStatus.PENDING
    assigned_agent: str | None = None
    plan: Plan | None = None
    approval: ApprovalRequirement = field(
        default_factory=lambda: ApprovalRequirement(required=False)
    )
    owner: str | None = None
    lease_holder: str | None = None
    lease_expires_at_ms: int | None = None
    last_checkpoint_id: str | None = None
    last_quality_verdict: str | None = None
    last_deployment_verdict: str | None = None
    created_at_ms: int = field(default_factory=_now)
    updated_at_ms: int = field(default_factory=_now)
    duplicate_of: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_order_id": self.work_order_id,
            "project_id": self.project_id,
            "decision_id": self.decision_id,
            "title": self.title,
            "description": self.description,
            "required_capabilities": sorted(self.required_capabilities),
            "status": self.status.value,
            "assigned_agent": self.assigned_agent,
            "plan": self.plan.to_dict() if self.plan else None,
            "approval": asdict(self.approval),
            "owner": self.owner,
            "lease_holder": self.lease_holder,
            "lease_expires_at_ms": self.lease_expires_at_ms,
            "last_checkpoint_id": self.last_checkpoint_id,
            "last_quality_verdict": self.last_quality_verdict,
            "last_deployment_verdict": self.last_deployment_verdict,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "duplicate_of": self.duplicate_of,
        }


@dataclass
class Checkpoint:
    checkpoint_id: str
    work_order_id: str
    owner: str
    producer: str
    consumer: str | None
    summary: str
    state: dict[str, Any]
    state_check: CheckpointState
    created_at_ms: int = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "work_order_id": self.work_order_id,
            "owner": self.owner,
            "producer": self.producer,
            "consumer": self.consumer,
            "summary": self.summary,
            "state_check": self.state_check.value,
            "created_at_ms": self.created_at_ms,
        }


@dataclass
class QualityGateResult:
    gate_id: str
    work_order_id: str
    verdict: QualityVerdict
    reviewer: str
    issues: list[str] = field(default_factory=list)
    evidence: str = ""
    created_at_ms: int = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DeploymentGateResult:
    gate_id: str
    work_order_id: str
    verdict: DeploymentVerdict
    reviewer: str
    rollback_ready: bool = False
    note: str = ""
    created_at_ms: int = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AuditEvidence:
    event_id: str
    work_order_id: str
    actor: str
    action: str
    from_state: str
    to_state: str
    reason: str
    timestamp_ms: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HandoffRejection(Exception):
    """Raised when the Handoff Agent rejects a checkpoint or handoff."""


class NoApproval(Exception):
    """Raised when a transition requires an approval that is missing."""


class DuplicateWork(Exception):
    """Raised when an identical active work order already exists."""


class CoordinatorViolation(Exception):
    """Raised when the coordination contract is violated (bypass attempt)."""


class InvalidTransition(Exception):
    """Raised for a state transition that the contract forbids."""


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

# Transition table: state -> set of allowed successor states.
ALLOWED_TRANSITIONS: dict[WorkOrderStatus, set[WorkOrderStatus]] = {
    WorkOrderStatus.PENDING: {
        WorkOrderStatus.PLANNED,
        WorkOrderStatus.CANCELLED,
        WorkOrderStatus.CONFLICT,
    },
    WorkOrderStatus.PLANNED: {
        WorkOrderStatus.EXECUTING,
        WorkOrderStatus.CANCELLED,
        WorkOrderStatus.REJECTED,
    },
    WorkOrderStatus.EXECUTING: {
        WorkOrderStatus.CHECKPOINTED,
        WorkOrderStatus.FAILED,
        WorkOrderStatus.REJECTED,
        WorkOrderStatus.CANCELLED,
        WorkOrderStatus.CONFLICT,
    },
    WorkOrderStatus.CHECKPOINTED: {
        WorkOrderStatus.EXECUTING,  # resumed after FAIL/REQUIRE_FIX
        WorkOrderStatus.QUALITY_GATED,
        WorkOrderStatus.FAILED,
        WorkOrderStatus.REJECTED,
        WorkOrderStatus.CONFLICT,
    },
    WorkOrderStatus.QUALITY_GATED: {
        WorkOrderStatus.CHECKPOINTED,  # REQUIRE_FIX
        WorkOrderStatus.EXECUTING,     # FAIL
        WorkOrderStatus.DEPLOYMENT_GATED,
        WorkOrderStatus.REJECTED,
    },
    WorkOrderStatus.DEPLOYMENT_GATED: {
        WorkOrderStatus.RELEASED,
        WorkOrderStatus.QUALITY_GATED,  # re-review
        WorkOrderStatus.REJECTED,
    },
    WorkOrderStatus.RELEASED: set(),
    WorkOrderStatus.REJECTED: {WorkOrderStatus.PLANNED},
    WorkOrderStatus.FAILED: {WorkOrderStatus.EXECUTING},
    WorkOrderStatus.CANCELLED: set(),
    WorkOrderStatus.CONFLICT: {
        WorkOrderStatus.PENDING,
        WorkOrderStatus.PLANNED,
        WorkOrderStatus.EXECUTING,
    },
}


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class CompanyFCoordinator:
    """Central coordination contract for Development Company F.

    Thread-safe. State may be persisted to a JSON directory with
    :meth:`save` / :meth:`load` so that an agent failing or stopping never
    loses a work order.
    """

    def __init__(
        self,
        state_dir: str | Path | None = None,
        *,
        tracer: Any | None = None,
    ) -> None:
        self.state_dir = Path(state_dir) if state_dir else None
        self._lock = threading.RLock()
        self._projects: dict[str, ProjectIdentity] = {}
        self._roles: dict[str, RoleIdentity] = {}
        self._revoked: set[str] = set()
        self._decisions: dict[str, Decision] = {}
        self._plans: dict[str, Plan] = {}
        self._work_orders: dict[str, WorkOrder] = {}
        self._checkpoints: dict[str, Checkpoint] = {}
        self._quality_gates: dict[str, QualityGateResult] = {}
        self._deployment_gates: dict[str, DeploymentGateResult] = {}
        self._audit: list[AuditEvidence] = []
        self._tracer = tracer
        self._project_trace_ids: dict[str, str] = {}
        self._project_span_ids: dict[str, str] = {}
        if self.state_dir:
            self.load()

    # -- registration ----------------------------------------------------

    def register_project(self, project: ProjectIdentity) -> str:
        with self._lock:
            self._projects[project.project_id] = project
            self._trace(
                domain=TelemetryDomain.PROJECT.value,
                operation="project.register",
                status=TelemetryStatus.OK.value,
                project_id=project.project_id,
                resource=project.project_id,
                actor="system",
                metadata={"name": project.name, "root": project.root},
            )
            return project.project_id

    def register_role(self, identity: RoleIdentity) -> str:
        with self._lock:
            self._roles[identity.agent_id] = identity
            return identity.agent_id

    def revoke_role(self, agent_id: str) -> None:
        with self._lock:
            self._revoked.add(agent_id)

    def _authorize(self, identity: RoleIdentity | str, action: str) -> RoleIdentity:
        agent_id = identity.agent_id if isinstance(identity, RoleIdentity) else identity
        if agent_id in self._revoked:
            raise CoordinatorViolation(f"identity {agent_id} is revoked")
        role = self._roles.get(agent_id)
        if role is None:
            raise CoordinatorViolation(f"identity {agent_id} is not registered")
        if not role.requires(action):
            raise CoordinatorViolation(
                f"identity {agent_id} (trust={role.trust_level}) cannot perform {action!r}"
            )
        return role

    def _ensure_project(self, project_id: str) -> None:
        if project_id not in self._projects:
            raise CoordinatorViolation(f"unknown project {project_id}")

    def _audit_event(
        self,
        work_order_id: str,
        actor: str,
        action: str,
        from_state: str,
        to_state: str,
        reason: str,
    ) -> AuditEvidence:
        evt = AuditEvidence(
            event_id=new_id("evt"),
            work_order_id=work_order_id,
            actor=actor,
            action=action,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
            timestamp_ms=_now(),
        )
        self._audit.append(evt)
        return evt

    def _trace_project(self, project_id: str) -> tuple[str, str]:
        if project_id not in self._project_trace_ids:
            trace_id = make_trace_id()
            span_id = start_trace_span(
                self._tracer,
                domain=TelemetryDomain.PROJECT.value,
                operation="project.trace",
                resource=project_id,
                trace_id=trace_id,
                actor="coordinator",
            )
            self._project_trace_ids[project_id] = trace_id
            self._project_span_ids[project_id] = span_id
        return self._project_trace_ids[project_id], self._project_span_ids[project_id]

    def _trace(
        self,
        *,
        domain: str,
        operation: str,
        status: str = TelemetryStatus.OK.value,
        project_id: str = "",
        resource: str = "",
        actor: str = "system",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self._tracer is None:
            return
        trace_id, parent_span_id = "", ""
        if project_id and project_id in self._projects:
            trace_id, parent_span_id = self._trace_project(project_id)
        emit_event(
            self._tracer,
            domain=domain,
            operation=operation,
            status=status,
            resource=resource or project_id,
            actor=actor,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            metadata=metadata,
        )

    def _transition(
        self,
        work_order: WorkOrder,
        status: WorkOrderStatus,
        actor: str,
        action: str,
        reason: str,
    ) -> None:
        if status not in ALLOWED_TRANSITIONS.get(work_order.status, set()):
            raise InvalidTransition(
                f"{work_order.status.value} -> {status.value} is not allowed"
            )
        from_state = work_order.status
        work_order.status = status
        work_order.updated_at_ms = _now()
        self._audit_event(
            work_order.work_order_id, actor, action, from_state.value, status.value, reason
        )

    # -- decisions -------------------------------------------------------

    def make_decision(
        self,
        identity: RoleIdentity | str,
        project_id: str,
        status: DecisionStatus,
        rationale: str,
    ) -> Decision:
        with self._lock:
            role = self._authorize(identity, "decide")
            if role.role not in (CompanyRole.AI_COUNCIL, CompanyRole.HUMAN):
                raise CoordinatorViolation(
                    f"role {role.role.value} cannot make a GO / NO-GO decision"
                )
            self._ensure_project(project_id)
            decision = Decision(
                decision_id=new_id("dec"),
                project_id=project_id,
                decision_status=status,
                made_by=role.agent_id,
                rationale=rationale,
                timestamp_ms=_now(),
            )
            existing = [d for d in self._decisions.values() if d.project_id == project_id]
            if existing and self._active_go(project_id):
                if status != existing[-1].decision_status:
                    # conflicting decision -> safe CONFLICT state
                    for wo in self._work_orders.values():
                        if wo.project_id == project_id and wo.status in (
                            WorkOrderStatus.PENDING,
                            WorkOrderStatus.EXECUTING,
                            WorkOrderStatus.CHECKPOINTED,
                        ):
                            self._transition(
                                wo,
                                WorkOrderStatus.CONFLICT,
                                role.agent_id,
                                "decision_conflict",
                                "conflicting decision requires review",
                            )
            self._decisions[decision.decision_id] = decision
            self._trace(
                domain=TelemetryDomain.DECISION.value,
                operation="decision.make",
                status=(
                    TelemetryStatus.OK.value
                    if status == DecisionStatus.GO
                    else TelemetryStatus.BLOCKED.value
                ),
                project_id=project_id,
                resource=project_id,
                actor=role.agent_id,
                metadata={
                    "decision_id": decision.decision_id,
                    "status": status.value,
                    "conflict": any(
                        wo.status == WorkOrderStatus.CONFLICT
                        for wo in self._work_orders.values()
                        if wo.project_id == project_id
                    ),
                },
            )
            return decision

    def _active_go(self, project_id: str) -> bool:
        for d in self._decisions.values():
            if d.project_id == project_id and d.decision_status == DecisionStatus.GO:
                return bool(d.work_orders_created)
        return False

    def latest_decision(self, project_id: str) -> Decision | None:
        for decision in reversed(list(self._decisions.values())):
            if decision.project_id == project_id:
                return decision
        return None

    # -- work orders -----------------------------------------------------

    def _fingerprint(self, title: str, description: str) -> str:
        raw = f"{title.strip().lower()}:::{description.strip().lower()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def create_work_order(
        self,
        identity: RoleIdentity | str,
        project_id: str,
        title: str,
        description: str,
        required_capabilities: Iterable[str] = (),
        *,
        approval_required: bool = False,
        approval_note: str = "",
    ) -> WorkOrder:
        with self._lock:
            role = self._authorize(identity, "plan")
            if role.role != CompanyRole.PLANNING_COUNCIL:
                raise CoordinatorViolation(
                    f"role {role.role.value} cannot create a Work Order"
                )
            self._ensure_project(project_id)
            decision = self.latest_decision(project_id)
            if decision is None or decision.decision_status != DecisionStatus.GO:
                raise CoordinatorViolation(
                    "Work Order creation requires a prior GO decision"
                )
            # duplicate prevention
            fp = self._fingerprint(title, description)
            for existing in self._work_orders.values():
                if (
                    existing.project_id == project_id
                    and existing.duplicate_of is None
                    and self._fingerprint(existing.title, existing.description) == fp
                    and existing.status
                    not in (WorkOrderStatus.REJECTED, WorkOrderStatus.CANCELLED, WorkOrderStatus.RELEASED)
                ):
                    raise DuplicateWork(
                        f"duplicate work order already active: {existing.work_order_id}"
                    )
            approval = ApprovalRequirement(
                required=approval_required,
                approver_role=CompanyRole.HUMAN,
                note=approval_note,
            )
            work_order = WorkOrder(
                work_order_id=new_id("wo"),
                project_id=project_id,
                decision_id=decision.decision_id,
                title=title,
                description=description,
                required_capabilities=frozenset(required_capabilities),
                approval=approval,
            )
            decision.work_orders_created.append(work_order.work_order_id)
            self._work_orders[work_order.work_order_id] = work_order
            self._audit_event(
                work_order.work_order_id,
                role.agent_id,
                "work_order.created",
                "none",
                WorkOrderStatus.PENDING.value,
                "work order registered after GO decision",
            )
            self._trace(
                domain=TelemetryDomain.WORKORDER.value,
                operation="workorder.create",
                status=TelemetryStatus.OK.value,
                project_id=project_id,
                resource=work_order.work_order_id,
                actor=role.agent_id,
                metadata={
                    "work_order_id": work_order.work_order_id,
                    "decision_id": decision.decision_id,
                    "approval_required": approval_required,
                },
            )
            return work_order

    def get_work_order(self, work_order_id: str) -> WorkOrder | None:
        with self._lock:
            return self._work_orders.get(work_order_id)

    def list_work_orders(self) -> list[WorkOrder]:
        with self._lock:
            return sorted(self._work_orders.values(), key=lambda w: w.created_at_ms)

    # -- planning & approval --------------------------------------------

    def add_plan(
        self,
        identity: RoleIdentity | str,
        work_order_id: str,
        strategy: str,
        risks: Iterable[Risk] = (),
    ) -> Plan:
        with self._lock:
            role = self._authorize(identity, "plan")
            if role.role != CompanyRole.PLANNING_COUNCIL:
                raise CoordinatorViolation(
                    f"role {role.role.value} cannot attach a plan"
                )
            work_order = self._require_work_order(work_order_id)
            if work_order.status != WorkOrderStatus.PENDING:
                raise InvalidTransition("plan may only be attached to a PENDING work order")
            plan = Plan(
                work_order_id=work_order_id,
                strategy=strategy,
                risks=[Risk(**r) if isinstance(r, dict) else r for r in risks],
            )
            work_order.plan = plan
            self._transition(
                work_order,
                WorkOrderStatus.PLANNED,
                role.agent_id,
                "plan.attached",
                "plan and risk register attached",
            )
            self._trace(
                domain=TelemetryDomain.WORKORDER.value,
                operation="workorder.plan",
                status=TelemetryStatus.OK.value,
                project_id=work_order.project_id,
                resource=work_order.work_order_id,
                actor=role.agent_id,
                metadata={
                    "work_order_id": work_order.work_order_id,
                    "strategy": strategy,
                    "risk_count": len(plan.risks),
                },
            )
            return plan

    def approve_work_order(
        self,
        identity: RoleIdentity | str,
        work_order_id: str,
        note: str = "",
    ) -> ApprovalRequirement:
        with self._lock:
            role = self._authorize(identity, "approve")
            work_order = self._require_work_order(work_order_id)
            if not work_order.approval.required:
                raise CoordinatorViolation("this work order does not require approval")
            work_order.approval.satisfied = True
            work_order.approval.approved_by = role.agent_id
            work_order.approval.approved_at_ms = _now()
            self._audit_event(
                work_order.work_order_id,
                role.agent_id,
                "approval.granted",
                work_order.status.value,
                work_order.status.value,
                note or "human approval granted",
            )
            self._trace(
                domain=TelemetryDomain.WORKORDER.value,
                operation="workorder.approve",
                status=TelemetryStatus.OK.value,
                project_id=work_order.project_id,
                resource=work_order.work_order_id,
                actor=role.agent_id,
                metadata={
                    "work_order_id": work_order.work_order_id,
                    "approval_satisfied": True,
                },
            )
            return work_order.approval

    def _require_approval(self, work_order: WorkOrder, actor: str) -> None:
        if work_order.approval.required and not work_order.approval.satisfied:
            raise NoApproval(
                f"work order {work_order.work_order_id} requires approval "
                "that has not been granted"
            )

    # -- execution & routing --------------------------------------------

    def assign_agent(
        self,
        identity: RoleIdentity | str,
        work_order_id: str,
        agent: RoleIdentity,
    ) -> str:
        with self._lock:
            role = self._authorize(identity, "plan")
            if role.role != CompanyRole.PLANNING_COUNCIL:
                raise CoordinatorViolation("only the Planning Council assigns agents")
            if agent.agent_id in self._revoked:
                raise CoordinatorViolation(f"agent {agent.agent_id} is revoked")
            if agent.role != CompanyRole.CODING_AGENT:
                raise CoordinatorViolation("only Coding Agents may be assigned")
            work_order = self._require_work_order(work_order_id)
            missing = set(work_order.required_capabilities) - set(agent.capabilities)
            if missing:
                raise CoordinatorViolation(
                    f"agent {agent.agent_id} lacks required capability(ies): "
                    f"{','.join(sorted(missing))} — refusing silent fallback"
                )
            work_order.assigned_agent = agent.agent_id
            work_order.owner = agent.agent_id
            self._transition(
                work_order,
                WorkOrderStatus.EXECUTING,
                role.agent_id,
                "work_order.assigned",
                f"assigned to {agent.agent_id} after capability verification",
            )
            self._trace(
                domain=TelemetryDomain.WORKORDER.value,
                operation="workorder.assign",
                status=TelemetryStatus.OK.value,
                project_id=work_order.project_id,
                resource=work_order.work_order_id,
                actor=role.agent_id,
                metadata={
                    "work_order_id": work_order.work_order_id,
                    "agent_id": agent.agent_id,
                    "capabilities_verified": sorted(work_order.required_capabilities),
                },
            )
            return agent.agent_id

    def acquire_lease(self, work_order_id: str, agent: RoleIdentity, ttl_ms: int = 600_000) -> None:
        with self._lock:
            work_order = self._require_work_order(work_order_id)
            if work_order.owner != agent.agent_id:
                raise CoordinatorViolation(
                    f"agent {agent.agent_id} is not the owner of {work_order_id}"
                )
            if work_order.lease_holder and work_order.lease_holder != agent.agent_id:
                raise CoordinatorViolation(f"lease held by {work_order.lease_holder}")
            work_order.lease_holder = agent.agent_id
            work_order.lease_expires_at_ms = _now() + ttl_ms

    def release_lease(self, work_order_id: str, agent: RoleIdentity) -> None:
        with self._lock:
            work_order = self._require_work_order(work_order_id)
            if work_order.lease_holder == agent.agent_id:
                work_order.lease_holder = None
                work_order.lease_expires_at_ms = None

    def _owner_valid(self, work_order: WorkOrder, owner: str) -> bool:
        if work_order.owner is None or work_order.owner != owner:
            return False
        if work_order.lease_holder is not None and work_order.lease_holder != owner:
            return False
        return True

    # -- checkpoint / handoff -------------------------------------------

    def submit_checkpoint(
        self,
        identity: RoleIdentity | str,
        work_order_id: str,
        summary: str,
        state: dict[str, Any],
        *,
        consumer: str | None = None,
    ) -> Checkpoint:
        with self._lock:
            role = self._authorize(identity, "checkpoint")
            work_order = self._require_work_order(work_order_id)
            reason: str | None = None
            check = CheckpointState.VALID
            if not self._owner_valid(work_order, role.agent_id):
                check = CheckpointState.NO_OWNERSHIP
                reason = "checkpoint submitted without valid ownership"
            elif work_order.status == WorkOrderStatus.CONFLICT:
                check = CheckpointState.CONFLICT
                reason = "work order is in CONFLICT and requires review"
            elif work_order.status not in (
                WorkOrderStatus.EXECUTING,
                WorkOrderStatus.CHECKPOINTED,
            ):
                check = CheckpointState.STALE
                reason = f"checkpoint submitted for stale state {work_order.status.value}"
            if check != CheckpointState.VALID:
                raise HandoffRejection(reason or f"checkpoint rejected ({check.value})")
            serialized = json.dumps(state, sort_keys=True, default=str)
            if _contains_secret_like_content(serialized):
                raise HandoffRejection("checkpoint payload looks like it contains a secret")
            checkpoint = Checkpoint(
                checkpoint_id=new_id("cp"),
                work_order_id=work_order_id,
                owner=role.agent_id,
                producer=role.agent_id,
                consumer=consumer,
                summary=summary,
                state=state,
                state_check=check,
            )
            self._checkpoints[checkpoint.checkpoint_id] = checkpoint
            work_order.last_checkpoint_id = checkpoint.checkpoint_id
            self._transition(
                work_order,
                WorkOrderStatus.CHECKPOINTED,
                role.agent_id,
                "checkpoint.accepted",
                "handoff accepted valid checkpoint",
            )
            self._trace(
                domain=TelemetryDomain.WORKORDER.value,
                operation="workorder.checkpoint",
                status=TelemetryStatus.OK.value,
                project_id=work_order.project_id,
                resource=work_order.work_order_id,
                actor=role.agent_id,
                metadata={
                    "work_order_id": work_order.work_order_id,
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "consumer": consumer or "",
                },
            )
            return checkpoint

    def evaluate_handoff(
        self,
        identity: RoleIdentity | str,
        checkpoint: Checkpoint,
    ) -> HandoffVerdict:
        with self._lock:
            role = self._authorize(identity, "checkpoint")
            if role.role not in (CompanyRole.HANDOFF_AGENT, CompanyRole.CODING_AGENT):
                raise CoordinatorViolation(
                    f"role {role.role.value} cannot evaluate a handoff"
                )
            if checkpoint.work_order_id not in {
                cp.work_order_id for cp in self._checkpoints.values()
            }:
                verdict = HandoffVerdict.REJECT
                handoff_project = ""
            else:
                work_order = self._require_work_order(checkpoint.work_order_id)
                handoff_project = work_order.project_id
                if checkpoint.owner != work_order.owner:
                    verdict = HandoffVerdict.REJECT
                elif checkpoint.state_check != CheckpointState.VALID:
                    verdict = HandoffVerdict.REJECT
                else:
                    verdict = HandoffVerdict.ACCEPT
            self._trace(
                domain=TelemetryDomain.HANDOFF.value,
                operation=(
                    "handoff.accept" if verdict == HandoffVerdict.ACCEPT else "handoff.reject"
                ),
                status=(
                    TelemetryStatus.OK.value
                    if verdict == HandoffVerdict.ACCEPT
                    else TelemetryStatus.BLOCKED.value
                ),
                project_id=handoff_project,
                resource=checkpoint.work_order_id,
                actor=role.agent_id,
                metadata={
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "verdict": verdict.value,
                    "checkpoint_owner": checkpoint.owner,
                },
            )
            return verdict

    # -- quality & deployment gates -------------------------------------

    def quality_gate(
        self,
        identity: RoleIdentity | str,
        work_order_id: str,
        verdict: QualityVerdict,
        issues: Iterable[str] = (),
        evidence: str = "",
    ) -> QualityGateResult:
        with self._lock:
            role = self._authorize(identity, "quality_gate")
            if role.role != CompanyRole.QUALITY_GUARDIAN:
                raise CoordinatorViolation(
                    f"role {role.role.value} cannot pass a quality verdict"
                )
            work_order = self._require_work_order(work_order_id)
            if work_order.status != WorkOrderStatus.CHECKPOINTED:
                raise InvalidTransition(
                    "quality gate requires a valid checkpointed work order"
                )
            result = QualityGateResult(
                gate_id=new_id("qg"),
                work_order_id=work_order_id,
                verdict=verdict,
                reviewer=role.agent_id,
                issues=list(issues),
                evidence=evidence,
            )
            self._quality_gates[result.gate_id] = result
            work_order.last_quality_verdict = verdict.value
            if verdict == QualityVerdict.PASS:
                self._transition(
                    work_order,
                    WorkOrderStatus.QUALITY_GATED,
                    role.agent_id,
                    "quality.gate",
                    "quality verdict PASS",
                )
            elif verdict == QualityVerdict.REQUIRE_FIX:
                self._transition(
                    work_order,
                    WorkOrderStatus.EXECUTING,
                    role.agent_id,
                    "quality.require_fix",
                    "quality requires rework",
                )
            else:
                self._transition(
                    work_order,
                    WorkOrderStatus.EXECUTING,
                    role.agent_id,
                    "quality.fail",
                    "quality failed — returning for rework",
                )
            self._trace(
                domain=TelemetryDomain.QUALITY.value,
                operation="quality.gate",
                status=(
                    TelemetryStatus.OK.value
                    if verdict == QualityVerdict.PASS
                    else TelemetryStatus.DEGRADED.value
                    if verdict == QualityVerdict.REQUIRE_FIX
                    else TelemetryStatus.ERROR.value
                ),
                project_id=work_order.project_id,
                resource=work_order.work_order_id,
                actor=role.agent_id,
                metadata={
                    "work_order_id": work_order.work_order_id,
                    "gate_id": result.gate_id,
                    "verdict": verdict.value,
                    "issues": list(issues)[:10],
                },
            )
            return result

    def deployment_gate(
        self,
        identity: RoleIdentity | str,
        work_order_id: str,
        rollback_ready: bool = False,
        note: str = "",
    ) -> DeploymentGateResult:
        with self._lock:
            role = self._authorize(identity, "deployment_gate")
            if role.role != CompanyRole.DEPLOYMENT_CHECK:
                raise CoordinatorViolation(
                    f"role {role.role.value} cannot run the deployment gate"
                )
            work_order = self._require_work_order(work_order_id)
            self._require_approval(work_order, role.agent_id)
            if work_order.status != WorkOrderStatus.QUALITY_GATED:
                raise InvalidTransition(
                    "deployment gate requires a quality PASSed work order"
                )
            if work_order.last_quality_verdict != QualityVerdict.PASS.value:
                raise InvalidTransition("deployment gate requires a Quality PASS")
            result = DeploymentGateResult(
                gate_id=new_id("dg"),
                work_order_id=work_order_id,
                verdict=DeploymentVerdict.APPROVE,
                reviewer=role.agent_id,
                rollback_ready=rollback_ready,
                note=note,
            )
            self._deployment_gates[result.gate_id] = result
            work_order.last_deployment_verdict = result.verdict.value
            self._transition(
                work_order,
                WorkOrderStatus.DEPLOYMENT_GATED,
                role.agent_id,
                "deployment.gate",
                "deployment approved with rollback readiness",
            )
            self._trace(
                domain=TelemetryDomain.DEPLOYMENT.value,
                operation="deployment.gate",
                status=TelemetryStatus.OK.value,
                project_id=work_order.project_id,
                resource=work_order.work_order_id,
                actor=role.agent_id,
                metadata={
                    "work_order_id": work_order.work_order_id,
                    "gate_id": result.gate_id,
                    "rollback_ready": rollback_ready,
                },
            )
            return result

    def release(self, identity: RoleIdentity | str, work_order_id: str) -> WorkOrder:
        with self._lock:
            role = self._authorize(identity, "deployment_gate")
            if role.role != CompanyRole.DEPLOYMENT_CHECK:
                raise CoordinatorViolation("only the Deployment Check may release")
            work_order = self._require_work_order(work_order_id)
            self._require_approval(work_order, role.agent_id)
            if work_order.status != WorkOrderStatus.DEPLOYMENT_GATED:
                raise InvalidTransition(
                    "release requires a deployment-gated work order"
                )
            self._transition(
                work_order,
                WorkOrderStatus.RELEASED,
                role.agent_id,
                "work_order.released",
                "released to production",
            )
            self._trace(
                domain=TelemetryDomain.WORKORDER.value,
                operation="workorder.release",
                status=TelemetryStatus.OK.value,
                project_id=work_order.project_id,
                resource=work_order.work_order_id,
                actor=role.agent_id,
                metadata={"work_order_id": work_order.work_order_id},
            )
            return work_order

    # -- failure handling --------------------------------------------------

    def mark_failed(
        self,
        identity: RoleIdentity | str,
        work_order_id: str,
        reason: str,
    ) -> WorkOrder:
        with self._lock:
            role = self._authorize(identity, "execute")
            work_order = self._require_work_order(work_order_id)
            self._transition(
                work_order,
                WorkOrderStatus.FAILED,
                role.agent_id,
                "work_order.failed",
                reason,
            )
            self._trace(
                domain=TelemetryDomain.WORKORDER.value,
                operation="workorder.failed",
                status=TelemetryStatus.ERROR.value,
                project_id=work_order.project_id,
                resource=work_order.work_order_id,
                actor=role.agent_id,
                metadata={"work_order_id": work_order.work_order_id},
            )
            return work_order

    def reject(self, identity: RoleIdentity | str, work_order_id: str, reason: str) -> WorkOrder:
        with self._lock:
            role = self._authorize(identity, "execute")
            work_order = self._require_work_order(work_order_id)
            self._transition(
                work_order,
                WorkOrderStatus.REJECTED,
                role.agent_id,
                "work_order.rejected",
                reason,
            )
            self._trace(
                domain=TelemetryDomain.WORKORDER.value,
                operation="workorder.reject",
                status=TelemetryStatus.BLOCKED.value,
                project_id=work_order.project_id,
                resource=work_order.work_order_id,
                actor=role.agent_id,
                metadata={"work_order_id": work_order.work_order_id},
            )
            return work_order

    def cancel(self, identity: RoleIdentity | str, work_order_id: str, reason: str) -> WorkOrder:
        with self._lock:
            role = self._authorize(identity, "plan")
            work_order = self._require_work_order(work_order_id)
            self._transition(
                work_order,
                WorkOrderStatus.CANCELLED,
                role.agent_id,
                "work_order.cancelled",
                reason,
            )
            self._trace(
                domain=TelemetryDomain.WORKORDER.value,
                operation="workorder.cancel",
                status=TelemetryStatus.CANCELLED.value,
                project_id=work_order.project_id,
                resource=work_order.work_order_id,
                actor=role.agent_id,
                metadata={"work_order_id": work_order.work_order_id},
            )
            return work_order

    def resume(self, identity: RoleIdentity | str, work_order_id: str) -> WorkOrder:
        with self._lock:
            role = self._authorize(identity, "execute")
            work_order = self._require_work_order(work_order_id)
            self._transition(
                work_order,
                WorkOrderStatus.EXECUTING,
                role.agent_id,
                "work_order.resumed",
                "recovered after failure or interruption",
            )
            self._trace(
                domain=TelemetryDomain.WORKORDER.value,
                operation="workorder.resume",
                status=TelemetryStatus.RETRY.value,
                project_id=work_order.project_id,
                resource=work_order.work_order_id,
                actor=role.agent_id,
                metadata={"work_order_id": work_order.work_order_id},
            )
            return work_order

    # -- query helpers ---------------------------------------------------

    def _require_work_order(self, work_order_id: str) -> WorkOrder:
        work_order = self._work_orders.get(work_order_id)
        if work_order is None:
            raise CoordinatorViolation(f"unknown work order {work_order_id}")
        return work_order

    def audit_trail(self, work_order_id: str | None = None) -> list[AuditEvidence]:
        with self._lock:
            events = self._audit if work_order_id is None else [
                e for e in self._audit if e.work_order_id == work_order_id
            ]
            return list(events)

    # -- persistence -------------------------------------------------------

    def _snap(self) -> dict[str, Any]:
        return {
            "version": 1,
            "projects": [asdict(p) for p in self._projects.values()],
            "roles": [
                {
                    "role": r.role.value,
                    "agent_id": r.agent_id,
                    "name": r.name,
                    "capabilities": sorted(r.capabilities),
                    "trust_level": r.trust_level,
                }
                for r in self._roles.values()
            ],
            "revoked": sorted(self._revoked),
            "decisions": [d.to_dict() for d in self._decisions.values()],
            "work_orders": [w.to_dict() for w in self._work_orders.values()],
            "checkpoints": [c.to_dict() for c in self._checkpoints.values()],
            "quality_gates": [g.to_dict() for g in self._quality_gates.values()],
            "deployment_gates": [g.to_dict() for g in self._deployment_gates.values()],
            "audit": [e.to_dict() for e in self._audit],
        }

    def save(self) -> Path:
        """Persist the full coordination state atomically."""
        if self.state_dir is None:
            raise CoordinatorViolation("no state_dir configured")
        with self._lock:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._snap(), sort_keys=True, indent=2)
        tmp = self.state_dir / f".company_f.{os.getpid()}.tmp"
        target = self.state_dir / "state.json"
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, target)
        return target

    def load(self) -> None:
        """Load persisted coordination state (idempotent, safe on missing file)."""
        if self.state_dir is None:
            return
        target = self.state_dir / "state.json"
        if not target.exists():
            return
        with self._lock:
            blob = json.loads(target.read_text(encoding="utf-8"))
            self._projects = {
                p["project_id"]: ProjectIdentity(**p) for p in blob.get("projects", [])
            }
            self._roles = {
                r["agent_id"]: RoleIdentity(
                    role=CompanyRole(r["role"]),
                    agent_id=r["agent_id"],
                    name=r.get("name", ""),
                    capabilities=frozenset(r.get("capabilities", [])),
                    trust_level=r.get("trust_level", 1),
                )
                for r in blob.get("roles", [])
            }
            self._revoked = set(blob.get("revoked", []))
            for d in blob.get("decisions", []):
                decision = Decision(
                    decision_id=d["decision_id"],
                    project_id=d["project_id"],
                    decision_status=DecisionStatus(d["decision_status"]),
                    made_by=d["made_by"],
                    rationale=d["rationale"],
                    timestamp_ms=d["timestamp_ms"],
                )
                self._decisions[decision.decision_id] = decision
            for w in blob.get("work_orders", []):
                self._work_orders[w["work_order_id"]] = self._work_order_from_dict(w)
            for c in blob.get("checkpoints", []):
                self._checkpoints[c["checkpoint_id"]] = Checkpoint(
                    checkpoint_id=c["checkpoint_id"],
                    work_order_id=c["work_order_id"],
                    owner=c["owner"],
                    producer=c["producer"],
                    consumer=c["consumer"],
                    summary=c["summary"],
                    state={},
                    state_check=CheckpointState(c["state_check"]),
                    created_at_ms=c["created_at_ms"],
                )
            for g in blob.get("quality_gates", []):
                self._quality_gates[g["gate_id"]] = QualityGateResult(
                    gate_id=g["gate_id"],
                    work_order_id=g["work_order_id"],
                    verdict=QualityVerdict(g["verdict"]),
                    reviewer=g["reviewer"],
                    issues=list(g.get("issues", [])),
                    evidence=g.get("evidence", ""),
                )
            for g in blob.get("deployment_gates", []):
                self._deployment_gates[g["gate_id"]] = DeploymentGateResult(
                    gate_id=g["gate_id"],
                    work_order_id=g["work_order_id"],
                    verdict=DeploymentVerdict(g["verdict"]),
                    reviewer=g["reviewer"],
                    rollback_ready=g.get("rollback_ready", False),
                    note=g.get("note", ""),
                )
            for e in blob.get("audit", []):
                self._audit.append(
                    AuditEvidence(
                        event_id=e["event_id"],
                        work_order_id=e["work_order_id"],
                        actor=e["actor"],
                        action=e["action"],
                        from_state=e["from_state"],
                        to_state=e["to_state"],
                        reason=e["reason"],
                        timestamp_ms=e["timestamp_ms"],
                    )
                )

    @staticmethod
    def _work_order_from_dict(w: dict[str, Any]) -> WorkOrder:
        approval = w.get("approval") or {}
        plan = w.get("plan")
        return WorkOrder(
            work_order_id=w["work_order_id"],
            project_id=w["project_id"],
            decision_id=w["decision_id"],
            title=w["title"],
            description=w["description"],
            required_capabilities=frozenset(w.get("required_capabilities", [])),
            status=WorkOrderStatus(w["status"]),
            assigned_agent=w.get("assigned_agent"),
            plan=(
                Plan(
                    work_order_id=plan["work_order_id"],
                    strategy=plan["strategy"],
                    risks=[Risk(**r) for r in plan.get("risks", [])],
                )
                if plan
                else None
            ),
            approval=ApprovalRequirement(
                required=approval.get("required", False),
                approver_role=CompanyRole(approval.get("approver_role", "human")),
                note=approval.get("note", ""),
                approved_by=approval.get("approved_by"),
                approved_at_ms=approval.get("approved_at_ms"),
                satisfied=approval.get("satisfied", False),
            ),
            owner=w.get("owner"),
            lease_holder=w.get("lease_holder"),
            lease_expires_at_ms=w.get("lease_expires_at_ms"),
            last_checkpoint_id=w.get("last_checkpoint_id"),
            last_quality_verdict=w.get("last_quality_verdict"),
            last_deployment_verdict=w.get("last_deployment_verdict"),
            created_at_ms=w.get("created_at_ms", 0),
            updated_at_ms=w.get("updated_at_ms", 0),
            duplicate_of=w.get("duplicate_of"),
        )

    def __len__(self) -> int:
        with self._lock:
            return len(self._work_orders)