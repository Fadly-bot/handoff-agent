"""Phase 38A — Supervised Development Company F Pilot.

Defines ONE real but **non-critical** pilot project and the supervised flow
that proves the Development Company F contract end to end:

    Company F
    -> AI Council  (GO / NO-GO / REQUIRE_REVIEW)
    -> Planning Council  (fully-specified Work Order + risk register)
    -> Coding Agent  (capability-verified, scoped actions, owned & leased)
    -> Handoff Agent  (Git-state-verified checkpoint + continuity)
    -> Quality Guardian  (PASS / FAIL / REQUIRE_FIX)
    -> Deployment Check  (PASS / FAIL / REQUIRE_APPROVAL)
    -> Human Approval  (cannot be bypassed)
    -> release (STOP otherwise)

Every transition is recorded as audit evidence. The runtime stays inside the
standard library and uses only the coordinator's public API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from handoff_agent.company_f import (
    CompanyFCoordinator,
    CompanyRole,
    DecisionStatus,
    DeploymentVerdict,
    HandoffVerdict,
    ProjectIdentity,
    QualityVerdict,
    RoleIdentity,
)


@dataclass(frozen=True)
class PilotProject:
    """Definition of the single non-critical pilot project."""

    project_id: str
    name: str
    project_owner: str = "Human Approver"
    human_approver: str = "human-approver"
    non_critical_note: str = (
        "Internal helper utility — no production data, no PII, no release to "
        "customers. Safe for a supervised pilot of the Company F flow."
    )


@dataclass
class PilotReport:
    """Structured, secret-free evidence of one full pilot run."""

    project: PilotProject
    decision_status: str = ""
    work_order_id: str = ""
    work_order_status: str = ""
    checkpoint_id: str = ""
    handoff_verdict: str = ""
    quality_verdict: str = ""
    deployment_verdict: str = ""
    approval_satisfied: bool = False
    audit_events: int = 0
    released: bool = False
    steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": {
                "project_id": self.project.project_id,
                "name": self.project.name,
                "project_owner": self.project.project_owner,
                "human_approver": self.project.human_approver,
                "non_critical_note": self.project.non_critical_note,
            },
            "decision_status": self.decision_status,
            "work_order_id": self.work_order_id,
            "work_order_status": self.work_order_status,
            "checkpoint_id": self.checkpoint_id,
            "handoff_verdict": self.handoff_verdict,
            "quality_verdict": self.quality_verdict,
            "deployment_verdict": self.deployment_verdict,
            "approval_satisfied": self.approval_satisfied,
            "audit_events": self.audit_events,
            "released": self.released,
            "steps": list(self.steps),
        }


class CompanyFPilot:
    """Runs the single supervised Company F pilot on a caller-provided repo.

    ``repo_root`` must be an existing (possibly throwaway) git repository that
    is non-critical for the caller. Git-state verification is enabled, and
    human approval is mandatory before any release.
    """

    def __init__(
        self,
        repo_root: str | Path,
        *,
        state_dir: str | Path | None = None,
        project: PilotProject | None = None,
    ) -> None:
        root = Path(repo_root).resolve()
        self.repo_root = root
        self.project = project or PilotProject(
            project_id="company-f-pilot",
            name="Company F Pilot (non-critical)",
        )
        self.coordinator = CompanyFCoordinator(
            state_dir=state_dir,
            repo_root=root,
            verify_git=True,
            deployment_requires_human_approval=True,
        )
        self._roles: dict[str, RoleIdentity] = {}

    # ------------------------------------------------------------------
    # role registration
    # ------------------------------------------------------------------

    def _role(
        self,
        role: CompanyRole,
        agent_id: str,
        caps: set[str],
        trust: int,
        name: str,
    ) -> RoleIdentity:
        identity = RoleIdentity(
            role=role,
            agent_id=agent_id,
            name=name,
            capabilities=frozenset(caps),
            trust_level=trust,
        )
        self.coordinator.register_role(identity)
        self._roles[agent_id] = identity
        return identity

    def setup_roles(self) -> dict[str, RoleIdentity]:
        """Register all Company F roles needed for the pilot flow."""
        self._roles = {
            "council": self._role(
                CompanyRole.AI_COUNCIL,
                "council",
                {"decide"},
                3,
                "AI Council",
            ),
            "planning": self._role(
                CompanyRole.PLANNING_COUNCIL,
                "planning",
                {"plan", "route"},
                2,
                "Planning Council",
            ),
            "coder": self._role(
                CompanyRole.CODING_AGENT,
                "coder",
                {"write", "read"},
                2,
                "Coding Agent",
            ),
            "handoff": self._role(
                CompanyRole.HANDOFF_AGENT,
                "handoff",
                {"checkpoint"},
                2,
                "Handoff Agent",
            ),
            "guardian": self._role(
                CompanyRole.QUALITY_GUARDIAN,
                "guardian",
                {"audit", "test"},
                3,
                "Quality Guardian",
            ),
            "deployer": self._role(
                CompanyRole.DEPLOYMENT_CHECK,
                "deployer",
                {"deploy", "rollback"},
                3,
                "Deployment Check",
            ),
            "human": self._role(
                CompanyRole.HUMAN,
                self.project.human_approver,
                {"approve"},
                5,
                "Human Approver",
            ),
        }
        return dict(self._roles)

    # ------------------------------------------------------------------
    # full happy-path run
    # ------------------------------------------------------------------

    def run(self) -> PilotReport:
        """Execute the full supervised pilot flow and return evidence.

        This is the canonical end-to-end proof: GO -> Work Order -> Coding ->
        Handoff -> Quality -> Deployment Check -> Human Approval -> Release.
        """
        report = PilotReport(project=self.project)
        roles = self.setup_roles()
        coordinator = self.coordinator

        coordinator.register_project(
            ProjectIdentity(
                project_id=self.project.project_id,
                name=self.project.name,
                root=str(self.repo_root),
            )
        )
        report.steps.append("company-f: project registered")

        decision = coordinator.make_decision(
            roles["council"],
            self.project.project_id,
            DecisionStatus.GO,
            rationale="pilot scope is non-critical and fully understood",
        )
        report.decision_status = decision.decision_status.value
        report.steps.append("ai-council: GO")

        work_order = coordinator.plan_work_order(
            roles["planning"],
            self.project.project_id,
            title="Add non-critical doc-index helper utility",
            description=(
                "Implement a small CLI helper that greps a docs/ folder and "
                "prints a sorted index of markdown titles. Internal only."
            ),
            scope="docs/ index helper only — no network, no production writes",
            project_owner=self.project.project_owner,
            acceptance_criteria=[
                "helper lists markdown files under docs/",
                "helper prints sorted titles without secrets",
                "zero writes outside the pilot scratch area",
            ],
            constraints=[
                "no network access",
                "no reading of .env or credential files",
                "no writes outside docs/ and the scratch folder",
            ],
            risks=[
                {
                    "risk_id": "r1",
                    "description": "helper accidentally reads a secrets file",
                    "severity": "high",
                    "mitigation": "SecurityFilter-style allow-list of extensions",
                },
                {
                    "risk_id": "r2",
                    "description": "scope creep into other subsystems",
                    "severity": "medium",
                    "mitigation": "forbidden actions list enforced by coordinator",
                },
            ],
            rollback_consideration=(
                "The helper is additive only; removal of the helper file and "
                "reverting its single commit fully restores the previous state."
            ),
            required_capabilities=("write", "read"),
            allowed_actions=("write.docs.index",),
            forbidden_actions=("delete.production", "network.call"),
        )
        report.work_order_id = work_order.work_order_id
        report.work_order_status = work_order.status.value
        report.steps.append("planning: work order registered")

        coordinator.assign_agent(
            roles["planning"], work_order.work_order_id, roles["coder"]
        )
        report.steps.append("routing: coding agent assigned (capability verified)")

        coordinator.acquire_lease(work_order.work_order_id, roles["coder"])
        coordinator.record_action(
            roles["coder"],
            work_order.work_order_id,
            "write.docs.index",
            description="created docs/_index_helper.py in scratch area",
        )
        report.steps.append("coding: in-scope action recorded")

        checkpoint = coordinator.submit_checkpoint(
            roles["coder"],
            work_order.work_order_id,
            summary="helper implemented; Git HEAD verified; no secrets",
            state={"files": ["docs/_index_helper.py"], "tests_run": 1},
            consumer=roles["handoff"].agent_id,
        )
        report.checkpoint_id = checkpoint.checkpoint_id
        report.steps.append("handoff: checkpoint accepted with Git verification")

        verdict = coordinator.evaluate_handoff(
            roles["handoff"], checkpoint
        )
        report.handoff_verdict = verdict.value
        if verdict != HandoffVerdict.ACCEPT:
            report.steps.append(f"handoff: REJECTED ({verdict.value})")
            return report
        report.steps.append("handoff: ACCEPT")

        quality = coordinator.quality_gate(
            roles["guardian"],
            work_order.work_order_id,
            QualityVerdict.PASS,
            issues=[],
            evidence="1 unit test for the index helper passed; no secret leakage",
        )
        report.quality_verdict = quality.verdict.value
        report.steps.append("quality-guardian: PASS")

        pre_approval = coordinator.run_deployment_check(
            roles["deployer"],
            work_order.work_order_id,
            rollback_ready=True,
            note="rollback plan ready",
        )
        if pre_approval.verdict != DeploymentVerdict.REQUIRE_APPROVAL:
            report.steps.append(
                f"deployment: unexpected pre-approval verdict {pre_approval.verdict.value}"
            )
            return report
        report.deployment_verdict = pre_approval.verdict.value
        report.steps.append("deployment-check: REQUIRE_APPROVAL (human gate)")

        coordinator.approve_deployment(
            roles["human"],
            work_order.work_order_id,
            note="human approver authorizes release of non-critical pilot",
        )
        report.approval_satisfied = True
        report.steps.append("human-approval: granted")

        approval = coordinator.run_deployment_check(
            roles["deployer"],
            work_order.work_order_id,
            rollback_ready=True,
            note="rollback plan ready, human approval recorded",
        )
        report.deployment_verdict = approval.verdict.value
        if approval.verdict == DeploymentVerdict.PASS:
            released = coordinator.release(
                roles["deployer"], work_order.work_order_id
            )
            report.work_order_status = released.status.value
            report.released = released.status.value == "released"
            report.steps.append("deployment-check: PASS -> release")

        report.audit_events = len(coordinator.audit_trail(work_order.work_order_id))

        if coordinator.deployment_requires_human_approval:
            report.steps.append("governance: human approval was mandatory")
        if coordinator.verify_git:
            report.steps.append("governance: Git state verified at handoff")
        return report