"""Phase 23 — Persistent Multi-Agent Orchestration tests."""

from pathlib import Path

import pytest

from handoff_agent.adapters.base import BaseAdapter
from handoff_agent.capability import (
    ALL_CAPABILITIES,
    CapabilityDeniedError,
    PermissionBoundary,
)
from handoff_agent.orchestration import (
    ApprovalRequiredError,
    CorruptStateError,
    DuplicateWorkflowError,
    InvalidTaskDependencyError,
    OrchestrationError,
    PersistentOrchestrator,
    StaleTaskError,
    StaleWorkflowError,
    TaskStateError,
    WorkflowNotFoundError,
    TaskStatus,
    WorkflowStatus,
    detect_stale,
    validate_state,
)


def _orchestrator(tmp_path: Path, **kwargs) -> PersistentOrchestrator:
    state_dir = tmp_path / "state"
    return PersistentOrchestrator(state_dir, **kwargs)


def _wf(orch: PersistentOrchestrator, *, tasks=("a", "b", "c")):
    rec = orch.create_workflow("proj-1", "orchestration test")
    for task in tasks:
        rec = orch.add_task(rec.workflow_id, task_id=task, name=f"task-{task}")
    return rec


class TestWorkflowLifecycle:
    def test_create_assigns_ids(self) -> None:
        orch = PersistentOrchestrator()
        rec = orch.create_workflow("proj", "wf")
        assert rec.workflow_id
        assert rec.correlation_id
        assert rec.request_id
        assert rec.status == WorkflowStatus.CREATED

    def test_duplicate_workflow_rejected(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = orch.create_workflow("p", "n")
        with pytest.raises(DuplicateWorkflowError):
            orch.create_workflow("p", "n", workflow_id=rec.workflow_id)

    def test_workflow_persists_loads_restores(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch)
        rec = orch.add_task(rec.workflow_id, task_id="d", name="task-d", priority=1)
        loaded = orch.get_workflow(rec.workflow_id)
        assert loaded.to_dict() == rec.to_dict()
        restored = orch.list_workflows()
        assert [w.workflow_id for w in restored] == [rec.workflow_id]

    def test_unknown_workflow_raises(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        with pytest.raises(WorkflowNotFoundError):
            orch.get_workflow("missing")

    def test_pause_resume_cancel(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch)
        rec, running = orch.dispatch(rec.workflow_id)
        orch.complete_task(rec.workflow_id, "a", agent_id="a1", execution_id=running[0].execution_id)
        rec = orch.pause(rec.workflow_id)
        assert rec.status == WorkflowStatus.PAUSED
        with pytest.raises(TaskStateError):
            orch.dispatch(rec.workflow_id)
        rec = orch.resume(rec.workflow_id)
        assert rec.status == WorkflowStatus.RUNNING
        rec = orch.cancel(rec.workflow_id, reason="stop")
        assert rec.status == WorkflowStatus.CANCELLED
        assert rec.count(TaskStatus.CANCELLED) == 2

    def test_recovery_restores_runnable_tasks(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch)
        rec, running = orch.dispatch(rec.workflow_id)
        old_exec = running[0].execution_id
        rec = orch.recover(rec.workflow_id)
        assert rec.tasks["a"].status == TaskStatus.QUEUED
        assert rec.tasks["a"].execution_id != old_exec

    def test_retry_resets_failed_task(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch)
        rec, running = orch.dispatch(rec.workflow_id)
        rec = orch.fail_task(rec.workflow_id, "a", execution_id=running[0].execution_id, error="boom")
        assert rec.tasks["a"].status == TaskStatus.FAILED
        rec = orch.retry_task(rec.workflow_id, "a")
        assert rec.tasks["a"].status in (TaskStatus.QUEUED, TaskStatus.APPROVAL_REQUIRED)
        assert rec.tasks["a"].attempts >= 1

    def test_timeout_fails_stalled_task(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path, timeout_seconds=1)
        rec = _wf(orch)
        rec, _ = orch.dispatch(rec.workflow_id)
        rec = orch.apply_timeouts(rec.workflow_id, now="2099-01-01T00:00:00+00:00")
        assert rec.tasks["a"].status == TaskStatus.FAILED

    def test_completion_status(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch)
        pending = {"a", "b", "c"}
        completed: set[str] = set()
        while pending - completed:
            rec, running = orch.dispatch(rec.workflow_id)
            for task in running:
                completed.add(task.task_id)
                rec = orch.complete_task(
                    rec.workflow_id, task.task_id, agent_id="a1",
                    execution_id=task.execution_id, result={"ok": True},
                )
        assert rec.status == WorkflowStatus.COMPLETED
        assert completed == {"a", "b", "c"}


class TestTaskStateMachine:
    def test_all_states_reachable(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = orch.create_workflow("p", "states")
        rec = orch.add_task(rec.workflow_id, task_id="t", name="t")
        assert rec.tasks["t"].status == TaskStatus.PENDING
        rec, running = orch.dispatch(rec.workflow_id)
        assert rec.tasks["t"].status == TaskStatus.RUNNING
        assert running[0].status == TaskStatus.RUNNING
        rec = orch.block_task(rec.workflow_id, "t", reason="manual hold")
        assert rec.tasks["t"].status == TaskStatus.BLOCKED
        rec = orch.unblock_task(rec.workflow_id, "t")
        assert rec.tasks["t"].status == TaskStatus.QUEUED
        rec = orch.flag_approval(rec.workflow_id, "t")
        assert rec.tasks["t"].status == TaskStatus.APPROVAL_REQUIRED
        rec = orch.approve_task(rec.workflow_id, "t", human="human-1")
        rec, running = orch.dispatch(rec.workflow_id, parallel=True)
        rec = orch.complete_task(rec.workflow_id, "t", agent_id="", execution_id=running[0].execution_id, result={})
        assert rec.tasks["t"].status == TaskStatus.SUCCESS
        assert rec.status == WorkflowStatus.COMPLETED

    def test_pending_queued_running_sequence(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch, tasks=("x",))
        rec, running = orch.dispatch(rec.workflow_id)
        assert running[0].task_id == "x"
        assert running[0].execution_id

    def test_waited_task_feeds_ready(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = orch.create_workflow("p", "seq")
        rec = orch.add_task(rec.workflow_id, task_id="a", name="a")
        rec = orch.add_task(rec.workflow_id, task_id="b", name="b", dependencies=("a",))
        rec = orch.set_dependencies(rec.workflow_id, "b", ("a",))
        rec, running = orch.dispatch(rec.workflow_id)
        assert [t.task_id for t in running] == ["a"]
        rec = orch.complete_task(rec.workflow_id, "a", agent_id="", execution_id=running[0].execution_id, result={})
        assert rec.tasks["b"].status in (TaskStatus.QUEUED, TaskStatus.PENDING)
        rec, running2 = orch.dispatch(rec.workflow_id)
        assert running2[0].task_id == "b"


class TestDependencies:
    def test_dependency_validation_rejects_missing_and_cycles(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch, tasks=("a", "b"))
        with pytest.raises(InvalidTaskDependencyError):
            orch.set_dependencies(rec.workflow_id, "a", ("ghost",))
        with pytest.raises(InvalidTaskDependencyError):
            orch.set_dependencies(rec.workflow_id, "a", ("a",))
        rec = orch.set_dependencies(rec.workflow_id, "b", ("a",))
        with pytest.raises(InvalidTaskDependencyError):
            orch.set_dependencies(rec.workflow_id, "a", ("b",))

    def test_sequential_dispatch_runs_one_at_a_time(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch)
        rec, running = orch.dispatch(rec.workflow_id)
        assert len(running) == 1
        # still only one running
        rec, running2 = orch.dispatch(rec.workflow_id)
        assert running2 == ()
        orch.complete_task(rec.workflow_id, running[0].task_id, agent_id="", execution_id=running[0].execution_id, result={})
        rec, running3 = orch.dispatch(rec.workflow_id)
        assert len(running3) == 1

    def test_parallel_dispatch_runs_all_ready(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch)
        rec, running = orch.dispatch(rec.workflow_id, parallel=True)
        assert len(running) == 3
        assert {t.task_id for t in running} == {"a", "b", "c"}

    def test_failure_propagates_to_dependents(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = orch.create_workflow("p", "dag")
        rec = orch.add_task(rec.workflow_id, task_id="a", name="a")
        rec = orch.add_task(rec.workflow_id, task_id="b", name="b", dependencies=("a",))
        rec, running = orch.dispatch(rec.workflow_id)
        rec = orch.fail_task(rec.workflow_id, "a", execution_id=running[0].execution_id, error="provider down")
        assert rec.tasks["b"].status == TaskStatus.FAILED
        assert rec.status == WorkflowStatus.FAILED

    def test_completion_propagates_and_unblocks(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = orch.create_workflow("p", "dag")
        rec = orch.add_task(rec.workflow_id, task_id="a", name="a")
        rec = orch.add_task(rec.workflow_id, task_id="b", name="b", dependencies=("a",))
        rec, running = orch.dispatch(rec.workflow_id)
        rec = orch.complete_task(rec.workflow_id, "a", agent_id="", execution_id=running[0].execution_id, result={})
        rec, running2 = orch.dispatch(rec.workflow_id)
        assert running2[0].task_id == "b"


class TestAgentAssignment:
    def test_assign_tracks_agent(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch)
        rec = orch.assign_agent(rec.workflow_id, "a", "agent-1")
        assert rec.assignments["a"] == "agent-1"
        assert rec.tasks["a"].agent_id == "agent-1"

    def test_capability_negotiation_denies_shortfall(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = orch.create_workflow("p", "caps")
        rec = orch.add_task(
            rec.workflow_id, task_id="a", name="a",
            required_capabilities=("checkpoint.create",),
        )
        with pytest.raises(CapabilityDeniedError):
            orch.assign_agent(rec.workflow_id, "a", "weak-agent", agent_capabilities=("checkpoint.read",))
        rec = orch.assign_agent(
            rec.workflow_id, "a", "strong-agent",
            agent_capabilities=("checkpoint.read", "checkpoint.create"),
        )
        assert rec.tasks["a"].agent_id == "strong-agent"

    def test_unassigned_agent_cannot_complete_others_task(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch)
        rec = orch.assign_agent(rec.workflow_id, "a", "owner")
        rec, running = orch.dispatch(rec.workflow_id)
        with pytest.raises(Exception):
            orch.complete_task(rec.workflow_id, "a", agent_id="intruder", execution_id=running[0].execution_id, result={})


class TestApprovalBoundary:
    def test_approval_required_blocks_dispatch(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = orch.create_workflow("p", "approval")
        rec = orch.add_task(rec.workflow_id, task_id="a", name="a", approval_required=True)
        rec, running = orch.dispatch(rec.workflow_id)
        assert running == ()
        assert rec.tasks["a"].status == TaskStatus.APPROVAL_REQUIRED
        assert rec.status == WorkflowStatus.WAITING_APPROVAL

    def test_only_human_approve_clears_gate(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = orch.create_workflow("p", "approval")
        rec = orch.add_task(rec.workflow_id, task_id="a", name="a", approval_required=True)
        rec, running = orch.dispatch(rec.workflow_id)
        assert running == ()
        with pytest.raises(ApprovalRequiredError):
            orch.approve_task(rec.workflow_id, "a", human="")
        rec = orch.approve_task(rec.workflow_id, "a", human="human-1")
        assert rec.tasks["a"].approved_by == "human-1"
        rec, running = orch.dispatch(rec.workflow_id)
        assert running[0].task_id == "a"

    def test_global_human_approval_boundary(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path, require_human_approval=True)
        rec = _wf(orch)
        rec, running = orch.dispatch(rec.workflow_id)
        assert running == ()


class TestStaleIdempotency:
    def test_stale_base_rejected(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch)
        base = rec.state_hash
        rec = orch.pause(rec.workflow_id)
        with pytest.raises(StaleWorkflowError):
            orch.add_task(rec.workflow_id, task_id="z", name="z", expected_base=base)

    def test_stale_workflow_detection_helper(self) -> None:
        assert detect_stale("a", "b") is True
        assert detect_stale("a", "a") is False

    def test_duplicate_execution_prevented(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch)
        rec, running = orch.dispatch(rec.workflow_id)
        exec_id = running[0].execution_id
        rec = orch.complete_task(rec.workflow_id, "a", agent_id="", execution_id=exec_id, result={"v": 1})
        # same execution id + same result -> idempotent no-op
        again = orch.complete_task(rec.workflow_id, "a", agent_id="", execution_id=exec_id, result={"v": 1})
        assert again.tasks["a"].status == TaskStatus.SUCCESS
        # different result for same execution -> stale
        rec2, running2 = orch.dispatch(rec.workflow_id)
        assert running2  # b runs now
        with pytest.raises(Exception):
            orch.complete_task(rec.workflow_id, "a", agent_id="", execution_id=exec_id, result={"v": 2})

    def test_stale_result_for_superseded_execution(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch)
        rec, running = orch.dispatch(rec.workflow_id)
        exec_id = running[0].execution_id
        rec = orch.fail_task(rec.workflow_id, "a", execution_id=exec_id, error="x")
        rec = orch.retry_task(rec.workflow_id, "a")
        rec, running2 = orch.dispatch(rec.workflow_id)
        assert running2[0].execution_id != exec_id
        with pytest.raises(StaleTaskError):
            orch.complete_task(rec.workflow_id, "a", agent_id="", execution_id=exec_id, result={"v": 0})
        reloaded = orch.get_workflow(rec.workflow_id)
        assert reloaded.tasks["a"].status == TaskStatus.RUNNING


class TestPersistenceIntegrity:
    def test_corrupt_state_detected(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch)
        path = orch._workflow_path(rec.workflow_id)
        path.write_text('{"not":"valid"}', encoding="utf-8")
        with pytest.raises(CorruptStateError):
            orch.get_workflow(rec.workflow_id)

    def test_state_validation_reports_bad_dependency(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch)
        rec.tasks["a"].dependencies = ("ghost",)
        rec.state_hash = ""
        report = validate_state(rec)
        assert report["ok"] is False
        assert any("ghost" in e for e in report["errors"])

    def test_secret_tainted_result_refused(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = orch.create_workflow("p", "secret")
        rec = orch.add_task(rec.workflow_id, task_id="s", name="s")
        rec, running = orch.dispatch(rec.workflow_id)
        with pytest.raises(OrchestrationError):
            orch.complete_task(
                rec.workflow_id, "s", agent_id="",
                execution_id=running[0].execution_id,
                result={"api_key": "thisisasecrettokenvalue1"},
            )

    def test_atomic_write_keeps_previous_on_failure(self, tmp_path: Path, monkeypatch) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch)
        import handoff_agent.orchestration as orch_mod

        def _boom(target, content):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(orch_mod, "_atomic_write_file", _boom)
        with pytest.raises(OrchestrationError):
            orch.add_task(rec.workflow_id, task_id="z", name="z")
        monkeypatch.undo()
        reloaded = orch.get_workflow(rec.workflow_id)
        assert "z" not in reloaded.tasks

    def test_no_unrestricted_git_mutation(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src" / "handoff_agent" / "orchestration.py"
        ).read_text(encoding="utf-8")
        assert "subprocess" not in source
        assert "shell=True" not in source


class TestRecoveryClasses:
    def test_recover_agent_failure(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch)
        rec = orch.assign_agent(rec.workflow_id, "a", "agent-x")
        rec, running = orch.dispatch(rec.workflow_id)
        rec = orch.recover_agent_failure(rec.workflow_id, failed_agent="agent-x")
        assert rec.tasks["a"].status == TaskStatus.QUEUED

    def test_recover_provider_adapter_mcp_failure(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch)
        rec, _ = orch.dispatch(rec.workflow_id)
        rec = orch.recover_provider_failure(rec.workflow_id)
        assert rec.tasks["a"].status == TaskStatus.QUEUED
        rec, _ = orch.dispatch(rec.workflow_id)
        rec = orch.recover_adapter_failure(rec.workflow_id)
        assert rec.tasks["a"].status in (TaskStatus.QUEUED, TaskStatus.RUNNING)
        rec, _ = orch.dispatch(rec.workflow_id)
        rec = orch.recover_mcp_failure(rec.workflow_id)
        assert rec.tasks["a"].status in (TaskStatus.QUEUED, TaskStatus.RUNNING)

    def test_graceful_shutdown_and_restart(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch)
        rec, running = orch.dispatch(rec.workflow_id)
        orch.complete_task(rec.workflow_id, "a", agent_id="", execution_id=running[0].execution_id, result={})
        rec = orch.graceful_shutdown(rec.workflow_id)
        assert rec.status == WorkflowStatus.PAUSED
        rec = orch.graceful_restart(rec.workflow_id)
        assert rec.status == WorkflowStatus.RUNNING


class TestEventAndAudit:
    def test_event_log_append_only(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch)
        events = orch.events(rec.workflow_id)
        assert [e.action for e in events] == [
            "workflow.created", "task.created", "task.created", "task.created",
        ]
        rec, running = orch.dispatch(rec.workflow_id)
        actions = [e.action for e in orch.events(rec.workflow_id)]
        assert "workflow.running" in actions
        assert "task.running" in actions
        rec = orch.complete_task(rec.workflow_id, "a", agent_id="", execution_id=running[0].execution_id, result={})
        assert any(e.action == "task.complete" for e in orch.events(rec.workflow_id))

    def test_state_transitions_auditable(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch)
        rec, running = orch.dispatch(rec.workflow_id)
        assert any(e.action == "task.running" for e in orch.state_transitions(rec.workflow_id))


class TestCheckpointIntegration:
    def _adapter(self, repo: Path) -> BaseAdapter:
        from handoff_agent.adapters import create_adapter
        from handoff_agent.capability import (
            AdapterIdentity,
            AgentIdentity,
        )
        agent = AgentIdentity(name="orch-test", version="1", kind="ai")
        contract = __import__(
            "handoff_agent.capability", fromlist=["build_contract"]
        ).build_contract(
            agent,
            ALL_CAPABILITIES,
            boundary=PermissionBoundary(name="orch-test", allowed=ALL_CAPABILITIES),
            adapter=AdapterIdentity("file", "1"),
        )
        adapter = create_adapter("file", project_root=str(repo), contract=contract)
        adapter.start()
        return adapter

    def test_emit_checkpoint_writes_handoff_and_changelog(self, tmp_path: Path) -> None:
        from conftest import init_repo
        repo = tmp_path / "repo"
        init_repo(repo)
        orch = _orchestrator(tmp_path)
        adapter = self._adapter(repo)
        try:
            orch.set_checkpoint_adapter(adapter)
            rec = _wf(orch)
            rec, running = orch.dispatch(rec.workflow_id)
            orch.complete_task(rec.workflow_id, "a", agent_id="", execution_id=running[0].execution_id, result={})
            text = orch.emit_checkpoint(rec.workflow_id, project_name="orchestrated-repo")
            assert "handoff-protocol" in text
            assert (repo / "docs/HANDOFF.md").exists()
            from handoff_agent.protocol import parse_handoff_document
            cp = parse_handoff_document(text)
            assert cp is not None
            assert "task-a" in cp.state.completed
        finally:
            adapter.stop()

    def test_read_only_boundary_cannot_emit(self, tmp_path: Path) -> None:
        orch = _orchestrator(
            tmp_path,
            boundary=PermissionBoundary(
                name="read-only", allowed=("checkpoint.read",), deny_all=False
            ),
        )
        rec = _wf(orch)
        with pytest.raises(CapabilityDeniedError):
            orch.emit_checkpoint(rec.workflow_id)


class TestHealthAndReports:
    def test_health_check(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        _wf(orch)
        health = orch.health_check()
        assert health["writable"] is True
        assert health["workflows"] == 1

    def test_diagnostics_after_failure(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch)
        rec, running = orch.dispatch(rec.workflow_id)
        orch.fail_task(rec.workflow_id, "a", execution_id=running[0].execution_id, error="x")
        diag = orch.diagnostics()
        assert rec.workflow_id in diag["failed_workflows"]

    def test_report_shape(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch, tasks=("a",))
        report = orch.report(rec.workflow_id)
        assert report["status"] == "created"
        assert report["integrity"]["ok"] is True
        assert report["task_summary"][0][0] == "a"

    def test_status(self, tmp_path: Path) -> None:
        orch = _orchestrator(tmp_path)
        rec = _wf(orch)
        status = orch.status(rec.workflow_id)
        assert status["name"] == "orchestration test"
        assert status["tasks"]["pending"] == 3