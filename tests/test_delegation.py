"""Phase 25 — Task Delegation & Agent Routing tests."""

from pathlib import Path

import pytest

from handoff_agent.capability import CapabilityDeniedError, PermissionBoundary
from handoff_agent.delegation import (
    ApprovalRequiredError,
    AssignmentError,
    CorruptionError,
    Delegator,
    DuplicateResultError,
    DuplicateTaskError,
    InvalidDependencyError,
    NoAgentAvailableError,
    StaleResultError,
    TaskDefinition,
    TaskValidationError,
    UnknownTaskError,
)
from handoff_agent.registry import AgentRecord, AgentRegistry, AvailabilityStatus


_CAPS = frozenset({"checkpoint.read", "checkpoint.create", "checkpoint.update"})


def _agent(agent_id: str, **changes) -> AgentRecord:
    fields: dict = {
        "agent_id": agent_id,
        "name": f"worker-{agent_id}",
        "provider": "anthropic",
        "platform": "claude",
        "capabilities": _CAPS,
        "permission_scope": _CAPS,
        "status": AvailabilityStatus.ONLINE,
    }
    fields.update(changes)
    return AgentRecord(**fields)


def _setup(tmp_path: Path, *agents: AgentRecord, require_human_approval: bool = False):
    reg = AgentRegistry(str(tmp_path / "registry"))
    for agent in agents:
        reg.register(agent, actor="admin-1")
        reg.heartbeat(agent.agent_id, actor=agent.agent_id)
    delegator = Delegator(
        reg,
        state_dir=str(tmp_path / "delegation"),
        require_human_approval=require_human_approval,
    )
    return reg, delegator


def _task(task_id: str = "t1", **changes) -> TaskDefinition:
    fields: dict = {
        "task_id": task_id,
        "description": "do the thing",
        "task_type": "generic",
        "required_capabilities": {"checkpoint.create"},
        "trust_min": 1,
    }
    fields.update(changes)
    return TaskDefinition(**fields)


def _accepted_execution(tmp_path: Path):
    _, del2 = _setup(tmp_path, _agent("w1"))
    del2.create_task(_task(), actor="requester")
    del2.assign("t1", "w1", actor="orchestrator")
    del2.confirm_assignment("t1", "w1")
    del2.accept_task("t1", agent_id="w1")
    return del2, del2._execution["t1"]


