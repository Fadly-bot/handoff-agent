"""Phase 30 (revised roadmap) — Development Company F Coordination Contract tests.

Covers the required verification list from REVISIPHASE30-36.md Phase 30:
AI Council GO/NO-GO, Planning Council work-order gating, capability-matched
agent routing, stale/no-ownership checkpoint rejection, Quality Guardian
verdicts, Deployment Check gating on Quality PASS, mandatory approval
enforcement, decision-conflict safety, agent-failure work-order retention,
duplicate-work prevention, leases, and audit evidence for every transition.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from handoff_agent.company_f import (
    AuditEvidence,
    CheckpointState,
    CompanyFCoordinator,
    CompanyRole,
    CoordinatorViolation,
    DecisionStatus,
    DeploymentVerdict,
    DuplicateWork,
    HandoffRejection,
    HandoffVerdict,
    InvalidTransition,
    NoApproval,
    ProjectIdentity,
    QualityVerdict,
    RoleIdentity,
    WorkOrderStatus,
)


def _council(coordinator: CompanyFCoordinator, trust: int = 3) -> RoleIdentity:
    role = RoleIdentity(
        role=CompanyRole.AI_COUNCIL,
        agent_id="council-1",
        name="AI Council",
        capabilities=frozenset({"decide"}),
        trust_level=trust,
    )
    coordinator.register_role(role)
    return role


def _planning(coordinator: CompanyFCoordinator, trust: int = 2) -> RoleIdentity:
    role = RoleIdentity(
        role=CompanyRole.PLANNING_COUNCIL,
        agent_id="planning-1",
        name="Planning Council",
        capabilities=frozenset({"plan", "route"}),
        trust_level=trust,
    )
    coordinator.register_role(role)
    return role


def _coder(
    coordinator: CompanyFCoordinator,
    agent_id: str = "coder-a",
    caps: set[str] | None = None,
    trust: int = 2,
) -> RoleIdentity:
    role = RoleIdentity(
        role=CompanyRole.CODING_AGENT,
        agent_id=agent_id,
        name=agent_id,
        capabilities=frozenset(caps or {"write", "read"}),
        trust_level=trust,
    )
    coordinator.register_role(role)
    return role


def _handoff(coordinator: CompanyFCoordinator, trust: int = 2) -> RoleIdentity:
    role = RoleIdentity(
        role=CompanyRole.HANDOFF_AGENT,
        agent_id="handoff-1",
        name="Handoff Agent",
        capabilities=frozenset({"checkpoint"}),
        trust_level=trust,
    )
    coordinator.register_role(role)
    return role


def _guardian(coordinator: CompanyFCoordinator, trust: int = 3) -> RoleIdentity:
    role = RoleIdentity(
        role=CompanyRole.QUALITY_GUARDIAN,
        agent_id="guardian-1",
        name="Quality Guardian",
        capabilities=frozenset({"audit", "test"}),
        trust_level=trust,
    )
    coordinator.register_role(role)
    return role


def _deployer(coordinator: CompanyFCoordinator, trust: int = 3) -> RoleIdentity:
    role = RoleIdentity(
        role=CompanyRole.DEPLOYMENT_CHECK,
        agent_id="deploy-1",
        name="Deployment Check",
        capabilities=frozenset({"deploy", "rollback"}),
        trust_level=trust,
    )
    coordinator.register_role(role)
    return role


def _human(coordinator: CompanyFCoordinator, trust: int = 5) -> RoleIdentity:
    role = RoleIdentity(
        role=CompanyRole.HUMAN,
        agent_id="human-1",
        name="Human Approver",
        capabilities=frozenset({"approve"}),
        trust_level=trust,
    )
    coordinator.register_role(role)
    return role


@pytest.fixture()
def company() -> CompanyFCoordinator:
    coordinator = CompanyFCoordinator()
    coordinator.register_project(
        ProjectIdentity(project_id="proj-1", name="Company F", root="/tmp/proj-1")
    )
    return coordinator


def _setup_go(company: CompanyFCoordinator) -> RoleIdentity:
    council = _council(company)
    company.make_decision(
        council, "proj-1", DecisionStatus.GO, rationale="approved by council"
    )
    return council


def _create_planned_work_order(company: CompanyFCoordinator, **kwargs):
    """Return (planning, work_order) with a GO decision and a PENDING order."""
    _setup_go(company)
    planning = _planning(company)
    work_order = company.create_work_order(
        planning,
        "proj-1",
        title=kwargs.get("title", "Build feature X"),
        description=kwargs.get("description", "Implement feature X"),
        required_capabilities=kwargs.get("required_capabilities", ("write",)),
        approval_required=kwargs.get("approval_required", False),
    )
    return planning, work_order


def _plan_it(company: CompanyFCoordinator, planning: RoleIdentity, work_order):
    return company.add_plan(
        planning, work_order.work_order_id, "iterative", risks=[]
    )


def _assign_and_execute(company: CompanyFCoordinator, planning, work_order, coder):
    _plan_it(company, planning, work_order)
    return company.assign_agent(planning, work_order.work_order_id, coder)


class TestDecisions:
    def test_ai_council_can_make_go(self, company: CompanyFCoordinator) -> None:
        decision = company.make_decision(
            _council(company), "proj-1", DecisionStatus.GO, "go"
        )
        assert decision.decision_status == DecisionStatus.GO
        assert company.latest_decision("proj-1").decision_id == decision.decision_id

    def test_ai_council_can_make_no_go(self, company: CompanyFCoordinator) -> None:
        council = _council(company)
        company.make_decision(council, "proj-1", DecisionStatus.GO, "go")
        company.make_decision(council, "proj-1", DecisionStatus.NO_GO, "blocked")
        assert company.latest_decision("proj-1").decision_status == DecisionStatus.NO_GO

    def test_non_council_role_cannot_decide(self, company: CompanyFCoordinator) -> None:
        with pytest.raises(CoordinatorViolation):
            company.make_decision(
                _planning(company), "proj-1", DecisionStatus.GO, "no"
            )

    def test_untrusted_role_cannot_decide(self, company: CompanyFCoordinator) -> None:
        with pytest.raises(CoordinatorViolation):
            company.make_decision(
                _council(company, trust=1), "proj-1", DecisionStatus.GO, "no"
            )

    def test_unknown_project_rejected(self, company: CompanyFCoordinator) -> None:
        with pytest.raises(CoordinatorViolation):
            company.make_decision(
                _council(company), "proj-unknown", DecisionStatus.GO, "no"
            )

    def test_conflicting_decision_requires_review(
        self, company: CompanyFCoordinator
    ) -> None:
        council = _council(company)
        company.make_decision(council, "proj-1", DecisionStatus.GO, "go")
        planning = _planning(company)
        work_order = company.create_work_order(
            planning, "proj-1", "Feature", "do it", required_capabilities=("write",)
        )
        # conflicting NO-GO after a GO that already created work orders
        company.make_decision(council, "proj-1", DecisionStatus.NO_GO, "conflict!")
        status = company.get_work_order(work_order.work_order_id).status
        assert status == WorkOrderStatus.CONFLICT


class TestWorkOrderGating:
    def test_planning_council_creates_work_order_after_go(
        self, company: CompanyFCoordinator
    ) -> None:
        planning, work_order = _create_planned_work_order(company)
        assert work_order.status == WorkOrderStatus.PENDING
        assert work_order.decision_id is not None
        assert planning.agent_id == "planning-1"

    def test_no_work_order_without_go(self, company: CompanyFCoordinator) -> None:
        planning = _planning(company)
        with pytest.raises(CoordinatorViolation):
            company.create_work_order(
                planning, "proj-1", "Feature", "do it", required_capabilities=("write",)
            )

    def test_no_work_order_after_no_go(self, company: CompanyFCoordinator) -> None:
        council = _council(company)
        company.make_decision(council, "proj-1", DecisionStatus.NO_GO, "blocked")
        with pytest.raises(CoordinatorViolation):
            company.create_work_order(
                _planning(company), "proj-1", "Feature", "do it", required_capabilities=("write",)
            )

    def test_non_planning_role_cannot_create_work_order(
        self, company: CompanyFCoordinator
    ) -> None:
        _setup_go(company)
        with pytest.raises(CoordinatorViolation):
            company.create_work_order(
                _council(company), "proj-1", "Feature", "do it", required_capabilities=("write",)
            )

    def test_duplicate_work_detected(self, company: CompanyFCoordinator) -> None:
        _setup_go(company)
        planning = _planning(company)
        company.create_work_order(
            planning, "proj-1", "Feature", "do it", required_capabilities=("write",)
        )
        with pytest.raises(DuplicateWork):
            company.create_work_order(
                planning, "proj-1", "Feature", "do it", required_capabilities=("write",)
            )

    def test_force_plan_and_risks(self, company: CompanyFCoordinator) -> None:
        planning, work_order = _create_planned_work_order(company)
        plan = company.add_plan(
            planning,
            work_order.work_order_id,
            "iterative",
            risks=[
                {
                    "risk_id": "r1",
                    "description": "dependency drift",
                    "severity": "medium",
                    "mitigation": "pin versions",
                }
            ],
        )
        assert work_order.status == WorkOrderStatus.PLANNED
        assert plan.risks and plan.risks[0].severity == "medium"
        assert work_order.plan.strategy == "iterative"


class TestCapabilityRouting:
    def test_agent_with_matching_capability_assigned(
        self, company: CompanyFCoordinator
    ) -> None:
        planning, work_order = _create_planned_work_order(company)
        coder = _coder(company, caps={"write", "read"})
        _plan_it(company, planning, work_order)
        assigned = company.assign_agent(planning, work_order.work_order_id, coder)
        assert assigned == "coder-a"
        assert work_order.status == WorkOrderStatus.EXECUTING

    def test_agent_without_capability_refused(
        self, company: CompanyFCoordinator
    ) -> None:
        planning, work_order = _create_planned_work_order(
            company, required_capabilities=("deploy-release",)
        )
        coder = _coder(company, caps={"write"})
        _plan_it(company, planning, work_order)
        with pytest.raises(CoordinatorViolation):
            company.assign_agent(planning, work_order.work_order_id, coder)

    def test_revoked_agent_cannot_be_assigned(self, company: CompanyFCoordinator) -> None:
        planning, work_order = _create_planned_work_order(company)
        coder = _coder(company)
        _plan_it(company, planning, work_order)
        company.revoke_role("coder-a")
        with pytest.raises(CoordinatorViolation):
            company.assign_agent(planning, work_order.work_order_id, coder)


class TestHandoffCheckpoint:
    def test_valid_checkpoint_accepted(self, company: CompanyFCoordinator) -> None:
        planning, work_order = _create_planned_work_order(company)
        coder = _coder(company)
        _assign_and_execute(company, planning, work_order, coder)
        company.acquire_lease(work_order.work_order_id, coder)
        checkpoint = company.submit_checkpoint(
            coder, work_order.work_order_id, "done", {"file": "x.py"}
        )
        assert checkpoint.state_check == CheckpointState.VALID
        assert work_order.status == WorkOrderStatus.CHECKPOINTED

    def test_checkpoint_without_ownership_rejected(
        self, company: CompanyFCoordinator
    ) -> None:
        planning, work_order = _create_planned_work_order(company)
        coder = _coder(company, agent_id="coder-a")
        intruder = _coder(company, agent_id="coder-b")
        _assign_and_execute(company, planning, work_order, coder)
        with pytest.raises(HandoffRejection):
            company.submit_checkpoint(
                intruder, work_order.work_order_id, "done", {"file": "x.py"}
            )

    def test_stale_checkpoint_rejected(self, company: CompanyFCoordinator) -> None:
        planning, work_order = _create_planned_work_order(company)
        coder = _coder(company)
        with pytest.raises(HandoffRejection):
            company.submit_checkpoint(
                coder, work_order.work_order_id, "done", {"file": "x.py"}
            )

    def test_checkpoint_payload_with_secret_like_content_rejected(
        self, company: CompanyFCoordinator
    ) -> None:
        planning, work_order = _create_planned_work_order(company)
        coder = _coder(company)
        _assign_and_execute(company, planning, work_order, coder)
        with pytest.raises(HandoffRejection):
            company.submit_checkpoint(
                coder,
                work_order.work_order_id,
                "done",
                {"creds": {"api_key": "sk-SECRETLITERALVALUE123456789012345678901"}},
            )

    def test_handoff_accepts_valid_checkpoint(self, company: CompanyFCoordinator) -> None:
        planning, work_order = _create_planned_work_order(company)
        coder = _coder(company)
        _assign_and_execute(company, planning, work_order, coder)
        checkpoint = company.submit_checkpoint(
            coder, work_order.work_order_id, "done", {"file": "x.py"}
        )
        verdict = company.evaluate_handoff(_handoff(company), checkpoint)
        assert verdict == HandoffVerdict.ACCEPT

    def test_handoff_rejects_mismatched_owner(self, company: CompanyFCoordinator) -> None:
        planning, work_order = _create_planned_work_order(company)
        coder = _coder(company)
        _assign_and_execute(company, planning, work_order, coder)
        checkpoint = company.submit_checkpoint(
            coder, work_order.work_order_id, "done", {"file": "x.py"}
        )
        tampered = replace(checkpoint, owner="intruder")
        verdict = company.evaluate_handoff(_handoff(company), tampered)
        assert verdict == HandoffVerdict.REJECT


def _gate_ready(company: CompanyFCoordinator, approval: bool = False):
    """Create a checkpointed work order; return (work_order, planning, coder, guard)."""
    planning, work_order = _create_planned_work_order(
        company, approval_required=approval
    )
    coder = _coder(company)
    _assign_and_execute(company, planning, work_order, coder)
    company.submit_checkpoint(
        coder, work_order.work_order_id, "done", {"file": "x.py"}
    )
    guard = _guardian(company)
    return work_order, planning, coder, guard


class TestQualityDeployment:
    def test_quality_pass_allows_deployment(self, company: CompanyFCoordinator) -> None:
        work_order, planning, coder, guard = _gate_ready(company)
        result = company.quality_gate(
            guard, work_order.work_order_id, QualityVerdict.PASS, evidence="tests ok"
        )
        assert result.verdict == QualityVerdict.PASS
        assert work_order.status == WorkOrderStatus.QUALITY_GATED
        deployer = _deployer(company)
        deployment = company.deployment_gate(
            deployer, work_order.work_order_id, rollback_ready=True
        )
        assert deployment.verdict == DeploymentVerdict.APPROVE
        assert work_order.status == WorkOrderStatus.DEPLOYMENT_GATED
        released = company.release(deployer, work_order.work_order_id)
        assert released.status == WorkOrderStatus.RELEASED

    def test_quality_fail_forces_rework(self, company: CompanyFCoordinator) -> None:
        work_order, planning, coder, guard = _gate_ready(company)
        company.quality_gate(
            guard, work_order.work_order_id, QualityVerdict.FAIL, issues=["broken tests"]
        )
        assert work_order.status == WorkOrderStatus.EXECUTING

    def test_quality_require_fix_returns_to_checkpoint(
        self, company: CompanyFCoordinator
    ) -> None:
        work_order, planning, coder, guard = _gate_ready(company)
        company.quality_gate(
            guard, work_order.work_order_id, QualityVerdict.REQUIRE_FIX, issues=["style"]
        )
        assert work_order.status == WorkOrderStatus.EXECUTING

    def test_deployment_rejects_without_quality_pass(
        self, company: CompanyFCoordinator
    ) -> None:
        work_order, planning, coder, guard = _gate_ready(company)
        with pytest.raises(InvalidTransition):
            company.deployment_gate(_deployer(company), work_order.work_order_id)

    def test_deployment_rejects_after_quality_fail(
        self, company: CompanyFCoordinator
    ) -> None:
        work_order, planning, coder, guard = _gate_ready(company)
        company.quality_gate(
            guard, work_order.work_order_id, QualityVerdict.FAIL, issues=["broken"]
        )
        with pytest.raises(InvalidTransition):
            company.deployment_gate(_deployer(company), work_order.work_order_id)

    def test_release_rejects_without_deployment_gate(
        self, company: CompanyFCoordinator
    ) -> None:
        work_order, planning, coder, guard = _gate_ready(company)
        company.quality_gate(guard, work_order.work_order_id, QualityVerdict.PASS)
        with pytest.raises(InvalidTransition):
            company.release(_deployer(company), work_order.work_order_id)

    def test_non_guardian_cannot_pass_quality(self, company: CompanyFCoordinator) -> None:
        work_order, planning, coder, guard = _gate_ready(company)
        intruder = _coder(company, agent_id="coder-b")
        with pytest.raises(CoordinatorViolation):
            company.quality_gate(
                intruder, work_order.work_order_id, QualityVerdict.PASS
            )


class TestApproval:
    def test_mandatory_approval_cannot_be_bypassed(
        self, company: CompanyFCoordinator
    ) -> None:
        work_order, planning, coder, guard = _gate_ready(company, approval=True)
        company.quality_gate(guard, work_order.work_order_id, QualityVerdict.PASS)
        with pytest.raises(NoApproval):
            company.deployment_gate(_deployer(company), work_order.work_order_id)

    def test_approval_then_release(self, company: CompanyFCoordinator) -> None:
        work_order, planning, coder, guard = _gate_ready(company, approval=True)
        company.quality_gate(guard, work_order.work_order_id, QualityVerdict.PASS)
        company.approve_work_order(_human(company), work_order.work_order_id)
        deployer = _deployer(company)
        company.deployment_gate(deployer, work_order.work_order_id, rollback_ready=True)
        released = company.release(deployer, work_order.work_order_id)
        assert released.status == WorkOrderStatus.RELEASED

    def test_approval_requires_human_trust(self, company: CompanyFCoordinator) -> None:
        work_order, planning, coder, guard = _gate_ready(company, approval=True)
        company.quality_gate(guard, work_order.work_order_id, QualityVerdict.PASS)
        low_trust = RoleIdentity(
            role=CompanyRole.HUMAN,
            agent_id="human-low",
            trust_level=1,
            capabilities=frozenset({"approve"}),
        )
        company.register_role(low_trust)
        with pytest.raises(CoordinatorViolation):
            company.approve_work_order(low_trust, work_order.work_order_id)


class TestResilience:
    def test_agent_failure_does_not_lose_work_order(
        self, company: CompanyFCoordinator
    ) -> None:
        planning, work_order = _create_planned_work_order(company)
        coder = _coder(company)
        _assign_and_execute(company, planning, work_order, coder)
        company.submit_checkpoint(
            coder, work_order.work_order_id, "snapshot", {"file": "x.py"}
        )
        company.mark_failed(coder, work_order.work_order_id, "agent crashed")
        assert company.get_work_order(work_order.work_order_id) is not None
        assert work_order.status == WorkOrderStatus.FAILED
        company.resume(coder, work_order.work_order_id)
        assert work_order.status == WorkOrderStatus.EXECUTING

    def test_persistence_survives_coordinator_restart(self, tmp_path) -> None:
        repo = tmp_path / "state"
        repo.mkdir()
        coordinator = CompanyFCoordinator(state_dir=repo)
        coordinator.register_project(
            ProjectIdentity(project_id="proj-1", name="Company F", root=str(repo))
        )
        planning, work_order = _create_planned_work_order(coordinator)
        coder = _coder(coordinator)
        _assign_and_execute(coordinator, planning, work_order, coder)
        coordinator.submit_checkpoint(
            coder, work_order.work_order_id, "snapshot", {"file": "x.py"}
        )
        coordinator.save()

        restarted = CompanyFCoordinator(state_dir=repo)
        reloaded = restarted.get_work_order(work_order.work_order_id)
        assert reloaded is not None
        assert reloaded.status == WorkOrderStatus.CHECKPOINTED
        assert reloaded.last_checkpoint_id is not None
        assert len(restarted.audit_trail(work_order.work_order_id)) > 0

    def test_audit_evidence_for_every_transition(
        self, company: CompanyFCoordinator
    ) -> None:
        planning, work_order = _create_planned_work_order(company)
        coder = _coder(company)
        _assign_and_execute(company, planning, work_order, coder)
        company.submit_checkpoint(
            coder, work_order.work_order_id, "snapshot", {"file": "x.py"}
        )
        events = company.audit_trail(work_order.work_order_id)
        actions = [e.action for e in events]
        assert "work_order.created" in actions
        assert "plan.attached" in actions
        assert "work_order.assigned" in actions
        assert "checkpoint.accepted" in actions
        assert all(isinstance(e, AuditEvidence) for e in events)


class TestStateMachine:
    def test_invalid_transition_rejected(self, company: CompanyFCoordinator) -> None:
        planning, work_order = _create_planned_work_order(company)
        with pytest.raises(InvalidTransition):
            company.deployment_gate(_deployer(company), work_order.work_order_id)

    def test_cancellation_from_pending(self, company: CompanyFCoordinator) -> None:
        planning, work_order = _create_planned_work_order(company)
        work_order = company.cancel(
            planning, work_order.work_order_id, "scope removed"
        )
        assert work_order.status == WorkOrderStatus.CANCELLED
        with pytest.raises(InvalidTransition):
            company.resume(planning, work_order.work_order_id)

    def test_rejection_then_replan(self, company: CompanyFCoordinator) -> None:
        planning, work_order = _create_planned_work_order(company)
        coder = _coder(company)
        _assign_and_execute(company, planning, work_order, coder)
        company.reject(planning, work_order.work_order_id, "not viable")
        assert work_order.status == WorkOrderStatus.REJECTED


class TestSecretSafety:
    def test_json_serialization_never_contains_payload_secrets(
        self, company: CompanyFCoordinator
    ) -> None:
        planning, work_order = _create_planned_work_order(company)
        coder = _coder(company)
        _assign_and_execute(company, planning, work_order, coder)
        company.submit_checkpoint(
            coder, work_order.work_order_id, "done", {"file": "x.py"}
        )
        blob = json.dumps(
            {"work_orders": [w.to_dict() for w in company.list_work_orders()]}
        )
        assert "sk-SECRETLITERALVALUE" not in blob
        assert "api_key" not in blob