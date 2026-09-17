"""Phase 39B — Quality Guardian & Deployment Control tests.

Covers the lphase37.md Phase 39B verification list: every operational gate,
deployment refusal paths (test failure, security finding, secret leakage,
dirty Git, invalid artifact/version, missing doc => REQUIRE_FIX, no approval,
Quality FAIL), rollback mandatory, dry-run zero-write, readiness reports,
failure classification, audit trail, and a Coding -> Quality -> Deployment
end-to-end pilot.
"""

from __future__ import annotations

import json

import pytest

from handoff_agent.quality_gate import (
    GateEvidence,
    GateResult,
    GateStatus,
    QualityGuardian,
    QualityGateReport,
    classify_failure,
    deployment_dry_run,
    evaluate_deployment_readiness,
    rollback_readiness_report,
)


def _clean_evidence(**overrides) -> GateEvidence:
    base = dict(
        test_total=100,
        test_failures=0,
        regression_total=50,
        regression_failures=0,
        security_critical=0,
        security_high=0,
        secret_like_hits=0,
        dependency_critical=0,
        missing_docs=[],
        git_clean=True,
        version="1.0.0",
        artifacts_ok=True,
    )
    base.update(overrides)
    return GateEvidence(**base)


def _passing_report() -> QualityGateReport:
    return QualityGuardian().evaluate(_clean_evidence())


class TestQualityGates:
    def test_all_clean_pass(self) -> None:
        report = _passing_report()
        assert report.overall == GateStatus.PASS
        assert report.passed is True
        assert all(g.status == GateStatus.PASS for g in report.gates)
        assert len(report.gates) == 8

    def test_test_failure_blocked(self) -> None:
        report = QualityGuardian().evaluate(
            _clean_evidence(test_failures=3, test_total=100)
        )
        assert report.overall == GateStatus.FAIL
        test_result = next(g for g in report.gates if g.gate == "test")
        assert test_result.status == GateStatus.FAIL
        assert "3" in test_result.reason

    def test_regression_failure_blocked(self) -> None:
        report = QualityGuardian().evaluate(_clean_evidence(regression_failures=1))
        assert report.overall == GateStatus.FAIL

    def test_critical_security_finding_blocked(self) -> None:
        report = QualityGuardian().evaluate(_clean_evidence(security_critical=1))
        assert report.overall == GateStatus.FAIL
        sec = next(g for g in report.gates if g.gate == "security")
        assert sec.status == GateStatus.FAIL

    def test_high_security_finding_blocked(self) -> None:
        report = QualityGuardian().evaluate(_clean_evidence(security_high=2))
        assert report.overall == GateStatus.FAIL

    def test_secret_leakage_blocked(self) -> None:
        report = QualityGuardian().evaluate(_clean_evidence(secret_like_hits=1))
        assert report.overall == GateStatus.FAIL
        secret = next(g for g in report.gates if g.gate == "secret")
        assert secret.status == GateStatus.FAIL

    def test_dirty_git_state_blocked(self) -> None:
        report = QualityGuardian().evaluate(_clean_evidence(git_clean=False))
        assert report.overall == GateStatus.FAIL
        git_gate = next(g for g in report.gates if g.gate == "git")
        assert git_gate.status == GateStatus.FAIL

    def test_missing_documentation_requires_fix(self) -> None:
        report = QualityGuardian().evaluate(
            _clean_evidence(missing_docs=["docs/UPGRADE.md"])
        )
        assert report.overall == GateStatus.REQUIRE_FIX
        doc = next(g for g in report.gates if g.gate == "documentation")
        assert doc.status == GateStatus.REQUIRE_FIX

    def test_invalid_version_blocked(self) -> None:
        for bad in ("", "latest", "1.0", "v.next"):
            report = QualityGuardian().evaluate(_clean_evidence(version=bad))
            assert report.overall == GateStatus.FAIL, bad

    def test_invalid_artifact_blocked(self) -> None:
        report = QualityGuardian().evaluate(_clean_evidence(artifacts_ok=False))
        assert report.overall == GateStatus.FAIL

    def test_dependency_critical_blocked(self) -> None:
        report = QualityGuardian().evaluate(_clean_evidence(dependency_critical=1))
        assert report.overall == GateStatus.FAIL

    def test_quality_decision_has_reason(self) -> None:
        report = QualityGuardian().evaluate(
            _clean_evidence(test_failures=1, security_critical=1)
        )
        assert all(g.reason for g in report.gates)

    def test_failure_classification(self) -> None:
        failures = _passing_report().classifications()
        assert failures == []
        fail_report = QualityGuardian().evaluate(
            _clean_evidence(security_critical=1, test_failures=2)
        )
        classified = fail_report.classifications()
        assert len(classified) == 2
        by_gate = {c.gate: c for c in classified}
        assert by_gate["security"].severity.value == "critical"
        assert by_gate["test"].severity.value == "high"
        assert classify_failure("unknown", "x").severity.value == "medium"


