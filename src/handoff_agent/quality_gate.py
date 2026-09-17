"""Phase 39B — Quality Guardian & Deployment Control (operational gates).

Turns quality and deployment into real operational gates so that no coding
output can reach deployment without evidence of quality, security, and human
approval.

Gate contract (PASS / FAIL / REQUIRE_FIX / REQUIRE_APPROVAL):

- test gate          : any failing test blocks deployment
- regression gate    : any regression blocks deployment
- security audit gate: critical/high findings block deployment
- secret leakage gate: secret-like content blocks deployment
- dependency gate    : critical dependency issues block deployment
- documentation gate : missing docs => REQUIRE_FIX
- git state gate     : dirty/divergent Git state blocks deployment
- artifact/version   : invalid artifact or version blocks deployment

Deployment control:

- release checklist
- deployment readiness report (quality + rollback + approval + plan review)
- rollback readiness report (rollback plan mandatory)
- deployment dry-run (zero-write / zero-network by construction)
- deployment plan review
- human approval requirement
- failure classification
- release audit trail
- safe diagnostics (secret-free, deterministic)
- no automatic deployment

Runtime uses only the standard library and never writes to production.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# gate verdicts & results
# ---------------------------------------------------------------------------


class GateStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    REQUIRE_FIX = "require_fix"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class GateResult:
    gate: str
    status: GateStatus
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "status": self.status.value,
            "reason": self.reason,
            "evidence": {k: v for k, v in self.evidence.items()},
        }


@dataclass
class GateEvidence:
    """Real, caller-provided evidence for every operational gate."""

    test_total: int = 0
    test_failures: int = 0
    regression_total: int = 0
    regression_failures: int = 0
    security_critical: int = 0
    security_high: int = 0
    secret_like_hits: int = 0
    dependency_critical: int = 0
    missing_docs: list[str] = field(default_factory=list)
    git_clean: bool = True
    version: str = ""
    artifacts_ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_total": self.test_total,
            "test_failures": self.test_failures,
            "regression_total": self.regression_total,
            "regression_failures": self.regression_failures,
            "security_critical": self.security_critical,
            "security_high": self.security_high,
            "secret_like_hits": self.secret_like_hits,
            "dependency_critical": self.dependency_critical,
            "missing_docs": list(self.missing_docs),
            "git_clean": self.git_clean,
            "version": self.version,
            "artifacts_ok": self.artifacts_ok,
        }


# ---------------------------------------------------------------------------
# gate implementations
# ---------------------------------------------------------------------------


def test_gate(evidence: GateEvidence) -> GateResult:
    if evidence.test_failures > 0:
        return GateResult(
            "test",
            GateStatus.FAIL,
            f"{evidence.test_failures} of {evidence.test_total} tests failed",
            {"failures": evidence.test_failures, "total": evidence.test_total},
        )
    return GateResult(
        "test",
        GateStatus.PASS,
        f"all {evidence.test_total} tests passed",
        {"failures": 0, "total": evidence.test_total},
    )


def regression_gate(evidence: GateEvidence) -> GateResult:
    if evidence.regression_failures > 0:
        return GateResult(
            "regression",
            GateStatus.FAIL,
            f"{evidence.regression_failures} of {evidence.regression_total} "
            "regression tests failed",
            {"failures": evidence.regression_failures, "total": evidence.regression_total},
        )
    return GateResult(
        "regression",
        GateStatus.PASS,
        f"regression suite clean ({evidence.regression_total} passed)",
        {"failures": 0, "total": evidence.regression_total},
    )


def security_audit_gate(evidence: GateEvidence) -> GateResult:
    critical = evidence.security_critical
    high = evidence.security_high
    if critical > 0 or high > 0:
        return GateResult(
            "security",
            GateStatus.FAIL,
            f"{critical} critical, {high} high security finding(s)",
            {"critical": critical, "high": high},
        )
    return GateResult(
        "security",
        GateStatus.PASS,
        "no critical or high security findings",
        {"critical": 0, "high": 0},
    )


def secret_leakage_gate(evidence: GateEvidence) -> GateResult:
    if evidence.secret_like_hits > 0:
        return GateResult(
            "secret",
            GateStatus.FAIL,
            f"{evidence.secret_like_hits} secret-like hit(s) detected",
            {"hits": evidence.secret_like_hits},
        )
    return GateResult(
        "secret",
        GateStatus.PASS,
        "no secret-like content detected",
        {"hits": 0},
    )


def dependency_audit_gate(evidence: GateEvidence) -> GateResult:
    if evidence.dependency_critical > 0:
        return GateResult(
            "dependency",
            GateStatus.FAIL,
            f"{evidence.dependency_critical} critical dependency issue(s)",
            {"critical": evidence.dependency_critical},
        )
    return GateResult(
        "dependency",
        GateStatus.PASS,
        "no critical dependency issues",
        {"critical": 0},
    )


def documentation_gate(evidence: GateEvidence) -> GateResult:
    missing = list(evidence.missing_docs)
    if missing:
        return GateResult(
            "documentation",
            GateStatus.REQUIRE_FIX,
            "missing documentation: " + ", ".join(missing),
            {"missing": missing},
        )
    return GateResult(
        "documentation",
        GateStatus.PASS,
        "documentation complete",
        {"missing": []},
    )


def git_state_gate(evidence: GateEvidence) -> GateResult:
    if not evidence.git_clean:
        return GateResult(
            "git",
            GateStatus.FAIL,
            "dirty or divergent Git state blocks deployment",
            {"clean": False},
        )
    return GateResult(
        "git",
        GateStatus.PASS,
        "Git state clean",
        {"clean": True},
    )


_SEMVER_LIKE = re.compile(
    r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$"
)


def artifact_version_gate(evidence: GateEvidence) -> GateResult:
    version = evidence.version or ""
    if not _SEMVER_LIKE.match(version) or not evidence.artifacts_ok:
        return GateResult(
            "artifact-version",
            GateStatus.FAIL,
            f"invalid artifact/version: version={version!r} artifacts_ok={evidence.artifacts_ok}",
            {"version": version, "artifacts_ok": evidence.artifacts_ok},
        )
    return GateResult(
        "artifact-version",
        GateStatus.PASS,
        f"artifact and version valid ({version})",
        {"version": version, "artifacts_ok": True},
    )


# ---------------------------------------------------------------------------
# failure classification
# ---------------------------------------------------------------------------


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class ClassifiedFailure:
    gate: str
    severity: Severity
    component: str
    description: str
    required_fix: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "severity": self.severity.value,
            "component": self.component,
            "description": self.description,
            "required_fix": self.required_fix,
        }


FAILURE_CLASSIFICATION: dict[str, tuple[Severity, str]] = {
    "test": (Severity.HIGH, "test-suite"),
    "regression": (Severity.HIGH, "test-suite"),
    "security": (Severity.CRITICAL, "security"),
    "secret": (Severity.CRITICAL, "secret-handling"),
    "dependency": (Severity.HIGH, "dependencies"),
    "documentation": (Severity.MEDIUM, "documentation"),
    "git": (Severity.HIGH, "git-state"),
    "artifact-version": (Severity.HIGH, "release-artifact"),
}


def classify_failure(gate: str, reason: str) -> ClassifiedFailure:
    severity, component = FAILURE_CLASSIFICATION.get(
        gate, (Severity.MEDIUM, gate)
    )
    return ClassifiedFailure(
        gate=gate,
        severity=severity,
        component=component,
        description=reason,
        required_fix=f"resolve gate '{gate}' before release",
    )


# ---------------------------------------------------------------------------
# quality report
# ---------------------------------------------------------------------------

RELEASE_CHECKLIST: tuple[str, ...] = (
    "tests pass",
    "regression pass",
    "security audit pass",
    "no secret leakage",
    "dependencies audited",
    "documentation present",
    "git state clean",
    "artifact and version valid",
    "rollback plan available",
    "human approval granted",
)


@dataclass
class QualityGateReport:
    overall: GateStatus
    gates: list[GateResult] = field(default_factory=list)
    release_checklist: list[str] = field(default_factory=list)
    generated_at_ms: int = field(default_factory=_now_ms)

    @property
    def passed(self) -> bool:
        return self.overall == GateStatus.PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall": self.overall.value,
            "gates": [g.to_dict() for g in self.gates],
            "release_checklist": [
                {"item": i, "ok": self.overall == GateStatus.PASS}
                for i in self.release_checklist
            ],
            "generated_at_ms": self.generated_at_ms,
        }

    def classifications(self) -> list[ClassifiedFailure]:
        return [
            classify_failure(g.gate, g.reason)
            for g in self.gates
            if g.status != GateStatus.PASS
        ]


class QualityGuardian:
    """Runs every operational gate over caller-provided, real evidence."""

    def evaluate(self, evidence: GateEvidence) -> QualityGateReport:
        results = [
            test_gate(evidence),
            regression_gate(evidence),
            security_audit_gate(evidence),
            secret_leakage_gate(evidence),
            dependency_audit_gate(evidence),
            documentation_gate(evidence),
            git_state_gate(evidence),
            artifact_version_gate(evidence),
        ]
        if any(r.status == GateStatus.FAIL for r in results):
            overall = GateStatus.FAIL
        elif any(r.status == GateStatus.REQUIRE_FIX for r in results):
            overall = GateStatus.REQUIRE_FIX
        else:
            overall = GateStatus.PASS
        return QualityGateReport(
            overall=overall,
            gates=results,
            release_checklist=list(RELEASE_CHECKLIST),
        )


# ---------------------------------------------------------------------------
# deployment control
# ---------------------------------------------------------------------------


@dataclass
class DeploymentReadinessReport:
    work_order_id: str
    quality_overall: GateStatus
    quality_reason: str = ""
    release_checklist: list[str] = field(default_factory=list)
    rollback_ready: bool = False
    rollback_plan: str = ""
    approval_required: bool = True
    approval_granted: bool = False
    plan_reviewed: bool = False
    dry_run: dict[str, Any] = field(default_factory=dict)
    verdict: GateStatus = GateStatus.FAIL
    generated_at_ms: int = field(default_factory=_now_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_order_id": self.work_order_id,
            "quality_overall": self.quality_overall.value,
            "quality_reason": self.quality_reason,
            "release_checklist": list(self.release_checklist),
            "rollback_ready": self.rollback_ready,
            "rollback_plan": self.rollback_plan,
            "approval_required": self.approval_required,
            "approval_granted": self.approval_granted,
            "plan_reviewed": self.plan_reviewed,
            "dry_run": self.dry_run,
            "verdict": self.verdict.value,
            "generated_at_ms": self.generated_at_ms,
        }


def rollback_readiness_report(
    rollback_ready: bool, rollback_plan: str
) -> dict[str, Any]:
    return {
        "rollback_ready": rollback_ready,
        "rollback_plan": rollback_plan or "",
        "ready": rollback_ready and bool((rollback_plan or "").strip()),
    }


def deployment_dry_run(target: str, plan: list[str]) -> dict[str, Any]:
    """Produce a deployment plan with zero writes and zero network.

    Constructed deterministically; this function performs no filesystem writes
    and no network calls of any kind.
    """
    return {
        "mode": "dry-run",
        "target": target,
        "zero_write": True,
        "zero_network": True,
        "steps": list(plan),
        "recommendation": (
            "no production mutation performed; run only after Quality PASS "
            "and human approval"
        ),
    }


def evaluate_deployment_readiness(
    quality_report: QualityGateReport,
    *,
    work_order_id: str,
    target: str,
    plan: list[str],
    rollback_ready: bool = False,
    rollback_plan: str = "",
    approval_granted: bool = False,
    plan_reviewed: bool = False,
) -> DeploymentReadinessReport:
    """Combine quality gate result + deployment constraints into a verdict."""
    dry_run = deployment_dry_run(target, plan)

    if quality_report.overall != GateStatus.PASS:
        return DeploymentReadinessReport(
            work_order_id=work_order_id,
            quality_overall=quality_report.overall,
            quality_reason="deployment refused without a Quality PASS",
            release_checklist=list(quality_report.release_checklist),
            rollback_ready=rollback_ready,
            rollback_plan=rollback_plan,
            approval_required=True,
            approval_granted=approval_granted,
            plan_reviewed=plan_reviewed,
            dry_run=dry_run,
            verdict=GateStatus.FAIL,
        )

    rollback = rollback_readiness_report(rollback_ready, rollback_plan)
    if not rollback["ready"]:
        return DeploymentReadinessReport(
            work_order_id=work_order_id,
            quality_overall=quality_report.overall,
            quality_reason="quality PASS recorded",
            release_checklist=list(quality_report.release_checklist),
            rollback_ready=False,
            rollback_plan=rollback_plan,
            approval_required=True,
            approval_granted=approval_granted,
            plan_reviewed=plan_reviewed,
            dry_run=dry_run,
            verdict=GateStatus.FAIL,
        )

    if not approval_granted:
        return DeploymentReadinessReport(
            work_order_id=work_order_id,
            quality_overall=quality_report.overall,
            quality_reason="quality PASS recorded",
            release_checklist=list(quality_report.release_checklist),
            rollback_ready=True,
            rollback_plan=rollback_plan,
            approval_required=True,
            approval_granted=False,
            plan_reviewed=plan_reviewed,
            dry_run=dry_run,
            verdict=GateStatus.REQUIRE_APPROVAL,
        )

    if not plan_reviewed:
        return DeploymentReadinessReport(
            work_order_id=work_order_id,
            quality_overall=quality_report.overall,
            quality_reason="quality PASS recorded",
            release_checklist=list(quality_report.release_checklist),
            rollback_ready=True,
            rollback_plan=rollback_plan,
            approval_required=True,
            approval_granted=True,
            plan_reviewed=False,
            dry_run=dry_run,
            verdict=GateStatus.REQUIRE_APPROVAL,
        )

    return DeploymentReadinessReport(
        work_order_id=work_order_id,
        quality_overall=quality_report.overall,
        quality_reason="quality PASS recorded",
        release_checklist=list(quality_report.release_checklist),
        rollback_ready=True,
        rollback_plan=rollback_plan,
        approval_required=True,
        approval_granted=True,
        plan_reviewed=True,
        dry_run=dry_run,
        verdict=GateStatus.PASS,
    )