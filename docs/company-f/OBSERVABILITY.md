# Development Company F — Observability, Trace & Audit Evidence (Phase 31)

Status: **PASS**.

Implementation:
- Trace instrumentation integrated into `src/handoff_agent/company_f.py`
  (optional `tracer=` collector on the coordinator).
- Secret-free audit export: `src/handoff_agent/audit.py`.
- Telemetry extensions (domains + redaction parity + span pruning):
  `src/handoff_agent/telemetry.py`.
- Contract tests: `tests/test_phase31_observability.py` (10 tests).

## Trace model

- One **project trace** is started when a project is registered; everything
  that happens under that project (decision → work order → checkpoint →
  quality → deployment → release) is emitted on the same `trace_id`.
- Every event is a `TelemetryEvent` carrying `trace_id`, `span_id`,
  `parent_span_id`, `event_chain` (SHA-256 hash chain), domain, operation,
  status, duration/latency, actor, resource, error classification, and
  redacted metadata.
- Stages (domains): `project`, `decision`, `workorder`, `handoff`, `quality`,
  `deployment`. Operations: `project.register`, `decision.make`,
  `workorder.create/plan/assign/checkpoint/approve/release/failed/reject/
  cancel/resume`, `handoff.accept/reject`, `quality.gate`, `deployment.gate`.
- A `workorder.approve` event links the human approval boundary into the same
  trace as the Work Order and checkpoint/review that preceded it.

## Audit export

- `build_company_trace(coordinator)` → a JSON-ready dict representation of the
  full journey (projects, decisions, work orders, plans, checkpoints, quality
  gates, deployment gates, approvals, and the ordered immutable audit
  evidence).
- `export_audit_json(coordinator, collector, path)` → writes the fully
  redacted export to a single caller-provided path (creates parent dirs if
  missing) and refuses to write if secret-like content survives redaction.
- Every string leaf passes through the telemetry redactor; the export contains
  actors' role IDs and identifiers, never credentials or raw payload state.

## Secret safety

- Redaction parity is closed against `security.py`: telemetry now also scrubs
  bare `token =`, `private_key =`, and **quoted** `ghp_...`, `sk-...`,
  `xox...` values. Verified by `test_redaction_parity_with_security_patterns`.
- `submit_checkpoint` already rejects payloads whose serialization looks like a
  secret; the tracked checkpoint `state` is never re-emitted in event metadata.
- Disabled mode: with `enabled=False` the collector no-ops; the coordinator
  remains fully functional (verified by `test_disabled_collector_no_events`).
- Local-only mode is the default; sampling keeps all events when local-only.

## Metrics and health

- Domain metrics: success/failure/retry/timeout/cancellation counters plus
  latency aggregates (min/avg/max). Verified by `test_retry_timeout_failure_
  cancellation_metrics`.
- Degraded-state detection at the resource level (threshold 0.6 failure rate,
  ≥3 events). Verified by `test_degraded_state_detected`.
- Health report: ok/failure_rate/degraded/anomalies/sampling/enabled flags.
- Structural diagnostic report and timeline per trace remain available.

## Retention

- `RetentionPolicy(max_traces=…)` evicts oldest traces **and their spans**
  (previously spans leaked after trace eviction). Verified by
  `test_pruned_trace_spans_are_removed`.

## Conformance evidence

`tests/test_phase31_observability.py` covers the Phase 31 checklist: end-to-end
trace from decision to deployment gate, trace linking of work order/checkpoint/
review/approval, retry/timeout/failure/cancellation metrics, degraded-state
detection, secret-free log/report/JSON/telemetry, and telemetry disabled mode.