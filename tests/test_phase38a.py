"""Phase 38A — Supervised Development Company F Pilot tests.

Covers the full required verification list for lphase37.md Phase 38A:
AI Council GO / NO-GO / REQUIRE_REVIEW, mandatory Work Order fields, planning
gating, capability routing, scoped action enforcement, Git-state divergence
handling, stale / no-ownership checkpoints, duplicate detection, conflict ->
human review, no silent overwrite, crash safety, Quality Guardian verdicts,
Deployment Check PASS / FAIL / REQUIRE_APPROVAL, mandatory human approval, a
full end-to-end pilot, and secret-leakage safety.
"""

from __future__ import annotations

import json

import pytest

from handoff_agent.company_f import (
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
from handoff_agent.pilot import CompanyFPilot, PilotProject

from conftest import commit_all, init_repo, write_file


# ---------------------------------------------------------------------------
# role helpers
# ---------------------------------------------------------------------------


def _role(
    coordinator: CompanyFCoordinator,
    role: CompanyRole,
    agent_id: str,
    caps: set[str],
    trust: int,
    name: str = "",
) -> RoleIdentity:
    identity = RoleIdentity(
        role=role,
        agent_id=agent_id,
        name=name or agent_id,
        capabilities=frozenset(caps),
        trust_level=trust,
    )
    coordinator.register_role(identity)
    return identity


def _register_and_go(coordinator: CompanyFCoordinator, project_id: str = "pilot-1") -> None:
    coordinator.register_project(
        ProjectIdentity(project_id=project_id, name="pilot", root="/tmp/pilot")
    )
    council = _role(coordinator, CompanyRole.AI_COUNCIL, "council-1", {"decide"}, 3)
    coordinator.make_decision(council, project_id, DecisionStatus.GO, "approved")
    return council


def _plan_full(coordinator: CompanyFCoordinator, project_id: str = "pilot-1", **overrides):
    _register_and_go(coordinator, project_id)
    planning = _role(
        coordinator, CompanyRole.PLANNING_COUNCIL, "planning-1", {"plan", "route"}, 2
    )
    work_order = coordinator.plan_work_order(
        planning,
        project_id,
        title=overrides.get("title", "Build index helper"),
        description=overrides.get("description", "Implement a docs index helper"),
        scope=overrides.get("scope", "docs/ index helper only"),
        project_owner=overrides.get("project_owner", "owner-1"),
        acceptance_criteria=overrides.get("acceptance_criteria", ["lists files", "prints titles"]),
        constraints=overrides.get("constraints", ["no network", "no secrets"]),
        risks=overrides.get(
            "risks",
            [
                {
                    "risk_id": "r1",
                    "description": "empty docs dir",
                    "severity": "low",
                    "mitigation": "graceful empty output",
                }
            ],
        ),
        rollback_consideration=overrides.get("rollback_consideration", "revert commit"),
        required_capabilities=overrides.get("required_capabilities", ("write",)),
        allowed_actions=overrides.get("allowed_actions", ("write.docs.",)),
        forbidden_actions=overrides.get("forbidden_actions", ("delete.production",)),
    )
    return planning, work_order


def _assign_executed(coordinator: CompanyFCoordinator, **wo_overrides):
    planning, work_order = _plan_full(coordinator, **wo_overrides)
    coder = _role(coordinator, CompanyRole.CODING_AGENT, "coder-1", {"write", "read"}, 2)
    coordinator.assign_agent(planning, work_order.work_order_id, coder)
    coordinator.acquire_lease(work_order.work_order_id, coder)
    return planning, work_order, coder


def _checkpointed(coordinator: CompanyFCoordinator, **wo_overrides):
    planning, work_order, coder = _assign_executed(coordinator, **wo_overrides)
    coordinator.record_action(coder, work_order.work_order_id, "write.docs.", "created helper")
    checkpoint = coordinator.submit_checkpoint(
        coder, work_order.work_order_id, "done", {"file": "index_helper.py"}
    )
    return planning, work_order, coder, checkpoint


def _quality_passed(coordinator: CompanyFCoordinator, **wo_overrides):
    planning, work_order, coder, checkpoint = _checkpointed(coordinator, **wo_overrides)
    guardian = _role(
        coordinator, CompanyRole.QUALITY_GUARDIAN, "guardian-1", {"audit", "test"}, 3
    )
    coordinator.quality_gate(
        guardian, work_order.work_order_id, QualityVerdict.PASS, issues=[], evidence="tests ok"
    )
    return planning, work_order, coder, checkpoint, guardian


# ---------------------------------------------------------------------------
# AI Council decisions
# ---------------------------------------------------------------------------


class TestCouncilVerdicts:
    def test_go_valid(self) -> None:
        c = CompanyFCoordinator()
        _register_and_go(c)
        assert c.latest_decision("pilot-1").decision_status == DecisionStatus.GO

    def test_no_go_valid(self) -> None:
        c = CompanyFCoordinator()
        _register_and_go(c)
        council = _role(c, CompanyRole.AI_COUNCIL, "council-2", {"decide"}, 3)
        c.make_decision(council, "pilot-1", DecisionStatus.NO_GO, "blocked")
        assert c.latest_decision("pilot-1").decision_status == DecisionStatus.NO_GO

    def test_require_review_valid(self) -> None:
        c = CompanyFCoordinator()
        _register_and_go(c)
        council = _role(c, CompanyRole.AI_COUNCIL, "council-2", {"decide"}, 3)
        c.make_decision(council, "pilot-1", DecisionStatus.REQUIRE_REVIEW, "needs review")
        assert c.latest_decision("pilot-1").decision_status == DecisionStatus.REQUIRE_REVIEW

    def test_planning_without_go_rejected(self) -> None:
        c = CompanyFCoordinator()
        c.register_project(ProjectIdentity(project_id="pilot-1", name="pilot", root="/tmp/p"))
        planning = _role(c, CompanyRole.PLANNING_COUNCIL, "planning-1", {"plan"}, 2)
        with pytest.raises(CoordinatorViolation):
            c.plan_work_order(
                planning,
                "pilot-1",
                title="t",
                description="d",
                scope="s",
                project_owner="o",
                acceptance_criteria=["a"],
                constraints=["c"],
                risks=[{"risk_id": "r", "description": "risk", "severity": "low"}],
                rollback_consideration="rc",
            )


# ---------------------------------------------------------------------------
# mandatory Work Order fields
# ---------------------------------------------------------------------------


class TestWorkOrderMandatoryFields:
    def _valid_kwargs(self) -> dict:
        return {
            "title": "t",
            "description": "d",
            "scope": "docs/ only",
            "project_owner": "owner-1",
            "acceptance_criteria": ["a1"],
            "constraints": ["c1"],
            "risks": [{"risk_id": "r", "description": "risk", "severity": "low"}],
            "rollback_consideration": "revert",
        }

    def _missing_field(self, field: str) -> None:
        c = CompanyFCoordinator()
        _register_and_go(c)
        planning = _role(c, CompanyRole.PLANNING_COUNCIL, "planning-1", {"plan"}, 2)
        kwargs = self._valid_kwargs()
        kwargs[field] = ""
        if field == "acceptance_criteria":
            kwargs[field] = []
        if field == "constraints":
            kwargs[field] = []
        if field == "risks":
            kwargs[field] = []
        with pytest.raises(CoordinatorViolation):
            c.plan_work_order(planning, "pilot-1", **kwargs)

    def test_work_order_without_scope_rejected(self) -> None:
        self._missing_field("scope")

    def test_work_order_without_owner_rejected(self) -> None:
        self._missing_field("project_owner")

    def test_work_order_without_acceptance_criteria_rejected(self) -> None:
        self._missing_field("acceptance_criteria")

    def test_work_order_without_risk_rejected(self) -> None:
        self._missing_field("risks")

    def test_work_order_without_constraint_rejected(self) -> None:
        self._missing_field("constraints")

    def test_work_order_without_rollback_rejected(self) -> None:
        self._missing_field("rollback_consideration")

    def test_fully_specified_work_order_accepted(self) -> None:
        c = CompanyFCoordinator()
        planning, work_order = _plan_full(c)
        assert work_order.scope == "docs/ index helper only"
        assert work_order.project_owner == "owner-1"
        assert work_order.acceptance_criteria
        assert work_order.constraints
        assert work_order.plan is not None and work_order.plan.risks
        assert work_order.rollback_consideration


# ---------------------------------------------------------------------------
# capability & scope enforcement
# ---------------------------------------------------------------------------


class TestCapabilityAndScope:
    def test_capability_mismatch_rejected(self) -> None:
        c = CompanyFCoordinator()
        planning, work_order = _plan_full(c, required_capabilities=("deploy-release",))
        coder = _role(c, CompanyRole.CODING_AGENT, "coder-x", {"write"}, 2)
        with pytest.raises(CoordinatorViolation):
            c.assign_agent(planning, work_order.work_order_id, coder)

    def test_out_of_scope_action_rejected(self) -> None:
        c = CompanyFCoordinator()
        planning, work_order, coder = _assign_executed(c)
        with pytest.raises(CoordinatorViolation):
            c.record_action(coder, work_order.work_order_id, "delete.production")
        trail = c.audit_trail(work_order.work_order_id)
        assert any(e.action == "action.out_of_scope" for e in trail)

    def test_in_scope_action_recorded(self) -> None:
        c = CompanyFCoordinator()
        planning, work_order, coder = _assign_executed(c)
        assert c.record_action(coder, work_order.work_order_id, "write.docs.")
        trail = [e.action for e in c.audit_trail(work_order.work_order_id)]
        assert "action.recorded" in trail


# ---------------------------------------------------------------------------
# Git-state verification
# ---------------------------------------------------------------------------


class TestGitState:
    def _repo_coordinator(self, tmp_path):
        repo = tmp_path / "pilot-repo"
        init_repo(repo)
        write_file(repo, "README.md", "# pilot\n")
        commit_all(repo, "baseline")
        coordinator = CompanyFCoordinator(
            repo_root=repo,
            verify_git=True,
            deployment_requires_human_approval=True,
        )
        return coordinator, repo

    def test_checkpoint_accepted_when_git_state_matches(self, tmp_path) -> None:
        coordinator, repo = self._repo_coordinator(tmp_path)
        planning, work_order = _plan_full(coordinator)
        coder = _role(coordinator, CompanyRole.CODING_AGENT, "coder-1", {"write"}, 2)
        coordinator.assign_agent(planning, work_order.work_order_id, coder)
        assert work_order.base_head is not None
        coordinator.record_action(coder, work_order.work_order_id, "write.docs.")
        checkpoint = coordinator.submit_checkpoint(
            coder, work_order.work_order_id, "done", {"file": "x.py"}
        )
        assert checkpoint.state_check.value == "valid"
        assert work_order.status == WorkOrderStatus.CHECKPOINTED

    def test_divergent_git_state_rejected(self, tmp_path) -> None:
        coordinator, repo = self._repo_coordinator(tmp_path)
        planning, work_order = _plan_full(coordinator)
        coder = _role(coordinator, CompanyRole.CODING_AGENT, "coder-1", {"write"}, 2)
        coordinator.assign_agent(planning, work_order.work_order_id, coder)
        write_file(repo, "NEW.md", "# divergent\n")
        commit_all(repo, "divergent change")
        with pytest.raises(HandoffRejection) as exc_info:
            coordinator.submit_checkpoint(
                coder, work_order.work_order_id, "done", {"file": "x.py"}
            )
        assert "diverged" in str(exc_info.value)


# ---------------------------------------------------------------------------
# checkpoint / handoff safety
# ---------------------------------------------------------------------------


class TestCheckpointSafety:
    def test_stale_checkpoint_rejected(self) -> None:
        c = CompanyFCoordinator()
        planning, work_order = _plan_full(c)
        coder = _role(c, CompanyRole.CODING_AGENT, "coder-1", {"write"}, 2)
        with pytest.raises(HandoffRejection):
            c.submit_checkpoint(coder, work_order.work_order_id, "stale", {"f": "x"})

    def test_checkpoint_without_ownership_rejected(self) -> None:
        c = CompanyFCoordinator()
        planning, work_order, coder = _assign_executed(c)
        intruder = _role(c, CompanyRole.CODING_AGENT, "coder-2", {"write"}, 2)
        with pytest.raises(HandoffRejection):
            c.submit_checkpoint(intruder, work_order.work_order_id, "nope", {"f": "x"})

    def test_duplicate_work_detected(self) -> None:
        c = CompanyFCoordinator()
        planning, work_order = _plan_full(c)
        with pytest.raises(DuplicateWork):
            _plan_full_again(c)

    def test_conflict_requires_human_review(self) -> None:
        c = CompanyFCoordinator()
        planning, work_order, coder = _assign_executed(c)
        council = _role(c, CompanyRole.AI_COUNCIL, "council-c", {"decide"}, 3)
        c.make_decision(council, "pilot-1", DecisionStatus.NO_GO, "conflict!")
        assert c.get_work_order(work_order.work_order_id).status == WorkOrderStatus.CONFLICT
        coder2 = _role(c, CompanyRole.CODING_AGENT, "coder-n", {"write"}, 2)
        with pytest.raises(CoordinatorViolation):
            c.resume(coder2, work_order.work_order_id)
        human = _role(c, CompanyRole.HUMAN, "human-1", {"approve"}, 5)
        resumed = c.resume(human, work_order.work_order_id)
        assert resumed.status == WorkOrderStatus.EXECUTING

    def test_no_silent_overwrite(self) -> None:
        c = CompanyFCoordinator()
        planning, work_order, coder = _assign_executed(c)
        c.acquire_lease(work_order.work_order_id, coder)
        other = _role(c, CompanyRole.CODING_AGENT, "coder-9", {"write"}, 2)
        with pytest.raises(CoordinatorViolation):
            c.acquire_lease(work_order.work_order_id, other)
        with pytest.raises(HandoffRejection):
            c.submit_checkpoint(other, work_order.work_order_id, "overwrite", {"f": "x"})

    def test_agent_crash_keeps_state(self, tmp_path) -> None:
        state = tmp_path / "state"
        state.mkdir()
        c = CompanyFCoordinator(state_dir=state)
        planning, work_order, coder = _assign_executed(c)
        c.submit_checkpoint(coder, work_order.work_order_id, "snapshot", {"f": "x"})
        c.save()
        restarted = CompanyFCoordinator(state_dir=state)
        reloaded = restarted.get_work_order(work_order.work_order_id)
        assert reloaded is not None
        assert reloaded.status == WorkOrderStatus.CHECKPOINTED

    def test_handoff_accepts_valid_checkpoint(self) -> None:
        c = CompanyFCoordinator()
        planning, work_order, coder, checkpoint = _checkpointed(c)
        handoff = _role(c, CompanyRole.HANDOFF_AGENT, "handoff-1", {"checkpoint"}, 2)
        verdict = c.evaluate_handoff(handoff, checkpoint)
        assert verdict == HandoffVerdict.ACCEPT


# ---------------------------------------------------------------------------
# Quality Guardian
# ---------------------------------------------------------------------------


class TestQualityGuardian:
    def test_quality_pass(self) -> None:
        c = CompanyFCoordinator()
        planning, work_order, coder, checkpoint, guardian = _quality_passed(c)
        assert work_order.status == WorkOrderStatus.QUALITY_GATED
        assert work_order.last_quality_verdict == QualityVerdict.PASS.value

    def test_quality_fail(self) -> None:
        c = CompanyFCoordinator()
        planning, work_order, coder, checkpoint = _checkpointed(c)
        guardian = _role(c, CompanyRole.QUALITY_GUARDIAN, "guardian-1", {"audit"}, 3)
        c.quality_gate(guardian, work_order.work_order_id, QualityVerdict.FAIL, issues=["broken"])
        assert work_order.status == WorkOrderStatus.EXECUTING

    def test_quality_require_fix(self) -> None:
        c = CompanyFCoordinator()
        planning, work_order, coder, checkpoint = _checkpointed(c)
        guardian = _role(c, CompanyRole.QUALITY_GUARDIAN, "guardian-1", {"audit"}, 3)
        c.quality_gate(
            guardian, work_order.work_order_id, QualityVerdict.REQUIRE_FIX, issues=["style"]
        )
        assert work_order.status == WorkOrderStatus.EXECUTING


# ---------------------------------------------------------------------------
# Deployment Check & human approval
# ---------------------------------------------------------------------------


class TestDeploymentCheck:
    def _guardian(self, c: CompanyFCoordinator):
        return _role(c, CompanyRole.QUALITY_GUARDIAN, "guardian-1", {"audit", "test"}, 3)

    def _deployer(self, c: CompanyFCoordinator):
        return _role(c, CompanyRole.DEPLOYMENT_CHECK, "deploy-1", {"deploy", "rollback"}, 3)

    def _human(self, c: CompanyFCoordinator):
        return _role(c, CompanyRole.HUMAN, "human-1", {"approve"}, 5)

    def test_deployment_without_quality_pass_blocked(self) -> None:
        c = CompanyFCoordinator(
            deployment_requires_human_approval=True,
        )
        planning, work_order, coder, checkpoint = _checkpointed(c)
        with pytest.raises(InvalidTransition):
            c.run_deployment_check(self._deployer(c), work_order.work_order_id)

    def test_deployment_after_quality_fail_blocked(self) -> None:
        c = CompanyFCoordinator(deployment_requires_human_approval=True)
        planning, work_order, coder, checkpoint = _checkpointed(c)
        c.quality_gate(
            self._guardian(c), work_order.work_order_id, QualityVerdict.FAIL, issues=["x"]
        )
        assert work_order.status == WorkOrderStatus.EXECUTING
        with pytest.raises(InvalidTransition):
            c.run_deployment_check(self._deployer(c), work_order.work_order_id)

    def test_deployment_without_human_approval_returns_require_approval(self) -> None:
        c = CompanyFCoordinator(deployment_requires_human_approval=True)
        planning, work_order, coder, checkpoint, guardian = _quality_passed(c)
        result = c.run_deployment_check(
            self._deployer(c), work_order.work_order_id, rollback_ready=True
        )
        assert result.verdict == DeploymentVerdict.REQUIRE_APPROVAL
        assert work_order.status == WorkOrderStatus.QUALITY_GATED

    def test_release_without_approval_blocked(self) -> None:
        c = CompanyFCoordinator(deployment_requires_human_approval=True)
        planning, work_order, coder, checkpoint, guardian = _quality_passed(c)
        c.run_deployment_check(self._deployer(c), work_order.work_order_id, rollback_ready=True)
        with pytest.raises((InvalidTransition, NoApproval)):
            c.release(self._deployer(c), work_order.work_order_id)

    def test_deployment_with_approval_and_rollback_passes(self) -> None:
        c = CompanyFCoordinator(deployment_requires_human_approval=True)
        planning, work_order, coder, checkpoint, guardian = _quality_passed(c)
        c.approve_deployment(self._human(c), work_order.work_order_id, note="approved")
        result = c.run_deployment_check(
            self._deployer(c), work_order.work_order_id, rollback_ready=True
        )
        assert result.verdict == DeploymentVerdict.PASS
        assert work_order.status == WorkOrderStatus.DEPLOYMENT_GATED

    def test_non_human_cannot_approve_deployment(self) -> None:
        c = CompanyFCoordinator(deployment_requires_human_approval=True)
        planning, work_order, coder, checkpoint, guardian = _quality_passed(c)
        coder_b = _role(c, CompanyRole.CODING_AGENT, "coder-b", {"write"}, 2)
        with pytest.raises(CoordinatorViolation):
            c.approve_deployment(coder_b, work_order.work_order_id)


# ---------------------------------------------------------------------------
# full end-to-end pilot
# ---------------------------------------------------------------------------


class TestPilotEndToEnd:
    def test_full_pilot_flow(self, tmp_path) -> None:
        repo = tmp_path / "pilot-repo"
        init_repo(repo)
        write_file(repo, "README.md", "# pilot\n")
        commit_all(repo, "baseline")

        pilot = CompanyFPilot(repo_root=repo)
        report = pilot.run()

        assert report.decision_status == DecisionStatus.GO.value
        assert report.handoff_verdict == HandoffVerdict.ACCEPT.value
        assert report.quality_verdict == QualityVerdict.PASS.value
        assert report.deployment_verdict == DeploymentVerdict.PASS.value
        assert report.approval_satisfied is True
        assert report.released is True
        assert report.work_order_status == WorkOrderStatus.RELEASED.value
        assert report.audit_events > 0
        steps = " ".join(report.steps)
        assert "REQUIRE_APPROVAL" in steps
        assert "human approval was mandatory" in steps
        assert "Git state verified" in steps

    def test_pilot_audit_trail_has_full_evidence(self, tmp_path) -> None:
        repo = tmp_path / "pilot-repo"
        init_repo(repo)
        write_file(repo, "README.md", "# pilot\n")
        commit_all(repo, "baseline")

        pilot = CompanyFPilot(repo_root=repo)
        report = pilot.run()
        trail = [
            e.action for e in pilot.coordinator.audit_trail(report.work_order_id)
        ]
        for expected in (
            "work_order.created",
            "action.recorded",
            "checkpoint.accepted",
            "quality.gate",
            "deployment.approved",
            "deployment.pass",
            "work_order.released",
        ):
            assert expected in trail


# ---------------------------------------------------------------------------
# secret-safety
# ---------------------------------------------------------------------------


class TestSecretSafety:
    def test_checkpoint_payload_with_secret_rejected(self) -> None:
        c = CompanyFCoordinator()
        planning, work_order, coder = _assign_executed(c)
        with pytest.raises(HandoffRejection):
            c.submit_checkpoint(
                coder,
                work_order.work_order_id,
                "done",
                {"api_key": "sk-SECRETLITERALVALUE123456789012345678901"},
            )

    def test_pilot_report_is_secret_free(self, tmp_path) -> None:
        repo = tmp_path / "pilot-repo"
        init_repo(repo)
        write_file(repo, "README.md", "# pilot\n")
        commit_all(repo, "baseline")

        pilot = CompanyFPilot(repo_root=repo)
        report = pilot.run()
        blob = report.to_dict()
        marshalled = json.dumps(blob, default=str)
        assert "sk-" not in marshalled
        assert "api_key" not in marshalled
        audit = json.dumps(
            [e.to_dict() for e in pilot.coordinator.audit_trail()], default=str
        )
        assert "sk-" not in audit

    def test_no_arbitrary_command_execution_in_pilot_source(self) -> None:
        from handoff_agent import pilot as pilot_module

        source = open(pilot_module.__file__, encoding="utf-8").read()
        assert "eval(" not in source
        assert "exec(" not in source
        assert "os.system(" not in source
        assert "subprocess" not in source


def _plan_full_again(c: CompanyFCoordinator) -> None:
    planning = _role(c, CompanyRole.PLANNING_COUNCIL, "planning-2", {"plan"}, 2)
    c.plan_work_order(
        planning,
        "pilot-1",
        title="Build index helper",
        description="Implement a docs index helper",
        scope="docs/ index helper only",
        project_owner="owner-1",
        acceptance_criteria=["lists files", "prints titles"],
        constraints=["no network", "no secrets"],
        risks=[{"risk_id": "r", "description": "empty docs dir", "severity": "low"}],
        rollback_consideration="revert commit",
    )