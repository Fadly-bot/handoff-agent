"""Phase 27 — Advanced Multi-Agent Workflow Engine tests."""

from pathlib import Path

import pytest

from handoff_agent.workflow_engine import (
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowExecution,
    WorkflowStatus,
    WorkflowTrigger,
    NodeDefinition,
    NodeStatus,
    NodeType,
    WorkflowDefinitionError,
    UnknownWorkflowError,
    DuplicateWorkflowError,
    UnknownExecutionError,
    WorkflowStateError,
    ApprovalRequiredError,
    CircularDependencyError,
    CorruptionError,
    NoNodeAvailableError,
    NodeUnavailableError,
    build_workflow_template,
)


def _def(node_ids: list[tuple[str, str]] | None = None, **changes) -> WorkflowDefinition:
    if node_ids:
        nodes = tuple(
            NodeDefinition(node_id=nid, node_type=ntype, next_nodes=())
            for nid, ntype in node_ids
        )
    else:
        nodes = (
            NodeDefinition(
                node_id="start",
                node_type=NodeType.START.value,
                next_nodes=("task1",),
            ),
            NodeDefinition(
                node_id="task1",
                node_type=NodeType.TASK.value,
                dependencies=("start",),
                next_nodes=("task2",),
            ),
            NodeDefinition(
                node_id="task2",
                node_type=NodeType.TASK.value,
                dependencies=("task1",),
                next_nodes=("end",),
            ),
            NodeDefinition(
                node_id="end",
                node_type=NodeType.END.value,
                dependencies=("task2",),
            ),
        )
    fields = {
        "workflow_id": "wf-test-1",
        "name": "test-workflow",
        "version": "1",
        "nodes": nodes,
        "start_node": "start",
        "end_nodes": ("end",),
    }
    fields.update(changes)
    return WorkflowDefinition(**fields)


def _setup(tmp_path: Path, require_human_approval: bool = False, **kwargs) -> WorkflowEngine:
    return WorkflowEngine(
        str(tmp_path / "workflow_engine"),
        require_human_approval=require_human_approval,
        **kwargs,
    )


def _create_execution(engine: WorkflowEngine, workflow_id: str = "wf-test-1") -> WorkflowExecution:
    return engine.trigger_workflow(workflow_id, actor="orchestrator")


# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------