class TestDeploymentControl:
    def test_deployment_without_quality_pass_blocked(self) -> None:
        failing = QualityGuardian().evaluate(_clean_evidence(test_failures=1))
        report = evaluate_deployment_readiness(
            failing,
            work_order_id="wo-1",
            target="prod",
            plan=["tag release"],
            rollback_ready=True,
            rollback_plan="revert tag",
            approval_granted=True,
            plan_reviewed=True,
        )
        assert report.verdict == GateStatus.FAIL
        assert "Quality PASS" in report.quality_reason

    def test_deployment_with_quality_fail_blocked(self) -> None:
        failing = QualityGuardian().evaluate(_clean_evidence(security_high=1))
        report = evaluate_deployment_readiness(
            failing,
            work_order_id="wo-1",
            target="prod",
            plan=["deploy"],
            rollback_ready=True,
            rollback_plan="revert",
            approval_granted=True,
            plan_reviewed=True,
        )
        assert report.verdict == GateStatus.FAIL

    def test_deployment_without_approval_requires_approval(self) -> None:
        report = evaluate_deployment_readiness(
            _passing_report(),
            work_order_id="wo-1",
            target="prod",
            plan=["deploy"],
            rollback_ready=True,
            rollback_plan="revert",
            approval_granted=False,
            plan_reviewed=True,
        )
        assert report.verdict == GateStatus.REQUIRE_APPROVAL

    def test_deployment_without_plan_review_requires_approval(self) -> None:
        report = evaluate_deployment_readiness(
            _passing_report(),
            work_order_id="wo-1",
            target="prod",
            plan=["deploy"],
            rollback_ready=True,
            rollback_plan="revert",
            approval_granted=True,
            plan_reviewed=False,
        )
        assert report.verdict == GateStatus.REQUIRE_APPROVAL

    def test_rollback_plan_mandatory(self) -> None:
        report = evaluate_deployment_readiness(
            _passing_report(),
            work_order_id="wo-1",
            target="prod",
            plan=["deploy"],
            rollback_ready=False,
            rollback_plan="",
            approval_granted=True,
            plan_reviewed=True,
        )
        assert report.verdict == GateStatus.FAIL
        rb = rollback_readiness_report(True, "")
        assert rb["ready"] is False

    def test_deployment_passes_with_all_requirements(self) -> None:
        report = evaluate_deployment_readiness(
            _passing_report(),
            work_order_id="wo-1",
            target="prod",
            plan=["tag release", "apply artifact"],
            rollback_ready=True,
            rollback_plan="git revert release tag",
            approval_granted=True,
            plan_reviewed=True,
        )
        assert report.verdict == GateStatus.PASS
        assert report.dry_run["zero_write"] is True

    def test_deployment_readiness_report_created(self) -> None:
        report = evaluate_deployment_readiness(
            _passing_report(),
            work_order_id="wo-42",
            target="prod",
            plan=["deploy"],
            rollback_ready=True,
            rollback_plan="revert",
            approval_granted=True,
            plan_reviewed=True,
        )
        blob = json.dumps(report.to_dict())
        assert "wo-42" in blob
        assert "rollback_plan" in blob
        assert "verdict" in blob

    def test_no_automatic_deployment(self) -> None:
        dry = deployment_dry_run("prod", ["step1", "step2"])
        assert dry["mode"] == "dry-run"
        assert dry["zero_write"] is True
        assert dry["zero_network"] is True
        assert "no production mutation performed" in dry["recommendation"]

    def test_dry_run_zero_write(self, tmp_path) -> None:
        before = {p.name: p.stat().st_size for p in tmp_path.iterdir()}
        deployment_dry_run(str(tmp_path / "prod"), ["deploy"])
        after = {p.name: p.stat().st_size for p in tmp_path.iterdir()}
        assert before == after
        assert not (tmp_path / "prod").exists()


class TestEndToEndPilot:
    def test_coding_quality_deployment_end_to_end(self, tmp_path) -> None:
        from conftest import commit_all, init_repo, write_file

        repo = tmp_path / "pilot-repo"
        init_repo(repo)
        write_file(repo, "README.md", "# pilot\n")
        commit_all(repo, "baseline")

        from handoff_agent.pilot import CompanyFPilot

        pilot = CompanyFPilot(repo_root=repo)
        report = pilot.run()
        assert report.released is True

        evidence = _clean_evidence(
            test_total=1933,
            test_failures=0,
            regression_total=1933,
            regression_failures=0,
            version="1.0.0",
        )
        quality = QualityGuardian().evaluate(evidence)
        assert quality.overall == GateStatus.PASS

        readiness = evaluate_deployment_readiness(
            quality,
            work_order_id=report.work_order_id,
            target="company-f-pilot-release",
            plan=["tag", "attach artifact", "mark released"],
            rollback_ready=True,
            rollback_plan="remove release tag and revert single commit",
            approval_granted=True,
            plan_reviewed=True,
        )
        assert readiness.verdict == GateStatus.PASS

        audit = pilot.coordinator.audit_trail(report.work_order_id)
        actions = [e.action for e in audit]
        for expected in (
            "checkpoint.accepted",
            "quality.gate",
            "deployment.approved",
            "deployment.pass",
            "work_order.released",
        ):
            assert expected in actions

        trace = pilot.coordinator.audit_trail()
        marshalled = json.dumps([e.to_dict() for e in trace], default=str)
        assert "sk-" not in marshalled


class TestSecretSafety:
    def test_gate_results_never_embed_secrets(self) -> None:
        evidence = _clean_evidence(
            secret_like_hits=1,
            missing_docs=["SECRETS.md"],
        )
        report = QualityGuardian().evaluate(evidence)
        blob = json.dumps(report.to_dict(), default=str)
        assert "sk-" not in blob
        assert "api_key" not in blob

    def test_readiness_report_secret_free(self) -> None:
        report = evaluate_deployment_readiness(
            _passing_report(),
            work_order_id="wo-1",
            target="prod",
            plan=["deploy"],
            rollback_ready=True,
            rollback_plan="revert",
            approval_granted=False,
            plan_reviewed=True,
        )
        blob = json.dumps(report.to_dict(), default=str)
        assert "sk-" not in blob

    def test_no_arbitrary_command_execution(self) -> None:
        from handoff_agent import quality_gate

        source = open(quality_gate.__file__, encoding="utf-8").read()
        assert "eval(" not in source
        assert "exec(" not in source
        assert "os.system(" not in source
        assert "subprocess" not in source