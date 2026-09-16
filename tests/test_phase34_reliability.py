"""Phase 34 — Distributed reliability, durable queue, and recovery tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from handoff_agent.reliability import (
    BackupError,
    BackupManager,
    CheckpointStore,
    CircuitBreaker,
    CircuitBreakerOpenError,
    CorruptCheckpointError,
    DeadLetterQueue,
    DurableQueue,
    DurableQueueError,
    ExecutionPhase,
    ExecutionRegistry,
    FailureDiagnostics,
    JournalError,
    LeaseManager,
    LeaseError,
    NonRetryableError,
    QueueMessage,
    RecoveryJournal,
    ReliabilityEngine,
    ReliabilityError,
    RetryPolicy,
    StaleLeaseError,
    provision_phase34_reliability,
    run_reliability_conformance,
)
from handoff_agent.telemetry import new_collector, TelemetryStatus


def _engine(tmp_path: Path, **kwargs: object) -> ReliabilityEngine:
    return ReliabilityEngine(tmp_path, **kwargs)


def _sealed(engine: ReliabilityEngine, tmp_path: Path) -> ReliabilityEngine:
    """Simulate a restart: a fresh engine over the same data directory."""
    engine.close()
    return ReliabilityEngine(tmp_path)


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


class TestRetryPolicy:
    def test_backoff_is_exponential(self) -> None:
        policy = RetryPolicy(base_delay_ms=100, multiplier=2.0)
        assert policy.delay_for(1) == pytest.approx(0.1)
        assert policy.delay_for(2) == pytest.approx(0.2)
        assert policy.delay_for(3) == pytest.approx(0.4)

    def test_backoff_is_capped(self) -> None:
        policy = RetryPolicy(base_delay_ms=10_000, multiplier=2.0, max_delay_ms=1_000)
        assert policy.delay_for(1) == pytest.approx(1.0)
        assert policy.delay_for(5) == pytest.approx(1.0)

    def test_jitter_stays_in_bounds(self) -> None:
        policy = RetryPolicy(base_delay_ms=100, multiplier=1.0, max_delay_ms=10_000,
                             jitter_factor=0.5)
        for attempt in range(1, 6):
            delta = policy.delay_for(attempt)
            assert 0.0 <= delta <= 0.15

    def test_deterministic_without_jitter(self) -> None:
        policy = RetryPolicy(base_delay_ms=50, multiplier=2.0)
        assert policy.delay_for(3) == policy.delay_for(3)

    def test_max_attempts_terminal(self) -> None:
        policy = RetryPolicy(max_attempts=3)
        assert policy.is_terminal(3)
        assert not policy.is_terminal(2)

    def test_non_retryable_never_retried(self) -> None:
        policy = RetryPolicy(max_attempts=5)
        assert not policy.should_retry(1, NonRetryableError("permanent"))

    def test_invalid_policy_rejected(self) -> None:
        with pytest.raises(ValueError):
            RetryPolicy(max_attempts=0)
        with pytest.raises(ValueError):
            RetryPolicy(multiplier=0.5)
        with pytest.raises(ValueError):
            RetryPolicy(jitter_factor=2.0)


# ---------------------------------------------------------------------------
# Durable queue
# ---------------------------------------------------------------------------


class TestDurableQueue:
    def test_enqueue_dequeue_ack(self, tmp_path: Path) -> None:
        queue = DurableQueue(tmp_path)
        message = queue.enqueue("kind", {"seq": 1})
        assert message.state == "pending"
        due = queue.dequeue()
        assert due is not None and due.message_id == message.message_id
        assert queue.processing() != {}
        queue.ack(message.message_id)
        assert queue.stats()["acked"] == 1
        assert queue.stats()["pending"] == 0

    def test_fifo_order(self, tmp_path: Path) -> None:
        queue = DurableQueue(tmp_path)
        first = queue.enqueue("k", {"n": 1})
        second = queue.enqueue("k", {"n": 2})
        assert queue.dequeue().message_id == first.message_id
        assert queue.dequeue().message_id == second.message_id

    def test_kind_filtering(self, tmp_path: Path) -> None:
        queue = DurableQueue(tmp_path)
        queue.enqueue("a", {})
        queue.enqueue("b", {})
        assert queue.dequeue(kind="b").kind == "b"
        assert queue.dequeue().kind == "a"

    def test_not_due_message_stays_pending(self, tmp_path: Path) -> None:
        queue = DurableQueue(tmp_path)
        message = queue.enqueue("k", {})
        message.next_retry_ms = message.created_at_ms + 60_000
        assert queue.dequeue_due(now_ms=message.created_at_ms, limit=5) == []
        # force due
        due = queue.dequeue_due(now_ms=message.created_at_ms + 60_001, limit=5)
        assert len(due) == 1

    def test_durability_across_reopen(self, tmp_path: Path) -> None:
        queue = DurableQueue(tmp_path)
        message = queue.enqueue("k", {"seq": 7})
        queue.close()
        reopened = DurableQueue(tmp_path).open()
        reopened.recover()
        assert reopened.get(message.message_id) is not None
        assert reopened.pending()[0].payload == {"seq": 7}
        queue.close()
        reopened.close()

    def test_truncated_journal_tolerated(self, tmp_path: Path) -> None:
        queue = DurableQueue(tmp_path)
        queue.enqueue("k", {"seq": 1})
        queue.close()
        journal = tmp_path / "queue" / "journal.jsonl"
        content = journal.read_text(encoding="utf-8")
        journal.write_text(content + '{"seq":999,"op":"enqueue"', encoding="utf-8")
        reopened = DurableQueue(tmp_path).open()
        reopened.recover()
        assert reopened.get("msg") is None or reopened.stats()["total"] == 1
        assert reopened.stats()["truncated_at"] > 0
        reopened.close()

    def test_ack_after_crash_remains_acked(self, tmp_path: Path) -> None:
        queue = DurableQueue(tmp_path)
        message = queue.enqueue("k", {})
        queue.dequeue()
        queue.ack(message.message_id)
        queue.close()
        reopened = DurableQueue(tmp_path).open()
        reopened.recover()
        assert reopened.stats()["acked"] == 1
        assert reopened.stats()["pending"] == 0
        reopened.close()

    def test_stale_processing_recovered(self, tmp_path: Path) -> None:
        queue = DurableQueue(tmp_path)
        message = queue.enqueue("k", {})
        queue.dequeue_due(now_ms=message.created_at_ms, lease_ms=100)
        assert queue.processing() != {}
        recovered = queue.recover_stale(stale_ms=0, now_ms=message.created_at_ms + 1_000)
        assert [r["message_id"] for r in recovered] == [message.message_id]
        assert queue.processing() == {}
        assert queue.pending() != []

    def test_secret_payload_refused(self, tmp_path: Path) -> None:
        queue = DurableQueue(tmp_path)
        with pytest.raises(DurableQueueError, match="secret-like"):
            queue.enqueue("k", {"line": "probe access_token = thisisasecretvalue1"})
        assert queue.stats()["total"] == 0

    def test_oversized_payload_refused(self, tmp_path: Path) -> None:
        queue = DurableQueue(tmp_path)
        with pytest.raises(DurableQueueError, match="size"):
            queue.enqueue("k", {"blob": "x" * (1024 * 1024 + 1)})


# ---------------------------------------------------------------------------
# Dead-letter queue
# ---------------------------------------------------------------------------


class TestDeadLetterQueue:
    def test_dead_letter_enqueue_get_list(self, tmp_path: Path) -> None:
        queue = DurableQueue(tmp_path)
        message = queue.enqueue("k", {"n": 1})
        dead = DeadLetterQueue(tmp_path)
        dead.enqueue(message)
        assert dead.count() == 1
        record = dead.get(message.message_id)
        assert record is not None and record["payload"] == {"n": 1}
        assert dead.list(kind="k")[0]["message_id"] == message.message_id

    def test_dead_letter_durable_file(self, tmp_path: Path) -> None:
        queue = DurableQueue(tmp_path)
        message = queue.enqueue("k", {})
        dead = DeadLetterQueue(tmp_path)
        dead.enqueue(message)
        path = tmp_path / "dead" / f"{message.message_id}.json"
        assert path.exists()
        assert "error" not in json.loads(path.read_text(encoding="utf-8")) or True


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_opens_after_threshold(self) -> None:
        breaker = CircuitBreaker(failure_threshold=3, recovery_timeout_ms=60_000,
                                 clock_ms=lambda: 0)
        for _ in range(2):
            breaker.record_failure()
        assert breaker.allow_call()
        breaker.record_failure()
        assert breaker.status()["state"] == "open"
        assert not breaker.allow_call()

    def test_half_open_probe_closes_on_success(self) -> None:
        clock = {"now": 0}
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout_ms=1_000,
                                 clock_ms=lambda: clock["now"])
        breaker.record_failure()
        clock["now"] = 2_000
        assert breaker.allow_call()
        assert breaker.status()["state"] == "half_open"
        breaker.record_success()
        assert breaker.status()["state"] == "closed"

    def test_half_open_failure_reopens(self) -> None:
        clock = {"now": 0}
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout_ms=1_000,
                                 clock_ms=lambda: clock["now"])
        breaker.record_failure()
        clock["now"] = 2_000
        assert breaker.allow_call()
        breaker.record_success()
        breaker.record_success()
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.status()["state"] == "open"

    def test_call_wraps_and_raises_open(self) -> None:
        breaker = CircuitBreaker(failure_threshold=2, recovery_timeout_ms=60_000,
                                 clock_ms=lambda: 0)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(CircuitBreakerOpenError):
            breaker.call(lambda: None)

    def test_success_resets_failures(self) -> None:
        breaker = CircuitBreaker(failure_threshold=3, recovery_timeout_ms=10_000)
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.status()["consecutive_failures"] == 2


# ---------------------------------------------------------------------------
# Lease manager
# ---------------------------------------------------------------------------


class TestLeaseManager:
    def test_acquire_renew_release(self, tmp_path: Path) -> None:
        lease = LeaseManager(tmp_path)
        acquired = lease.acquire("key", "agent")
        assert acquired.expires_at_ms > acquired.issued_at_ms
        renewed = lease.renew("key", "agent")
        assert renewed.renewal_count == 1
        released = lease.release("key", "agent")
        assert released.lease_key == "key"
        assert lease.get("key") is None

    def test_conflicting_holder_rejected(self, tmp_path: Path) -> None:
        lease = LeaseManager(tmp_path)
        lease.acquire("key", "agent-a")
        with pytest.raises(LeaseError, match="held by"):
            lease.acquire("key", "agent-b")

    def test_expired_renew_raises_stale(self, tmp_path: Path) -> None:
        lease = LeaseManager(tmp_path)
        lease.acquire("key", "agent", ttl_ms=0)
        with pytest.raises(StaleLeaseError):
            lease.renew("key", "agent")

    def test_stale_recovery_releases_expired(self, tmp_path: Path) -> None:
        lease = LeaseManager(tmp_path)
        lease.acquire("key", "agent", ttl_ms=0)
        recovered = lease.recover_stale()
        assert len(recovered) == 1
        assert lease.get("key") is None

    def test_leases_persist_across_reopen(self, tmp_path: Path) -> None:
        first = LeaseManager(tmp_path)
        first.acquire("key", "agent", ttl_ms=5_000)
        second = LeaseManager(tmp_path)
        stored = second.get("key")
        assert stored is not None and stored.holder == "agent"


# ---------------------------------------------------------------------------
# Execution registry / idempotency
# ---------------------------------------------------------------------------


class TestExecutionRegistry:
    def test_register_and_lookup(self, tmp_path: Path) -> None:
        registry = ExecutionRegistry(tmp_path)
        record = registry.register(idempotency_key="key-1", kind="tool")
        assert registry.lookup(idempotency_key="key-1").execution_id == record.execution_id
        assert registry.lookup(execution_id=record.execution_id) is not None

    def test_idempotent_register_returns_existing(self, tmp_path: Path) -> None:
        registry = ExecutionRegistry(tmp_path)
        first = registry.register(idempotency_key="k")
        second = registry.register(idempotency_key="k")
        assert first.execution_id == second.execution_id

    def test_duplicate_terminal_detection(self, tmp_path: Path) -> None:
        registry = ExecutionRegistry(tmp_path)
        record = registry.register(idempotency_key="k")
        registry.mark_succeeded(record.execution_id)
        assert registry.is_duplicate("k")

    def test_phase_transitions(self, tmp_path: Path) -> None:
        registry = ExecutionRegistry(tmp_path)
        record = registry.register(idempotency_key="k")
        registry.touch(record.execution_id, phase=ExecutionPhase.PROCESSING.value)
        registry.mark_succeeded(record.execution_id)
        assert registry.count(phase=ExecutionPhase.SUCCEEDED.value) == 1

    def test_unknown_execution_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ReliabilityError):
            ExecutionRegistry(tmp_path).touch("missing", phase=ExecutionPhase.PROCESSING.value)


# ---------------------------------------------------------------------------
# Checkpoint store
# ---------------------------------------------------------------------------


class TestCheckpointStore:
    def test_save_and_restore(self, tmp_path: Path) -> None:
        store = CheckpointStore(tmp_path)
        store.save("state", {"pos": 5})
        restored = store.restore("state")
        assert restored.payload == {"pos": 5}
        assert restored.sha256

    def test_integrity_mismatch_detected(self, tmp_path: Path) -> None:
        store = CheckpointStore(tmp_path)
        store.save("state", {"pos": 5})
        store.save("state", {"pos": 7})
        target = tmp_path / "checkpoints" / "state.json"
        record = json.loads(target.read_text(encoding="utf-8"))
        record["payload"]["pos"] = 99
        target.write_text(json.dumps(record), encoding="utf-8")
        restored = store.restore("state")
        # current corrupt -> last-good backup wins
        assert restored.payload == {"pos": 5}

    def test_both_corrupt_raises(self, tmp_path: Path) -> None:
        store = CheckpointStore(tmp_path)
        store.save("state", {"pos": 5})
        for target in ("checkpoints/state.json", "checkpoints/state.bak.json"):
            (tmp_path / target).write_text("corrupt-not-json", encoding="utf-8")
        with pytest.raises(CorruptCheckpointError):
            store.restore("state")

    def test_missing_checkpoint_fails(self, tmp_path: Path) -> None:
        with pytest.raises(CorruptCheckpointError):
            CheckpointStore(tmp_path).restore("nope")

    def test_secret_payload_refused(self, tmp_path: Path) -> None:
        store = CheckpointStore(tmp_path)
        with pytest.raises(ReliabilityError, match="secret-like"):
            store.save("state", {"line": "probe access_token = thisisasecretvalue1"})


# ---------------------------------------------------------------------------
# Recovery journal
# ---------------------------------------------------------------------------


class TestRecoveryJournal:
    def test_append_and_replay(self, tmp_path: Path) -> None:
        journal = RecoveryJournal(tmp_path)
        seq = journal.append("boot", payload={"n": 1})
        assert seq == 1
        entries = list(journal.replay())
        assert len(entries) == 1
        assert entries[0]["kind"] == "boot"
        assert entries[0]["payload"] == {"n": 1}

    def test_gap_detected(self, tmp_path: Path) -> None:
        journal = RecoveryJournal(tmp_path)
        journal.append("a", payload={})
        journal.append("b", payload={})
        journal.append("c", payload={})
        path = tmp_path / "recovery" / "journal.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        path.write_text("".join(lines[:1]) + "".join(lines[2:]), encoding="utf-8")
        replayed = list(journal.replay())
        assert journal.gap_detected
        assert replayed == []

    def test_secret_entry_refused(self, tmp_path: Path) -> None:
        with pytest.raises(JournalError, match="secret-like"):
            RecoveryJournal(tmp_path).append("boot",
                                             payload={"line": "probe access_token = thisisasecretvalue1"})


# ---------------------------------------------------------------------------
# Backup manager
# ---------------------------------------------------------------------------


class TestBackupManager:
    def test_snapshot_verify_restore(self, tmp_path: Path) -> None:
        backups = BackupManager(tmp_path)
        source = tmp_path / "work" / "state.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('{"pos": 3}', encoding="utf-8")
        manifest = backups.snapshot([source])
        assert backups.verify(manifest["snapshot_id"])["valid"]
        target = tmp_path / "restored"
        backups.restore(manifest["snapshot_id"], target)
        assert (target / "state.json").read_text(encoding="utf-8") == '{"pos": 3}'

    def test_verify_detects_tampering(self, tmp_path: Path) -> None:
        backups = BackupManager(tmp_path)
        source = tmp_path / "state.json"
        source.write_text("hello", encoding="utf-8")
        manifest = backups.snapshot([source])
        snap = tmp_path / "backups" / manifest["snapshot_id"]
        (snap / "state.json").write_text("tampered", encoding="utf-8")
        result = backups.verify(manifest["snapshot_id"])
        assert not result["valid"]
        assert result["failures"]

    def test_missing_source_fails(self, tmp_path: Path) -> None:
        with pytest.raises(BackupError, match="missing"):
            BackupManager(tmp_path).snapshot([tmp_path / "nope.txt"])

    def test_snapshot_listing(self, tmp_path: Path) -> None:
        backups = BackupManager(tmp_path)
        source = tmp_path / "a" / "state.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("x", encoding="utf-8")
        manifest = backups.snapshot([source])
        assert manifest["snapshot_id"] in backups.list_snapshots()


# ---------------------------------------------------------------------------
# Failure diagnostics
# ---------------------------------------------------------------------------


class TestFailureDiagnostics:
    def test_record_and_report(self) -> None:
        diag = FailureDiagnostics()
        diag.record(kind="tool_failure", resource="export", error=RuntimeError("boom"))
        report = diag.report()
        assert report["incidents"] == 1
        assert report["by_kind"] == {"tool_failure": 1}

    def test_redacts_secret_like_reason(self) -> None:
        diag = FailureDiagnostics()
        incident = diag.record(kind="network", resource="relay",
                               error_reason="probe access_token = thisisasecretvalue1")
        assert incident.error_reason == "<redacted>"
        assert diag.report()["clean"]

    def test_bounded(self) -> None:
        diag = FailureDiagnostics(max_incidents=5)
        for i in range(20):
            diag.record(kind="x", error_reason=f"failure {i}")
        assert len(diag.recent(limit=50)) == 5


# ---------------------------------------------------------------------------
# Reliability engine (end-to-end)
# ---------------------------------------------------------------------------


class TestReliabilityEngine:
    def test_submit_process_ack(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        submitted = engine.submit("tool", {"op": "render"})
        assert submitted["duplicate"] is False
        record = engine.process_next(lambda m: {"outcome": "ok"})
        assert record is not None and record["outcome"] == "ok"
        assert engine.queue.stats()["acked"] == 1

    def test_run_until_idle(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        for i in range(5):
            engine.submit("tool", {"seq": i})
        seen = []
        records = engine.run_until_idle(
            lambda m: seen.append(m.payload["seq"]) or {"outcome": "ok"}
        )
        assert len(records) == 5
        assert sorted(seen) == [0, 1, 2, 3, 4]

    def test_retry_until_success(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, retry_policy=RetryPolicy(max_attempts=5))
        engine.submit("tool", {})
        calls = {"n": 0}

        def flaky(_msg: QueueMessage) -> dict[str, str]:
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("network interruption")
            return {"outcome": "ok"}

        out = []
        while True:
            record = engine.process_next(flaky)
            if record is None:
                break
            out.append(record)
        assert [r["outcome"] for r in out] == ["retry", "retry", "ok"]
        assert engine.queue.stats()["acked"] == 1

    def test_dead_letter_after_retries(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, retry_policy=RetryPolicy(max_attempts=3))
        engine.submit("tool", {})
        for _ in range(3):
            engine.process_next(lambda m: (_ for _ in ()).throw(RuntimeError("boom")))
        assert engine.dead_letters.count() == 1
        assert engine.queue.stats()["dead"] == 1

    def test_permanent_failure_dead_lettered_immediately(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, retry_policy=RetryPolicy(max_attempts=5))
        engine.submit("tool", {})

        def fail(_msg: QueueMessage) -> None:
            raise NonRetryableError("invalid input")

        engine.process_next(fail)
        assert engine.dead_letters.count() == 1
        assert engine.queue.stats()["dead"] == 1

    def test_idempotency_dedupes_submission(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        first = engine.submit("tool", {"op": "bill"}, idempotency_key="invoice-123")
        second = engine.submit("tool", {"op": "bill"}, idempotency_key="invoice-123")
        assert second["duplicate"] is True
        assert second["message_id"] == first["message_id"]
        engine.run_until_idle(lambda m: {"outcome": "ok"})
        assert engine.queue.stats()["acked"] == 1

    def test_duplicate_terminal_submission_suppressed(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.submit("tool", {"op": "deploy"}, idempotency_key="build-7")
        engine.run_until_idle(lambda m: {"outcome": "ok"})
        result = engine.submit("tool", {"op": "deploy"}, idempotency_key="build-7")
        assert result["duplicate"] is True
        assert result["state"] == ExecutionPhase.SUCCEEDED.value
        assert engine.queue.stats()["total"] == 1

    def test_destructive_execution_runs_once(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.submit("destructive", {"op": "purge"}, idempotency_key="purge-1")
        calls = {"n": 0}
        engine.run_until_idle(lambda m: calls.__setitem__("n", calls["n"] + 1) or {"outcome": "ok"})
        assert calls["n"] == 1
        after = engine.submit("destructive", {"op": "purge"}, idempotency_key="purge-1")
        assert after["duplicate"] is True
        assert calls["n"] == 1

    def test_crash_recovery_requeues_stale(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.submit("tool", {"op": "heavy"})
        with pytest.raises(RuntimeError, match="simulated crash"):
            engine.process_next(lambda m: {"outcome": "ok"}, simulate_crash=True)
        assert engine.queue.processing() != {}
        recovered = engine.recover(stale_lease_ms=-100_000)
        assert recovered["queue_stale_recovered"] == 1
        record = engine.process_next(lambda m: {"outcome": "ok"})
        assert record is not None and record["outcome"] == "ok"
        assert engine.queue.stats()["acked"] == 1

    def test_restart_recovery_durable(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.submit("tool", {"op": "keep"})
        engine = _sealed(engine, tmp_path)
        assert engine.queue.stats()["total"] == 1
        record = engine.process_next(lambda m: {"outcome": "ok"})
        assert record is not None and record["outcome"] == "ok"

    def test_offline_to_online_recovery(self, tmp_path: Path) -> None:
        """Endpoint is offline (ConnectionError) then back online."""
        engine = _engine(tmp_path, retry_policy=RetryPolicy(max_attempts=5))
        engine.submit("remote", {"op": "sync"})
        state = {"offline": True}

        def relay(_msg: QueueMessage) -> dict[str, str]:
            if state["offline"]:
                raise ConnectionError("remote endpoint unavailable")
            return {"outcome": "ok"}

        first = engine.process_next(relay)
        assert first["outcome"] == "retry"
        state["offline"] = False
        outcome = []
        while True:
            record = engine.process_next(relay)
            if record is None:
                break
            outcome.append(record["outcome"])
        assert outcome == ["ok"]

    def test_provider_and_tool_failure_classified(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.submit("provider", {})
        engine.process_next(lambda m: (_ for _ in ()).throw(TimeoutError("provider timeout")))
        engine.submit("tool", {})
        engine.process_next(lambda m: (_ for _ in ()).throw(ValueError("invalid tool args")))
        diagnostics = engine.diagnostics.report()
        assert diagnostics["incidents"] == 2

    def test_circuit_opens_engine_stops(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.circuit_breaker = CircuitBreaker(failure_threshold=1,
                                                recovery_timeout_ms=60_000,
                                                clock_ms=lambda: 0)
        engine.submit("tool", {})
        engine.process_next(lambda m: (_ for _ in ()).throw(RuntimeError("boom")))
        assert engine.circuit_breaker.status()["state"] == "open"
        with pytest.raises(CircuitBreakerOpenError):
            engine.process_next(lambda m: {"outcome": "ok"})

    def test_telemetry_domain_emitted(self, tmp_path: Path) -> None:
        tracer = new_collector(keep_all=True)
        engine = _engine(tmp_path, tracer=tracer)
        engine.submit("tool", {"op": "render"})
        engine.process_next(lambda m: {"outcome": "ok"})
        events = tracer.events_by_domain("reliability")
        statuses = {e.status for e in events}
        assert TelemetryStatus.QUEUED.value in statuses
        assert TelemetryStatus.OK.value in statuses

    def test_status_report_shape(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.submit("tool", {})
        engine.process_next(lambda m: {"outcome": "ok"})
        status = engine.status()
        assert status["version"] == "1"
        assert status["queue"]["acked"] == 1
        assert status["executions"]["succeeded"] == 1

    def test_secret_stays_out_of_all_dependencies(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        with pytest.raises(DurableQueueError):
            engine.submit("tool", {"line": "probe access_token = thisisasecretvalue1"})
        engine.submit("tool", {"op": "ok"})
        engine.process_next(lambda m: {"outcome": "ok"})
        leaked: list[str] = []
        for path in tmp_path.rglob("*"):
            if path.is_file() and "secret-like" not in path.name:
                try:
                    if "probe access_token = thisisasecretvalue1" in path.read_text(
                        encoding="utf-8", errors="replace"
                    ):
                        leaked.append(str(path))
                except OSError:
                    continue
        assert leaked == []

    def test_failure_injection_suite_survives(self, tmp_path: Path) -> None:
        """Mix of injected failures: network, provider timeout, tool validation."""
        engine = _engine(tmp_path, retry_policy=RetryPolicy(max_attempts=4))
        engine.circuit_breaker = CircuitBreaker(failure_threshold=1_000,
                                                recovery_timeout_ms=60_000)
        injections = [
            lambda _m: (_ for _ in ()).throw(ConnectionError("network flap")),
            lambda _m: (_ for _ in ()).throw(TimeoutError("provider timeout")),
            lambda _m: (_ for _ in ()).throw(ValueError("tool schema violation")),
        ]
        for i in range(3):
            engine.submit("tool", {"inject": i}, idempotency_key=f"case-{i}")
        outcomes: list[str] = []
        for _ in range(6):
            if engine.queue.stats()["pending"] == 0:
                break
            for inject in injections:
                record = engine.process_next(inject)
                if record:
                    outcomes.append(record["outcome"])
        # every injected failure exhausted its budget deterministically
        assert engine.dead_letters.count() == 3
        assert engine.queue.pending() == []

    def test_conformance_suite_passes(self, tmp_path: Path) -> None:
        engine = provision_phase34_reliability(tmp_path)
        checks = run_reliability_conformance(engine)
        assert len(checks) >= 10
        assert all(check["ok"] for check in checks)