class TestTaskModel:
    def test_create_validates_and_normalizes(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"))
        task = del2.create_task(
            _task(description="  tidy ", constraints=("b", "a", "b")),
            actor="requester",
        )
        assert task.description == "tidy"
        assert task.constraints == ("a", "b")

    def test_missing_identity_rejected(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"))
        with pytest.raises(TaskValidationError):
            del2.create_task(_task(task_id=""))

    def test_unknown_capability_rejected(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"))
        with pytest.raises(TaskValidationError):
            del2.create_task(
                _task(required_capabilities={"not.a.capability"})
            )

    def test_duplicate_task_rejected(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"))
        del2.create_task(_task(), actor="requester")
        with pytest.raises(DuplicateTaskError):
            del2.create_task(_task(), actor="requester")

    def test_secret_like_context_rejected(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"))
        with pytest.raises(TaskValidationError):
            del2.create_task(
                _task(requirements={"api_key": "thisisasecrettokenvalue3"})
            )

    def test_missing_dependency_rejected(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"))
        with pytest.raises(InvalidDependencyError):
            del2.create_task(_task(dependencies=("ghost",)))

    def test_unknown_task_raises(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"))
        with pytest.raises(UnknownTaskError):
            del2.get_task("missing")


class TestDecomposition:
    def test_decompose_builds_chain(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"))
        parent = del2.create_task(_task(task_id="p"), actor="requester")
        subs = del2.decompose(
            parent.task_id,
            [TaskDefinition(task_id="", description="sub-a"),
             TaskDefinition(task_id="", description="sub-b")],
            actor="orchestrator",
        )
        assert [t.task_id for t in subs] == ["p-1", "p-2"]
        assert subs[0].parent_task_id == "p"
        assert subs[1].dependencies == ("p-1",)
        assert subs[0].correlation_id == "p"
        assert subs[1].correlation_id == "p"
        assert subs[0].request_id == "p"


class TestRouting:
    def test_capability_based_deterministic_routing(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"), _agent("w2"))
        del2.create_task(_task(), actor="requester")
        first = del2.route("t1")
        second = del2.route("t1")
        assert first.agent_id == second.agent_id
        assert first.reason
        assert first.ranked == ("w1", "w2")

    def test_no_available_agent_raises(self, tmp_path: Path) -> None:
        reg, del2 = _setup(tmp_path)
        reg.register(_agent("off", status=AvailabilityStatus.OFFLINE), actor="admin-1")
        del2.create_task(_task(required_capabilities={"checkpoint.create"}), actor="requester")
        with pytest.raises(NoAgentAvailableError):
            del2.route("t1")

    def test_preferred_provider_wins(self, tmp_path: Path) -> None:
        reg, del2 = _setup(
            tmp_path,
            _agent("anthropic-w", provider="anthropic"),
            _agent("google-w", provider="google"),
        )
        del2.create_task(
            _task(preferred_provider="google"),
            actor="requester",
        )
        decision = del2.route("t1")
        assert decision.agent_id == "google-w"

    def test_required_provider_restricts(self, tmp_path: Path) -> None:
        reg, del2 = _setup(
            tmp_path,
            _agent("anthropic-w", provider="anthropic"),
            _agent("google-w", provider="google"),
        )
        del2.create_task(
            _task(required_provider="anthropic"),
            actor="requester",
        )
        assert del2.route("t1").agent_id == "anthropic-w"

    def test_rule_ignores_unavailable(self, tmp_path: Path) -> None:
        reg, del2 = _setup(tmp_path, _agent("good"))
        reg.register(
            _agent("bad", status=AvailabilityStatus.UNAVAILABLE), actor="admin-1"
        )
        del2.create_task(_task(), actor="requester")
        decision = del2.route("t1")
        assert decision.agent_id == "good"


class TestAssignment:
    def test_assign_confirm_accept(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"))
        del2.create_task(_task(), actor="requester")
        del2.assign("t1", "w1", actor="orchestrator")
        assert del2.get_task("t1").assigned_agent_id == "w1"
        assert del2.status_of("t1") == "assigned"
        del2.confirm_assignment("t1", "w1")
        del2.accept_task("t1", agent_id="w1")
        assert del2.status_of("t1") == "running"

    def test_unregistered_agent_rejected(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"))
        del2.create_task(_task(), actor="requester")
        with pytest.raises(AssignmentError):
            del2.assign("t1", "ghost", actor="orchestrator")

    def test_unhealthy_agent_rejected(self, tmp_path: Path) -> None:
        reg, del2 = _setup(tmp_path)
        reg.register(_agent("silent", status=AvailabilityStatus.ONLINE), actor="admin-1")
        del2.create_task(_task(), actor="requester")
        with pytest.raises(AssignmentError):
            del2.assign("t1", "silent", actor="orchestrator")

    def test_capability_shortfall_rejected(self, tmp_path: Path) -> None:
        reg, del2 = _setup(
            tmp_path,
            _agent("limited", capabilities={"checkpoint.read"}, permission_scope={"checkpoint.read"}),
        )
        del2.create_task(_task(required_capabilities={"checkpoint.create"}), actor="requester")
        with pytest.raises(AssignmentError):
            del2.assign("t1", "limited", actor="orchestrator")

    def test_scope_restrictions_enforced(self, tmp_path: Path) -> None:
        _, del2 = _setup(
            tmp_path,
            _agent("scoped", task_scope={"codegen"}, project_scope={"proj-1"}),
        )
        del2.create_task(_task(task_type="docs", project_id="proj-2"), actor="requester")
        with pytest.raises(AssignmentError):
            del2.assign("t1", "scoped", actor="orchestrator")

    def test_reject_assignment_returns_to_pending(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"))
        del2.create_task(_task(), actor="requester")
        del2.assign("t1", "w1", actor="orchestrator")
        task = del2.reject_assignment("t1", "w1", actor="w1", reason="too busy")
        assert task.assigned_agent_id == ""
        assert del2.status_of("t1") == "pending"

    def test_refuse_task(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"))
        del2.create_task(_task(), actor="requester")
        del2.assign("t1", "w1", actor="orchestrator")
        del2.refuse_task("t1", agent_id="w1", reason="no budget")
        assert del2.status_of("t1") == "refused"

    def test_wrong_agent_cannot_accept(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"), _agent("w2"))
        del2.create_task(_task(assigned_agent_id="w1"), actor="requester")
        with pytest.raises(AssignmentError):
            del2.accept_task("t1", agent_id="w2")


class TestApprovalGate:
    def test_approval_required_blocks_routing(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"), require_human_approval=True)
        del2.create_task(_task(approval_required=True), actor="requester")
        with pytest.raises(ApprovalRequiredError):
            del2.route("t1")
        assert del2.status_of("t1") == "waiting_approval"

    def test_approve_requires_human(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"))
        del2.create_task(_task(approval_required=True), actor="requester")
        del2.require_approval("t1")
        with pytest.raises(ApprovalRequiredError):
            del2.approve("t1", human="")
        del2.approve("t1", human="alice")
        assert del2.status_of("t1") == "pending"

    def test_reject_approval_cancels(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"))
        del2.create_task(_task(approval_required=True), actor="requester")
        del2.require_approval("t1")
        del2.reject_approval("t1", human="alice", reason="scope changed")
        assert del2.status_of("t1") == "cancelled"

    def test_not_approval_required_rejected_on_approve(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"))
        del2.create_task(_task(), actor="requester")
        with pytest.raises(ApprovalRequiredError):
            del2.approve("t1", human="alice")


class TestBlockTimeoutCancelRetry:
    def test_block_unblock(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"))
        del2.create_task(_task(), actor="requester")
        del2.block("t1", reason="waiting on vendor")
        assert del2.status_of("t1") == "blocked"
        del2.unblock("t1")
        assert del2.status_of("t1") == "pending"

    def test_timeout_fails_and_propagates(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"))
        del2.create_task(_task(task_id="a"), actor="requester")
        del2.create_task(_task(task_id="b", dependencies=("a",)), actor="requester")
        del2.timeout("a")
        assert del2.status_of("a") == "failed"
        assert del2.status_of("b") == "failed"

    def test_cancel(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"))
        del2.create_task(_task(), actor="requester")
        del2.cancel("t1", reason="obsolete")
        assert del2.status_of("t1") == "cancelled"
        with pytest.raises(AssignmentError):
            del2.cancel("t1")

    def test_retry_after_failure(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"))
        del2.create_task(_task(), actor="requester")
        del2.fail_task("t1", "provider down")
        del2.retry("t1")
        assert del2.status_of("t1") == "pending"

    def test_reassign_after_failure(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"), _agent("w2"))
        del2.create_task(_task(), actor="requester")
        del2.fail_task("t1", "agent crashed")
        task = del2.reassign("t1", "w2")
        assert task.assigned_agent_id == "w2"
        assert del2.status_of("t1") == "assigned"

    def test_reassign_failed_agent_escalates_when_no_alternative(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"))
        del2.create_task(_task(), actor="requester")
        del2.assign("t1", "w1", actor="orchestrator")
        del2.fail_task("t1", "w1 failed")
        with pytest.raises(NoAgentAvailableError):
            del2.reassign_failed_agent("t1", failed_agent="w1")
        assert del2.status_of("t1") == "escalated"

    def test_reassign_unavailable_agent(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"), _agent("w2"))
        del2.create_task(_task(), actor="requester")
        del2.assign("t1", "w1", actor="orchestrator")
        del2.fail_task("t1", "w1 unavailable")
        task = del2.reassign_unavailable_agent("t1", unavailable_agent="w1")
        assert task.assigned_agent_id == "w2"

    def test_escalation(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"))
        del2.create_task(_task(), actor="requester")
        del2.escalate("t1", human=True, reason="blocker")
        assert del2.status_of("t1") == "escalated"


class TestResults:
    def _accepted_execution(self, tmp_path: Path):
        return _accepted_execution(tmp_path)

    def test_result_accepted(self, tmp_path: Path) -> None:
        del2, exec_id = _accepted_execution(tmp_path)
        acceptance = del2.submit_result(
            "t1", agent_id="w1", result={"ok": True}, execution_id=exec_id
        )
        assert acceptance.accepted is True
        assert acceptance.status == "success"
        assert del2.status_of("t1") == "success"

    def test_duplicate_result_idempotent(self, tmp_path: Path) -> None:
        del2, exec_id = _accepted_execution(tmp_path)
        del2.submit_result("t1", agent_id="w1", result={"ok": True}, execution_id=exec_id)
        duplicate = del2.submit_result(
            "t1", agent_id="w1", result={"ok": True}, execution_id=exec_id
        )
        assert duplicate.accepted is True
        assert "idempotent" in duplicate.reason

    def test_conflicting_duplicate_rejected(self, tmp_path: Path) -> None:
        del2, exec_id = _accepted_execution(tmp_path)
        del2.submit_result("t1", agent_id="w1", result={"ok": True}, execution_id=exec_id)
        with pytest.raises(DuplicateResultError):
            del2.submit_result(
                "t1", agent_id="w1", result={"ok": False}, execution_id=exec_id
            )

    def test_stale_result_rejected(self, tmp_path: Path) -> None:
        del2, exec_id = _accepted_execution(tmp_path)
        del2.fail_task("t1", "retry needed")
        del2.retry("t1")
        del2.assign("t1", "w1", actor="orchestrator", require_confirmation=False)
        del2.accept_task("t1", agent_id="w1")
        with pytest.raises(StaleResultError):
            del2.submit_result(
                "t1", agent_id="w1", result={"old": True}, execution_id=exec_id
            )

    def test_wrong_agent_result_rejected(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"), _agent("w2"))
        del2.create_task(_task(), actor="requester")
        del2.assign("t1", "w2", actor="orchestrator", require_confirmation=False)
        del2.accept_task("t1", agent_id="w2")
        acceptance = del2.submit_result(
            "t1", agent_id="w1", result={"hijack": True},
            execution_id=del2._execution["t1"],
        )
        assert acceptance.accepted is False

    def test_secret_result_refused(self, tmp_path: Path) -> None:
        del2, exec_id = _accepted_execution(tmp_path)
        with pytest.raises(Exception):
            del2.submit_result(
                "t1", agent_id="w1",
                result={"api_key": "thisisasecrettokenvalue4"},
                execution_id=exec_id,
            )

    def test_partial_result_accumulates(self, tmp_path: Path) -> None:
        del2, exec_id = _accepted_execution(tmp_path)
        first = del2.submit_result(
            "t1", agent_id="w1", result={"commit": "abc"}, execution_id=exec_id, partial=True
        )
        assert first.status == "running"
        second = del2.submit_result(
            "t1", agent_id="w1", result={"diff": "d"}, execution_id=exec_id, partial=True
        )
        assert second.result == {"commit": "abc", "diff": "d"}
        final = del2.submit_result(
            "t1", agent_id="w1", result={"done": True}, execution_id=exec_id
        )
        assert final.accepted is True

    def test_result_rejection_keeps_terminal(self, tmp_path: Path) -> None:
        del2, exec_id = _accepted_execution(tmp_path)
        del2.reject_result("t1", "verification failed")
        assert del2.status_of("t1") == "failed"


class TestPropagation:
    def test_dependency_unblocked_on_success(self, tmp_path: Path) -> None:
        del2, _ = _accepted_execution(tmp_path)
        del2.create_task(_task(task_id="b", dependencies=("t1",)), actor="requester")
        assert del2.status_of("b") == "created"
        del2.submit_result(
            "t1", agent_id="w1", result={"ok": True}, execution_id=del2._execution["t1"], partial=False
        )
        assert del2.status_of("b") == "pending"

    def test_checkpoint_and_correlation_propagation(self, tmp_path: Path) -> None:
        del2, exec_id = _accepted_execution(tmp_path)
        fragment = del2.checkpoint_propagation("t1")
        assert fragment["task_id"] == "t1"
        assert fragment["assigned_agent_id"] == "w1"
        handoff = del2.execution_handoff("t1")
        assert "handoff" in handoff
        assert "handoff-protocol" in handoff["handoff"]
        assert handoff["correlation_id"] in ("", "t1")


class TestSecurityBoundary:
    def test_deny_all_boundary_blocks_creation(self, tmp_path: Path) -> None:
        reg = AgentRegistry(str(tmp_path / "registry"))
        reg.register(_agent("w1"), actor="admin-1")
        reg.heartbeat("w1", actor="w1")
        restricted = Delegator(
            reg,
            state_dir=str(tmp_path / "delegation"),
            boundary=PermissionBoundary(name="read-only", deny_all=True),
        )
        with pytest.raises(CapabilityDeniedError):
            restricted.create_task(_task(), actor="requester")

    def test_read_only_boundary_blocks_execution(self, tmp_path: Path) -> None:
        reg = AgentRegistry(str(tmp_path / "registry"))
        reg.register(_agent("w1"), actor="admin-1")
        reg.heartbeat("w1", actor="w1")
        restricted = Delegator(
            reg,
            state_dir=str(tmp_path / "delegation"),
            boundary=PermissionBoundary(
                name="read-only",
                allowed=frozenset({"checkpoint.read", "checkpoint.create"}),
                deny_all=False,
            ),
        )
        restricted.create_task(_task(), actor="requester")
        with pytest.raises(CapabilityDeniedError):
            restricted.accept_task("t1", agent_id="w1")

    def test_no_unrestricted_git_or_execution(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src" / "handoff_agent" / "delegation.py"
        ).read_text(encoding="utf-8")
        assert "subprocess" not in source
        assert "shell=True" not in source
        assert "\"push\"," not in source
        assert "eval(" not in source


class TestPersistenceAndReport:
    def test_state_reloads(self, tmp_path: Path) -> None:
        reg, del2 = _setup(tmp_path, _agent("w1"))
        del2.create_task(_task(), actor="requester")
        state = str(tmp_path / "delegation")
        reloaded = Delegator(reg, state_dir=state)
        assert reloaded.get_task("t1").description == "do the thing"
        assert reloaded.status_of("t1") in ("created", "pending")

    def test_corrupt_state_raises(self, tmp_path: Path) -> None:
        reg, del2 = _setup(tmp_path, _agent("w1"))
        del2.create_task(_task(), actor="requester")
        Path(del2.path).write_text('{"tasks":', encoding="utf-8")
        with pytest.raises(CorruptionError):
            Delegator(reg, state_dir=str(tmp_path / "delegation"))

    def test_audit_and_reports(self, tmp_path: Path) -> None:
        del2, exec_id = _accepted_execution(tmp_path)
        del2.submit_result("t1", agent_id="w1", result={}, execution_id=exec_id)
        report = del2.report()
        assert report["count"] == 1
        assert report["by_status"]["success"] == 1
        assert report["audit_events"] >= 5
        assert [e["action"] for e in del2.events()] == [
            "task.created", "task.assigned", "task.assigned_confirmed",
            "task.running", "task.result_accepted",
        ]
        diag = del2.diagnostics()
        assert diag["tasks"] == 1
        assert diag["failed"] == []
        health = del2.health_check()
        assert health["ok"] is True

    def test_failed_report_and_diagnostics(self, tmp_path: Path) -> None:
        _, del2 = _setup(tmp_path, _agent("w1"))
        del2.create_task(_task(), actor="requester")
        del2.fail_task("t1", "boom")
        assert del2.health_check()["ok"] is False
        assert del2.diagnostics()["failed"] == ["t1"]