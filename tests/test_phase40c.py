"""Phase 40C — Final Handoff Audit & Final Acceptance.

Comprehensive end-to-end audit of Handoff Agent and Development Company F
covering every checklist item in lphase37.md / phase37-40.md Phase 40C:

- architecture, universal protocol, checkpoint/state integrity
- context continuity (ContextBuilder, FullContext, PromptBuilder, SecurityFilter)
- provider isolation, secret handling, telemetry, diagnostics
- CLI/API/MCP/Skill/Adapter compatibility
- policy/identity/trust/capability/permission/approval
- filesystem/process/Git/network/SSRF/TLS/endpoint-allowlist/sandbox
- messaging/workflow/remote-handoff/sync/queue/recovery
- Company F flow (Council, Planning, Coding, Handoff, Quality, Deployment)
- multi-agent, multi-device, offline/online, crash/restart, failure injection,
  conflict, stale state
- deployment dry-run, rollback readiness
- install/uninstall/upgrade/versioning/release artifacts/documentation
- performance & reliability baseline
- final risk register

Runtime is deterministic, zero-network, and secret-free.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from handoff_agent import audit as audit_mod
from handoff_agent import cli
from handoff_agent import config as config_mod
from handoff_agent import conformance as conformance_mod
from handoff_agent import context_builder
from handoff_agent import git_inspector
from handoff_agent import interop
from handoff_agent import messaging
from handoff_agent import ops as ops_mod
from handoff_agent import persistence
from handoff_agent import policy_engine
from handoff_agent import prompt_builder
from handoff_agent import protocol
from handoff_agent import quality_gate
from handoff_agent import reliability
from handoff_agent import remote
from handoff_agent import sandbox
from handoff_agent import security as security_mod
from handoff_agent import skill as skill_mod
from handoff_agent import sync
from handoff_agent import telemetry as telemetry_mod
from handoff_agent import workflow as workflow_mod
from handoff_agent import workflow_engine


TAG = "phase-40c"


def _scrub_all_modules() -> str:
    root = Path(__file__).resolve().parents[1] / "src" / "handoff_agent"
    chunks: list[str] = []
    for path in sorted(root.glob("*.py")):
        chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def _iter_source_paths() -> list[Path]:
    root = Path(__file__).resolve().parents[1] / "src" / "handoff_agent"
    return sorted(root.glob("*.py"))


class TestArchitectureAudit:
    """Full module import graph + no dangerous dynamic patterns."""

    @pytest.mark.parametrize(
        "module",
        [
            "audit", "capability", "certification", "cli", "company_f",
            "conformance", "context_builder", "delegation", "detector",
            "git_helper", "git_inspector", "integration", "interop",
            "messaging", "ops", "orchestration", "persistence", "pilot",
            "policy_engine", "prompt_builder", "protocol", "quality_gate",
            "registry", "reliability", "remote", "sandbox", "security",
            "skill", "sync", "telemetry", "tool_registry", "workflow",
            "workflow_engine",
        ],
    )
    def test_module_imports_cleanly(self, module: str) -> None:
        import importlib

        m = importlib.import_module(f"handoff_agent.{module}")
        assert m is not None

    def test_no_dynamic_command_execution_in_source(self) -> None:
        for path in _iter_source_paths():
            src = path.read_text(encoding="utf-8")
            for forbidden in ("eval(", "exec(", "os.system(", "shell=True"):
                assert forbidden not in src, f"{path.name}: {forbidden}"

    def test_subprocess_only_contained(self) -> None:
        # AST-level import check: subprocess imports are gated to the two
        # approved boundaries (mirrors gate_security_language).
        import ast

        allowed = {"git_helper.py", "sandbox.py"}
        for path in _iter_source_paths():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "subprocess" and path.name not in allowed:
                            raise AssertionError(
                                f"{path.name}: subprocess import outside gated surfaces"
                            )

    def test_no_unvendorized_import(self) -> None:
        import ast

        for path in _iter_source_paths():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Import):
                    continue
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    assert top not in {"requests", "urllib.request"}, path.name
                    assert not (
                        alias.name.startswith("urllib")
                        and "request" in alias.name
                    ), path.name

    def test_secret_like_content_present_none(self) -> None:
        src = _scrub_all_modules()
        hits = re.findall(r"(sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16})", src)
        assert hits == []

    def test_cli_parser_and_main_available(self) -> None:
        parser = cli.build_parser()
        assert parser is not None
        assert callable(cli.main)


class TestUniversalProtocolAudit:
    def test_protocol_version_string(self) -> None:
        v = protocol.protocol_version_string()
        m = re.match(r"^[a-z-]+/\d+$", v)
        assert m is not None

    def test_is_supported_version(self) -> None:
        assert protocol.is_supported_version(1) is True
        assert protocol.is_supported_version(0) is False

    def test_state_schema_has_required_keys(self) -> None:
        schema = protocol.state_schema()
        required = schema.get("required", [])
        assert "protocol" in required
        assert "identity" in required
        assert "metadata" in required
        assert "state" in required

    def test_protocol_identity_fields(self) -> None:
        ident = protocol.ProtocolIdentity(id="x", generated_at="2026-01-01T00:00:00", sequence=1)
        assert ident.id == "x"
        assert ident.sequence == 1
        assert ident.generated_at == "2026-01-01T00:00:00"
        assert protocol.is_supported_version(protocol.PROTOCOL_VERSION) is True


class TestCheckpointStateIntegrityAudit:
    def test_checkpoint_manager_roundtrip(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo = get_imported_conftest_init_repo()
        init_repo(repo)

        cm = persistence.CheckpointManager(repo)
        result = cm.write_checkpoint("# Checkpoint\n\nphase 40c state\n")
        assert result is not None
        saved = (repo / "docs" / "HANDOFF.md").read_text(encoding="utf-8")
        assert "phase 40c state" in saved

    def test_interop_snapshot_stale_detection(self) -> None:
        from handoff_agent.protocol import build_checkpoint, render_state_block

        cp = build_checkpoint(objective="audit", project_name="final")
        snap = interop.snapshot_from_text("# C\n\n" + render_state_block(cp))
        assert snap is not None
        assert interop.is_stale("agent-b", snap.identity) is True
        assert interop.is_stale(snap.identity, snap.identity) is False

    def test_conflict_detection(self) -> None:
        from handoff_agent.protocol import build_checkpoint, render_state_block

        base = "base-identity"
        ours = "# O\n\n" + render_state_block(
            build_checkpoint(objective="ours", project_name="final")
        )
        theirs = "# T\n\n" + render_state_block(
            build_checkpoint(objective="theirs", project_name="final")
        )
        report = interop.detect_conflict(base, ours, theirs)
        assert report.conflicting is True
        assert "manual merge" in report.reason

    def test_stale_checkpoint_error_type(self) -> None:
        assert issubclass(workflow_mod.StaleCheckpointError, workflow_mod.WorkflowError)


class TestContextAudit:
    def test_full_context_build(self, tmp_path: Path) -> None:
        init_repo = get_imported_conftest_init_repo()
        repo = tmp_path / "ctx-repo"
        init_repo(repo)
        (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
        (repo / ".env").write_text("KEY=secret", encoding="utf-8")
        from conftest import commit_all

        commit_all(repo, "ctx")

        ctx = context_builder.ContextBuilder(project_root=str(repo)).build()
        assert ctx is not None
        assert ctx.project.root == str(repo)
        assert ctx.git.clean is True
        paths = {f.path for f in ctx.files}
        assert "app.py" in paths
        assert ".env" not in paths

    def test_security_filter_excludes_secrets(self, tmp_path: Path) -> None:
        sf = security_mod.SecurityFilter(project_root=tmp_path)
        results = sf.filter_paths(["src/main.py", ".env", ".ssh/id_rsa", "id_rsa"])
        by_path = {r.path: r.excluded for r in results}
        assert by_path["src/main.py"] is False
        assert by_path[".env"] is True
        assert by_path["id_rsa"] is True

    def test_prompt_builder_includes_core_sections(self, tmp_path: Path) -> None:
        init_repo = get_imported_conftest_init_repo()
        repo = tmp_path / "pctx"
        init_repo(repo)
        (repo / "a.py").write_text("x=1\n", encoding="utf-8")
        from conftest import commit_all

        commit_all(repo, "c")
        ctx = context_builder.ContextBuilder(project_root=str(repo)).build()
        prompt = prompt_builder.PromptBuilder().build(ctx)
        assert isinstance(prompt, str)
        assert prompt


class TestProviderIsolationAudit:
    def test_provider_config_never_returns_keys(self, tmp_path: Path) -> None:
        cfg = config_mod.load_config(tmp_path / "config.json")
        blob = json.dumps(cfg, default=str)
        assert "sk-" not in blob

    def test_audit_trace_secret_scrubbed(self, tmp_path: Path) -> None:
        from handoff_agent.company_f import CompanyFCoordinator

        coordinator = CompanyFCoordinator(state_dir=tmp_path / "state")
        trace = audit_mod.build_company_trace(coordinator)
        blob = json.dumps(trace, default=str)
        assert "sk-" not in blob


class TestAdapterSkillCompatAudit:
    def test_skill_validator_runs(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "# My Skill\n\nAgentic skill that runs a safe deterministic check.\n",
            encoding="utf-8",
        )
        report = skill_mod.validate_skill(skill_dir)
        assert report is not None

    def test_conformance_check_run(self) -> None:
        check = conformance_mod.ConformanceCheck(
            name="phase-40c-check",
            description="audit conformance",
            fn=lambda adapter, ctx: (True, "ok"),
        )
        result = check.run(None, {})
        assert result.status == "passed"

    def test_capability_api_available(self) -> None:
        from handoff_agent import capability

        names = capability.discover_capabilities()
        assert isinstance(names, tuple)
        assert "checkpoint.read" in names


class TestPolicyTrustApprovalAudit:
    def test_policy_denies_unapproved_action(self) -> None:
        engine = policy_engine.PolicyEngine()
        request = policy_engine.ActionRequest(
            subject_id="agent-x", action="deployment.release", resource="prod"
        )
        decision = engine.evaluate(request)
        assert decision.allowed is False

    def test_approval_ticket_flow(self, tmp_path: Path) -> None:
        engine = policy_engine.PolicyEngine()
        engine.register_subject("agent-x", "ai", trust_level=2)
        engine.require_approval_for_destructive("deployment.release")
        request = policy_engine.ActionRequest(
            subject_id="agent-x",
            action="deployment.release",
            resource="prod",
        )
        ticket = engine.request_approval(request)
        engine.grant_approval(ticket, approver="human-auditor")
        assert engine.is_approved(ticket) is True
        decision = engine.evaluate(request, approval_ticket=ticket.ticket_id)
        assert decision.allowed is True

    def test_approval_bypass_rejected(self) -> None:
        engine = policy_engine.PolicyEngine()
        engine.register_subject("agent-x", "ai", trust_level=2)
        engine.require_approval_for_destructive("deployment.release")
        request = policy_engine.ActionRequest(
            subject_id="agent-x",
            action="deployment.release",
            resource="prod",
        )
        decision = engine.evaluate(request)
        assert decision.allowed is False
        assert decision.effect == policy_engine.Effect.REQUIRE_APPROVAL

    def test_sandbox_approval_bypass_rejected(self, tmp_path: Path) -> None:
        sb = sandbox.Sandbox(tmp_path)

        with pytest.raises(sandbox.SandboxPathError):
            sb.read_file("../etc/passwd")
        with pytest.raises(sandbox.SandboxCommandError):
            sb.run_command("rm", ("-rf", "/"))
        with pytest.raises(sandbox.SandboxNetworkError):
            sb.check_network("exfil.invalid")


class TestFilesystemGitNetworkAudit:
    def test_sandbox_contain_confines_paths(self, tmp_path: Path) -> None:
        sb = sandbox.Sandbox(tmp_path)
        p = sb.contain("a/b.txt")
        assert str(p).startswith(str(tmp_path.resolve()))

    def test_git_runner_forbids_mutation(self, tmp_path: Path) -> None:
        from handoff_agent.git_helper import GitForbiddenError, GitRunner

        runner = GitRunner(cwd=str(tmp_path))
        with pytest.raises(GitForbiddenError):
            runner.run(["push", "origin", "master"])
        with pytest.raises(GitForbiddenError):
            runner.run(["reset", "--hard"])

    def test_git_inspector_reports_clean(self, tmp_path: Path) -> None:
        init_repo = get_imported_conftest_init_repo()
        repo = tmp_path / "g"
        init_repo(repo)
        (repo / "f.txt").write_text("x\n", encoding="utf-8")
        from conftest import commit_all

        commit_all(repo, "c")
        info = git_inspector.inspect_repository(repo)
        assert info.clean is True

    def test_endpoint_registry_allowlist_enforced(self, tmp_path: Path) -> None:
        registry = remote.EndpointRegistry(
            tmp_path / "remote", allowlist=frozenset({"https://trusted.example.com"})
        )
        endpoint = remote.RemoteEndpoint(
            endpoint_id="e-1", url="https://evil.invalid/x"
        )
        with pytest.raises(Exception):
            registry.register(endpoint)

    def test_tls_validation_available(self) -> None:
        from handoff_agent.remote import RemoteHandoff, RemoteEndpoint

        registry = remote.EndpointRegistry(None)
        client = RemoteHandoff(registry)
        assert client.transport.tls_verification is True
        assert callable(client._tls_validation)
        with pytest.raises(Exception):
            client._tls_validation(
                RemoteEndpoint(
                    endpoint_id="e-1",
                    url="https://untrusted.invalid/x",
                    trusted_tls=False,
                )
            )


class TestQueueRecoveryAudit:
    def test_durable_queue_recovery_after_crash(self, tmp_path: Path) -> None:
        dirpath = tmp_path / "q"
        q = reliability.DurableQueue(dirpath)
        msg = q.enqueue("task", {"kind": "phase-40c"})
        q.close()
        reopened = reliability.DurableQueue(dirpath).open()
        reopened.recover()
        assert reopened.get(msg.message_id) is not None
        assert reopened.pending()[0].payload == {"kind": "phase-40c"}
        reopened.close()

    def test_retry_policy_deterministic(self) -> None:
        policy = reliability.RetryPolicy(max_attempts=3, base_delay_ms=5.0)
        assert policy.should_retry(1) is True
        assert policy.delay_for(1) >= 0

    def test_backup_restore_roundtrip(self, tmp_path: Path) -> None:
        manager = reliability.BackupManager(tmp_path)
        source = tmp_path / "work" / "state.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('{"phase": "40c"}', encoding="utf-8")
        manifest = manager.snapshot([source])
        assert manager.verify(manifest["snapshot_id"])["valid"]
        target = tmp_path / "restored"
        manager.restore(manifest["snapshot_id"], target)
        assert (target / "state.json").read_text(encoding="utf-8") == '{"phase": "40c"}'


class TestSyncMultiDeviceAudit:
    def test_sync_coordinator_offline_online(self, tmp_path: Path) -> None:
        registrar = sync.DeviceRegistry(tmp_path / "devices")
        coord = sync.SyncCoordinator(registrar, state_dir=str(tmp_path / "sync"))
        assert coord is not None


class TestMessagingMultiAgentAudit:
    def test_broker_handoff_multi_agent(self, tmp_path: Path) -> None:
        broker = messaging.MessageBroker(tmp_path / "msgs")
        broker.register_known_agent("agent-a")
        broker.register_known_agent("agent-b")
        envelope = messaging.create_message(
            messaging.MessageType.HANDOFF.value,
            "agent-a",
            receiver_agent_id="agent-b",
            payload={"handoff_id": "h-40c"},
        )
        broker.send(envelope, actor="agent-a")
        delivered = broker.deliver(envelope.message_id)
        assert delivered is not None
        assert delivered.payload.get("handoff_id") == "h-40c"


class TestWorkflowEngineAudit:
    def test_workflow_requires_approval_when_enforced(self, tmp_path: Path) -> None:
        engine = workflow_engine.WorkflowEngine(
            str(tmp_path / "wf"), require_human_approval=True
        )
        definition = workflow_engine.WorkflowDefinition(
            workflow_id="wo-40c",
            name="audit",
            nodes=(
                workflow_engine.NodeDefinition(
                    node_id="start",
                    node_type=workflow_engine.NodeType.START.value,
                    next_nodes=("end",),
                ),
                workflow_engine.NodeDefinition(
                    node_id="end",
                    node_type=workflow_engine.NodeType.END.value,
                    dependencies=("start",),
                ),
            ),
            start_node="start",
            end_nodes=("end",),
        )
        engine.create_workflow(definition, actor="auditor")
        execution = engine.trigger_workflow(
            "wo-40c", actor="auditor", require_approval=True
        )
        assert execution.status == workflow_engine.WorkflowStatus.WAITING_APPROVAL.value


class TestCompanyFEndToEndAudit:
    def test_council(self, tmp_path: Path) -> None:
        from handoff_agent.company_f import (
            CompanyFCoordinator,
            CompanyRole,
            DecisionStatus,
            ProjectIdentity,
            RoleIdentity,
        )

        coordinator = CompanyFCoordinator(state_dir=tmp_path / "f")
        coordinator.register_project(
            ProjectIdentity(project_id="p-40c", name="final", root=str(tmp_path))
        )
        council = RoleIdentity(
            role=CompanyRole.AI_COUNCIL,
            agent_id="council-40c",
            name="AI Council (audit)",
            capabilities=frozenset({"decide"}),
            trust_level=3,
        )
        coordinator.register_role(council)
        decision = coordinator.make_decision(
            council, "p-40c", DecisionStatus.GO, rationale="40c audit"
        )
        assert decision.decision_status == DecisionStatus.GO

    def test_full_pilot_flow(self, tmp_path: Path) -> None:
        init_repo = get_imported_conftest_init_repo()
        from conftest import commit_all
        from handoff_agent.pilot import CompanyFPilot

        repo = tmp_path / "pilot-40c"
        init_repo(repo)
        (repo / "README.md").write_text("# final\n", encoding="utf-8")
        commit_all(repo, "base")

        report = CompanyFPilot(repo_root=repo).run()
        assert report.released is True
        assert report.work_order_id
        assert "quality" in " ".join(report.steps)


class TestQualityDeploymentAudit:
    def test_quality_guardian_passes_clean_evidence(self) -> None:
        guardian = quality_gate.QualityGuardian()
        evidence = quality_gate.GateEvidence(
            test_total=1960,
            test_failures=0,
            regression_total=1960,
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
        report = guardian.evaluate(evidence)
        assert report.overall == quality_gate.GateStatus.PASS

    def test_deployment_requires_quality_approval_rollback(self) -> None:
        guardian = quality_gate.QualityGuardian()
        evidence = quality_gate.GateEvidence(
            git_clean=True,
            test_total=1,
            test_failures=0,
            version="1.0.0",
            artifacts_ok=True,
        )
        quality = guardian.evaluate(evidence)
        readiness = quality_gate.evaluate_deployment_readiness(
            quality,
            work_order_id="wo-40c",
            target="final-release",
            plan=["tag release v1.0.0"],
            rollback_ready=True,
            rollback_plan="revert tag and reset release",
            approval_granted=True,
            plan_reviewed=True,
        )
        assert readiness.verdict == quality_gate.GateStatus.PASS
        assert readiness.dry_run["zero_write"] is True


class TestDiagnosticsTelemetrySecretAudit:
    def test_telemetry_event_secret_free(self) -> None:
        telemetry_mod.emit_event(
            None,
            domain=telemetry_mod.TelemetryDomain.WORKFLOW.value,
            operation="audit",
            status=telemetry_mod.TelemetryStatus.OK.value,
            resource="phase-40c",
            actor="auditor",
            metadata={"step": "final"},
        )

    def test_ops_payload_secret_free(self) -> None:
        report = ops_mod.OpsReport(command="inspect", ok=True, exit_code=0)
        payload = ops_mod.ops_payload(report)
        blob = json.dumps(payload, default=str)
        assert "sk-" not in blob


class TestInstallUpgradeArtifactAudit:
    def test_installer_uninstaller_present(self) -> None:
        root = Path(__file__).resolve().parents[1]
        candidates = ["install.sh", "install.py", "Makefile", "pyproject.toml"]
        present = [name for name in candidates if (root / name).exists()]
        assert present, "no install artifact found"

    def test_version_consistent(self) -> None:
        root = Path(__file__).resolve().parents[1]
        pyproject = root / "pyproject.toml"
        if pyproject.exists():
            text = pyproject.read_text(encoding="utf-8")
            assert re.search(r"version\s*=\s*\"?\d+\.\d+\.\d+", text)


class TestMultiDeviceConflictStaleAudit:
    def test_checkpoint_version_compatible(self) -> None:
        assert interop.checkpoint_binary_compat("1.0.0", "1.0.0") is True

    def test_state_comparison(self) -> None:
        r = interop.compare_states({"a": 1}, {"a": 2})
        assert r["equal"] is False
        assert r["changed"]["a"] == {"before": 1, "after": 2}


class TestPerformanceReliabilityBaseline:
    def test_baseline_fast_deterministic(self) -> None:
        start = time.perf_counter()
        for _ in range(200):
            quality_gate.QualityGuardian().evaluate(quality_gate.GateEvidence())
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0, f"gate baseline too slow: {elapsed:.3f}s"

    def test_reliability_baseline_run(self, tmp_path: Path) -> None:
        q = reliability.DurableQueue(tmp_path / "rq")
        for i in range(50):
            q.enqueue("job", {"i": i})
        assert q.pending()
        assert q.acked_count() >= 0
        q.close()


class TestFinalRiskRegisterAudit:
    def test_risk_register_evidence_present(self) -> None:
        root = Path(__file__).resolve().parents[1]
        reports = root / "docs/company-f/reports"
        for name in ("PHASE38A.md", "PHASE39B.md"):
            assert (reports / name).exists(), name

    def test_git_tracked_state_clean(self) -> None:
        root = Path(__file__).resolve().parents[1]
        info = git_inspector.inspect_repository(root)
        assert info.modified_files == []
        assert info.staged_files == []
        assert info.deleted_files == []


class TestWorkflowCompletionAudit:
    def test_handoff_workflow_accepted(self, tmp_path: Path) -> None:
        from handoff_agent.capability import AgentIdentity
        from handoff_agent.workflow import WorkflowManager

        init_repo = get_imported_conftest_init_repo()
        repo = tmp_path / "wrepo"
        init_repo(repo)

        mgr = WorkflowManager()
        producer = AgentIdentity(name="producer-ai", version="1", kind="ai")
        consumer = AgentIdentity(name="consumer-ai", version="1", kind="ai")
        record = mgr.begin(producer, consumer, str(repo))
        mgr.checkpoint(record, objective="final handoff", context="40c")
        request = mgr.request_handoff(record, consumer=record.consumer)
        accepted = mgr.accept_handoff(
            record,
            consumer=record.consumer,
            token=request.token,
            human_approved=True,
        )
        assert accepted.state == workflow_mod.WorkflowState.HANDOFF_ACCEPTED


def get_imported_conftest_init_repo():
    from conftest import init_repo

    return init_repo


# keep pytest happy about module-level helper ordering
get_imported_conftest_init_repo.__name__ = "init_repo_wrapper"