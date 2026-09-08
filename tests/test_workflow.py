"""Phase 20 — Universal AI-to-AI Workflow tests."""

from pathlib import Path

import pytest

from handoff_agent.adapters.platforms import create_platform_adapter
from handoff_agent.capability import AgentIdentity
from handoff_agent.interop import ConflictReport
from handoff_agent.protocol import parse_handoff_document, validate_checkpoint, verify_identity
from handoff_agent.workflow import (
    CONTINUITY_FIELDS,
    WorkflowManager,
    HumanApprovalRequiredError,
    OwnershipError,
    StaleCheckpointError,
    UnknownAgentError,
    UnknownWorkflowError,
    WorkflowConflictError,
    WorkflowError,
    WorkflowState,
    WorkflowStateError,
    continuity_report,
)
from conftest import init_repo


def _agents():
    producer = AgentIdentity(name="producer-ai", version="1", kind="ai")
    consumer = AgentIdentity(name="consumer-ai", version="1", kind="ai")
    return producer, consumer


def _manager(repo: Path) -> tuple[WorkflowManager, object]:
    adapter = create_platform_adapter("claude").concrete_adapter("file", project_root=str(repo))
    return WorkflowManager(adapter=adapter), adapter


def _checkpoint(mgr: WorkflowManager, record, objective: str = "Build the feature") -> None:
    mgr.checkpoint(
        record,
        objective=objective,
        completed=("task-a",),
        in_progress=("task-b",),
        decisions=("use stdlib only",),
        artifacts=("src/lib.py",),
        context="multi-agent continuation",
        validation_status="pending",
    )


