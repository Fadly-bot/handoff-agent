"""Phase 31 — Audit Evidence Export (secret-free, deterministic).

Provides a structured, secret-free export of the full journey of work inside
Development Company F: from AI Council decision through Work Order creation,
assignment, checkpoint, handoff, quality gate, deployment gate, and release.

The export contains only data available via the public coordinator and
telemetry APIs. Every string is passed through the telemetry redaction
pipeline before serialization. The module writes to a single caller-provided
``Path`` and performs no arbitrary filesystem discovery.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TYPE_CHECKING

from handoff_agent.telemetry import is_sensitive_value, redact_text

if TYPE_CHECKING:
    from handoff_agent.company_f import CompanyFCoordinator
    from handoff_agent.telemetry import TelemetryCollector


def _scrub(obj: Any) -> Any:
    """Recursively redact string leaves through the telemetry redactor."""
    if isinstance(obj, dict):
        return {str(k): _scrub(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, bytes):
        try:
            return redact_text(obj.decode("utf-8", errors="replace"))
        except Exception:
            return "[redacted]"
    return obj


def build_company_trace(coordinator: CompanyFCoordinator) -> dict[str, Any]:
    """Build a secret-free dict representing the full company trace.

    The dict is ready for JSON serialization and contains:
    - project registry summary
    - ordered decision trail (GO / NO-GO with conflict flags)
    - ordered work order lifecycle (status, assignment, capabilities, gates)
    - checkpoint ownership evidence
    - approval boundary evidence
    - full audit evidence trail
    """
    with coordinator._lock:
        projects = {
            pid: {"name": p.name, "root": p.root}
            for pid, p in coordinator._projects.items()
        }
        decisions = [d.to_dict() for d in coordinator._decisions.values()]
        work_orders = [w.to_dict() for w in coordinator.list_work_orders()]
        plans = {wid: plan.to_dict() for wid, plan in coordinator._plans.items()}
        checkpoints = {cid: cp.to_dict() for cid, cp in coordinator._checkpoints.items()}
        quality_gates = [q.to_dict() for q in coordinator._quality_gates.values()]
        deployment_gates = [g.to_dict() for g in coordinator._deployment_gates.values()]
        approvals = {
            w.work_order_id: {
                "required": w.approval.required,
                "satisfied": w.approval.satisfied,
                "approved_by": w.approval.approved_by,
                "approved_at_ms": w.approval.approved_at_ms,
            }
            for w in coordinator._work_orders.values()
            if w.approval.required
        }
        audit_evidence = [e.to_dict() for e in coordinator.audit_trail()]

    return _scrub(
        {
            "schema": "company_f_audit_trace_v1",
            "projects": projects,
            "decisions": decisions,
            "work_orders": work_orders,
            "plans": plans,
            "checkpoints": checkpoints,
            "quality_gates": quality_gates,
            "deployment_gates": deployment_gates,
            "approvals": approvals,
            "audit_evidence": audit_evidence,
        }
    )


def export_audit_json(
    coordinator: CompanyFCoordinator,
    collector: TelemetryCollector | None,
    path: str | Path,
    *,
    pretty: bool = True,
) -> dict[str, Any]:
    """Write a fully-redacted audit export to ``path``.

    Returns a summary dict: ``{"exported": True, "bytes": N, "path": "..."}``.
    Raises ``ValueError`` if secret-like content is detected after redaction.
    The function writes only to the provided path and creates parent dirs if
    necessary.
    """
    from handoff_agent.persistence import _contains_secret_like_content

    trace = build_company_trace(coordinator)
    if collector is not None:
        trace["telemetry"] = collector.health()
        trace["timeline_count"] = len(collector.traces())
        trace["span_count"] = len(collector.spans())

    trace = _scrub(trace)

    serialized = json.dumps(trace, default=str, sort_keys=False, indent=2 if pretty else None)
    if _contains_secret_like_content(serialized):
        raise ValueError(
            "audit export still contains secret-like content after redaction — "
            "refusing to write"
        )

    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(serialized, encoding="utf-8")
    return {"exported": True, "bytes": len(serialized.encode("utf-8")), "path": str(target)}