class TestWorkflowDefinition:
    def test_create_workflow(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        definition = engine.create_workflow(_def(), actor="orchestrator")
        assert definition.workflow_id == "wf-test-1"
        assert len(definition.nodes) == 4

    def test_template_linear(self) -> None:
        definition = build_workflow_template("linear")
        assert definition.template == "linear"
        assert definition.start_node == "start"
        assert definition.end_nodes == ("end",)

    def test_template_approval_gate(self) -> None:
        definition = build_workflow_template("approval_gate")
        assert definition.template == "approval_gate"
        assert any(n.node_type == NodeType.HUMAN_APPROVAL.value for n in definition.nodes)

    def test_template_parallel(self) -> None:
        definition = build_workflow_template("parallel")
        assert definition.template == "parallel"
        assert any(n.node_type == NodeType.PARALLEL.value for n in definition.nodes)
        assert any(n.node_type == NodeType.MERGE.value for n in definition.nodes)

    def test_template_conditional(self) -> None:
        definition = build_workflow_template("conditional")
        assert definition.template == "conditional"
        assert any(n.node_type == NodeType.DECISION.value for n in definition.nodes)

    def test_template_retry_loop(self) -> None:
        definition = build_workflow_template("retry_loop")
        assert definition.template == "retry_loop"
        assert any(n.node_type == NodeType.RETRY.value for n in definition.nodes)
        assert any(n.node_type == NodeType.RECOVERY.value for n in definition.nodes)

    def test_unknown_template_rejected(self) -> None:
        with pytest.raises(WorkflowDefinitionError):
            build_workflow_template("not-a-template")

    def test_empty_nodes_rejected(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        definition = _def(node_ids=[])
        definition = WorkflowDefinition(
            workflow_id="wf-bad", name="bad", nodes=(), start_node="", end_nodes=(),
        )
        with pytest.raises(WorkflowDefinitionError):
            engine.create_workflow(definition, actor="orchestrator")

    def test_missing_start_node_rejected(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        definition = _def(
            workflow_id="wf-nostart",
            nodes=(
                NodeDefinition(node_id="task1", node_type="task"),
            ),
            start_node="nonexistent",
            end_nodes=(),
        )
        ok, errors = definition.validate()
        assert not ok
        assert any("start_node" in e for e in errors)

    def test_unknown_node_type_rejected(self) -> None:
        definition = _def(
            nodes=(NodeDefinition(node_id="n1", node_type="bogus"),),
            start_node="n1",
            end_nodes=(),
        )
        ok, errors = definition.validate()
        assert not ok
        assert any("bogus" in e for e in errors)

    def test_duplicate_workflow_rejected(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        with pytest.raises(DuplicateWorkflowError):
            engine.create_workflow(_def(), actor="orchestrator")

    def test_get_unknown_workflow_rejected(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        with pytest.raises(UnknownWorkflowError):
            engine.get_workflow("nonexistent")

    def test_update_workflow(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        updated = engine.update_workflow("wf-test-1", description="updated", actor="orchestrator")
        assert updated.description == "updated"


# ---------------------------------------------------------------------------
# Workflow versioning
# ---------------------------------------------------------------------------

class TestWorkflowVersioning:
    def test_version_workflow(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        new_def = engine.version_workflow("wf-test-1", "2", actor="orchestrator")
        assert new_def.version == "2"
        assert new_def.workflow_id != "wf-test-1"
        assert "wf-test-1" in engine._workflows

    def test_list_workflows(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        engine.version_workflow("wf-test-1", "2", actor="orchestrator")
        assert len(engine.list_workflows()) == 2


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------

class TestWorkflowTriggers:
    def test_manual_trigger(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = engine.trigger_workflow("wf-test-1", actor="orchestrator")
        assert execution.trigger == WorkflowTrigger.MANUAL.value
        assert execution.status == WorkflowStatus.READY.value

    def test_event_trigger(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(trigger=WorkflowTrigger.EVENT.value), actor="orchestrator")
        execution = engine.trigger_workflow("wf-test-1", trigger=WorkflowTrigger.EVENT.value, actor="orchestrator")
        assert execution.trigger == WorkflowTrigger.EVENT.value

    def test_scheduled_trigger(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(trigger=WorkflowTrigger.SCHEDULED.value), actor="orchestrator")
        execution = engine.trigger_workflow("wf-test-1", trigger=WorkflowTrigger.SCHEDULED.value, actor="orchestrator")
        assert execution.trigger == WorkflowTrigger.SCHEDULED.value

    def test_task_trigger(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(trigger=WorkflowTrigger.TASK.value), actor="orchestrator")
        execution = engine.trigger_workflow("wf-test-1", trigger=WorkflowTrigger.TASK.value, actor="orchestrator")
        assert execution.trigger == WorkflowTrigger.TASK.value

    def test_unknown_trigger_rejected(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        with pytest.raises(WorkflowDefinitionError):
            engine.trigger_workflow("wf-test-1", trigger="cron", actor="orchestrator")


# ---------------------------------------------------------------------------
# Input / output / variables
# ---------------------------------------------------------------------------

class TestInputOutput:
    def test_input_schema(self) -> None:
        definition = _def(input_schema={"type": "object", "properties": {"foo": {"type": "string"}}})
        ok, _ = definition.validate()
        assert ok

    def test_output_schema(self) -> None:
        definition = _def(output_schema={"type": "object"})
        ok, _ = definition.validate()
        assert ok

    def test_variables(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(variables={"project_id": "p1", "owner": "alice"}), actor="orchestrator")
        execution = engine.trigger_workflow("wf-test-1", actor="orchestrator")
        assert execution.variables["project_id"] == "p1"
        assert execution.variables["owner"] == "alice"


# ---------------------------------------------------------------------------
# Execution lifecycle
# ---------------------------------------------------------------------------

class TestExecutionLifecycle:
    def test_start_execution(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        assert execution.status == WorkflowStatus.RUNNING.value
        assert len(execution.node_executions) == 4

    def test_start_from_wrong_state(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        engine.start_execution(execution.execution_id, actor="orchestrator")
        with pytest.raises(WorkflowStateError):
            engine.start_execution(execution.execution_id, actor="orchestrator")

    def test_unknown_execution(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        with pytest.raises(UnknownExecutionError):
            engine.get_execution("nonexistent")

    def test_list_executions(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution1 = _create_execution(engine)
        execution2 = _create_execution(engine)
        assert len(engine.list_executions()) == 2
        assert len(engine.list_executions("wf-test-1")) == 2

    def test_execution_status(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        assert engine.execution_status(execution.execution_id) == WorkflowStatus.READY.value


# ---------------------------------------------------------------------------
# Pause / resume / cancel
# ---------------------------------------------------------------------------

class TestPauseResumeCancel:
    def test_pause_resume(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        execution = engine.pause_execution(execution.execution_id, actor="orchestrator")
        assert execution.status == WorkflowStatus.PAUSED.value
        execution = engine.resume_execution(execution.execution_id, actor="orchestrator")
        assert execution.status == WorkflowStatus.RUNNING.value

    def test_pause_non_running_rejected(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        with pytest.raises(WorkflowStateError):
            engine.pause_execution(execution.execution_id, actor="orchestrator")

    def test_cancel(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.cancel_execution(execution.execution_id, actor="system", reason="done")
        assert execution.status == WorkflowStatus.CANCELLED.value

    def test_cancel_completed_rejected(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        engine.cancel_execution(execution.execution_id, actor="system")
        with pytest.raises(WorkflowStateError):
            engine.cancel_execution(execution.execution_id, actor="system")


# ---------------------------------------------------------------------------
# Retry / recovery / timeout
# ---------------------------------------------------------------------------

class TestRetryRecovery:
    def test_retry_failed(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        from handoff_agent.workflow_engine import WorkflowStatus
        engine._executions[execution.execution_id] = execution.with_(status=WorkflowStatus.FAILED.value)
        retried = engine.retry_execution(execution.execution_id, actor="orchestrator")
        assert retried.execution_id != execution.execution_id
        assert retried.status == WorkflowStatus.READY.value

    def test_retry_non_retryable_rejected(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        with pytest.raises(WorkflowStateError):
            engine.retry_execution(execution.execution_id, actor="orchestrator")

    def test_recover(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        from handoff_agent.workflow_engine import WorkflowStatus
        engine._executions[execution.execution_id] = execution.with_(status=WorkflowStatus.FAILED.value)
        recovered = engine.recover_execution(execution.execution_id)
        assert recovered.status == WorkflowStatus.RUNNING.value

    def test_recover_wrong_state(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        with pytest.raises(WorkflowStateError):
            engine.recover_execution(execution.execution_id)

    def test_timeout_execution(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        from datetime import datetime, timezone, timedelta
        old_start = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        engine._executions[execution.execution_id] = execution.with_(started_at=old_start)
        execution = engine.timeout_execution(
            execution.execution_id, timeout_seconds=60, actor="system"
        )
        assert execution.status == WorkflowStatus.TIMEOUT.value

    def test_timeout_not_exceeded(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        from handoff_agent.workflow_engine import TimeoutError
        with pytest.raises(TimeoutError):
            engine.timeout_execution(execution.execution_id, timeout_seconds=3600, actor="system")


# ---------------------------------------------------------------------------
# Conditional branching / decision nodes
# ---------------------------------------------------------------------------

class TestConditionalBranching:
    def test_evaluate_condition_equality(self) -> None:
        engine = _setup(Path("/tmp/x"))
        assert engine.evaluate_condition("env == prod", {"env": "prod"})
        assert not engine.evaluate_condition("env == dev", {"env": "prod"})

    def test_evaluate_condition_inequality(self) -> None:
        engine = _setup(Path("/tmp/x"))
        assert engine.evaluate_condition("env != dev", {"env": "prod"})
        assert not engine.evaluate_condition("env != prod", {"env": "prod"})

    def test_evaluate_condition_numeric(self) -> None:
        engine = _setup(Path("/tmp/x"))
        assert engine.evaluate_condition("count > 5", {"count": 10})
        assert not engine.evaluate_condition("count > 20", {"count": 10})

    def test_evaluate_condition_bare(self) -> None:
        engine = _setup(Path("/tmp/x"))
        assert engine.evaluate_condition("enabled", {"enabled": True})
        assert not engine.evaluate_condition("debug", {"debug": False})

    def test_choose_branch(self) -> None:
        engine = _setup(Path("/tmp/x"))
        node = NodeDefinition(
            node_id="decision",
            node_type=NodeType.DECISION.value,
            decision_branches=("yes", "no"),
            next_nodes=("yes_path", "no_path"),
            condition="result",
        )
        assert engine.choose_branch(node, {"result": "yes"}) == "yes"
        assert engine.choose_branch(node, {"result": "no"}) == "no"


# ---------------------------------------------------------------------------
# Approval nodes
# ---------------------------------------------------------------------------

class TestApprovalNode:
    def test_require_approval(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        execution = engine.require_approval(execution.execution_id, "task1")
        assert execution.status == WorkflowStatus.WAITING_APPROVAL.value

    def test_approve(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        execution = engine.require_approval(execution.execution_id, "task1")
        execution = engine.approve(execution.execution_id, human="admin")
        assert execution.status == WorkflowStatus.RUNNING.value

    def test_approve_requires_human(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        execution = engine.require_approval(execution.execution_id, "task1")
        with pytest.raises(ApprovalRequiredError):
            engine.approve(execution.execution_id, human="")

    def test_reject_approval(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        execution = engine.require_approval(execution.execution_id, "task1")
        execution = engine.reject_approval(execution.execution_id, human="admin", reason="not approved")
        assert execution.status == WorkflowStatus.CANCELLED.value

    def test_approval_requires_approval_state(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        with pytest.raises(ApprovalRequiredError):
            engine.approve(execution.execution_id, human="admin")


# ---------------------------------------------------------------------------
# Dependency graph
# ---------------------------------------------------------------------------

class TestDependencyGraph:
    def test_schedule_ready_nodes(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        ready = engine.schedule_ready_nodes(execution.execution_id)
        assert ready
        assert all(ne.node_id in ("start",) for ne in ready)

    def test_schedule_requires_running(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        with pytest.raises(WorkflowStateError):
            engine.schedule_ready_nodes(execution.execution_id)

    def test_detect_circular_dependency(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        definition = _def(
            workflow_id="wf-cycle",
            nodes=(
                NodeDefinition(node_id="a", node_type="task", dependencies=("b",)),
                NodeDefinition(node_id="b", node_type="task", dependencies=("a",)),
            ),
            start_node="a",
            end_nodes=(),
        )
        engine.create_workflow(definition, actor="orchestrator")
        cycles = engine.detect_circular_dependency("wf-cycle")
        assert cycles

    def test_detect_deadlock(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        definition = _def(
            workflow_id="wf-deadlock",
            nodes=(
                NodeDefinition(node_id="a", node_type="task", dependencies=("b",)),
                NodeDefinition(node_id="b", node_type="task", dependencies=("a",)),
            ),
            start_node="a",
            end_nodes=(),
        )
        engine.create_workflow(definition, actor="orchestrator")
        deadlocks = engine.detect_deadlock("wf-deadlock")
        assert deadlocks

    def test_detect_orphan_tasks(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        definition = _def(
            workflow_id="wf-orphan",
            nodes=(
                NodeDefinition(node_id="start", node_type="start", next_nodes=("task1",)),
                NodeDefinition(node_id="task1", node_type="task", dependencies=("start",)),
                NodeDefinition(node_id="orphan", node_type="task", dependencies=()),
            ),
            start_node="start",
            end_nodes=(),
        )
        engine.create_workflow(definition, actor="orchestrator")
        orphans = engine.detect_orphan_tasks("wf-orphan")
        assert "orphan" in orphans

    def test_detect_stuck_workflows(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        exec_obj = engine.start_execution(execution.execution_id, actor="orchestrator")
        stuck = engine.detect_stuck_workflows()
        assert execution.execution_id in stuck

    def test_detect_stale_executions(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        from datetime import datetime, timezone, timedelta
        old_start = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        engine._executions[execution.execution_id] = execution.with_(started_at=old_start)
        stale = engine.detect_stale_executions(max_age_seconds=60)
        assert execution.execution_id in stale


# ---------------------------------------------------------------------------
# Node execution
# ---------------------------------------------------------------------------

class TestNodeExecution:
    def test_execute_node(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        node = engine.execute_node(execution.execution_id, "start", actor="agent-1")
        assert node.status == NodeStatus.RUNNING.value

    def test_complete_node(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        engine.execute_node(execution.execution_id, "start", actor="agent-1")
        exec_obj = engine.complete_node(execution.execution_id, "start", result={"value": 42})
        start_exec = next(ne for ne in exec_obj.node_executions if ne.node_id == "start")
        assert start_exec.status == NodeStatus.COMPLETED.value
        assert start_exec.result == {"value": 42}

    def test_complete_node_propagates(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        engine.execute_node(execution.execution_id, "start", actor="agent-1")
        exec_obj = engine.complete_node(execution.execution_id, "start", result={"result": "ok"})
        assert exec_obj.variables.get("node.start") == {"result": "ok"}
        assert exec_obj.output.get("start") == {"result": "ok"}

    def test_fail_node(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        exec_obj = engine.fail_node(execution.execution_id, "task1", reason="error")
        failed = next(ne for ne in exec_obj.node_executions if ne.node_id == "task1")
        assert failed.status == NodeStatus.FAILED.value

    def test_execute_unknown_node(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        with pytest.raises(NodeUnavailableError):
            engine.execute_node(execution.execution_id, "nonexistent", actor="agent")

    def test_whole_workflow(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        for node_id in ("start", "task1", "task2"):
            engine.execute_node(execution.execution_id, node_id, actor="agent-1")
            execution = engine.complete_node(execution.execution_id, node_id, result={"done": node_id})
        assert execution.status == WorkflowStatus.COMPLETED.value


# ---------------------------------------------------------------------------
# Deadlock / duplicate prevention
# ---------------------------------------------------------------------------

class TestDeadlockDetection:
    def test_duplicate_execution_prevented(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        assert not engine.prevent_duplicate_execution("wf-test-1", correlation_id=execution.correlation_id)
        assert engine.prevent_duplicate_execution("wf-test-1", correlation_id="new-corr")

    def test_atomic_state_transition(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        assert engine.atomic_state_transition(execution.execution_id, WorkflowStatus.READY.value, WorkflowStatus.RUNNING.value)
        assert engine.execution_status(execution.execution_id) == WorkflowStatus.RUNNING.value

    def test_atomic_state_transition_invalid(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        assert not engine.atomic_state_transition(execution.execution_id, WorkflowStatus.RUNNING.value, WorkflowStatus.COMPLETED.value)


# ---------------------------------------------------------------------------
# Crash recovery / failure recovery
# ---------------------------------------------------------------------------

class TestCrashRecovery:
    def test_crash_recovery_detects_running_nodes(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        engine.execute_node(execution.execution_id, "task1", actor="agent-1")
        result = engine.crash_recovery()
        assert result["recovered"]
        exec_obj = engine.get_execution(execution.execution_id)
        assert exec_obj.status == WorkflowStatus.RECOVERING.value

    def test_provider_failure_recovery(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        exec_obj = engine.provider_failure_recovery(execution.execution_id, node_id="task1")
        task1 = next(ne for ne in exec_obj.node_executions if ne.node_id == "task1")
        assert task1.status == NodeStatus.FAILED.value


# ---------------------------------------------------------------------------
# Human approval enforcement
# ---------------------------------------------------------------------------

class TestHumanApprovalEnforcement:
    def test_approval_required_with_flag(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path, require_human_approval=True)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        execution = engine.require_approval(execution.execution_id, "task1")
        assert execution.status == WorkflowStatus.WAITING_APPROVAL.value
        with pytest.raises(ApprovalRequiredError):
            engine.start_execution(execution.execution_id, actor="orchestrator")


# ---------------------------------------------------------------------------
# Checkpoint creation/restoration
# ---------------------------------------------------------------------------

class TestCheckpoint:
    def test_create_checkpoint(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        checkpoint = engine.create_checkpoint(execution.execution_id)
        assert checkpoint["execution_id"] == execution.execution_id
        assert checkpoint["workflow_id"] == "wf-test-1"

    def test_restore_checkpoint(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        checkpoint = engine.create_checkpoint(execution.execution_id)
        restored_id = engine.restore_checkpoint(checkpoint)
        assert restored_id == execution.execution_id
        assert engine.get_execution(restored_id).workflow_id == "wf-test-1"

    def test_restore_invalid_checkpoint(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        with pytest.raises(WorkflowDefinitionError):
            engine.restore_checkpoint({})


# ---------------------------------------------------------------------------
# Handoff / CHANGELOG / Registry / Delegation / Messaging integration
# ---------------------------------------------------------------------------

class TestIntegrations:
    def test_handoff_integration(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        result = engine.handoff_integration(execution.execution_id, objective="do work")
        assert result["execution_id"] == execution.execution_id
        assert result["workflow_id"] == "wf-test-1"

    def test_changelog_entry(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        entry = engine.changelog_entry(execution.execution_id)
        assert entry["execution_id"] == execution.execution_id
        assert entry["workflow_id"] == "wf-test-1"

    def test_registry_integration(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        agents = engine.registry_integration("wf-test-1", ["agent-a", "agent-b"])
        assert "agent-a" in agents
        assert "agent-b" in agents

    def test_delegation_integration(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(variables={"project_id": "p1"}), actor="orchestrator")
        execution = _create_execution(engine)
        result = engine.delegation_integration(execution.execution_id, "task-1")
        assert result["task_id"] == "task-1"
        assert result["workflow_id"] == "wf-test-1"

    def test_messaging_integration(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        result = engine.messaging_integration(execution.execution_id)
        assert result["workflow_id"] == "wf-test-1"
        assert result["correlation_id"]


# ---------------------------------------------------------------------------
# Dynamic task creation / agent assignment
# ---------------------------------------------------------------------------

class TestDynamic:
    def test_dynamic_task_creation(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        exec_obj = engine.dynamic_task_creation(
            execution.execution_id, node_id="task1", new_task_ids=("sub1", "sub2")
        )
        assert len(exec_obj.node_executions) >= 6
        definition = engine.get_workflow("wf-test-1")
        assert any("sub1" in n.node_id for n in definition.nodes)

    def test_dynamic_agent_assignment(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        exec_obj = engine.dynamic_agent_assignment(execution.execution_id, "task1", "assigned-agent")
        task1 = next(ne for ne in exec_obj.node_executions if ne.node_id == "task1")
        assert task1.agent_id == "assigned-agent"

    def test_capability_routing(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        definition = _def()
        definition = definition.with_(
            nodes=(
                NodeDefinition(node_id="n1", node_type="agent", capability="checkpoint.create"),
            )
        )
        definition = WorkflowDefinition(
            workflow_id="wf-cap", name="capwf",
            nodes=(NodeDefinition(node_id="n1", node_type="agent", capability="checkpoint.create"),),
            start_node="n1", end_nodes=(),
        )
        engine.create_workflow(definition, actor="orchestrator")
        result = engine.capability_routing("wf-cap", capability="checkpoint.create")
        assert "n1" in result

    def test_agent_load_awareness(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        engine.execute_node(execution.execution_id, "task1", agent_id="agent-a")
        load = engine.agent_load_awareness()
        assert load.get("agent-a", 0) >= 1

    def test_priority_scheduling(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        ordered = engine.priority_scheduling(["a", "b", "c"], priority_map={"a": 1, "b": 2, "c": 3})
        assert ordered == ["a", "b", "c"]

    def test_bounded_concurrency(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path, max_concurrency=2)
        assert engine.bounded_concurrency(["a", "b", "c"]) == ["a", "b"]
        assert engine.bounded_concurrency(["a", "b", "c"], max_concurrency=1) == ["a"]

    def test_execution_isolation(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        execution = engine.start_execution(execution.execution_id, actor="orchestrator")
        assert engine.execution_isolation(execution.execution_id)

    def test_absent_awareness(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        result = engine.agent_availability_awareness()
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

class TestSecurity:
    def test_permission_enforcement(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        definition = WorkflowDefinition(
            workflow_id="wf-sec", name="secwf",
            nodes=(NodeDefinition(node_id="n1", node_type="agent", capability="bad.capability"),),
            start_node="n1", end_nodes=(),
        )
        ok, errors = definition.validate()
        assert not ok

    def test_no_destructive_bypass(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        assert engine.no_destructive_bypass("wf-test-1")

    def test_secret_filtering(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        assert engine.secret_filtering("wf-test-1")

    def test_credential_isolation(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        assert engine.credential_isolation("wf-test-1")

    def test_secret_in_workflow_rejected(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        definition = _def(variables={"api_key": "abcdefghijklmnop0123456789"})
        ok, errors = definition.validate()
        assert not ok
        assert any("secret" in e for e in errors)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_workflow_persistence(self, tmp_path: Path) -> None:
        engine1 = _setup(tmp_path)
        engine1.create_workflow(_def(), actor="orchestrator")
        engine1.trigger_workflow("wf-test-1", actor="orchestrator")
        engine2 = WorkflowEngine(str(tmp_path / "workflow_engine"))
        assert engine2.get_workflow("wf-test-1").name == "test-workflow"
        assert len(engine2.list_executions()) == 1

    def test_corrupt_state_rejected(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "workflow_engine"
        state_dir.mkdir(parents=True, exist_ok=True)
        with open(state_dir / "workflows.json", "w") as f:
            f.write("{invalid json")
        with pytest.raises(CorruptionError):
            WorkflowEngine(str(state_dir))


# ---------------------------------------------------------------------------
# Audit trail / reporting
# ---------------------------------------------------------------------------

class TestAuditAndReporting:
    def test_audit_trail(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        _create_execution(engine)
        trail = engine.audit_trail()
        assert len(trail) >= 2
        actions = {e["action"] for e in trail}
        assert "workflow.created" in actions
        assert "workflow.triggered" in actions

    def test_execution_report(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        _create_execution(engine)
        report = engine.execution_report()
        assert report["total_workflows"] == 1
        assert report["total_executions"] == 1

    def test_execution_report_single(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        report = engine.execution_report(execution.execution_id)
        assert report["workflow_id"] == "wf-test-1"


# ---------------------------------------------------------------------------
# Diagnostics / health
# ---------------------------------------------------------------------------

class TestDiagnostics:
    def test_diagnostics(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        diag = engine.diagnostics()
        assert diag["workflows"] == 1
        assert "integrity" in diag

    def test_health_check(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        health = engine.health_check()
        assert health["ok"]

    def test_health_check_fails_with_failures(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        from handoff_agent.workflow_engine import WorkflowStatus
        engine._executions[execution.execution_id] = execution.with_(status=WorkflowStatus.FAILED.value)
        health = engine.health_check()
        assert not health["ok"]


# ---------------------------------------------------------------------------
# CLI / API / MCP interfaces
# ---------------------------------------------------------------------------

class TestInterfaces:
    def test_cli_payload(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        payload = engine.cli_payload()
        assert "workflows" in payload
        assert "executions" in payload

    def test_api_payload(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        execution = _create_execution(engine)
        payload = engine.api_payload()
        assert "workflows" in payload
        payload_workflow = engine.api_payload(workflow_id="wf-test-1")
        assert payload_workflow["workflow"]["name"] == "test-workflow"
        payload_execution = engine.api_payload(execution_id=execution.execution_id)
        assert payload_execution["execution"]["workflow_id"] == "wf-test-1"

    def test_mcp_payload(self, tmp_path: Path) -> None:
        engine = _setup(tmp_path)
        engine.create_workflow(_def(), actor="orchestrator")
        payload = engine.mcp_payload()
        assert "workflows" in payload
        assert payload["count"] == 1