class TestRegistrationAndIdentity:
    def test_agent_registration_is_idempotent(self) -> None:
        mgr = WorkflowManager()
        producer, _ = _agents()
        first = mgr.register_agent(producer)
        second = mgr.register_agent(producer)
        assert first == second
        assert mgr.is_registered(first)
        assert tuple(mgr.known_agents()) == (first,)

    def test_unknown_agent_rejected_in_handoff(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        mgr, adapter = _manager(repo)
        producer, consumer = _agents()
        record = mgr.begin(producer, consumer, str(repo))
        _checkpoint(mgr, record)
        with pytest.raises(UnknownAgentError):
            mgr.request_handoff(record, consumer="ghost-agent", actor=record.producer)
        adapter.stop()

    def test_unknown_workflow_raises(self) -> None:
        mgr = WorkflowManager()
        with pytest.raises(UnknownWorkflowError):
            mgr.get("wf-missing")


class TestStateMachine:
    def test_begin_moves_idle_to_working(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        mgr = WorkflowManager()
        record = mgr.begin(*_agents(), str(repo))
        assert record.state == WorkflowState.WORKING
        assert record.producer != record.consumer

    def test_invalid_transition_rejected(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        mgr, adapter = _manager(repo)
        record = mgr.begin(*_agents(), str(repo))
        # complete is only valid from handoff_accepted
        with pytest.raises(WorkflowStateError):
            mgr.complete(record)
        # accept before request
        with pytest.raises(WorkflowStateError):
            mgr.accept_handoff(record, consumer=record.consumer, token="x", human_approved=True)
        adapter.stop()

    def test_terminal_states_cannot_transition(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        mgr = WorkflowManager()
        record = mgr.begin(*_agents(), str(repo))
        mgr.abandon(record, reason="out of scope")
        assert record.state == WorkflowState.ABANDONED
        with pytest.raises(WorkflowStateError):
            mgr.abandon(record)

    def test_full_lifecycle(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        mgr, adapter = _manager(repo)
        record = mgr.begin(*_agents(), str(repo))
        # producer works and checkpoints
        mgr.checkpoint(record, objective="Build feature", in_progress=("auth",))
        assert record.state == WorkflowState.CHECKPOINTED
        assert record.sequence == 1
        assert record.current_identity is not None
        # handoff request → idempotent duplicate
        request = mgr.request_handoff(record, consumer=record.consumer, message="take over")
        duplicate = mgr.request_handoff(record, consumer=record.consumer, message="take over")
        assert duplicate.token == request.token
        assert record.state == WorkflowState.HANDOFF_REQUESTED
        # acceptance requires approval
        with pytest.raises(HumanApprovalRequiredError):
            mgr.accept_handoff(record, consumer=record.consumer, token=request.token)
        mgr.accept_handoff(record, consumer=record.consumer, token=request.token, human_approved=True)
        assert record.state == WorkflowState.HANDOFF_ACCEPTED
        assert record.human_approved is True
        # consumer continues → works → completes
        continuing = mgr.verify_before_continue(record, expected_base=record.current_identity)
        assert continuing.consistent
        mgr.checkpoint(
            record,
            objective="Build feature",
            completed=("auth",),
            in_progress=(),
            actor=record.consumer,
        )
        mgr.complete(record)
        assert record.state == WorkflowState.COMPLETED
        adapter.stop()

    def test_ownership_enforced(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        mgr, adapter = _manager(repo)
        record = mgr.begin(*_agents(), str(repo))
        mgr.checkpoint(record, objective="x")
        stranger = AgentIdentity(name="stranger", version="1", kind="ai")
        mgr.register_agent(stranger)
        with pytest.raises(OwnershipError):
            mgr.request_handoff(record, consumer=record.consumer, actor=stranger.name)
        adapter.stop()


class TestCheckpointingAndContinuity:
    def test_duplicate_checkpoint_is_noop(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        mgr, adapter = _manager(repo)
        record = mgr.begin(*_agents(), str(repo))
        _checkpoint(mgr, record)
        seq_before, state_before = record.sequence, record.state
        mgr.checkpoint(record, objective="Build the feature", completed=("task-a",),
                       in_progress=("task-b",), decisions=("use stdlib only",),
                       artifacts=("src/lib.py",), context="multi-agent continuation",
                       validation_status="pending")
        assert record.sequence == seq_before
        assert record.state == state_before
        adapter.stop()

    def test_checkpoint_isachine_verified(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        mgr, adapter = _manager(repo)
        record = mgr.begin(*_agents(), str(repo))
        _checkpoint(mgr, record)
        result = mgr.verify_checkpoint(record)
        assert result["ok"] is True
        cp = parse_handoff_document(adapter.read_handoff())
        assert cp is not None
        assert validate_checkpoint(cp) == []
        assert verify_identity(cp) is True
        assert cp.identity.sequence == 1
        adapter.stop()

    def test_stale_base_rejected(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        mgr, adapter = _manager(repo)
        record = mgr.begin(*_agents(), str(repo))
        _checkpoint(mgr, record)
        stale = "0" * 64
        with pytest.raises(StaleCheckpointError):
            mgr.checkpoint(record, objective="diverge", expected_base=stale)
        adapter.stop()

    def test_continuity_preserved_across_sequence(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        mgr, adapter = _manager(repo)
        record = mgr.begin(*_agents(), str(repo))
        _checkpoint(mgr, record)
        report = mgr.continuity(record)
        # first checkpoint has nothing prior to compare → vacuous consistency
        assert report.consistent
        # second checkpoint preserves continuity fields (only work progress)
        second = mgr.checkpoint(
            record,
            objective="Build the feature",
            completed=("task-a", "task-b"),
            in_progress=(),
            decisions=("use stdlib only",),
            artifacts=("src/lib.py",),
            context="multi-agent continuation",
            validation_status="pending",
        )
        assert second.sequence == 2
        report2 = mgr.continuity(record)
        assert report2.consistent, report2.gaps
        adapter.stop()

    def test_continuity_detects_diverged_validation(self) -> None:
        a = parse_handoff_document(
            "```handoff-protocol\n{\"protocol\":{\"name\":\"universal-handoff-protocol\","
            "\"version\":1},\"identity\":{\"id\":\"0000000000000000000000000000000000000000000000000000000000000001\","
            "\"generated_at\":\"2024-01-01T00:00:01\",\"sequence\":1},\"metadata\":{\"project\":{\"name\":\"p\"},"
            "\"objective\":\"t\"},\"state\":{\"objective\":\"t\",\"completed\":[],\"in_progress\":[\"a\"],"
            "\"next_actions\":[],\"decisions\":[\"d1\"],\"constraints\":[\"c1\"]},\"validation\":{\"status\":\"pending\","
            "\"checks\":[]},\"git\":{},\"artifacts\":[\"f.py\"],\"risks\":[]}\n```"
        )
        b = parse_handoff_document(
            "```handoff-protocol\n{\"protocol\":{\"name\":\"universal-handoff-protocol\","
            "\"version\":1},\"identity\":{\"id\":\"0000000000000000000000000000000000000000000000000000000000000002\","
            "\"generated_at\":\"2024-01-01T00:00:02\",\"sequence\":2},\"metadata\":{\"project\":{\"name\":\"p\"},"
            "\"objective\":\"t\"},\"state\":{\"objective\":\"t\",\"completed\":[\"a\"],\"in_progress\":[],"
            "\"next_actions\":[],\"decisions\":[\"d2\"],\"constraints\":[\"c2\"]},\"validation\":{\"status\":\"pending\","
            "\"checks\":[]},\"git\":{},\"artifacts\":[\"other.py\"],\"risks\":[]}\n```"
        )
        assert a is not None and b is not None
        report = continuity_report(a, b)
        assert report.consistent is False
        assert "constraints" in report.gaps
        assert "decisions" in report.gaps

    def test_verify_before_continue_rejects_superseded_checkpoint(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        mgr, adapter = _manager(repo)
        record = mgr.begin(*_agents(), str(repo))
        _checkpoint(mgr, record)
        # an external writer replaces the checkpoint while we are not looking
        external = create_platform_adapter("claude").concrete_adapter("file", project_root=str(repo))
        assert external.write_checkpoint(render_rival_checkpoint()).modified
        with pytest.raises(StaleCheckpointError):
            mgr.verify_before_continue(record, expected_base=record.current_identity)
        adapter.stop()
        external.stop()


class TestRecoveryAndFailure:
    def test_fail_then_recover_restores_working(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        mgr, adapter = _manager(repo)
        record = mgr.begin(*_agents(), str(repo))
        _checkpoint(mgr, record)
        mgr.fail(record, reason="provider timeout")
        assert record.state == WorkflowState.FAILED
        assert "provider timeout" in mgr.export_audit(record)[-1]["detail"]
        mgr.recover(record)
        assert record.state == WorkflowState.WORKING
        # checkpoint on disk is untouched by recovery
        assert adapter.read_handoff() is not None
        adapter.stop()

    def test_recover_requires_terminal_state(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        mgr = WorkflowManager()
        record = mgr.begin(*_agents(), str(repo))
        with pytest.raises(WorkflowStateError):
            mgr.recover(record)

    def test_recovery_preserves_audit_trail(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        mgr = WorkflowManager()
        record = mgr.begin(*_agents(), str(repo))
        mgr.abandon(record, reason="shifted priority")
        trail_before = len(mgr.export_audit(record))
        mgr.recover(record, actor="human-operator")
        trail = mgr.export_audit(record)
        assert len(trail) == trail_before + 1
        assert trail[-1]["action"] == "transition:working"
        assert trail[-1]["actor"] == "human-operator"


class TestApprovalAndAudit:
    def test_acceptance_token_mismatch_rejected(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        mgr, adapter = _manager(repo)
        record = mgr.begin(*_agents(), str(repo))
        _checkpoint(mgr, record)
        request = mgr.request_handoff(record, consumer=record.consumer)
        with pytest.raises(WorkflowStateError):
            mgr.accept_handoff(record, consumer=record.consumer, token="wrong", human_approved=True)
        adapter.stop()

    def test_producer_revokes_by_continuing(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        mgr, adapter = _manager(repo)
        record = mgr.begin(*_agents(), str(repo))
        _checkpoint(mgr, record)
        mgr.request_handoff(record, consumer=record.consumer)
        assert record.state == WorkflowState.HANDOFF_REQUESTED
        # producer amends its own checkpoint → back to working
        mgr.checkpoint(record, objective="re-scope", actor=record.producer)
        assert record.state in (WorkflowState.WORKING, WorkflowState.CHECKPOINTED)
        adapter.stop()

    def test_append_only_audit_and_global_export(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        mgr = WorkflowManager()
        record = mgr.begin(*_agents(), str(repo))
        record2 = mgr.begin(*_agents()[::-1], str(repo))
        assert len(mgr.global_audit()) >= 4
        for entry in mgr.export_audit(record):
            assert set(entry) == {"timestamp", "actor", "action", "detail"}
        # audit entries are immutable copies, not live refs
        entry = mgr.export_audit(record)[0]
        entry["detail"] = "tampered"
        assert mgr.export_audit(record)[0]["detail"] != "tampered"


class TestVisualizationAndLayout:
    def test_state_diagram_renders_all_states(self) -> None:
        mgr = WorkflowManager()
        diagram = mgr.render_state_diagram()
        for state in WorkflowState:
            assert state.value in diagram
        assert "recover" in diagram

    def test_flow_trace_shows_journey(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        mgr, adapter = _manager(repo)
        record = mgr.begin(*_agents(), str(repo))
        _checkpoint(mgr, record)
        mgr.request_handoff(record, consumer=record.consumer)
        trace = mgr.render_flow(record)
        assert "working" in trace and "checkpointed" in trace and "handoff_requested" in trace
        adapter.stop()


def render_rival_checkpoint() -> str:
    from handoff_agent.protocol import render_state_block

    import handoff_agent.protocol as protocol

    cp = protocol.build_checkpoint(
        objective="rival objective",
        project_name="repo",
        completed=("external",),
        sequence=9,
    )
    return render_state_block(cp)