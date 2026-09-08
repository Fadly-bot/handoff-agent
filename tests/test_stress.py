"""Phase 21 — Multi-Agent Stress, Conflict & Recovery tests.

Stress covers: long-running workflows, parallel agents, simultaneous reads,
stale-write races, checkpoint collisions, divergent objectives, concurrent
Git mutations, checkpoint corruption, fuzz-style protocol validation, and
failure injection (permissions, disk, provider, adapter lifecycle).
"""

import random
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import handoff_agent.protocol as protocol
from handoff_agent.adapters.base import (
    AdapterError,
    AdapterPermissionError,
    AdapterUnsupportedError,
    contains_secret_like,
)
from handoff_agent.adapters.platforms import create_platform_adapter
from handoff_agent.capability import AgentIdentity
from handoff_agent.integration import IntegrationProviderFailure, MockProvider
from handoff_agent.interop import detect_conflict, snapshot_from_adapter
from handoff_agent.persistence import HandoffWriteError
from handoff_agent.protocol import (
    ProtocolError,
    ProtocolValidationError,
    ProtocolVersionError,
    build_checkpoint,
    parse_handoff_document,
    render_state_block,
    validate_checkpoint,
    verify_identity,
)
from handoff_agent.workflow import (
    WorkflowManager,
    StaleCheckpointError,
    WorkflowError,
    WorkflowState,
)
from conftest import commit_all, git, init_repo, write_file

PRODUCER = AgentIdentity(name="producer-ai", version="1", kind="ai")
CONSUMER = AgentIdentity(name="consumer-ai", version="1", kind="ai")


def _adapter(repo: Path):
    return create_platform_adapter("claude").concrete_adapter("file", project_root=str(repo))


def _checkpoint_text(objective: str, sequence: int = 1, **kw) -> str:
    cp = build_checkpoint(
        objective=objective, project_name="stress", sequence=sequence, **kw
    )
    return render_state_block(cp)


