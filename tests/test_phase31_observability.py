"""Phase 31 — Observability, trace, and audit evidence tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from handoff_agent.audit import build_company_trace, export_audit_json
from handoff_agent.company_f import (
    CheckpointState,
    CompanyFCoordinator,
    CompanyRole,
    DecisionStatus,
    InvalidTransition,
    ProjectIdentity,
    QualityVerdict,
    RoleIdentity,
    WorkOrderStatus,
)
from handoff_agent.telemetry import (
    TelemetryCollector,
    TelemetryDomain,
    TelemetryStatus,
    RetentionPolicy,
    emit_event,
    is_sensitive_value,
    new_collector,
    redact_text,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROJECT = ProjectIdentity(project_id="proj-1", name="Company F", root="/tmp/p")
COUNCIL = RoleIdentity(role=CompanyRole.AI_COUNCIL, agent_id="council-1", name="Council", capabilities=frozenset({"decide"}), trust_level=3)
HUMAN = RoleIdentity(role=CompanyRole.HUMAN, agent_id="human-1", name="Human", capabilities=frozenset({"decide", "approve"}), trust_level=5)
PLANNING = RoleIdentity(role=CompanyRole.PLANNING_COUNCIL, agent_id="planning-1", name="Planning", capabilities=frozenset({"plan", "route"}), trust_level=2)
CODER = RoleIdentity(role=CompanyRole.CODING_AGENT, agent_id="coder-a", name="Coder", capabilities=frozenset({"read", "write"}), trust_level=2)
HANDOFF = RoleIdentity(role=CompanyRole.HANDOFF_AGENT, agent_id="handoff-1", name="Handoff", capabilities=frozenset({"checkpoint", "route"}), trust_level=2)
GUARDIAN = RoleIdentity(role=CompanyRole.QUALITY_GUARDIAN, agent_id="guardian-1", name="Guardian", capabilities=frozenset({"quality_gate"}), trust_level=3)
DEPLOY = RoleIdentity(role=CompanyRole.DEPLOYMENT_CHECK, agent_id="deployer-1", name="Deployer", capabilities=frozenset({"deployment_gate"}), trust_level=3)


def _setup(*, approval_required: bool = False, tracer=None):
    c = CompanyFCoordinator(tracer=tracer)
    for identity in [COUNCIL, HUMAN, PLANNING, CODER, HANDOFF, GUARDIAN, DEPLOY]:
        c.register_role(identity)
    c.register_project(PROJECT)
    c.make_decision(COUNCIL, "proj-1", DecisionStatus.GO, "approved")
    wo = c.create_work_order(
        PLANNING, "proj-1", "Build feature", "desc",
        required_capabilities=("write",), approval_required=approval_required,
    )
    c.add_plan(PLANNING, wo.work_order_id, "iterative")
    c.assign_agent(PLANNING, wo.work_order_id, CODER)
    cp = c.submit_checkpoint(CODER, wo.work_order_id, "done", {"file": "x.py"})
    c.evaluate_handoff(HANDOFF, cp)
    c.quality_gate(GUARDIAN, wo.work_order_id, QualityVerdict.PASS)
    return c, wo


def _setup_full(*, approval_required: bool = False, tracer=None):
    c, wo = _setup(approval_required=approval_required, tracer=tracer)
    if approval_required:
        c.approve_work_order(HUMAN, wo.work_order_id, "ok")
    c.deployment_gate(DEPLOY, wo.work_order_id, rollback_ready=True)
    c.release(DEPLOY, wo.work_order_id)
    return c, wo


# ---------------------------------------------------------------------------
# End-to-end trace tests
# ---------------------------------------------------------------------------


class TestEndToEndTrace:
    def test_trace_connects_decision_to_deployment(self) -> None:
        collector = new_collector()
        c, wo = _setup_full(tracer=collector)
        traces = collector.traces()
        assert len(traces) >= 1
        trace = traces[0]
        ops = [e.operation for e in collector.events_by_trace(trace.trace_id)]
        assert "project.register" in ops
        assert "decision.make" in ops
        assert "workorder.create" in ops
        assert "workorder.plan" in ops
        assert "workorder.assign" in ops
        assert "workorder.checkpoint" in ops
        assert "handoff.accept" in ops
        assert "quality.gate" in ops
        assert "deployment.gate" in ops
        assert "workorder.release" in ops
        domains = {e.domain for e in collector.events_by_trace(trace.trace_id)}
        assert domains >= {
            TelemetryDomain.PROJECT.value,
            TelemetryDomain.DECISION.value,
            TelemetryDomain.WORKORDER.value,
            TelemetryDomain.HANDOFF.value,
            TelemetryDomain.QUALITY.value,
            TelemetryDomain.DEPLOYMENT.value,
        }

    def test_trace_connects_work_order_checkpoint_review_approval(self) -> None:
        collector = new_collector()
        c, wo = _setup_full(approval_required=True, tracer=collector)
        events = collector.events()
        op_counts = {}
        for e in events:
            op_counts[e.operation] = op_counts.get(e.operation, 0) + 1
        assert op_counts.get("workorder.approve", 0) >= 1
        assert op_counts.get("workorder.checkpoint", 0) >= 1
        assert op_counts.get("handoff.accept", 0) >= 1
        assert op_counts.get("quality.gate", 0) >= 1
        assert op_counts.get("deployment.gate", 0) >= 1

    def test_conflict_triggers_blocked_status_event(self) -> None:
        collector = new_collector()
        c = CompanyFCoordinator(tracer=collector)
        for ident in [COUNCIL, HUMAN, PLANNING, CODER, GUARDIAN, DEPLOY]:
            c.register_role(ident)
        c.register_project(PROJECT)
        c.make_decision(COUNCIL, "proj-1", DecisionStatus.GO, "go")
        c.create_work_order(PLANNING, "proj-1", "A", "a", required_capabilities=("write",))
        c.make_decision(HUMAN, "proj-1", DecisionStatus.NO_GO, "revoked")
        wo = list(c.list_work_orders())[0]
        assert wo.status == WorkOrderStatus.CONFLICT
        blocked = [e for e in collector.events() if e.status == TelemetryStatus.BLOCKED.value]
        assert any(e.operation == "decision.make" for e in blocked)


# ---------------------------------------------------------------------------
# Metrics and degraded-state detection
# ---------------------------------------------------------------------------


class TestMetrics:
    def test_retry_timeout_failure_cancellation_metrics(self) -> None:
        collector = new_collector()
        statuses = [
            TelemetryStatus.RETRY.value,
            TelemetryStatus.TIMEOUT.value,
            TelemetryStatus.ERROR.value,
            TelemetryStatus.CANCELLED.value,
        ]
        for status in statuses:
            emit_event(
                collector,
                domain=TelemetryDomain.WORKORDER.value,
                operation="wo.test",
                status=status,
                resource="wo-1",
                actor="test",
            )
        metrics = collector.metrics()
        wo = metrics.get(TelemetryDomain.WORKORDER.value, {})
        assert wo["retry_count"] >= 1
        assert wo["timeout_count"] >= 1
        assert wo["failure_count"] >= 1
        assert wo["cancellation_count"] >= 1

    def test_degraded_state_detected(self) -> None:
        collector = new_collector()
        for _ in range(3):
            emit_event(
                collector,
                domain=TelemetryDomain.WORKORDER.value,
                operation="wo.fail",
                status=TelemetryStatus.ERROR.value,
                resource="wo-2",
                actor="test",
            )
        degraded = collector.degraded()
        assert len(degraded) >= 1
        assert degraded[0].total == 3
        assert degraded[0].failures == 3


# ---------------------------------------------------------------------------
# Redaction, audit export, secret safety
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_audit_export_has_no_secrets(self, tmp_path: Path) -> None:
        collector = new_collector()
        c = CompanyFCoordinator(tracer=collector)
        for ident in [COUNCIL, PLANNING, CODER, HANDOFF, GUARDIAN, DEPLOY]:
            c.register_role(ident)
        c.register_project(PROJECT)
        c.make_decision(COUNCIL, "proj-1", DecisionStatus.GO, "use sk-1234567890abcdef1234")
        wo = c.create_work_order(
            PLANNING, "proj-1", "F", "desc", required_capabilities=("write",)
        )
        c.add_plan(PLANNING, wo.work_order_id, "linear")
        c.assign_agent(PLANNING, wo.work_order_id, CODER)
        cp = c.submit_checkpoint(CODER, wo.work_order_id, "done", {"api_key": "token=ghp_ABCDEFGHIJKLMNOP123456789012345"})
        trace_dict = build_company_trace(c)
        export = json.dumps(trace_dict, default=str)
        checkpoints_data = json.dumps(trace_dict["checkpoints"], default=str)
        assert "sk-1234567890abcdef1234" not in export
        assert "ghp_ABCDEFGHIJKLMNOP123456789012345" not in export
        assert "[redacted]" in checkpoints_data or "api_key" not in str(trace_dict["checkpoints"])
        out = tmp_path / "audit.json"
        result = export_audit_json(c, collector, out)
        assert result["exported"] is True
        assert out.exists()
        raw = out.read_text()
        assert "sk-1234567890abcdef1234" not in raw
        assert "ghp_ABCDEFGHIJKLMNOP123456789012345" not in raw

    def test_redaction_parity_with_security_patterns(self) -> None:
        cases = [
            "token=abc123def456ghi7890",
            "private_key: blah",
            '"ghp_12345678901234567890123456789012345678"',
            "sk-proj1234567890abcdef",
            "authorization: Bearer abc123def456ghi7890",
        ]
        for text in cases:
            redacted = redact_text(text)
            assert "[redacted]" in redacted or "PRIVATE" in redacted, f"failed to redact: {text!r}"


# ---------------------------------------------------------------------------
# Disabled mode
# ---------------------------------------------------------------------------


class TestDisabledMode:
    def test_disabled_collector_no_events(self) -> None:
        collector = new_collector(enabled=False)
        c = CompanyFCoordinator(tracer=collector)
        for ident in [COUNCIL, PLANNING, CODER, HANDOFF, GUARDIAN, DEPLOY]:
            c.register_role(ident)
        c.register_project(PROJECT)
        c.make_decision(COUNCIL, "proj-1", DecisionStatus.GO, "go")
        assert collector.total_emitted() == 0
        assert len(collector.events()) == 0


# ---------------------------------------------------------------------------
# Span pruning on trace eviction
# ---------------------------------------------------------------------------


class TestSpanPruning:
    def test_pruned_trace_spans_are_removed(self) -> None:
        collector = TelemetryCollector(
            enabled=True,
            local_only=True,
            retention=RetentionPolicy(max_traces=2),
        )
        for i in range(4):
            emit_event(
                collector,
                domain=TelemetryDomain.WORKORDER.value,
                operation="wo.emit",
                status=TelemetryStatus.OK.value,
                trace_id=f"trace-{i}",
                resource=f"wo-{i}",
                actor="test",
            )
        assert len(collector.traces()) == 2
        remaining_trace_ids = {t.trace_id for t in collector.traces()}
        assert remaining_trace_ids == {"trace-2", "trace-3"}
        remaining_span_trace_ids = {s.trace_id for s in collector.spans()}
        assert remaining_span_trace_ids <= remaining_trace_ids
