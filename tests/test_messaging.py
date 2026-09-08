"""Phase 26 — Universal Agent-to-Agent Messaging Protocol tests."""

from pathlib import Path

import pytest

from handoff_agent.messaging import (
    MessageBroker,
    MessageEnvelope,
    MessageExpiredError,
    MessageStateError,
    MessageStatus,
    MessageType,
    MessageValidationError,
    MessagingError,
    ReceiverUnavailableError,
    DuplicateMessageError,
    PermissionDeniedMessageError,
    ReplayDetectedError,
    ScopeViolationError,
    TrustViolationError,
    UnknownMessageError,
    create_message,
    _canonical,
)


def _setup(tmp_path: Path, *, agents: tuple[str, ...] = ("agent-a", "agent-b"), **kwargs) -> MessageBroker:
    broker = MessageBroker(str(tmp_path / "messaging"), **kwargs)
    for agent_id in agents:
        broker.register_known_agent(agent_id)
    return broker


def _msg(broker: MessageBroker, sender: str = "agent-a", receiver: str = "agent-b", **kwargs) -> MessageEnvelope:
    envelope = create_message(MessageType.DIRECT.value, sender, receiver_agent_id=receiver, **kwargs)
    return broker.send(envelope, actor=sender)


# ---------------------------------------------------------------------------
# Envelope creation and validation
# ---------------------------------------------------------------------------

class TestMessageEnvelope:
    def test_create_envelope(self) -> None:
        envelope = create_message(
            MessageType.DIRECT.value,
            "agent-a",
            receiver_agent_id="agent-b",
            payload={"key": "value"},
        )
        assert envelope.message_id.startswith("msg-")
        assert envelope.message_type == MessageType.DIRECT.value
        assert envelope.sender_agent_id == "agent-a"
        assert envelope.receiver_agent_id == "agent-b"
        assert envelope.payload == {"key": "value"}
        assert envelope.status == MessageStatus.QUEUED.value
        assert envelope.protocol_version

    def test_envelope_identity(self) -> None:
        e1 = create_message(MessageType.DIRECT.value, "a", receiver_agent_id="b", payload={"x": 1})
        e2 = create_message(MessageType.DIRECT.value, "a", receiver_agent_id="b", payload={"x": 1})
        assert e1.message_id != e2.message_id

    def test_envelope_validation_rejects_empty_id(self) -> None:
        env = MessageEnvelope(message_id="", message_type="direct", sender_agent_id="a", timestamp="2025-01-01T00:00:00+00:00")
        ok, errors = env.validate()
        assert not ok
        assert any("message_id" in e for e in errors)

    def test_envelope_validation_rejects_unknown_type(self) -> None:
        env = MessageEnvelope(message_id="m1", message_type="bogus", sender_agent_id="a", timestamp="2025-01-01T00:00:00+00:00")
        ok, errors = env.validate()
        assert not ok
        assert any("message_type" in e for e in errors)

    def test_envelope_validation_requires_receiver_for_direct(self) -> None:
        env = MessageEnvelope(
            message_id="m1", message_type="direct", sender_agent_id="a",
            timestamp="2025-01-01T00:00:00+00:00", receiver_agent_id="",
        )
        ok, errors = env.validate()
        assert not ok
        assert any("receiver_agent_id" in e for e in errors)

    def test_envelope_validation_priority_bounds(self) -> None:
        env = MessageEnvelope(message_id="m1", message_type="event", sender_agent_id="a", timestamp="2025-01-01T00:00:00+00:00", priority=0)
        ok, errors = env.validate()
        assert not ok
        assert any("priority" in e for e in errors)

    def test_envelope_with_(self) -> None:
        env = create_message(MessageType.DIRECT.value, "a", receiver_agent_id="b")
        updated = env.with_(priority=1)
        assert updated.priority == 1
        assert updated.message_id == env.message_id

    def test_envelope_serialization_roundtrip(self) -> None:
        env = create_message(MessageType.DIRECT.value, "a", receiver_agent_id="b", payload={"data": 42})
        data = env.to_dict()
        restored = MessageEnvelope.from_dict(data)
        assert restored.message_id == env.message_id
        assert restored.payload == {"data": 42}
        assert restored.status == env.status

    def test_is_expired(self) -> None:
        env = create_message(MessageType.DIRECT.value, "a", receiver_agent_id="b", ttl_seconds=10)
        assert not env.is_expired(env.timestamp)
        from datetime import datetime, timezone, timedelta
        later = (datetime.fromisoformat(env.timestamp) + timedelta(seconds=11)).isoformat()
        assert env.is_expired(later)

    def test_ttl_zero_never_expires(self) -> None:
        env = create_message(MessageType.DIRECT.value, "a", receiver_agent_id="b", ttl_seconds=0)
        from datetime import datetime, timezone, timedelta
        later = (datetime.fromisoformat(env.timestamp) + timedelta(days=365)).isoformat()
        assert not env.is_expired(later)


# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------

class TestMessageTypes:
    def test_all_message_types_exist(self) -> None:
        expected = {
            "direct", "broadcast", "targeted", "request_response", "event",
            "task_delegation", "task_result", "checkpoint", "handoff",
            "capability_negotiation", "approval_request", "approval_response",
            "error", "heartbeat", "workflow_event",
        }
        actual = {mt.value for mt in MessageType}
        assert expected == actual

    def test_direct_messaging(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = broker.send_direct("agent-a", "agent-b", {"msg": "hello"})
        assert env.status == MessageStatus.QUEUED.value
        assert env.receiver_agent_id == "agent-b"

    def test_broadcast_messaging(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path, agents=("agent-a", "agent-b", "agent-c"))
        sent = broker.broadcast("agent-a", ("agent-b", "agent-c"), {"announcement": "hi"})
        assert len(sent) == 2
        assert all(m.sender_agent_id == "agent-a" for m in sent)

    def test_targeted_messaging(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = broker.send_targeted("agent-a", "agent-b", {"task": "deploy"}, capabilities=("checkpoint.create",))
        assert env.message_type == MessageType.TARGETED.value
        assert env.capabilities == ("checkpoint.create",)

    def test_request_response_messaging(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        req = broker.send_request("agent-a", "agent-b", {"question": "status?"})
        resp = broker.send_response("agent-b", "agent-a", req.message_id, {"answer": "ok"})
        assert resp.correlation_id == req.correlation_id
        assert resp.acknowledgement_id == req.message_id

    def test_event_messaging(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = broker.send_event("agent-a", "deployment.started", {"env": "staging"}, workflow_id="w1")
        assert env.message_type == MessageType.EVENT.value
        assert env.payload["event_type"] == "deployment.started"

    def test_task_delegation_messaging(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = broker.send_task_delegation("agent-a", "agent-b", "task-1", {"instructions": "build"})
        assert env.message_type == MessageType.TASK_DELEGATION.value
        assert env.task_id == "task-1"

    def test_task_result_messaging(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = broker.send_task_result("agent-b", "agent-a", "task-1", {"output": "done"})
        assert env.message_type == MessageType.TASK_RESULT.value

    def test_checkpoint_messaging(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = broker.send_checkpoint("agent-a", {"state": "checkpointed"})
        assert env.message_type == MessageType.CHECKPOINT.value
        assert env.checkpoint == {"state": "checkpointed"}

    def test_handoff_messaging(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = agent = broker.send_handoff("agent-a", {"handoff_data": "transfer"})
        assert env.message_type == MessageType.HANDOFF.value
        assert env.handoff == {"handoff_data": "transfer"}

    def test_capability_negotiation_messaging(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = broker.send_capability_negotiation("agent-a", "agent-b", ("checkpoint.read", "checkpoint.create"))
        assert env.message_type == MessageType.CAPABILITY_NEGOTIATION.value
        assert "checkpoint.read" in env.capabilities

    def test_approval_request_messaging(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = broker.send_approval_request("agent-a", "agent-b", "task-1", {"action": "deploy"})
        assert env.message_type == MessageType.APPROVAL_REQUEST.value
        assert env.task_id == "task-1"

    def test_approval_response_messaging(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        req = broker.send_approval_request("agent-a", "agent-b", "task-1", {"action": "deploy"})
        resp = broker.send_approval_response("agent-b", "agent-a", req.message_id, approved=True, reason="looks good")
        assert resp.message_type == MessageType.APPROVAL_RESPONSE.value
        assert resp.payload["approved"] is True

    def test_error_messaging(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = broker.send_error("agent-a", "agent-b", "E001", "something broke")
        assert env.message_type == MessageType.ERROR.value
        assert env.payload["error_code"] == "E001"

    def test_heartbeat_messaging(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = broker.send_heartbeat("agent-a", status_detail="all good")
        assert env.message_type == MessageType.HEARTBEAT.value
        assert env.ttl_seconds == 60

    def test_workflow_event_messaging(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = broker.send_workflow_event("agent-a", "wf-1", "started", {"step": 1})
        assert env.message_type == MessageType.WORKFLOW_EVENT.value
        assert env.workflow_id == "wf-1"


# ---------------------------------------------------------------------------
# Message lifecycle (states)
# ---------------------------------------------------------------------------

class TestMessageLifecycle:
    def test_full_lifecycle(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = _msg(broker)
        assert env.status == MessageStatus.QUEUED.value
        env = broker.deliver(env.message_id, actor="agent-b")
        assert env.status == MessageStatus.DELIVERED.value
        env = broker.acknowledge(env.message_id, actor="agent-b")
        assert env.status == MessageStatus.ACKNOWLEDGED.value
        env = broker.process(env.message_id, actor="agent-b")
        assert env.status == MessageStatus.PROCESSING.value
        env = broker.complete(env.message_id, actor="agent-b")
        assert env.status == MessageStatus.COMPLETED.value

    def test_lifecycle_failed(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = _msg(broker)
        env = broker.deliver(env.message_id)
        env = broker.fail(env.message_id, "error occurred")
        assert env.status == MessageStatus.FAILED.value
        assert env.error_detail == "error occurred"

    def test_lifecycle_cancelled(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = _msg(broker)
        env = broker.cancel(env.message_id, reason="no longer needed")
        assert env.status == MessageStatus.CANCELLED.value

    def test_lifecycle_expired(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        from datetime import datetime, timezone, timedelta
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        env = create_message(MessageType.DIRECT.value, "agent-a", receiver_agent_id="agent-b", ttl_seconds=1)
        env = env.with_(timestamp=old_ts)
        env = broker.send(env)
        stale = broker.detect_stale()
        assert any(m.message_id == env.message_id for m in stale)

    def test_invalid_transition_rejected(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = _msg(broker)
        with pytest.raises(MessageStateError):
            broker.acknowledge(env.message_id)  # can't ack queued

    def test_complete_records_timestamp(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = _msg(broker)
        env = broker.deliver(env.message_id)
        env = broker.acknowledge(env.message_id)
        env = broker.process(env.message_id)
        env = broker.complete(env.message_id)
        assert env.completed_at

    def test_complete_with_result(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = _msg(broker)
        env = broker.deliver(env.message_id)
        env = broker.acknowledge(env.message_id)
        env = broker.process(env.message_id)
        env = broker.complete(env.message_id, result={"output": "done"})
        assert env.payload.get("output") == "done"


# ---------------------------------------------------------------------------
# ACK mechanism
# ---------------------------------------------------------------------------

class TestAckMechanism:
    def test_acknowledge_records_time(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = _msg(broker)
        env = broker.deliver(env.message_id)
        env = broker.acknowledge(env.message_id, actor="agent-b")
        assert env.acknowledged_at
        assert env.acknowledgement_id == env.message_id

    def test_duplicate_ack_rejected(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = _msg(broker)
        broker.deliver(env.message_id)
        broker.acknowledge(env.message_id)
        with pytest.raises(MessageStateError):
            broker.acknowledge(env.message_id)


# ---------------------------------------------------------------------------
# Sender / receiver authorization
# ---------------------------------------------------------------------------

class TestAuthorization:
    def test_unauthorized_sender_rejected(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path, agents=("agent-b",))
        env = create_message(MessageType.DIRECT.value, "unknown-agent", receiver_agent_id="agent-b")
        with pytest.raises(PermissionDeniedMessageError):
            broker.send(env)

    def test_unauthorized_receiver_rejected(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path, agents=("agent-a",))
        env = create_message(MessageType.DIRECT.value, "agent-a", receiver_agent_id="unknown-agent")
        with pytest.raises(ReceiverUnavailableError):
            broker.send(env)

    def test_authorization_bypass(self, tmp_path: Path) -> None:
        broker = MessageBroker(
            str(tmp_path / "messaging"),
            require_sender_authorization=False,
            require_receiver_authorization=False,
        )
        env = create_message(MessageType.DIRECT.value, "anyone", receiver_agent_id="anyone")
        sent = broker.send(env)
        assert sent.status == MessageStatus.QUEUED.value

    def test_register_unregister_agent(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        broker.register_known_agent("agent-c")
        assert "agent-c" in broker.known_agents()
        broker.unregister_known_agent("agent-c")
        assert "agent-c" not in broker.known_agents()


# ---------------------------------------------------------------------------
# Scope validation
# ---------------------------------------------------------------------------

class TestScopeValidation:
    def test_project_scope_violation(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = create_message(MessageType.DIRECT.value, "agent-a", receiver_agent_id="agent-b", project_id="secret-project")
        with pytest.raises(ScopeViolationError):
            broker.send(env, allowed_projects=frozenset({"public-project"}))

    def test_task_scope_violation(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = create_message(MessageType.DIRECT.value, "agent-a", receiver_agent_id="agent-b", task_id="task-99")
        with pytest.raises(ScopeViolationError):
            broker.send(env, allowed_tasks=frozenset({"task-1"}))

    def test_scope_passes_within_bounds(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = create_message(MessageType.DIRECT.value, "agent-a", receiver_agent_id="agent-b", project_id="p1")
        sent = broker.send(env, allowed_projects=frozenset({"p1"}))
        assert sent.status == MessageStatus.QUEUED.value


# ---------------------------------------------------------------------------
# Message ordering
# ---------------------------------------------------------------------------

class TestMessageOrdering:
    def test_sequence_increments(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        m1 = broker.send_direct("agent-a", "agent-b", {"seq": 1})
        m2 = broker.send_direct("agent-a", "agent-b", {"seq": 2})
        assert m2.sequence > m1.sequence

    def test_independent_sequences(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        m1 = broker.send_direct("agent-a", "agent-b", {"seq": 1})
        m2 = broker.send_direct("agent-b", "agent-a", {"seq": 1})
        assert m1.sequence == m2.sequence

    def test_verify_sequence(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        m1 = broker.send_direct("agent-a", "agent-b", {"seq": 1})
        assert broker.verify_sequence("agent-a", "agent-b", m1.sequence)


# ---------------------------------------------------------------------------
# Duplicate detection / idempotency
# ---------------------------------------------------------------------------

class TestDuplicateDetection:
    def test_duplicate_message_id_rejected(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env1 = create_message(MessageType.DIRECT.value, "agent-a", receiver_agent_id="agent-b", payload={"x": 1})
        broker.send(env1)
        env2 = env1.with_(idempotency_key="different-key")
        with pytest.raises(DuplicateMessageError):
            broker.send(env2)

    def test_check_duplicate(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = _msg(broker)
        assert broker.check_duplicate(env.message_id)
        assert not broker.check_duplicate("nonexistent-id")

    def test_idempotency_key_lookup(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = _msg(broker, idempotency_key="idem-123")
        found = broker.get_by_idempotency_key("idem-123")
        assert found is not None
        assert found.message_id == env.message_id

    def test_idempotent_duplicate_returns_existing(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = create_message(MessageType.DIRECT.value, "agent-a", receiver_agent_id="agent-b", idempotency_key="idem-1")
        sent1 = broker.send(env)
        sent2 = broker.send(env)
        assert sent1.message_id == sent2.message_id


# ---------------------------------------------------------------------------
# Replay protection
# ---------------------------------------------------------------------------

class TestReplayProtection:
    def test_same_idempotency_key_same_message(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = create_message(MessageType.DIRECT.value, "agent-a", receiver_agent_id="agent-b", idempotency_key="replay-1")
        sent1 = broker.send(env)
        sent2 = broker.send(env)
        assert sent1.message_id == sent2.message_id


# ---------------------------------------------------------------------------
# Stale / expired message detection
# ---------------------------------------------------------------------------

class TestStaleExpired:
    def test_stale_detection(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        from datetime import datetime, timezone, timedelta
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        env = create_message(MessageType.DIRECT.value, "agent-a", receiver_agent_id="agent-b", ttl_seconds=1)
        env = env.with_(timestamp=old_ts)
        sent = broker.send(env)
        stale = broker.detect_stale()
        assert any(m.message_id == sent.message_id for m in stale)
        assert broker.get_status(sent.message_id) == MessageStatus.EXPIRED.value

    def test_non_expired_not_detected(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = _msg(broker, ttl_seconds=3600)
        stale = broker.detect_stale()
        assert not any(m.message_id == env.message_id for m in stale)


# ---------------------------------------------------------------------------
# Retry / timeout
# ---------------------------------------------------------------------------

class TestRetryTimeout:
    def test_retry_failed_message(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = _msg(broker)
        broker.fail(env.message_id, "timeout")
        retried = broker.retry(env.message_id)
        assert retried.status == MessageStatus.QUEUED.value
        assert retried.retry_count == 1

    def test_retry_max_exceeded(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = _msg(broker, max_retries=1)
        broker.fail(env.message_id, "error")
        broker.retry(env.message_id)
        broker.fail(env.message_id, "error again")
        with pytest.raises(MessageStateError):
            broker.retry(env.message_id)

    def test_retry_non_retryable_rejected(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = _msg(broker)
        with pytest.raises(MessageStateError):
            broker.retry(env.message_id)

    def test_detect_delivery_failures(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = _msg(broker)
        broker.deliver(env.message_id)
        env2 = _msg(broker, payload={"more": "data"})
        failures = broker.detect_delivery_failures()
        assert env2.message_id in [f.message_id for f in failures]

    def test_retry_stuck_messages(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = _msg(broker)
        broker.deliver(env.message_id)
        env2 = _msg(broker, payload={"retry": "me"})
        retried = broker.retry_stuck_messages()
        assert any(m.message_id == env2.message_id for m in retried)

    def test_handle_unavailable_agent(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path, agents=("agent-a", "agent-b"))
        _msg(broker, receiver="agent-b")
        _msg(broker, receiver="agent-b", payload={"more": "data"})
        cancelled = broker.handle_unavailable_agent("agent-b")
        assert len(cancelled) == 2
        assert all(m.status == MessageStatus.CANCELLED.value for m in cancelled)


# ---------------------------------------------------------------------------
# Unavailable agent handling
# ---------------------------------------------------------------------------

class TestUnavailableAgent:
    def test_cancel_pending_to_unavailable(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path, agents=("agent-a", "agent-b"))
        m1 = _msg(broker, receiver="agent-b")
        m2 = _msg(broker, receiver="agent-b")
        broker.deliver(m1.message_id)
        cancelled = broker.handle_unavailable_agent("agent-b")
        assert len(cancelled) == 1
        assert cancelled[0].message_id == m2.message_id


# ---------------------------------------------------------------------------
# Message persistence / recovery
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_persistence_survives_reload(self, tmp_path: Path) -> None:
        broker1 = _setup(tmp_path)
        env = _msg(broker1)
        broker2 = MessageBroker(str(tmp_path / "messaging"))
        restored = broker2.get(env.message_id)
        assert restored.message_id == env.message_id

    def test_persistence_known_agents(self, tmp_path: Path) -> None:
        broker1 = _setup(tmp_path)
        broker1.register_known_agent("agent-d")
        broker2 = MessageBroker(str(tmp_path / "messaging"))
        assert "agent-d" in broker2.known_agents()

    def test_recover(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        _msg(broker)
        result = broker.recover()
        assert result["ok"]


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

class TestAuditTrail:
    def test_audit_trail_records_events(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        _msg(broker)
        env = broker.list_messages()[0]
        broker.deliver(env.message_id)
        trail = broker.audit_trail()
        assert len(trail) >= 2
        actions = {e["action"] for e in trail}
        assert "message.send" in actions
        assert "message.delivered" in actions

    def test_secret_in_detail_redacted(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = _msg(broker)
        broker.fail(env.message_id, "api_key=abcdefghijklmnop0123456789")
        trail = broker.audit_trail()
        failed_events = [e for e in trail if e["action"] == "message.failed"]
        assert failed_events
        assert "[redacted]" in failed_events[0]["detail"]


# ---------------------------------------------------------------------------
# Context propagation
# ---------------------------------------------------------------------------

class TestContextPropagation:
    def test_context_propagation(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = _msg(broker, workflow_id="w1", task_id="t1", project_id="p1", context={"step": 1})
        ctx = broker.context_propagation(env.message_id)
        assert ctx["workflow_id"] == "w1"
        assert ctx["task_id"] == "t1"
        assert ctx["project_id"] == "p1"
        assert ctx["context"] == {"step": 1}

    def test_handoff_propagation(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = _msg(broker, handoff={"transfer": "data"})
        ho = broker.handoff_propagation(env.message_id)
        assert ho["handoff"] == {"transfer": "data"}

    def test_checkpoint_propagation(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = _msg(broker, checkpoint={"state": "saved"})
        cp = broker.checkpoint_propagation(env.message_id)
        assert cp["checkpoint"] == {"state": "saved"}

    def test_capability_propagation(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = _msg(broker, capabilities=("cap-a", "cap-b"))
        cap = broker.capability_propagation(env.message_id)
        assert "cap-a" in cap["capabilities"]


# ---------------------------------------------------------------------------
# Permission / trust validation
# ---------------------------------------------------------------------------

class TestPermissionTrust:
    def test_unknown_message_raises(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        with pytest.raises(UnknownMessageError):
            broker.get("nonexistent")

    def test_unknown_deliver_raises(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        with pytest.raises(UnknownMessageError):
            broker.deliver("nonexistent")


# ---------------------------------------------------------------------------
# Security: no arbitrary command execution, no unrestricted filesystem, no unrestricted Git, no approval bypass, no security bypass
# ---------------------------------------------------------------------------

class TestSecurity:
    def test_no_secret_in_messages(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = create_message(MessageType.DIRECT.value, "agent-a", receiver_agent_id="agent-b", payload={"key": "value"})
        sent = broker.send(env)
        serialized = _canonical(sent.to_dict())
        assert "sk-" not in serialized
        assert "password" not in serialized.lower()

    def test_secret_content_rejected(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        with pytest.raises(MessageValidationError):
            env = create_message(MessageType.DIRECT.value, "agent-a", receiver_agent_id="agent-b", payload={"api_key": "sk-1234567890abcdef"})
            broker.send(env)


# ---------------------------------------------------------------------------
# CLI / API / MCP interfaces
# ---------------------------------------------------------------------------

class TestInterfaces:
    def test_cli_payload(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        _msg(broker)
        payload = broker.cli_payload()
        assert "messages" in payload
        assert "count" in payload
        assert "health" in payload

    def test_api_payload(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = _msg(broker)
        payload = broker.api_payload()
        assert "messages" in payload
        payload_single = broker.api_payload(env.message_id)
        assert "message" in payload_single
        assert "context" in payload_single

    def test_mcp_payload(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        _msg(broker)
        payload = broker.mcp_payload()
        assert "messages" in payload
        assert "health" in payload


# ---------------------------------------------------------------------------
# Health check / diagnostics / report
# ---------------------------------------------------------------------------

class TestHealthDiagnostics:
    def test_health_check_ok(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        health = broker.health_check()
        assert health["ok"]

    def test_health_check_fails_with_errors(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = _msg(broker)
        broker.fail(env.message_id, "error")
        health = broker.health_check()
        assert not health["ok"]

    def test_diagnostics(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        diag = broker.diagnostics()
        assert "total_messages" in diag
        assert "integrity" in diag

    def test_report(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        _msg(broker)
        report = broker.report()
        assert report["total"] >= 1
        assert "by_status" in report
        assert "by_type" in report


# ---------------------------------------------------------------------------
# Listing / filtering
# ---------------------------------------------------------------------------

class TestListing:
    def test_list_messages_by_sender(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path, agents=("agent-a", "agent-b", "agent-c"))
        broker.send_direct("agent-a", "agent-b", {"m": 1})
        broker.send_direct("agent-c", "agent-b", {"m": 2})
        results = broker.list_messages(sender="agent-a")
        assert len(results) == 1
        assert results[0].sender_agent_id == "agent-a"

    def test_list_messages_by_status(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        env = _msg(broker)
        broker.deliver(env.message_id)
        results = broker.list_messages(status=MessageStatus.DELIVERED.value)
        assert len(results) == 1

    def test_list_messages_by_type(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        broker.send_heartbeat("agent-a")
        broker.send_direct("agent-a", "agent-b", {"x": 1})
        results = broker.list_messages(message_type=MessageType.HEARTBEAT.value)
        assert len(results) == 1

    def test_count(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        _msg(broker)
        assert broker.count() == 1
        assert broker.count(sender="nonexistent") == 0


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

class TestEvents:
    def test_events_method(self, tmp_path: Path) -> None:
        broker = _setup(tmp_path)
        _msg(broker)
        events = broker.events()
        assert len(events) >= 1