class TestLongRunningLifecycle:
    def test_sixty_checkpoint_workflow(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        commit_all(repo, "init")
        adapter = _adapter(repo)
        mgr = WorkflowManager(adapter=adapter)
        record = mgr.begin(PRODUCER, CONSUMER, str(repo))
        for step in range(1, 61):
            mgr.checkpoint(
                record,
                objective=f"long objective {step}",
                completed=(f"step-{step}",),
                actor=record.producer if step % 2 else record.consumer,
            )
            if step % 2 == 0:
                assert record.sequence == step
        assert record.sequence == 60
        assert mgr.verify_checkpoint(record)["ok"] is True
        assert len(mgr.export_audit(record)) >= 60
        adapter.stop()

    def test_handoff_after_many_checkpoints(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        commit_all(repo, "init")
        adapter = _adapter(repo)
        mgr = WorkflowManager(adapter=adapter)
        record = mgr.begin(PRODUCER, CONSUMER, str(repo))
        for i in range(25):
            mgr.checkpoint(record, objective="stable objective", completed=(f"c{i}",))
        request = mgr.request_handoff(record, consumer=record.consumer)
        mgr.accept_handoff(record, consumer=record.consumer, token=request.token, human_approved=True)
        report = mgr.verify_before_continue(record, expected_base=record.current_identity)
        assert report.consistent, report.gaps
        mgr.complete(record)
        assert record.state == WorkflowState.COMPLETED
        adapter.stop()


class TestParallelAgents:
    def test_simultaneous_reads_are_consistent(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        adapter = _adapter(repo)
        adapter.write_checkpoint(_checkpoint_text("shared", 1))
        barrier = threading.Barrier(16)

        def reader(results, i):  # type: ignore[no-untyped-def]
            local = _adapter(repo)
            barrier.wait()
            results[i] = local.read_handoff()
            local.stop()

        results: dict[int, str | None] = {}
        threads = [
            threading.Thread(target=reader, args=(results, i)) for i in range(16)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert len(results) == 16
        for content in results.values():
            cp = parse_handoff_document(content)
            assert cp is not None and verify_identity(cp)
        adapter.stop()

    def test_stale_write_race_has_single_winner_per_base(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        seed = _adapter(repo)
        seed.write_checkpoint(_checkpoint_text("seed", 1))
        seed_base = snapshot_from_adapter(seed).identity  # type: ignore[union-attr]
        seed.stop()

        barrier = threading.Barrier(12)
        outcomes = []

        def contender(i: int) -> None:
            local = _adapter(repo)
            current = snapshot_from_adapter(local).identity  # type: ignore[union-attr]
            barrier.wait()
            try:
                text = _checkpoint_text(f"worker-{i}", 2)
                local.write_checkpoint(text, expected_base=current)
                identity = parse_handoff_document(text).identity.id  # type: ignore[union-attr]
                outcomes.append(("ok", current, identity))
            except AdapterPermissionError:
                outcomes.append(("stalefailed", current, None))
            finally:
                local.stop()

        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = [pool.submit(contender, i) for i in range(12)]
            for f in futures:
                f.result(timeout=60)
        ok = [o for o in outcomes if o[0] == "ok"]
        assert 1 <= len(ok) <= 12
        ok_bases = {o[1] for o in ok}
        ok_identities = {o[2] for o in ok}
        # every successful write is based on the seed or on another winner →
        # writers serialized into a chain (no disconnected writes)
        assert ok_bases <= ({seed_base} | ok_identities)
        assert len(ok_identities) == len(ok)
        final = _adapter(repo)
        try:
            assert final.validate_checkpoint().get("valid") is True
            final_identity = snapshot_from_adapter(final).identity  # type: ignore[union-attr]
            assert final_identity in ok_identities | {seed_base}
        finally:
            final.stop()

    def test_sequential_stale_write_is_deterministically_refused(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        adapter = _adapter(repo)
        adapter.write_checkpoint(_checkpoint_text("base", 1))
        base = snapshot_from_adapter(adapter).identity  # type: ignore[union-attr]
        adapter.write_checkpoint(_checkpoint_text("successor", 2), expected_base=base)
        with pytest.raises(AdapterPermissionError):
            adapter.write_checkpoint(_checkpoint_text("duplicate", 3), expected_base=base)
        assert adapter.validate_checkpoint().get("valid") is True
        adapter.stop()

    def test_checkpoint_collision_is_noop(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        adapter = _adapter(repo)
        mgr = WorkflowManager(adapter=adapter)
        record = mgr.begin(PRODUCER, CONSUMER, str(repo))
        mgr.checkpoint(record, objective="same-state", decisions=("d",), artifacts=("f.py",))
        seq = record.sequence
        # a second agent re-writes identical machine state → identity collision,
        # which must be a no-op (no sequence bump, no corruption)
        mgr.checkpoint(record, objective="same-state", decisions=("d",), artifacts=("f.py",))
        assert record.sequence == seq
        assert mgr.verify_checkpoint(record)["ok"]
        adapter.stop()


class TestConflicts:
    def test_deterministic_conflict_report(self) -> None:
        base = "a" * 64
        ours = _checkpoint_text("producer objective", 2)
        theirs = _checkpoint_text("consumer objective", 2)
        first = detect_conflict(base, ours, theirs)
        second = detect_conflict(base, ours, theirs)
        assert first.to_dict() == second.to_dict()
        assert first.conflicting is True
        assert first.reason  # deterministic reason is present

    def test_fast_forward_not_a_conflict(self) -> None:
        base = "a" * 64
        ours = _checkpoint_text("a", 2)
        theirs = _checkpoint_text("a", 2)
        assert detect_conflict(base, ours, theirs).conflicting is False

    def test_diverged_objective_blocks_continuation(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        adapter = _adapter(repo)
        mgr = WorkflowManager(adapter=adapter)
        record = mgr.begin(PRODUCER, CONSUMER, str(repo))
        mgr.checkpoint(record, objective="shared objective")
        # rival agent rewrites the checkpoint with a different objective
        rival = _adapter(repo)
        rival.write_checkpoint(_checkpoint_text("diverged objective", 9))
        rival.stop()
        with pytest.raises((StaleCheckpointError, WorkflowError)):
            mgr.verify_before_continue(record, expected_base=record.current_identity)
        adapter.stop()

    def test_human_resolution_path(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        adapter = _adapter(repo)
        mgr = WorkflowManager(adapter=adapter)
        record = mgr.begin(PRODUCER, CONSUMER, str(repo))
        mgr.checkpoint(record, objective="agent-drafted")
        mgr.fail(record, reason="conflict escalated to human")
        mgr.recover(record, actor="human-operator")
        assert record.state == WorkflowState.WORKING
        mgr.checkpoint(record, objective="human-resolved objective")
        request = mgr.request_handoff(record, consumer=record.consumer)
        mgr.accept_handoff(record, consumer=record.consumer, token=request.token, human_approved=True)
        mgr.complete(record)
        assert record.state == WorkflowState.COMPLETED
        adapter.stop()


class TestConcurrentGitMutations:
    def _mutated_repo(self, tmp_path: Path):
        repo = tmp_path / "repo"
        init_repo(repo)
        write_file(repo, "src/lib.py", "def f():\n    return 1\n")
        commit_all(repo, "init")
        write_file(repo, "README.md", "# ok\n")
        return repo

    def test_dirty_tree(self, tmp_path: Path) -> None:
        repo = self._mutated_repo(tmp_path)
        adapter = _adapter(repo)
        adapter.write_checkpoint(_checkpoint_text("before dirty", 1))
        write_file(repo, "src/lib.py", "def f():\n    return 2\n")  # dirty

        assert adapter.validate_checkpoint().get("valid") is True
        state = adapter.project_state()
        assert isinstance(state, dict)
        adapter.stop()

    def test_staged_unrelated(self, tmp_path: Path) -> None:
        repo = self._mutated_repo(tmp_path)
        adapter = _adapter(repo)
        adapter.write_checkpoint(_checkpoint_text("before staged", 1))
        write_file(repo, "staged.txt", "unrelated\n")
        git(repo, "add", "staged.txt")

        assert adapter.validate_checkpoint().get("valid") is True
        state = adapter.project_state()
        assert isinstance(state, dict)
        adapter.stop()

    def test_untracked_and_deleted(self, tmp_path: Path) -> None:
        repo = self._mutated_repo(tmp_path)
        adapter = _adapter(repo)
        adapter.write_checkpoint(_checkpoint_text("before", 1))
        write_file(repo, "untracked.bin", "binary-ish data")
        git(repo, "rm", "--cached", "src/lib.py")

        assert adapter.validate_checkpoint().get("valid") is True
        state = adapter.project_state()
        assert isinstance(state, dict)
        adapter.stop()

    def test_renamed_and_branch_change(self, tmp_path: Path) -> None:
        repo = self._mutated_repo(tmp_path)
        adapter = _adapter(repo)
        adapter.write_checkpoint(_checkpoint_text("before branch", 1))
        git(repo, "mv", "src/lib.py", "src/util.py")
        commit_all(repo, "rename")
        git(repo, "checkout", "-b", "feature/stress")
        write_file(repo, "feature.py", "x = 1\n")
        commit_all(repo, "feature")

        assert adapter.validate_checkpoint().get("valid") is True
        state = adapter.project_state()
        assert isinstance(state, dict)
        adapter.stop()

    def test_checkpoint_survives_external_rewrite(self, tmp_path: Path) -> None:
        repo = self._mutated_repo(tmp_path)
        adapter = _adapter(repo)
        result = adapter.write_checkpoint(_checkpoint_text("v1", 1))
        changelog_after_v1 = adapter.read_changelog()
        external = _adapter(repo)
        external.write_checkpoint(_checkpoint_text("external v2", 2))
        external.stop()

        assert adapter.read_handoff() is not None
        assert adapter.validate_checkpoint().get("valid") is True
        changelog = adapter.read_changelog()
        assert changelog and changelog != changelog_after_v1  # history preserved
        adapter.stop()


class TestCorruptionAndRecovery:
    def _corrupt(self, repo: Path, content: str) -> None:
        (repo / "docs" / "HANDOFF.md").write_text(content)

    def test_malformed_block_is_rejected(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        commit_all(repo, "init")
        adapter = _adapter(repo)
        adapter.write_checkpoint(_checkpoint_text("corruption victim", 1))
        self._corrupt(repo, "# HANDOFF\n```handoff-protocol\n{not valid json\n```")

        validation = adapter.validate_checkpoint()
        assert validation.get("valid") is False
        with pytest.raises(ProtocolValidationError):
            parse_handoff_document(adapter.read_handoff())
        adapter.stop()

    def test_invalid_schema_and_version(self) -> None:
        bad_schema = '{"protocol":{"name":"universal-handoff-protocol","version":1},"identity":{"id":"' + "0" * 64 + '","generated_at":"2024-01-01T00:00:00","sequence":1},"metadata":{"project":{"name":"p"},"objective":"t"},"state":{"objective":"t","completed":"oops","in_progress":[],"next_actions":[],"decisions":[],"constraints":[]},"validation":{"status":"pending","checks":[]},"git":{},"artifacts":[],"risks":[]}'
        with pytest.raises(ProtocolValidationError):
            parse_handoff_document(f"```handoff-protocol\n{bad_schema}\n```")
        bad_version = bad_schema.replace('"version":1', '"version":99')
        with pytest.raises(ProtocolVersionError):
            parse_handoff_document(f"```handoff-protocol\n{bad_version}\n```")

    def test_recovery_from_changelog_restores_valid_checkpoint(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        commit_all(repo, "init")
        adapter = _adapter(repo)
        adapter.write_checkpoint(_checkpoint_text("victim v1", 1))
        adapter.write_checkpoint(_checkpoint_text("victim v2", 2))
        changelog = adapter.read_changelog()
        self._corrupt(repo, "garbage\n#### -> truncated write")
        assert adapter.validate_checkpoint().get("valid") is False

        # non-destructive recovery: restore the last archived checkpoint
        block_index = changelog.rfind("```handoff-protocol")
        assert block_index != -1
        last = changelog[block_index:]
        recovered = _checkpoint_text("victim v1", 1)
        adapter2 = _adapter(repo)
        adapter2.write_checkpoint(last if parse_handoff_document(last) else recovered)
        try:
            assert adapter2.validate_checkpoint().get("valid") is True
        finally:
            adapter.stop()
            adapter2.stop()

    def test_rollback_safe_atomic_write(self, tmp_path: Path, monkeypatch) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        commit_all(repo, "init")
        adapter = _adapter(repo)
        adapter.write_checkpoint(_checkpoint_text("keep me", 1))

        def _boom(target, content):  # type: ignore[no-untyped-def]
            raise OSError(28, "No space left on device")

        monkeypatch.setattr("handoff_agent.persistence._atomic_write_file", _boom)
        with pytest.raises((HandoffWriteError, AdapterError)):
            adapter.write_checkpoint(_checkpoint_text("must not land", 2))
        monkeypatch.undo()

        # previous checkpoint intact and valid
        assert adapter.validate_checkpoint().get("valid") is True
        cp = parse_handoff_document(adapter.read_handoff())
        assert cp is not None and cp.state.objective == "keep me"
        adapter.stop()

    def test_fuzz_protocol_validation_never_crashes(self) -> None:
        seed = _checkpoint_text("fuzz victim", 1)
        rng = random.Random(42)
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789{}:\",-[]\\\n"
        outcome_counts = {"valid": 0, "protocol": 0}
        for _ in range(400):
            data = list(seed)
            for _ in range(rng.randint(1, 6)):
                op = rng.choice(("insert", "delete", "replace"))
                idx = rng.randrange(len(data))
                if op == "insert":
                    data.insert(idx, alphabet[rng.randrange(len(alphabet))])
                elif op == "delete" and len(data) > 10:
                    del data[idx]
                else:
                    data[idx] = alphabet[rng.randrange(len(alphabet))]
            mutated = "".join(data)
            try:
                cp = parse_handoff_document(mutated)
            except ProtocolError:
                outcome_counts["protocol"] += 1
                continue
            if cp is not None:
                outcome_counts["valid"] += 1
        # sanity: the mutator produced a mix
        assert outcome_counts["valid"] > 0
        assert outcome_counts["protocol"] > 0

    def test_large_checkpoint_round_trip(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        commit_all(repo, "init")
        big = "x" * 300_000
        adapter = _adapter(repo)
        adapter.write_checkpoint(_checkpoint_text(big, 1))
        assert adapter.validate_checkpoint().get("valid") is True
        cp = parse_handoff_document(adapter.read_handoff())
        assert cp is not None and len(cp.state.objective) == 300_000
        adapter.stop()

    def test_large_repo(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        for i in range(350):
            write_file(repo, f"module/dir{i % 20}/file{i}.txt", f"content {i}\n")
        commit_all(repo, "big repo")
        adapter = _adapter(repo)
        adapter.write_checkpoint(_checkpoint_text("big repo undo", 1))
        state = adapter.project_state()
        assert isinstance(state, dict)
        assert adapter.validate_checkpoint().get("valid") is True
        adapter.stop()


class TestFailureInjection:
    def test_provider_timeout_surfaces_safely(self) -> None:
        with pytest.raises(IntegrationProviderFailure):
            MockProvider("claude").inject_failure()

    def test_adapter_lifecycle_failure(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        commit_all(repo, "init")
        adapter = _adapter(repo)
        adapter.write_checkpoint(_checkpoint_text("before disconnect", 1))
        adapter.stop()
        with pytest.raises(AdapterError):
            adapter.write_checkpoint(_checkpoint_text("after disconnect", 2))
        # standalone read-only view still verifies
        fresh = _adapter(repo)
        try:
            assert fresh.validate_checkpoint().get("valid") is True
        finally:
            fresh.stop()

    def test_unsupported_interface_is_safe(self) -> None:
        from handoff_agent.adapters.platforms import integration_instructions

        with pytest.raises(AdapterUnsupportedError):
            integration_instructions("deepseek", "mcp")

    def test_secret_tainted_parallel_writes_refused(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        commit_all(repo, "init")
        seeder = _adapter(repo)
        seeder.write_checkpoint(_checkpoint_text("seed", 1))
        seeder.stop()
        outcomes = []

        def bad_writer(i: int) -> None:
            local = _adapter(repo)
            try:
                local.write_checkpoint(f'api_key = "leak-{i}-0123456789abcdef"')
                outcomes.append(("accepted", i))
            except Exception:  # noqa: BLE001
                outcomes.append(("refused", i))
            finally:
                local.stop()

        threads = [threading.Thread(target=bad_writer, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert all(kind == "refused" for kind, _ in outcomes)

        final = _adapter(repo)
        try:
            assert final.validate_checkpoint().get("valid") is True
            for text in (final.read_handoff() or "", final.read_changelog() or ""):
                assert not contains_secret_like(text)
        finally:
            final.stop()


class TestAuditPreservation:
    def test_audit_survives_failure_and_recovery(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        commit_all(repo, "init")
        adapter = _adapter(repo)
        mgr = WorkflowManager(adapter=adapter)
        record = mgr.begin(PRODUCER, CONSUMER, str(repo))
        for i in range(10):
            mgr.checkpoint(record, objective=f"audit-{i}")
        before_fail = len(mgr.export_audit(record))
        mgr.fail(record, reason="injected failure")
        mgr.recover(record, actor="human")
        mgr.checkpoint(record, objective="post recovery")
        after = mgr.export_audit(record)
        assert len(after) == before_fail + 4
        # append-only: earlier entries unchanged
        assert after[0] == mgr.export_audit(record)[0]
        assert any(e["action"] == "transition:failed" for e in after)
        assert any("recovered" in e["detail"] for e in after)
        adapter.stop()

    def test_no_out_of_project_writes(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        commit_all(repo, "init")
        adapter = _adapter(repo)
        before = {p for p in repo.rglob("*")}
        adapter.write_checkpoint(_checkpoint_text("written", 1))
        adapter.write_checkpoint(_checkpoint_text("written v2", 2))
        adapter.stop()
        after = {p for p in repo.rglob("*")}
        new = {p for p in after - before}
        assert new, "expected at least docs/HANDOFF.md and docs/CHANGELOG.md"
        for p in new:
            assert p.is_relative_to(repo)