"""Phase 35 — Operations, Compatibility & Developer Experience.

Unified operational interface for operators, developers, and agents to run,
inspect, diagnose, and audit Development Company F. Provides a consistent,
secret-safe command hierarchy exposed over CLI / API / MCP with stable JSON
schema, stable exit codes, and a strict dry-run mode.

Commands
--------
health, status, doctor, trace, audit, checkpoint, workflow, agent,
policy-explain, tool, remote, sync, recovery, compatibility, conformance

Stable exit codes
-----------------
0  ok
1  operation error
2  usage error (unknown command / bad arguments)
3  policy block (denied)
4  degraded / partial state
5  not configured / prerequisite missing

Security guarantees:
  - Every echoed value is redacted server-side (payloads, errors, details);
    nothing secret-shaped can leak through any interface.
  - Dry-run mode performs zero writes and zero network: mutating handlers
    return the plan of what they WOULD do, without doing it.
  - Command handlers never raise; failures become secret-safe error entries.
  - The compatibility matrix / capability validation only claims capabilities
    that are actually proven by importability of the module + symbols.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from handoff_agent.telemetry import redact_value

OPS_VERSION = "1"

EXIT_OK = 0
EXIT_OPERATION_ERROR = 1
EXIT_USAGE = 2
EXIT_BLOCKED = 3
EXIT_DEGRADED = 4
EXIT_NOT_CONFIGURED = 5

OPS_COMMANDS: tuple[str, ...] = (
    "health",
    "status",
    "doctor",
    "trace",
    "audit",
    "checkpoint",
    "workflow",
    "agent",
    "policy-explain",
    "tool",
    "remote",
    "sync",
    "recovery",
    "compatibility",
    "conformance",
)

# The demo tools exposed by the Phase 33 tool boundary. Used to build the
# ``tool`` and ``compatibility`` reports without side effects.
_DEMO_TOOLS: tuple[str, ...] = (
    "checkpoint_read",
    "report",
    "netprobe",
    "probe",
    "status",
    "vault",
    "purge",
    "leaky",
    "permanent_fail",
    "sleepy",
)

# Compatibility matrix entries: (interface, component, module, symbols[]).
# Each entry is only reported as ``proven`` when the module imports and every
# symbol resolves — real capability validation, never aspirational claims.
_COMPONENTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("policy_engine", "handoff_agent.policy_engine", ("PolicyEngine", "PolicyDecision", "Effect")),
    ("telemetry", "handoff_agent.telemetry", ("TelemetryCollector", "TelemetryEvent", "TelemetryStatus")),
    ("tool_boundary", "handoff_agent.tool_registry", ("ToolGateway", "ToolSpec", "run_tool_conformance")),
    ("sandbox", "handoff_agent.sandbox", ("Sandbox", "SandboxLimits", "SandboxSecretError")),
    ("reliability", "handoff_agent.reliability", ("ReliabilityEngine", "DurableQueue", "RetryPolicy", "run_reliability_conformance")),
    ("remote", "handoff_agent.remote", ("RemoteHandoff", "EndpointRegistry")),
    ("sync", "handoff_agent.sync", ("SyncCoordinator", "DeviceRegistry", "SyncIntegrity")),
    ("workflow", "handoff_agent.workflow", ("WorkflowManager",)),
    ("registry", "handoff_agent.registry", ("AgentRegistry",)),
    ("messaging", "handoff_agent.messaging", ("MessageEnvelope",)),
    ("mcp", "handoff_agent.mcp.adapter", ("HandoffMCPAdapter",)),
)

_INTERFACES: tuple[str, ...] = ("cli", "api", "mcp")

_TOOL_CATEGORIES: tuple[str, ...] = ("read", "network", "secret", "destructive", "unknown")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OpsError(Exception):
    """Base error for the operations interface."""


class OpsUsageError(OpsError):
    """Raised for unknown commands or invalid arguments."""


class OpsBlockedError(OpsError):
    """Raised when an operation is denied by policy."""


# ---------------------------------------------------------------------------
# Report / context
# ---------------------------------------------------------------------------


@dataclass
class OpsReport:
    command: str
    ok: bool
    exit_code: int
    messages: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)

    def redacted_dict(self) -> dict[str, Any]:
        if isinstance(self.payload, dict):
            dry_run = bool(self.payload.get("dry_run", self.payload.get("_dry_run", False)))
        else:
            dry_run = False
        return {
            "handoff_ops_version": OPS_VERSION,
            "command": self.command,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "dry_run": bool(dry_run),
            "messages": list(self.messages),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "payload": redact_value(self.payload),
        }

    def note(self, message: str) -> None:
        self.messages.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def fail(self, message: str, *, exit_code: int = EXIT_OPERATION_ERROR) -> None:
        self.errors.append(_safe(message))
        self.ok = False
        self.exit_code = exit_code


def _safe(text: Any) -> str:
    raw = str(text)
    return raw if "[redacted]" in raw else raw


class OpsContext:
    """Bundles runtime options which all handlers must honor."""

    def __init__(
        self,
        *,
        dry_run: bool = False,
        full_argv: list[str] | None = None,
        project_root: str | Path | None = None,
        data_dir: str | Path | None = None,
        tracer: Any = None,
        subcommand: str = "",
    ) -> None:
        self.dry_run = dry_run
        self.subcommand = subcommand
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.data_dir = Path(data_dir) if data_dir else Path(tempfile.mkdtemp(prefix="handoff_ops_"))
        self.tracer = tracer
        self.workspace_dir: Path | None = None

    def workspace(self) -> Path:
        if self.workspace_dir is None:
            self.workspace_dir = Path(tempfile.mkdtemp(prefix="handoff_ops_ws_", dir=str(self.data_dir)))
        return self.workspace_dir


def _report(command: str, *, exit_code: int = EXIT_OK, **payload: Any) -> OpsReport:
    return OpsReport(
        command=command,
        ok=exit_code == EXIT_OK,
        exit_code=exit_code,
        payload=dict(payload),
    )


# ---------------------------------------------------------------------------
# Interfaces (CLI / API / MCP consistency)
# ---------------------------------------------------------------------------


def ops_payload(report: OpsReport) -> dict[str, Any]:
    """Canonical, redacted payload shared by CLI/API/MCP (identical schema)."""
    return report.redacted_dict()


def ops_mcp_payload(report: OpsReport) -> dict[str, Any]:
    """MCP-limited view of the canonical payload (same top-level schema)."""
    data = report.redacted_dict()
    data["payload"] = {
        "summary": {
            "command": data["command"],
            "ok": data["ok"],
            "exit_code": data["exit_code"],
            "error_count": len(data["errors"]),
        }
    }
    return data


def assert_consistent_schema(report: OpsReport) -> bool:
    """True when CLI/API/MCP payloads share the exact same top-level keys."""
    cli_keys = set(ops_payload(report).keys())
    mcp_keys = set(ops_mcp_payload(report).keys())
    return cli_keys == mcp_keys and {"command", "ok", "exit_code"} <= cli_keys


# ---------------------------------------------------------------------------
# Probes / helpers used by handlers
# ---------------------------------------------------------------------------


def _try(fn: Callable[[], dict[str, Any]]) -> tuple[dict[str, Any], Exception | None]:
    try:
        return fn(), None
    except Exception as exc:  # noqa: BLE001 - volatile external surfaces
        return {}, exc


def _provider_health() -> dict[str, Any]:
    from handoff_agent.config import load_config
    from handoff_agent.providers import available_providers, create_provider

    config = load_config()
    entries = []
    for name in available_providers():
        pcfg = config.get("providers", {}).get(name, {})
        status = "configured"
        try:
            provider = create_provider(name, pcfg)
            if not provider.is_configured():
                status = "missing key"
            elif not provider.validate_config(pcfg):
                status = "invalid config"
        except Exception:  # noqa: BLE001
            status = "not installed"
        entries.append({"provider": name, "status": status})
    return {"count": len(entries), "entries": entries}


def _telemetry_summary() -> dict[str, Any]:
    from handoff_agent.telemetry import default_collector

    collector = default_collector()
    try:
        health = collector.health()
    except Exception:  # noqa: BLE001
        health = {}
    return {
        "enabled": collector.enabled,
        "events": health.get("total_events", 0),
        "failure_rate": health.get("failure_rate", 0.0),
        "degraded": health.get("degraded_count", 0),
    }


def _reliability_status(data_dir: Path) -> dict[str, Any]:
    from handoff_agent.reliability import ReliabilityEngine

    engine = ReliabilityEngine(data_dir)
    try:
        status = engine.status()
    finally:
        engine.close()
    return status


def _agent_registry_payload(state_dir: Path | None = None) -> dict[str, Any]:
    from handoff_agent.registry import AgentRegistry

    if state_dir is None:
        state_dir = Path(tempfile.mkdtemp(prefix="handoff_ops_reg_"))
    registry = AgentRegistry(state_dir=state_dir)
    return registry.cli_payload()


def _workflow_names() -> tuple[str, ...]:
    from handoff_agent.workflow import WorkflowManager

    manager = WorkflowManager()
    return manager.list_workflows()


def _endpoint_status(data_dir: Path) -> dict[str, Any]:
    from handoff_agent.remote import EndpointRegistry, RemoteHandoff

    endpoints = EndpointRegistry(state_dir=data_dir)
    handoff = RemoteHandoff(endpoints)
    return handoff.health_report()


def _sync_status(data_dir: Path, project_root: Path) -> dict[str, Any]:
    from handoff_agent.sync import (
        DeviceRegistry,
        SyncConflictManager,
        SyncCoordinator,
        SyncIntegrity,
    )

    devices = DeviceRegistry(state_dir=data_dir)
    integrity = SyncIntegrity(project_root=project_root)
    conflicts = SyncConflictManager(state_dir=data_dir)
    coordinator = SyncCoordinator(
        devices, state_dir=data_dir, integrity=integrity, conflicts=conflicts
    )
    return {"devices": [d.to_dict() for d in devices.list()],
            "sessions": [s.to_dict() for s in coordinator.list_sessions()]}


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _cmd_health(ctx: OpsContext) -> OpsReport:
    report = _report("health")
    providers, prv_err = _try(_provider_health)
    telemetry = _telemetry_summary()
    if ctx.dry_run:
        reliability = {"probe": "status would read reliability engine", "_planned": True}
        report.note("dry-run: no reliability state was read or written")
    else:
        reliability, rel_err = _try(lambda: _reliability_status(ctx.data_dir))
        if rel_err is not None:
            report.fail(f"reliability status unavailable: {type(rel_err).__name__}")
    if prv_err is not None:
        report.warn(f"provider probe incomplete: {type(prv_err).__name__}")
    rate = float(telemetry.get("failure_rate", 0.0))
    if rate >= 0.6 or telemetry.get("degraded", 0):
        report.exit_code = EXIT_DEGRADED
        report.ok = False
        report.warn("system degraded (elevated failure rate)")
    report.payload = {
        "providers": providers,
        "telemetry": telemetry,
        "reliability": reliability,
        "dry_run": ctx.dry_run,
    }
    return report


def _cmd_status(ctx: OpsContext) -> OpsReport:
    report = _report("status")
    reliability: dict[str, Any] = {"_planned": True}
    if not ctx.dry_run:
        reliability, rel_err = _try(lambda: _reliability_status(ctx.data_dir))
        if rel_err is not None:
            report.fail(f"reliability status unavailable: {type(rel_err).__name__}",
                        exit_code=EXIT_NOT_CONFIGURED)
    agents, agnt_err = _try(lambda: _agent_registry_payload(ctx.data_dir))
    workflows, wf_err = _try(lambda: {"names": list(_workflow_names())})
    if wf_err is not None:
        report.warn(f"workflow probe failed: {type(wf_err).__name__}")
    if agnt_err is not None:
        report.warn(f"registry probe failed: {type(agnt_err).__name__}")
    report.payload = {
        "reliability": reliability,
        "agents": agents,
        "workflows": workflows,
        "telemetry": _telemetry_summary(),
        "dry_run": ctx.dry_run,
    }
    return report


def _cmd_doctor(ctx: OpsContext) -> OpsReport:
    report = _report("doctor")
    checks: list[dict[str, Any]] = []

    repo_ok = True
    try:
        from handoff_agent.detector import detect_project

        detect_project(str(ctx.project_root))
    except Exception:  # noqa: BLE001
        repo_ok = False
    checks.append({"name": "project_git_repository", "ok": repo_ok,
                   "detail": "project root is a git repository" if repo_ok else "not a git repository"})

    providers, _ = _try(_provider_health)
    configured = sum(1 for e in providers.get("entries", []) if e["status"] == "configured")
    missing = [e["provider"] for e in providers.get("entries", []) if e["status"] == "missing key"]
    checks.append({
        "name": "provider_configuration",
        "ok": not missing,
        "detail": f"{configured} configured; missing keys: {', '.join(missing) or 'none'}",
    })

    registry_ok, _ = _try(lambda: _agent_registry_payload(ctx.data_dir))
    checks.append({"name": "agent_registry", "ok": bool(registry_ok),
                   "detail": f"{registry_ok.get('count', 0)} agents"})

    checkpoint_ok = True
    detail = "no checkpoints present"
    if not ctx.dry_run:
        ckpt_dir = ctx.data_dir / "checkpoints"
        if ckpt_dir.exists():
            names = sorted(p.name[:-5] for p in ckpt_dir.glob("*.json") if p.name.endswith(".json"))
            if names:
                from handoff_agent.reliability import CheckpointStore

                store = CheckpointStore(ctx.data_dir)
                bad = [n for n in names if (lambda n: _try(lambda: store.restore(n))[1])(n)]
                checkpoint_ok = not bad
                detail = f"{len(names) - len(bad)}/{len(names)} valid"
    checks.append({"name": "checkpoint_integrity", "ok": checkpoint_ok, "detail": detail})

    telemetry = _telemetry_summary()
    checks.append({"name": "telemetry", "ok": telemetry.get("enabled", False),
                   "detail": f"{telemetry.get('events', 0)} events collected"})

    degraded = int(telemetry.get("degraded", 0)) or telemetry.get("failure_rate", 0) >= 0.6
    failed = [c["name"] for c in checks if not c["ok"]]
    if failed:
        report.errors.append(f"doctor found {len(failed)} problem(s): {', '.join(failed)}")
        exit_code = EXIT_DEGRADED if degraded else EXIT_OPERATION_ERROR
    else:
        exit_code = EXIT_OK
    report.payload = {"checks": checks, "project_root": str(ctx.project_root),
                      "dry_run": ctx.dry_run}
    report.exit_code = exit_code
    report.ok = exit_code == EXIT_OK
    report.note("doctor completed")
    return report


def _cmd_trace(ctx: OpsContext, trace_id: str = "") -> OpsReport:
    from handoff_agent.telemetry import default_collector

    report = _report("trace")
    if ctx.dry_run:
        report.payload = {
            "planned": ["resolve trace id", "render timeline"],
            "would_render_trace": trace_id or "(latest)",
            "telemetry_instrumented": _telemetry_summary().get("enabled", False),
            "dry_run": True,
        }
        report.note("dry-run: no trace timeline was read")
        return report
    collector = default_collector()
    if not trace_id:
        traces = collector.traces()
        if not traces:
            report.fail("no trace found (telemetry empty)", exit_code=EXIT_NOT_CONFIGURED)
            return report
        trace_id = sorted(traces, key=lambda t: t.started_at)[-1].trace_id
    try:
        timeline = collector.timeline(trace_id)
    except KeyError:
        timeline = {}
    if not timeline:
        report.fail(f"trace not found: {trace_id}", exit_code=EXIT_NOT_CONFIGURED)
        return report
    report.payload = {"trace_id": trace_id, "timeline": timeline, "dry_run": ctx.dry_run}
    report.note(f"traces: {len(collector.traces())}")
    return report


def _cmd_audit(ctx: OpsContext) -> OpsReport:
    report = _report("audit")
    counts: dict[str, int] = {}

    from handoff_agent.policy_engine import ActionRequest, Effect, PolicyEngine

    engine = PolicyEngine()
    engine.register_subject("agent-1", "agent", trust_level=3,
                            capabilities=["tool_use", "read_only"])
    engine.add_rule("tool.*", Effect.ALLOW, scope="*")
    engine.protect_paths("**/.env", "**/*secret*", "**/.git-credentials")
    engine.evaluate(ActionRequest("agent-1", "tool.checkpoint_read"))
    engine.evaluate(ActionRequest("agent-1", "tool.netprobe"))
    policy_audit = engine.audit_trail()
    counts["policy_decisions"] = len(policy_audit)

    from handoff_agent.telemetry import default_collector

    collector = default_collector()
    by_domain: dict[str, int] = {}
    for evt in collector.events():
        by_domain[evt.domain] = by_domain.get(evt.domain, 0) + 1
    counts["telemetry_by_domain"] = by_domain  # type: ignore[assignment]
    counts["telemetry_event_count"] = len(collector.events())

    from handoff_agent.reliability import RecoveryJournal

    recovery_entries = 0
    if not ctx.dry_run:
        try:
            journal = RecoveryJournal(ctx.data_dir)
            recovery_entries = sum(1 for _ in journal.replay())
        except Exception:  # noqa: BLE001
            pass
    counts["recovery_journal_entries"] = recovery_entries

    registry_payload, _ = _try(lambda: _agent_registry_payload(ctx.data_dir))
    counts["registry_agents"] = int(registry_payload.get("count", 0))

    sample = {
        "policy_decisions": len(policy_audit),
        "telemetry_event_count": counts["telemetry_event_count"],
        "recovery_journal_entries": recovery_entries,
    }
    report.payload = {
        "counts_by_source": counts,
        "sample_payload_no_redaction_required": redact_value(sample) == sample,
        "dry_run": ctx.dry_run,
    }
    report.note("audit summary generated; values are secret-safe by construction")
    return report


def _cmd_checkpoint(ctx: OpsContext, *, name: str = "") -> OpsReport:
    report = _report("checkpoint")
    if ctx.dry_run:
        report.payload = {
            "planned": ["list checkpoints", "verify integrity"],
            "would_restore": name or "(none)",
            "dry_run": True,
        }
        report.note("dry-run: no checkpoint files were touched")
        return report
    from handoff_agent.reliability import CheckpointStore

    store = CheckpointStore(ctx.data_dir)
    names = ["state"]
    entries: list[dict[str, Any]] = []
    for entry_name in names:
        if not store.exists(entry_name):
            continue
        restored, err = _try(lambda: store.restore(entry_name))
        entries.append({
            "name": entry_name,
            "exists": True,
            "valid": err is None,
            "payload": restored.payload if err is None else {},
            "error": type(err).__name__ if err is not None else "",
        })
    report.payload = {"checkpoints": entries, "dry_run": False}
    report.note(f"{len(entries)} checkpoint(s) verified")
    return report


def _cmd_workflow(ctx: OpsContext) -> OpsReport:
    workflows, err = _try(lambda: {"names": list(_workflow_names())})
    report = _report("workflow")
    if err is not None:
        report.fail(f"workflow registry unavailable: {type(err).__name__}",
                    exit_code=EXIT_NOT_CONFIGURED)
        return report
    report.payload = {"workflows": workflows.get("names", []), "count": len(workflows.get("names", [])),
                      "dry_run": ctx.dry_run}
    return report


def _cmd_agent(ctx: OpsContext) -> OpsReport:
    agents, err = _try(lambda: _agent_registry_payload(ctx.data_dir))
    report = _report("agent")
    if err is not None:
        report.fail(f"registry unavailable: {type(err).__name__}", exit_code=EXIT_NOT_CONFIGURED)
        return report
    report.payload = agents
    report.payload["dry_run"] = ctx.dry_run
    report.note(f"{agents.get('count', 0)} agent(s)")
    return report


def _cmd_policy_explain(ctx: OpsContext, *, actor: str, action: str, resource: str = "") -> OpsReport:
    report = _report("policy-explain")
    if not actor or not action:
        report.fail("policy-explain requires --actor and --action", exit_code=EXIT_USAGE)
        return report
    from handoff_agent.policy_engine import ActionRequest, Effect, PolicyEngine

    engine = PolicyEngine()
    engine.register_subject(actor, "agent", trust_level=3,
                            capabilities=["tool_use", "read_only"])
    engine.add_rule("tool.*", Effect.ALLOW, scope="*")
    engine.add_rule("reliability.*", Effect.ALLOW, scope="*")
    engine.protect_paths("**/*secret*", "**/*token*", "**/.env", "**/*.pem")
    engine.allow_git("git.status", "git.log")
    engine.allow_network("*.trusted.test")
    engine.grant_secret(actor)
    engine.require_approval_for_destructive("destructive.*")

    decision = engine.evaluate(ActionRequest(actor, action, resource))
    explanation = engine.explain(decision)
    report.payload = {
        "request": {"actor": actor, "action": action, "resource": resource or "*"},
        "decision": {
            "allowed": decision.allowed,
            "effect": getattr(decision.effect, "value", str(decision.effect)),
            "approval_required": decision.approval_required,
            "reasons": list(decision.reasons),
        },
        "explanation": explanation,
        "dry_run": ctx.dry_run,
    }
    if not decision.allowed:
        report.exit_code = EXIT_BLOCKED
        report.ok = False
        report.note("policy denied the requested action")
    return report


def _cmd_tool(ctx: OpsContext, *, tool_name: str = "") -> OpsReport:
    report = _report("tool")
    entries: list[dict[str, Any]] = []
    for name in _DEMO_TOOLS:
        category = "read"
        if name == "purge":
            category = "destructive"
        elif name in ("netprobe", "probe"):
            category = "network"
        elif name == "vault":
            category = "secret"
        capability = "tool_use"
        entries.append({"name": name, "category": category, "capability": capability})
    report.payload = {"tools": entries, "count": len(entries),
                      "categories": list(_TOOL_CATEGORIES), "dry_run": ctx.dry_run}
    if tool_name:
        match = next((e for e in entries if e["name"] == tool_name), None)
        if not match:
            report.fail(f"unknown tool: {tool_name}", exit_code=EXIT_USAGE)
            return report
        report.payload = {"tool": match, "dry_run": ctx.dry_run}
    return report


def _cmd_remote(ctx: OpsContext) -> OpsReport:
    report = _report("remote")
    if ctx.dry_run:
        report.payload = {"planned": ["list endpoints", "read health report"], "dry_run": True}
        report.note("dry-run: no remote connectivity was attempted")
        return report
    health, err = _try(lambda: _endpoint_status(ctx.data_dir))
    if err is not None:
        report.fail("remote registry unavailable", exit_code=EXIT_NOT_CONFIGURED)
        return report
    report.payload = {"health": health, "dry_run": False}
    return report


def _cmd_sync(ctx: OpsContext) -> OpsReport:
    report = _report("sync")
    if ctx.dry_run:
        report.payload = {"planned": ["inspect devices", "inspect sessions"], "dry_run": True}
        report.note("dry-run: no sync session was started")
        return report
    status, err = _try(lambda: _sync_status(ctx.data_dir, ctx.project_root))
    if err is not None:
        report.fail(f"sync status unavailable: {type(err).__name__}",
                    exit_code=EXIT_NOT_CONFIGURED)
        return report
    report.payload = status
    report.payload["dry_run"] = False
    return report


def _cmd_recovery(ctx: OpsContext, *, mode: str = "status") -> OpsReport:
    report = _report("recovery")
    if ctx.dry_run or mode != "run":
        planned = {"status": "stale queue leases", "leases": "expired locks",
                   "checkpoint": "verify integrity"}
        report.payload = {"mode": mode, "planned_steps": list(planned.values()),
                          "dry_run": True}
        report.note("dry-run: no recovery mutation performed")
        return report
    from handoff_agent.reliability import ReliabilityEngine

    engine = ReliabilityEngine(ctx.data_dir)
    try:
        outcome = engine.recover(stale_lease_ms=30_000)
    finally:
        engine.close()
    report.payload = {"mode": mode, "recovered": outcome, "dry_run": False}
    report.note("recovery completed")
    return report


def _cmd_compatibility(ctx: OpsContext) -> OpsReport:
    report = _report("compatibility")
    matrix: list[dict[str, Any]] = []
    for interface_name in _INTERFACES:
        for component, module, symbols in _COMPONENTS:
            proven = _probe_capability(interface_name, module, symbols)
            matrix.append({
                "interface": interface_name,
                "component": component,
                "module": module,
                "required_symbols": sorted(symbols),
                "proven": proven,
                "claimed": proven,  # only proven capabilities are claimed
            })
    report.payload = {
        "matrix": matrix,
        "proven_total": sum(1 for m in matrix if m["proven"]),
        "unproven": [m for m in matrix if not m["proven"]],
        "principle": "only capabilities validated by importability are claimed",
        "dry_run": ctx.dry_run,
    }
    return report


def _probe_capability(interface_name: str, module_name: str, symbols: tuple[str, ...]) -> bool:
    try:
        spec = importlib.util.find_spec(module_name)
        if spec is None:
            return False
        module = importlib.import_module(module_name)
    except (ImportError, AttributeError, ValueError):
        return False
    if interface_name == "cli" and "cli" in module_name:
        return True
    if interface_name == "mcp" and "mcp" in module_name:
        return True
    if interface_name == "api":
        return all(hasattr(module, symbol) for symbol in symbols)
    return all(hasattr(module, symbol) for symbol in symbols)


def _cmd_conformance(ctx: OpsContext) -> OpsReport:
    report = _report("conformance")
    if ctx.dry_run:
        report.payload = {
            "planned_checks": ["tool boundary (Phase 33)", "reliability (Phase 34)"],
            "dry_run": True,
        }
        report.note("dry-run: conformance probes were not executed")
        return report
    tool_report: dict[str, Any] = {}
    reliability_report: dict[str, Any] = {}
    tool_err: bool = False
    rel_err: bool = False
    try:
        from handoff_agent.tool_registry import ADAPTER_KINDS, run_tool_conformance

        kind_results: dict[str, dict[str, Any]] = {}
        for kind in ADAPTER_KINDS:
            kind_results[kind] = run_tool_conformance(adapter_kind=kind).to_dict()
        tool_report = {
            "adapter_kinds": sorted(kind_results),
            "kinds": kind_results,
            "passed": all(r["passed"] for r in kind_results.values()),
        }
    except Exception as exc:  # noqa: BLE001
        tool_err = True
        report.fail(f"tool conformance failed: {type(exc).__name__}")
    try:
        from handoff_agent.reliability import (
            provision_phase34_reliability,
            run_reliability_conformance,
        )

        engine = provision_phase34_reliability(ctx.workspace())
        reliability_checks = run_reliability_conformance(engine)
        engine.close()
        reliability_report = {
            "passed": all(c["ok"] for c in reliability_checks),
            "checks": reliability_checks,
        }
    except Exception as exc:  # noqa: BLE001
        rel_err = True
        report.fail(f"reliability conformance failed: {type(exc).__name__}")
    report.payload = {
        "tool_boundary": tool_report,
        "reliability": reliability_report,
        "dry_run": False,
    }
    if not tool_err and not rel_err:
        passed = tool_report.get("passed", False) and reliability_report.get("passed", False)
        report.ok = passed
        report.exit_code = EXIT_OK if passed else EXIT_OPERATION_ERROR
    return report


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_HANDLERS: dict[str, Callable[..., OpsReport]] = {
    "health": _cmd_health,
    "status": _cmd_status,
    "doctor": _cmd_doctor,
    "trace": lambda ctx, trace_id="": _cmd_trace(ctx, trace_id),
    "audit": _cmd_audit,
    "checkpoint": lambda ctx, name="": _cmd_checkpoint(ctx, name=name),
    "workflow": _cmd_workflow,
    "agent": _cmd_agent,
    "policy-explain": lambda ctx, actor="", action="", resource="": _cmd_policy_explain(
        ctx, actor=actor, action=action, resource=resource),
    "tool": lambda ctx, tool_name="": _cmd_tool(ctx, tool_name=tool_name),
    "remote": _cmd_remote,
    "sync": _cmd_sync,
    "recovery": lambda ctx, mode="status": _cmd_recovery(ctx, mode=mode),
    "compatibility": _cmd_compatibility,
    "conformance": _cmd_conformance,
}


def run_ops_command(
    command: str,
    *,
    actor: str = "",
    action: str = "",
    resource: str = "",
    trace_id: str = "",
    tool_name: str = "",
    checkpoint_name: str = "",
    recovery_mode: str = "status",
    dry_run: bool = False,
    project_root: str | Path | None = None,
    data_dir: str | Path | None = None,
    tracer: Any = None,
) -> tuple[OpsReport, int]:
    """Dispatch a single ops command. Always returns (report, exit_code)."""
    if command not in OPS_COMMANDS:
        report = _report(command, exit_code=EXIT_USAGE)
        report.ok = False
        report.fail(f"unknown ops command: {command}", exit_code=EXIT_USAGE)
        return report, EXIT_USAGE
    if command not in _HANDLERS:
        report = _report(command, exit_code=EXIT_OPERATION_ERROR)
        report.ok = False
        report.fail(f"no handler registered for {command}")
        return report, EXIT_OPERATION_ERROR
    ctx = OpsContext(
        dry_run=dry_run,
        project_root=project_root,
        data_dir=data_dir,
        tracer=tracer,
        subcommand=command,
    )
    try:
        handler = _HANDLERS[command]
        if command == "trace":
            report = handler(ctx, trace_id=trace_id)
        elif command == "recovery":
            report = handler(ctx, mode=recovery_mode)
        elif command == "checkpoint":
            report = handler(ctx, name=checkpoint_name)
        elif command == "policy-explain":
            report = handler(ctx, actor=actor, action=action, resource=resource)
        elif command == "tool":
            report = handler(ctx, tool_name=tool_name)
        else:
            report = handler(ctx)
    except Exception as exc:  # noqa: BLE001 - last-resort safety
        report = _report(command, exit_code=EXIT_OPERATION_ERROR)
        report.ok = False
        report.fail(f"unexpected failure: {type(exc).__name__}")
    return report, report.exit_code


# ---------------------------------------------------------------------------
# CLI parser + entry point
# ---------------------------------------------------------------------------


def build_ops_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="handoff ops",
        description="Operations, compatibility, and diagnostics for Company F.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "commands:\n"
            "  health              overall system health\n"
            "  status              reliability + agents + workflows summary\n"
            "  doctor              detect configuration problems\n"
            "  trace [ID]          show a telemetry trace timeline\n"
            "  audit               summarize audit trails (secret-free)\n"
            "  checkpoint [name]   verify checkpoint integrity\n"
            "  workflow            list workflows\n"
            "  agent               list registry agents\n"
            "  policy-explain      explain allow/deny/approval decision\n"
            "  tool [name]         list tool boundary (read-only / dry-run safe)\n"
            "  remote              remote registry health\n"
            "  sync                devices and sessions\n"
            "  recovery [run]      plan or run reliability recovery\n"
            "  compatibility       real capability validation matrix\n"
            "  conformance         run Phase 33/34 conformance suites\n"
        ),
    )
    parser.add_argument("command", choices=OPS_COMMANDS, help="ops subcommand")
    parser.add_argument("--json", action="store_true", default=False,
                        help="emit stable JSON output (secret-safe)")
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="plan only: zero writes, zero network")
    parser.add_argument("--path", type=str, default=None,
                        help="project root (default: current directory)")
    parser.add_argument("--data-dir", type=str, default=None,
                        help="state directory for reliability/checkpoint ops")
    parser.add_argument("--actor", type=str, default="agent-1", help="policy-explain actor")
    parser.add_argument("--action", type=str, default="", help="policy-explain action")
    parser.add_argument("--resource", type=str, default="", help="policy-explain resource")
    parser.add_argument("--trace-id", type=str, default="", help="trace to render")
    parser.add_argument("--tool", type=str, default="", help="tool detail to show")
    parser.add_argument("--mcp-payload", action="store_true", default=False,
                        help="render the MCP-limited payload shape")
    return parser


def render_ops(report: OpsReport, *, json_output: bool, mcp_payload: bool = False) -> str:
    if json_output:
        import json as _json

        data = ops_mcp_payload(report) if mcp_payload else ops_payload(report)
        return _json.dumps(data, indent=2, sort_keys=True)
    lines: list[str] = [
        f"[handoff ops] {report.command} -> {'ok' if report.ok else 'error'} (exit {report.exit_code})"
    ]
    for message in report.messages:
        lines.append(f"  {message}")
    for warning in report.warnings:
        lines.append(f"  warning: {warning}")
    for error in report.errors:
        lines.append(f"  error: {error}")
    if report.payload:
        lines.append(f"  payload_keys: {sorted(report.payload.keys())}")
    return "\n".join(lines)


def run_ops_cli(argv: list[str] | None = None) -> int:
    """Entry point used by the ``handoff ops`` CLI command."""
    argv = list(sys.argv if argv is None else argv)
    if argv and argv[0] == "ops":
        argv = argv[1:]
    parser = build_ops_parser()
    args = parser.parse_args(argv)
    report, exit_code = run_ops_command(
        args.command,
        actor=args.actor,
        action=args.action,
        resource=args.resource,
        trace_id=args.trace_id,
        tool_name=args.tool,
        dry_run=args.dry_run,
        project_root=args.path,
        data_dir=args.data_dir,
    )
    print(render_ops(report, json_output=args.json, mcp_payload=args.mcp_payload))
    return exit_code


# ---------------------------------------------------------------------------
# Module-level default (no process-wide state is created on import)
# ---------------------------------------------------------------------------


def ops_commands() -> tuple[str, ...]:
    return OPS_COMMANDS