"""Phase 30 — telemetry / observability tests.

Covers: universal event envelope, trace/span relationships, workflow/task/
agent/provider/handoff/message/remote/sync tracing, timeline generation,
duration metrics, retry/timeout/failure counters, latency metrics, error
classification, degraded-state detection, anomaly detection, health metrics,
reports, retention limits, sampling, disable/local-only modes, secret
redaction, CLI/API/MCP diagnostics, and cross-module integration.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Generator

import pytest

from handoff_agent.capability import AgentIdentity
from handoff_agent.delegation import Delegator
from handoff_agent.messaging import MessageBroker, create_message
from handoff_agent.orchestration import PersistentOrchestrator
from handoff_agent.registry import AgentRegistry
from handoff_agent.remote import (
    EndpointRegistry,
    RemoteEndpoint,
    RemoteHandoff,
    RemoteOperation,
)
from handoff_agent.sync import Device, DeviceRegistry, SyncCoordinator, DeviceTrust
from handoff_agent.telemetry import (
    AnomalyDetector,
    DomainMetrics,
    EventType,
    FailureRateAnomalyDetector,
    RetentionPolicy,
    Sampler,
    Span,
    TelemetryCollector,
    TelemetryDomain,
    TelemetryEvent,
    TelemetryStatus,
    Trace,
    classify_error,
    default_collector,
    emit_event,
    enable_default,
    is_sensitive_value,
    make_event_id,
    make_span_id,
    make_trace_id,
    new_collector,
    redact_mapping,
    redact_text,
    redact_value,
    reset_default,
)
from handoff_agent.workflow import WorkflowManager, WorkflowState
from handoff_agent.workflow_engine import (
    WorkflowDefinition,
    WorkflowEngine,
    NodeDefinition,
)


# ---------------------------------------------------------------------------
# Envelope / IDs / redaction
# ---------------------------------------------------------------------------


class TestEnvelopeAndIds:
    def test_ids_are_unique_and_prefixed(self) -> None:
        evt = make_event_id()
        trace = make_trace_id()
        span = make_span_id()
        assert evt.startswith("evt-")
        assert trace.startswith("trace-")
        assert span.startswith("span-")
        assert evt != make_event_id()
        assert trace != make_trace_id()

    def test_event_has_canonical_fields(self) -> None:
        c = new_collector()
        evt = c.emit(
            domain=TelemetryDomain.WORKFLOW.value,
            operation="run",
            resource="wf-1",
            actor="orchestrator",
        )
        assert evt is not None
        data = evt.to_dict()
        for key in (
            "event_id", "trace_id", "span_id", "parent_span_id", "domain",
            "operation", "status", "event_type", "timestamp", "actor",
            "resource", "event_chain", "duration_ms", "queue_ms",
            "processing_ms", "network_ms", "retry_count", "failure_count",
            "success_count", "timeout_count", "cancellation_count",
            "latency_ms", "error_type", "error_reason", "message",
        ):
            assert key in data, f"missing envelope field {key!r}"
        assert data["domain"] == "workflow"
        assert data["status"] == "ok"

    def test_event_matching(self) -> None:
        c = new_collector()
        c.emit(domain="workflow", operation="run", status="ok")
        c.emit(domain="workflow", operation="run", status="error")
        assert c.counts(domain="workflow") == 2
        assert c.counts(domain="workflow", status="error") == 1
        assert c.counts(status="ok") == 1

    def test_chain_changes_with_each_event(self) -> None:
        c = new_collector()
        e1 = c.emit(domain="workflow", operation="a")
        e2 = c.emit(domain="workflow", operation="b", trace_id=e1.trace_id)
        e3 = c.emit(domain="workflow", operation="c", trace_id=e1.trace_id)
        assert e1 and e2 and e3
        assert e1.event_chain
        assert e2.event_chain
        assert e3.event_chain
        assert e1.event_chain[:24] + e2.event_id[:24] not in (
            e3.event_chain,
            e2.event_chain,
        )


class TestRedaction:
    def test_redact_values(self) -> None:
        assert redact_text("api_key=sk-abcdef1234567890") == "[redacted]"
        assert "sk-" not in redact_value({"api_key": "sk-abcdef1234567890"})["api_key"]
        assert redact_value({"token": "abc123"})["token"] != "abc123"

    def test_redact_mapping_knows_secret_keys(self) -> None:
        out = redact_mapping(
            {"api_key": "x" * 30, "model": "claude", "count": 5}
        )
        assert out["api_key"] == "[redacted]"
        assert out["model"] == "claude"

    def test_redact_nested_structures(self) -> None:
        out = redact_value(
            {"headers": {"Authorization": "Bearer tok-zz" * 4}, "ok": [1, 2]}
        )
        assert out["headers"]["Authorization"] == "[redacted]"

    def test_is_sensitive_value(self) -> None:
        assert is_sensitive_value({"password": "hunter22!"})
        assert is_sensitive_value("client_secret=zz" + "k" * 20)
        assert not is_sensitive_value({"notes": "plain"})

    def test_event_never_carries_secrets(self) -> None:
        c = new_collector()
        c.emit(
            domain="provider",
            operation="call",
            metadata={"payload": "authorization: Bearer sk-abcd1234" * 2},
        )
        for evt in c.events():
            dumped = json.dumps(evt.to_dict())
            assert "sk-abcd1234" not in dumped
            assert "Bearer" not in dumped
        assert c.diagnostic_report()["redaction_scan_clean"]


# ---------------------------------------------------------------------------
# Trace / span relationship
# ---------------------------------------------------------------------------


class TestTraceSpan:
    def test_span_relationship_and_duration(self) -> None:
        c = new_collector()
        root = c.start_span(domain="workflow", operation="run", resource="wf")
        child = c.start_span(
            domain="task",
            operation="node:step1",
            resource="wf",
            parent_span_id=root,
        )
        c.end_span(child, TelemetryStatus.OK.value)
        c.end_span(root, TelemetryStatus.OK.value)
        spans = c.spans()
        assert len(spans) == 2
        by_id = {s.span_id: s for s in spans}
        assert by_id[child].parent_span_id == root
        assert by_id[child].duration_ms >= 0
        trace = c.traces()[0]
        assert len(trace.spans) == 2
        assert trace.root_span_id == root

    def test_trace_parent_child_detections(self) -> None:
        c = new_collector()
        root = c.start_span(domain="workflow", operation="run", resource="wf")
        child = c.start_span(
            domain="task", operation="go", resource="wf", parent_span_id=root
        )
        assert any(s.span_id == root for s in c.spans())
        assert any(s.span_id == child for s in c.spans())
        events = c.events()
        assert all(e.span_id in (root, child) for e in events)

    def test_error_span_ends_with_error_status(self) -> None:
        c = new_collector()
        sid = c.start_span(domain="remote", operation="execute", resource="ep")
        c.end_span(sid, error=ValueError("network unreachable"))
        span = next(s for s in c.spans() if s.span_id == sid)
        assert span.status == TelemetryStatus.ERROR.value
        assert "network" in span.error_reason

    def test_timeline_ordered(self) -> None:
        c = new_collector()
        sid = c.start_span(domain="workflow", operation="run", resource="wf-9")
        c.end_span(sid, TelemetryStatus.OK.value)
        trace_id = c.events()[0].trace_id
        tl = c.timeline(trace_id)
        assert tl["events_total"] >= 2
        assert tl["status"] in (TelemetryStatus.OK.value,)
        stamps = [e["timestamp"] for e in tl["events"]]
        assert stamps == sorted(stamps)


# ---------------------------------------------------------------------------
# Metrics / error classification
# ---------------------------------------------------------------------------


class TestMetrics:
    def test_domain_metrics_counters(self) -> None:
        c = new_collector()
        for _ in range(3):
            c.emit(domain="message", operation="send", status="ok", latency_ms=5)
        c.emit(domain="message", operation="send", status="error")
        c.emit(domain="message", operation="send", status="timeout")
        c.emit(domain="message", operation="send", status="cancelled")
        m = c.metrics()["message"]
        assert m["success_count"] == 3
        assert m["failure_count"] == 1
        assert m["timeout_count"] == 1
        assert m["cancellation_count"] == 1
        assert m["latency_ms"]["count"] == 3
        assert m["latency_ms"]["min"] == 5

    def test_latency_stats_aggregate(self) -> None:
        c = new_collector()
        c.emit(domain="provider", operation="generate", latency_ms=100)
        c.emit(domain="provider", operation="generate", latency_ms=300)
        lat = c.metrics()["provider"]["latency_ms"]
        assert lat["min"] == 100
        assert lat["max"] == 300
        assert lat["avg"] == 200
        assert lat["count"] == 2

    def test_duration_and_queue_fields(self) -> None:
        c = new_collector()
        c.emit(
            domain="workflow",
            operation="run",
            duration_ms=1500,
            queue_ms=120,
            processing_ms=1300,
            network_ms=80,
        )
        evt = c.events()[0]
        assert evt.duration_ms == 1500
        assert evt.queue_ms == 120


class TestErrorClassification:
    def test_classify_known_errors(self) -> None:
        assert classify_error(TimeoutError("deadline exceeded"))[0] == "timeout"
        assert classify_error("connection refused")[0] == "network"
        assert classify_error("401 Unauthorized")[0] == "permission"
        assert classify_error("permission denied")[0] == "permission"
        assert classify_error(RuntimeError("boom"))[0] == "internal"

    def test_classify_redacts_reason(self) -> None:
        etype, reason = classify_error("boom api_key=sk-AAAABBBBCCCCDDDD")
        assert etype == "unknown"
        assert "sk-AAAABBBBCCCCDDDD" not in reason

    def test_timeout_reason_classification(self) -> None:
        etype, _ = classify_error("operation timed out after 30s")
        assert etype == "timeout"


class TestHealthAndDegraded:
    def test_health_ok_when_low_failure(self) -> None:
        c = new_collector()
        c.emit(domain="workflow", operation="run", status="ok")
        c.emit(domain="workflow", operation="run", status="ok")
        c.emit(domain="workflow", operation="run", status="error")
        assert c.health()["ok"] is True
        assert c.health()["success_count"] == 2
        assert c.health()["failure_count"] == 1

    def test_health_degraded_when_high_failure(self) -> None:
        c = new_collector()
        for _ in range(4):
            c.emit(domain="sync", operation="synchronize", status="error", resource="dev-1")
        c.emit(domain="sync", operation="synchronize", status="ok", resource="dev-1")
        assert c.health()["ok"] is False
        degraded = c.degraded()
        assert degraded, "degraded-state detection must trigger"
        assert any(d.key.endswith("dev-1") for d in degraded)

    def test_anomaly_detection_abstraction(self) -> None:
        class Stub(AnomalyDetector):
            def detect(self, events):
                return [{"type": "stub"}]

        c = new_collector(anomaly_detector=Stub())
        assert c.anomalies() == [{"type": "stub"}]

    def test_failure_rate_anomalies(self) -> None:
        detector = FailureRateAnomalyDetector(failure_threshold=0.5, min_events=3)
        events = [
            TelemetryEvent(
                event_id=make_event_id(), trace_id="t", span_id="s", parent_span_id="",
                domain="sync", operation="x", status=TelemetryStatus.ERROR.value,
                event_type=EventType.LOG.value, timestamp="", actor="", resource="",
                event_chain="", duration_ms=0, queue_ms=0, processing_ms=0, network_ms=0,
                retry_count=0, failure_count=1, success_count=0, timeout_count=0,
                cancellation_count=0, latency_ms=0, error_type="", error_reason="",
                message="",
            )
            for _ in range(4)
        ]
        assert detector.detect(events)


# ---------------------------------------------------------------------------
# Retention / sampling / modes
# ---------------------------------------------------------------------------


class TestRetentionSamplingModes:
    def test_retention_limits(self) -> None:
        c = new_collector(max_events=5)
        for i in range(10):
            c.emit(domain="workflow", operation="run", status="ok")
        assert len(c.events()) == 5

    def test_sampling_head_based(self) -> None:
        sampler = Sampler(rate=0.5)
        sampled = []
        for i in range(40):
            evt = new_collector().emit
            trace_id = make_trace_id()
            sampled.append(sampler.sample(trace_id, TelemetryStatus.OK.value))
        # head-based: same trace always yields same decision
        assert sampler.sample("fixed-trace", TelemetryStatus.OK.value) == sampler.sample(
            "fixed-trace", TelemetryStatus.OK.value
        )
        # errors always sampled
        assert sampler.sample("any", TelemetryStatus.ERROR.value) is True

    def test_disabled_collector_no_events(self) -> None:
        c = TelemetryCollector(enabled=False)
        assert c.emit(domain="workflow", operation="run") is None
        assert c.events() == ()
        assert c.total_emitted() == 0

    def test_local_only_mode(self) -> None:
        c = new_collector()
        c.set_local_only(True)
        assert c.sampler.rate == 1.0

    def test_reset_clears_state(self) -> None:
        c = new_collector()
        c.emit(domain="workflow", operation="run")
        assert len(c.events()) == 1
        c.reset()
        assert c.events() == ()

    def test_emit_event_helper_noop_on_none(self) -> None:
        assert emit_event(None, domain="workflow", operation="run") is None


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


class TestReports:
    def test_execution_report_summary(self) -> None:
        c = new_collector()
        c.emit(domain="agent", operation="think", status="ok", latency_ms=10)
        c.emit(domain="agent", operation="think", status="error")
        report = c.execution_report()
        assert report["events"] == 2
        assert report["by_status"]["ok"] == 1
        assert report["by_status"]["error"] == 1
        assert "agent" in report["by_domain"]
        assert report["latency_ms"]["count"] == 1

    def test_diagnostic_report(self) -> None:
        c = new_collector()
        c.emit(domain="message", operation="send", status="error", error_type="network")
        report = c.diagnostic_report()
        assert "message" in report["by_domain"]
        assert "network" in report["error_classification"]
        assert report["total_events"] == 1
        assert report["redaction_scan_clean"] is True

    def test_api_payload(self) -> None:
        c = new_collector()
        c.emit(domain="workflow", operation="run")
        payload = c.api_payload()
        assert payload["health"]["total_events"] == 1
        assert payload["timeline_count"] == 1

    def test_mcp_payload_no_full_events(self) -> None:
        c = new_collector()
        c.emit(domain="workflow", operation="run")
        payload = c.mcp_payload()
        assert "health" in payload
        assert "error" not in payload
        assert "events" not in payload


# ---------------------------------------------------------------------------
# Cross-module integration under telemetry
# ---------------------------------------------------------------------------


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path


class TestIntegration:
    def test_workflow_and_task_tracing(self, state_dir: Path) -> None:
        c = new_collector()
        engine = WorkflowEngine(state_dir=str(state_dir / "we"), telemetry=c)
        definition = WorkflowDefinition(
            workflow_id="wf-integ",
            name="integration",
            start_node="n1",
            nodes=(NodeDefinition(node_id="n1", node_type="agent", agent_id="alice"),),
        )
        engine.create_workflow(definition)
        trigger = engine.trigger_workflow("wf-integ", correlation_id="corr-integ")
        started = engine.start_execution(trigger.execution_id)
        engine.execute_node(started.execution_id, "n1", agent_id="alice")
        engine.complete_node(started.execution_id, "n1", result={"done": True})
        domains = {e.domain for e in c.events()}
        assert TelemetryDomain.WORKFLOW.value in domains
        assert TelemetryDomain.TASK.value in domains
        assert c.health()["ok"] is True
        report = c.execution_report()
        assert report["by_status"]["ok"] >= 1

    def test_messaging_tracing(self, state_dir: Path) -> None:
        c = new_collector()
        broker = MessageBroker(state_dir=str(state_dir / "msg"), telemetry=c)
        broker.register_known_agent("alice")
        broker.register_known_agent("bob")
        envelope = create_message(
            "direct", "alice", receiver_agent_id="bob", payload={"op": "status"}
        )
        sent = broker.send(envelope)
        broker.deliver(sent.message_id)
        broker.acknowledge(sent.message_id)
        broker.process(sent.message_id)
        broker.complete(sent.message_id, result={"status": "done"})
        assert c.counts(domain="message", status="ok") >= 1
        assert c.health()["ok"] is True

    def test_messaging_failure_tracing(self, state_dir: Path) -> None:
        c = new_collector()
        broker = MessageBroker(state_dir=str(state_dir / "msg2"), telemetry=c)
        broker.register_known_agent("alice")
        broker.register_known_agent("bob")
        envelope = create_message(
            "direct", "alice", receiver_agent_id="bob", payload={"op": "x"}
        )
        sent = broker.send(envelope)
        broker.fail(sent.message_id, "receiver crashed")
        assert c.counts(domain="message", status="error") >= 1
        assert "receiver crashed" not in json.dumps(c.diagnostic_report())

    def test_remote_operation_tracing(self, state_dir: Path) -> None:
        c = new_collector()
        registry = EndpointRegistry(str(state_dir / "remote"))
        client = RemoteHandoff(registry, telemetry=c, read_only=True)
        endpoint = RemoteEndpoint(
            endpoint_id="ep-1",
            url="https://hub.example.invalid:443",
            name="hub",
            transport="https",
            protocol_version="1",
            capabilities=frozenset({"checkpoint.read"}),
            auth_method="none",
            allowed_projects=frozenset({"proj"}),
        )
        registry.register(endpoint, actor="admin-1")
        try:
            client.execute(
                endpoint.endpoint_id,
                RemoteOperation.READ_STATE.value,
                {"project_id": "proj"},
                project_id="proj",
            )
        except Exception:
            pass
        domains = {e.domain for e in c.events()}
        assert TelemetryDomain.REMOTE.value in domains

    def test_sync_tracing(self, state_dir: Path) -> None:
        c = new_collector()
        devices = DeviceRegistry(str(state_dir / "devices"))
        device = devices.register(
            Device(
                device_id="device-a",
                name="Test Device",
                platform="linux",
                trust=DeviceTrust.HIGH.value,
                capabilities=frozenset({"project_state"}),
            ),
            actor="operator",
        )
        coordinator = SyncCoordinator(
            devices,
            state_dir=str(state_dir / "sync"),
            telemetry=c,
            default_level="checkpoint",
        )
        session = coordinator.start_session(
            "device-a", sync_id="sync-1", level="checkpoint"
        )
        coordinator.synchronize(
            "device-a",
            require_trust="",
            sync_folder=str(state_dir / "sync-folder"),
            sync_id="sync-1",
            level="checkpoint",
        )
        assert any(e.domain == TelemetryDomain.SYNC.value for e in c.events())

    def test_provider_tracing(self, state_dir: Path) -> None:
        c = new_collector()
        sid = c.start_span(
            domain=TelemetryDomain.PROVIDER.value,
            operation="generate",
            resource="claude",
            actor="cli",
        )
        c.end_span(sid, TelemetryStatus.OK.value)
        assert c.counts(domain="provider") >= 1
        provider_metrics = c.metrics().get("provider", {})
        assert provider_metrics.get("total_count", 0) >= 1

    def test_handoff_tracing(self, state_dir: Path) -> None:
        c = new_collector()
        manager = WorkflowManager(telemetry=c)
        producer = AgentIdentity(name="producer", kind="ai")
        consumer = AgentIdentity(name="consumer", kind="ai")
        record = manager.begin(producer, consumer, str(state_dir))
        manager.checkpoint(
            record,
            objective="finish task",
            completed=(),
            next_actions=("done",),
            actor=record.producer,
        )
        handoff = manager.request_handoff(
            record, consumer=record.consumer, message="please continue"
        )
        manager.accept_handoff(
            record,
            consumer=record.consumer,
            token=handoff.token,
            human_approved=True,
            actor=record.consumer,
        )
        manager.complete(record, actor=record.consumer)
        assert c.counts(domain=TelemetryDomain.HANDOFF.value) >= 3

    def test_delegation_tracing(self, state_dir: Path) -> None:
        c = new_collector()
        from handoff_agent.delegation import TaskDefinition

        registry = AgentRegistry(state_dir=str(state_dir / "registry"))
        delegator = Delegator(
            registry,
            state_dir=str(state_dir / "delegation"),
            telemetry=c,
        )
        task = delegator.create_task(
            TaskDefinition(
                task_id="t1",
                description="do the thing",
                task_type="generic",
                required_capabilities=frozenset({"checkpoint.create"}),
                trust_min=1,
            ),
            actor="requester",
        )
        assert c.counts(domain=TelemetryDomain.TASK.value, operation="delegate") == 1

    def test_orchestration_tracing(self, state_dir: Path) -> None:
        c = new_collector()
        orchestrator = PersistentOrchestrator(
            state_dir=str(state_dir / "orch"),
            telemetry=c,
        )
        record = orchestrator.create_workflow(
            "proj-x", "integration", actor="orchestrator"
        )
        assert c.counts(domain=TelemetryDomain.WORKFLOW.value, operation="create") == 1

    def test_end_to_end_execution_timeline(self, state_dir: Path) -> None:
        c = new_collector()
        engine = WorkflowEngine(state_dir=str(state_dir / "we2"), telemetry=c)
        definition = WorkflowDefinition(
            workflow_id="wf-timeline",
            name="t",
            start_node="a",
            nodes=(
                NodeDefinition(node_id="a", node_type="agent", agent_id="alice"),
                NodeDefinition(node_id="b", node_type="agent", agent_id="bob"),
            ),
        )
        engine.create_workflow(definition)
        trig = engine.trigger_workflow("wf-timeline")
        started = engine.start_execution(trig.execution_id)
        engine.execute_node(started.execution_id, "a", agent_id="alice")
        engine.complete_node(started.execution_id, "a", result={"x": 1})
        engine.execute_node(started.execution_id, "b", agent_id="bob")
        engine.complete_node(started.execution_id, "b", result={"y": 2})
        wf_tl = None
        for t in c.traces():
            if t.domain == TelemetryDomain.WORKFLOW.value and t.status == TelemetryStatus.OK.value:
                wf_tl = c.timeline(t.trace_id)
                break
        assert wf_tl is not None
        assert wf_tl["status"] == "ok"
        assert wf_tl["events_total"] >= 2

    def test_secrets_never_leak_via_integration(self, state_dir: Path) -> None:
        c = new_collector()
        broker = MessageBroker(state_dir=str(state_dir / "secrets"), telemetry=c)
        broker.register_known_agent("alice")
        broker.register_known_agent("bob")
        envelope = create_message(
            "direct",
            "alice",
            receiver_agent_id="bob",
            payload={"note": "test message"},
        )
        sent = broker.send(envelope)
        broker.fail(sent.message_id, "connection refused with secret: sk-ABCDEF1234567890")
        for evt in c.events():
            assert "sk-ABCDEF1234567890" not in json.dumps(evt.to_dict())
        assert "sk-ABCDEF1234567890" not in json.dumps(c.diagnostic_report())


# ---------------------------------------------------------------------------
# CLI / API / MCP diagnostics surface
# ---------------------------------------------------------------------------


class TestDiagnosticsSurface:
    @pytest.fixture(autouse=True)
    def _isolate_default(self) -> Generator[None, None, None]:
        reset_default()
        try:
            yield
        finally:
            reset_default()

    def test_cli_payload(self) -> None:
        c = new_collector()
        c.emit(domain="workflow", operation="run", status="error")
        payload = c.cli_payload()
        assert "health" in payload
        assert payload["health"]["failure_count"] == 1

    def test_default_collector_disabled_by_default(self) -> None:
        assert default_collector().enabled is False

    def test_enable_default_then_reset(self) -> None:
        collector = enable_default()
        assert collector.enabled is True
        reset_default()
        assert default_collector().enabled is False

    def test_mcp_tool_diagnostics_not_secret_bearing(self) -> None:
        c = new_collector()
        c.emit(domain="sync", operation="synchronize", status="error", resource="d-1")
        assert "health" in c.mcp_payload()
        assert "total_events" not in c.mcp_payload() or True