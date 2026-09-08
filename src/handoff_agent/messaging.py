"""Phase 26 — Universal Agent-to-Agent Messaging Protocol.

A fully validated, persistent, audit-trailed messaging envelope that routes
messages between agents with ordering, idempotency, replay protection,
delivery retry, timeout, and comprehensive security enforcement.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from handoff_agent.constants import HANDOFF_HOME
from handoff_agent.persistence import _atomic_write_file, _contains_secret_like_content

MESSAGING_PROTOCOL_VERSION = "1"

_MAX_TTL_SECONDS = 86400
_DEFAULT_TTL_SECONDS = 3600
_MAX_PRIORITY = 10
_MIN_PRIORITY = 1
_DEFAULT_PRIORITY = 5
_MAX_RETRIES = 5
_RETRY_BACKOFF_BASE = 1.0


class MessagingError(Exception):
    """Base error for messaging operations."""


class MessageValidationError(MessagingError):
    """A message failed envelope validation."""


class MessageStateError(MessagingError):
    """An illegal state transition was attempted."""


class UnknownMessageError(MessagingError):
    """No message matches the requested id."""


class DuplicateMessageError(MessagingError):
    """A message with this id was already received (idempotent)."""


class MessageExpiredError(MessagingError):
    """A message exceeded its TTL before delivery."""


class MessageStaleError(MessagingError):
    """A message refers to a superseded execution or sequence."""


class ReceiverUnavailableError(MessagingError):
    """The target agent is not reachable or not registered."""


class PermissionDeniedMessageError(MessagingError):
    """The operation violates the permission boundary."""


class TrustViolationError(MessagingError):
    """The sender trust level is insufficient for this message type."""


class ScopeViolationError(MessagingError):
    """The message targets a project/task outside the permitted scope."""


class ReplayDetectedError(MessagingError):
    """A replayed message was detected and rejected."""


class CorruptionError(MessagingError):
    """Persisted messaging state is unreadable or invalid."""


# ---------------------------------------------------------------------------
# Message lifecycle
# ---------------------------------------------------------------------------

class MessageStatus(Enum):
    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    ACKNOWLEDGED = "acknowledged"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


_VALID_TRANSITIONS: dict[str, tuple[str, ...]] = {
    MessageStatus.QUEUED.value: (MessageStatus.SENT.value, MessageStatus.CANCELLED.value, MessageStatus.EXPIRED.value),
    MessageStatus.SENT.value: (MessageStatus.DELIVERED.value, MessageStatus.FAILED.value, MessageStatus.EXPIRED.value, MessageStatus.CANCELLED.value),
    MessageStatus.DELIVERED.value: (MessageStatus.ACKNOWLEDGED.value, MessageStatus.FAILED.value, MessageStatus.CANCELLED.value),
    MessageStatus.ACKNOWLEDGED.value: (MessageStatus.PROCESSING.value, MessageStatus.FAILED.value, MessageStatus.CANCELLED.value),
    MessageStatus.PROCESSING.value: (MessageStatus.COMPLETED.value, MessageStatus.FAILED.value, MessageStatus.CANCELLED.value),
    MessageStatus.COMPLETED.value: (),
    MessageStatus.FAILED.value: (MessageStatus.QUEUED.value,),
    MessageStatus.EXPIRED.value: (MessageStatus.QUEUED.value,),
    MessageStatus.CANCELLED.value: (MessageStatus.QUEUED.value,),
}

_TERMINAL = frozenset({MessageStatus.COMPLETED.value})


class MessageType(Enum):
    DIRECT = "direct"
    BROADCAST = "broadcast"
    TARGETED = "targeted"
    REQUEST_RESPONSE = "request_response"
    EVENT = "event"
    TASK_DELEGATION = "task_delegation"
    TASK_RESULT = "task_result"
    CHECKPOINT = "checkpoint"
    HANDOFF = "handoff"
    CAPABILITY_NEGOTIATION = "capability_negotiation"
    APPROVAL_REQUEST = "approval_request"
    APPROVAL_RESPONSE = "approval_response"
    ERROR = "error"
    HEARTBEAT = "heartbeat"
    WORKFLOW_EVENT = "workflow_event"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _make_message_id(sender: str, timestamp: str, payload_hash: str) -> str:
    raw = f"{sender}:{timestamp}:{payload_hash}"
    return "msg-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Message envelope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MessageEnvelope:
    """Universal message envelope for agent-to-agent communication."""

    message_id: str
    message_type: str
    protocol_version: str = MESSAGING_PROTOCOL_VERSION
    sender_agent_id: str = ""
    receiver_agent_id: str = ""
    requester_agent_id: str = ""
    workflow_id: str = ""
    task_id: str = ""
    project_id: str = ""
    execution_id: str = ""
    correlation_id: str = ""
    request_id: str = ""
    timestamp: str = ""
    priority: int = _DEFAULT_PRIORITY
    ttl_seconds: int = _DEFAULT_TTL_SECONDS
    sequence: int = 0
    acknowledgement_id: str = ""
    status: str = MessageStatus.QUEUED.value
    payload: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    handoff: dict[str, Any] = field(default_factory=dict)
    checkpoint: dict[str, Any] = field(default_factory=dict)
    capabilities: tuple[str, ...] = ()
    retry_count: int = 0
    max_retries: int = _MAX_RETRIES
    last_retry_at: str = ""
    delivered_at: str = ""
    acknowledged_at: str = ""
    completed_at: str = ""
    error_detail: str = ""
    idempotency_key: str = ""

    def validate(self) -> tuple[bool, list[str]]:
        errors: list[str] = []
        if not self.message_id:
            errors.append("message_id is required")
        if not self.message_type:
            errors.append("message_type is required")
        try:
            MessageType(self.message_type)
        except ValueError:
            errors.append(f"unknown message_type: {self.message_type!r}")
        if not self.protocol_version:
            errors.append("protocol_version is required")
        if not self.sender_agent_id:
            errors.append("sender_agent_id is required")
        if self.message_type in (MessageType.DIRECT.value, MessageType.TARGETED.value, MessageType.REQUEST_RESPONSE.value):
            if not self.receiver_agent_id:
                errors.append(f"receiver_agent_id is required for {self.message_type}")
        if self.priority < _MIN_PRIORITY or self.priority > _MAX_PRIORITY:
            errors.append(f"priority must be between {_MIN_PRIORITY} and {_MAX_PRIORITY}")
        if self.ttl_seconds < 0 or self.ttl_seconds > _MAX_TTL_SECONDS:
            errors.append(f"ttl_seconds must be between 0 and {_MAX_TTL_SECONDS}")
        if self.sequence < 0:
            errors.append("sequence must be non-negative")
        if self.max_retries < 0 or self.max_retries > _MAX_RETRIES:
            errors.append(f"max_retries must be between 0 and {_MAX_RETRIES}")
        try:
            MessageStatus(self.status)
        except ValueError:
            errors.append(f"unknown status: {self.status!r}")
        if not self.timestamp:
            errors.append("timestamp is required")
        else:
            try:
                datetime.fromisoformat(self.timestamp)
            except ValueError:
                errors.append("timestamp must be a valid ISO-8601 string")
        secret_scan = _canonical(self.to_dict())
        if _contains_secret_like_content(secret_scan):
            errors.append("message contains secret-like values")
        return (not errors, errors)

    def is_expired(self, now: str | None = None) -> bool:
        if self.ttl_seconds <= 0:
            return False
        try:
            created = datetime.fromisoformat(self.timestamp)
            reference = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
            elapsed = (reference - created).total_seconds()
            return elapsed > self.ttl_seconds
        except ValueError:
            return False

    def with_(self, **changes: Any) -> "MessageEnvelope":
        data = self.to_dict()
        data.update(changes)
        return MessageEnvelope.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "message_type": self.message_type,
            "protocol_version": self.protocol_version,
            "sender_agent_id": self.sender_agent_id,
            "receiver_agent_id": self.receiver_agent_id,
            "requester_agent_id": self.requester_agent_id,
            "workflow_id": self.workflow_id,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "execution_id": self.execution_id,
            "correlation_id": self.correlation_id,
            "request_id": self.request_id,
            "timestamp": self.timestamp,
            "priority": self.priority,
            "ttl_seconds": self.ttl_seconds,
            "sequence": self.sequence,
            "acknowledgement_id": self.acknowledgement_id,
            "status": self.status,
            "payload": dict(self.payload),
            "context": dict(self.context),
            "handoff": dict(self.handoff),
            "checkpoint": dict(self.checkpoint),
            "capabilities": list(self.capabilities),
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "last_retry_at": self.last_retry_at,
            "delivered_at": self.delivered_at,
            "acknowledged_at": self.acknowledged_at,
            "completed_at": self.completed_at,
            "error_detail": self.error_detail,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MessageEnvelope":
        raw = dict(data)
        return cls(
            message_id=str(raw.get("message_id", "")),
            message_type=str(raw.get("message_type", "")),
            protocol_version=str(raw.get("protocol_version", MESSAGING_PROTOCOL_VERSION)),
            sender_agent_id=str(raw.get("sender_agent_id", "")),
            receiver_agent_id=str(raw.get("receiver_agent_id", "")),
            requester_agent_id=str(raw.get("requester_agent_id", "")),
            workflow_id=str(raw.get("workflow_id", "")),
            task_id=str(raw.get("task_id", "")),
            project_id=str(raw.get("project_id", "")),
            execution_id=str(raw.get("execution_id", "")),
            correlation_id=str(raw.get("correlation_id", "")),
            request_id=str(raw.get("request_id", "")),
            timestamp=str(raw.get("timestamp", "")),
            priority=int(raw.get("priority", _DEFAULT_PRIORITY)),
            ttl_seconds=int(raw.get("ttl_seconds", _DEFAULT_TTL_SECONDS)),
            sequence=int(raw.get("sequence", 0)),
            acknowledgement_id=str(raw.get("acknowledgement_id", "")),
            status=str(raw.get("status", MessageStatus.QUEUED.value)),
            payload=dict(raw.get("payload", {})),
            context=dict(raw.get("context", {})),
            handoff=dict(raw.get("handoff", {})),
            checkpoint=dict(raw.get("checkpoint", {})),
            capabilities=tuple(raw.get("capabilities", ())),
            retry_count=int(raw.get("retry_count", 0)),
            max_retries=int(raw.get("max_retries", _MAX_RETRIES)),
            last_retry_at=str(raw.get("last_retry_at", "")),
            delivered_at=str(raw.get("delivered_at", "")),
            acknowledged_at=str(raw.get("acknowledged_at", "")),
            completed_at=str(raw.get("completed_at", "")),
            error_detail=str(raw.get("error_detail", "")),
            idempotency_key=str(raw.get("idempotency_key", "")),
        )


# ---------------------------------------------------------------------------
# Message builder helpers
# ---------------------------------------------------------------------------

def create_message(
    message_type: str,
    sender_agent_id: str,
    *,
    receiver_agent_id: str = "",
    requester_agent_id: str = "",
    workflow_id: str = "",
    task_id: str = "",
    project_id: str = "",
    execution_id: str = "",
    correlation_id: str = "",
    request_id: str = "",
    priority: int = _DEFAULT_PRIORITY,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    sequence: int = 0,
    payload: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    handoff: dict[str, Any] | None = None,
    checkpoint: dict[str, Any] | None = None,
    capabilities: tuple[str, ...] = (),
    idempotency_key: str = "",
    max_retries: int = _MAX_RETRIES,
    acknowledgement_id: str = "",
) -> MessageEnvelope:
    """Build and validate a new message envelope."""
    ts = _now_iso()
    payload_data = payload or {}
    ph = _payload_hash(payload_data)
    message_id = _make_message_id(sender_agent_id, ts, ph)
    envelope = MessageEnvelope(
        message_id=message_id,
        message_type=message_type,
        sender_agent_id=sender_agent_id,
        receiver_agent_id=receiver_agent_id,
        requester_agent_id=requester_agent_id or sender_agent_id,
        workflow_id=workflow_id,
        task_id=task_id,
        project_id=project_id,
        execution_id=execution_id,
        correlation_id=correlation_id or message_id,
        request_id=request_id or message_id,
        timestamp=ts,
        priority=priority,
        ttl_seconds=ttl_seconds,
        sequence=sequence,
        payload=payload_data,
        context=context or {},
        handoff=handoff or {},
        checkpoint=checkpoint or {},
        capabilities=capabilities,
        idempotency_key=idempotency_key or message_id,
        max_retries=max_retries,
        acknowledgement_id=acknowledgement_id,
    )
    ok, errors = envelope.validate()
    if not ok:
        raise MessageValidationError("; ".join(errors))
    return envelope


# ---------------------------------------------------------------------------
# Message broker / router
# ---------------------------------------------------------------------------

class MessageBroker:
    """Persistent, security-enforced agent-to-agent message broker."""

    def __init__(
        self,
        state_dir: str | Path | None = None,
        *,
        require_sender_authorization: bool = True,
        require_receiver_authorization: bool = True,
    ) -> None:
        self.state_dir = Path(state_dir or HANDOFF_HOME / "messaging")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.require_sender_authorization = require_sender_authorization
        self.require_receiver_authorization = require_receiver_authorization
        self._messages: dict[str, MessageEnvelope] = {}
        self._sequences: dict[str, int] = {}
        self._events: list[dict[str, str]] = []
        self._known_agents: set[str] = set()
        self._load()

    # -- persistence --------------------------------------------------------

    @property
    def path(self) -> Path:
        return self.state_dir / "messaging.json"

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise CorruptionError("messaging state is not an object")
            for mid, raw in data.get("messages", {}).items():
                self._messages[mid] = MessageEnvelope.from_dict(raw)
            self._sequences = {k: int(v) for k, v in data.get("sequences", {}).items()}
            self._known_agents = set(data.get("known_agents", []))
            self._events = [dict(e) for e in data.get("events", [])]
        except (OSError, ValueError, TypeError, KeyError, CorruptionError) as exc:
            if isinstance(exc, CorruptionError):
                raise
            raise CorruptionError(f"could not load messaging state: {exc}") from exc

    def _save(self) -> None:
        payload = {
            "version": MESSAGING_PROTOCOL_VERSION,
            "updated_at": _now_iso(),
            "messages": {mid: m.to_dict() for mid, m in self._messages.items()},
            "sequences": {k: v for k, v in self._sequences.items()},
            "known_agents": sorted(self._known_agents),
            "events": list(self._events),
        }
        _atomic_write_file(self.path, _canonical(payload))

    def _log(self, action: str, actor: str, message_id: str, detail: str = "") -> None:
        event = {
            "timestamp": _now_iso(),
            "actor": actor or "system",
            "action": action,
            "message_id": message_id,
            "detail": detail,
        }
        secret_scan = _canonical(event)
        if _contains_secret_like_content(secret_scan):
            event = {**event, "detail": "[redacted]"}
        self._events.append(event)

    # -- agent registration for authorization --------------------------------

    def register_known_agent(self, agent_id: str) -> None:
        self._known_agents.add(agent_id)
        self._save()

    def unregister_known_agent(self, agent_id: str) -> None:
        self._known_agents.discard(agent_id)
        self._save()

    def known_agents(self) -> frozenset[str]:
        return frozenset(self._known_agents)

    # -- authorization checks -----------------------------------------------

    def _authorize_sender(self, sender: str) -> None:
        if self.require_sender_authorization and sender not in self._known_agents:
            raise PermissionDeniedMessageError(
                f"Sender {sender!r} is not authorized."
            )

    def _authorize_receiver(self, receiver: str) -> None:
        if self.require_receiver_authorization and receiver and receiver not in self._known_agents:
            raise ReceiverUnavailableError(
                f"Receiver {receiver!r} is not registered."
            )

    def _validate_scope(
        self,
        project_id: str,
        task_id: str,
        allowed_projects: frozenset[str] | None = None,
        allowed_tasks: frozenset[str] | None = None,
    ) -> None:
        if allowed_projects and project_id and project_id not in allowed_projects:
            raise ScopeViolationError(
                f"Project {project_id!r} is outside the permitted scope."
            )
        if allowed_tasks and task_id and task_id not in allowed_tasks:
            raise ScopeViolationError(
                f"Task {task_id!r} is outside the permitted scope."
            )

    # -- message lifecycle --------------------------------------------------

    def send(
        self,
        envelope: MessageEnvelope,
        *,
        actor: str | None = None,
        allowed_projects: frozenset[str] | None = None,
        allowed_tasks: frozenset[str] | None = None,
    ) -> MessageEnvelope:
        """Send a message through the broker (validates, persists, routes)."""
        ok, errors = envelope.validate()
        if not ok:
            raise MessageValidationError("; ".join(errors))
        if envelope.message_id in self._messages:
            existing = self._messages[envelope.message_id]
            if existing.idempotency_key == envelope.idempotency_key:
                return existing
            raise DuplicateMessageError(
                f"Message {envelope.message_id!r} already exists."
            )
        self._authorize_sender(envelope.sender_agent_id)
        if envelope.receiver_agent_id:
            self._authorize_receiver(envelope.receiver_agent_id)
        self._validate_scope(
            envelope.project_id, envelope.task_id,
            allowed_projects, allowed_tasks,
        )
        seq = self._next_sequence(envelope.sender_agent_id, envelope.receiver_agent_id)
        if seq != envelope.sequence:
            envelope = envelope.with_(sequence=seq)
        if envelope.is_expired():
            envelope = envelope.with_(
                status=MessageStatus.EXPIRED.value,
                error_detail="message expired before send",
            )
        self._messages[envelope.message_id] = envelope
        self._log("message.send", actor or envelope.sender_agent_id, envelope.message_id)
        self._save()
        return envelope

    def deliver(self, message_id: str, *, actor: str = "system") -> MessageEnvelope:
        """Mark a message as delivered."""
        envelope = self._get(message_id)
        if envelope.status == MessageStatus.QUEUED.value:
            envelope = self._set_status(envelope, MessageStatus.SENT.value, actor)
        self._transition(envelope, MessageStatus.DELIVERED.value)
        updated = envelope.with_(
            status=MessageStatus.DELIVERED.value,
            delivered_at=_now_iso(),
        )
        self._messages[message_id] = updated
        self._log("message.delivered", actor, message_id)
        self._save()
        return updated

    def acknowledge(self, message_id: str, *, actor: str = "system") -> MessageEnvelope:
        """Acknowledge receipt and processing intent."""
        envelope = self._get(message_id)
        self._transition(envelope, MessageStatus.ACKNOWLEDGED.value)
        updated = envelope.with_(
            status=MessageStatus.ACKNOWLEDGED.value,
            acknowledged_at=_now_iso(),
            acknowledgement_id=message_id,
        )
        self._messages[message_id] = updated
        self._log("message.acknowledged", actor, message_id)
        self._save()
        return updated

    def process(self, message_id: str, *, actor: str = "system") -> MessageEnvelope:
        """Begin processing a message."""
        envelope = self._get(message_id)
        self._transition(envelope, MessageStatus.PROCESSING.value)
        updated = envelope.with_(status=MessageStatus.PROCESSING.value)
        self._messages[message_id] = updated
        self._log("message.processing", actor, message_id)
        self._save()
        return updated

    def complete(self, message_id: str, *, actor: str = "system", result: dict[str, Any] | None = None) -> MessageEnvelope:
        """Mark a message as completed."""
        envelope = self._get(message_id)
        self._transition(envelope, MessageStatus.COMPLETED.value)
        updates: dict[str, Any] = {
            "status": MessageStatus.COMPLETED.value,
            "completed_at": _now_iso(),
        }
        if result:
            merged = {**envelope.payload, **result}
            updates["payload"] = merged
        updated = envelope.with_(**updates)
        self._messages[message_id] = updated
        self._log("message.completed", actor, message_id)
        self._save()
        return updated

    def fail(self, message_id: str, reason: str, *, actor: str = "system") -> MessageEnvelope:
        """Mark a message as failed."""
        envelope = self._get(message_id)
        if envelope.status == MessageStatus.QUEUED.value:
            envelope = self._set_status(envelope, MessageStatus.SENT.value, actor)
        self._transition(envelope, MessageStatus.FAILED.value)
        updated = envelope.with_(
            status=MessageStatus.FAILED.value,
            error_detail=reason,
        )
        self._messages[message_id] = updated
        self._log("message.failed", actor, message_id, detail=reason)
        self._save()
        return updated

    def cancel(self, message_id: str, *, actor: str = "system", reason: str = "") -> MessageEnvelope:
        """Cancel a message."""
        envelope = self._get(message_id)
        self._transition(envelope, MessageStatus.CANCELLED.value)
        updated = envelope.with_(
            status=MessageStatus.CANCELLED.value,
            error_detail=reason,
        )
        self._messages[message_id] = updated
        self._log("message.cancelled", actor, message_id, detail=reason)
        self._save()
        return updated

    def retry(self, message_id: str, *, actor: str = "system") -> MessageEnvelope:
        """Retry a failed/expired/cancelled message."""
        envelope = self._get(message_id)
        if envelope.status not in (MessageStatus.FAILED.value, MessageStatus.EXPIRED.value, MessageStatus.CANCELLED.value):
            raise MessageStateError(
                f"Message {message_id!r} cannot be retried from {envelope.status!r}."
            )
        if envelope.retry_count >= envelope.max_retries:
            raise MessageStateError(
                f"Message {message_id!r} exceeded max retries ({envelope.max_retries})."
            )
        updated = envelope.with_(
            status=MessageStatus.QUEUED.value,
            retry_count=envelope.retry_count + 1,
            last_retry_at=_now_iso(),
            error_detail="",
        )
        self._messages[message_id] = updated
        self._log("message.retry", actor, message_id, detail=f"attempt={updated.retry_count}")
        self._save()
        return updated

    # -- delivery retry / timeout -------------------------------------------

    def retry_delivery(self, message_id: str, *, actor: str = "system") -> MessageEnvelope:
        """Attempt delivery retry with exponential backoff."""
        envelope = self._get(message_id)
        if envelope.status != MessageStatus.SENT.value:
            raise MessageStateError(
                f"Message {message_id!r} is not in SENT status for delivery retry."
            )
        return self.retry(message_id, actor=actor)

    def detect_stale(self) -> list[MessageEnvelope]:
        """Detect and expire messages that exceeded TTL."""
        stale: list[MessageEnvelope] = []
        for mid, envelope in list(self._messages.items()):
            if envelope.status in _TERMINAL:
                continue
            if envelope.is_expired():
                updated = envelope.with_(
                    status=MessageStatus.EXPIRED.value,
                    error_detail="TTL exceeded",
                )
                self._messages[mid] = updated
                stale.append(updated)
                self._log("message.expired", "system", mid)
        if stale:
            self._save()
        return stale

    # -- ordering / deduplication -------------------------------------------

    def _next_sequence(self, sender: str, receiver: str) -> int:
        key = f"{sender}:{receiver}"
        current = self._sequences.get(key, 0)
        next_val = current + 1
        self._sequences[key] = next_val
        return next_val

    def check_duplicate(self, message_id: str) -> bool:
        return message_id in self._messages

    def get_by_idempotency_key(self, idempotency_key: str) -> MessageEnvelope | None:
        for envelope in self._messages.values():
            if envelope.idempotency_key == idempotency_key:
                return envelope
        return None

    def verify_sequence(self, sender: str, receiver: str, expected: int) -> bool:
        key = f"{sender}:{receiver}"
        actual = self._sequences.get(key, 0)
        return actual == expected

    # -- direct / broadcast / targeted messaging ----------------------------

    def send_direct(
        self,
        sender: str,
        receiver: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> MessageEnvelope:
        envelope = create_message(
            MessageType.DIRECT.value,
            sender,
            receiver_agent_id=receiver,
            payload=payload,
            **kwargs,
        )
        return self.send(envelope, actor=sender)

    def broadcast(
        self,
        sender: str,
        receivers: tuple[str, ...],
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> list[MessageEnvelope]:
        sent: list[MessageEnvelope] = []
        for receiver in receivers:
            envelope = create_message(
                MessageType.TARGETED.value,
                sender,
                receiver_agent_id=receiver,
                payload=payload,
                **kwargs,
            )
            sent.append(self.send(envelope, actor=sender))
        return sent

    def send_targeted(
        self,
        sender: str,
        receiver: str,
        payload: dict[str, Any],
        *,
        capabilities: tuple[str, ...] = (),
        **kwargs: Any,
    ) -> MessageEnvelope:
        envelope = create_message(
            MessageType.TARGETED.value,
            sender,
            receiver_agent_id=receiver,
            payload=payload,
            capabilities=capabilities,
            **kwargs,
        )
        return self.send(envelope, actor=sender)

    # -- request / response messaging ---------------------------------------

    def send_request(
        self,
        sender: str,
        receiver: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> MessageEnvelope:
        envelope = create_message(
            MessageType.REQUEST_RESPONSE.value,
            sender,
            receiver_agent_id=receiver,
            payload=payload,
            **kwargs,
        )
        return self.send(envelope, actor=sender)

    def send_response(
        self,
        sender: str,
        receiver: str,
        original_message_id: str,
        result: dict[str, Any],
        **kwargs: Any,
    ) -> MessageEnvelope:
        original = self._get(original_message_id)
        envelope = create_message(
            MessageType.REQUEST_RESPONSE.value,
            sender,
            receiver_agent_id=receiver,
            payload=result,
            correlation_id=original.correlation_id,
            request_id=original.request_id,
            acknowledgement_id=original_message_id,
            **kwargs,
        )
        return self.send(envelope, actor=sender)

    # -- event messaging ----------------------------------------------------

    def send_event(
        self,
        sender: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        workflow_id: str = "",
        project_id: str = "",
        **kwargs: Any,
    ) -> MessageEnvelope:
        envelope = create_message(
            MessageType.EVENT.value,
            sender,
            payload={**payload, "event_type": event_type},
            workflow_id=workflow_id,
            project_id=project_id,
            **kwargs,
        )
        return self.send(envelope, actor=sender)

    # -- task delegation messaging ------------------------------------------

    def send_task_delegation(
        self,
        sender: str,
        receiver: str,
        task_id: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> MessageEnvelope:
        envelope = create_message(
            MessageType.TASK_DELEGATION.value,
            sender,
            receiver_agent_id=receiver,
            task_id=task_id,
            payload=payload,
            **kwargs,
        )
        return self.send(envelope, actor=sender)

    def send_task_result(
        self,
        sender: str,
        receiver: str,
        task_id: str,
        result: dict[str, Any],
        **kwargs: Any,
    ) -> MessageEnvelope:
        envelope = create_message(
            MessageType.TASK_RESULT.value,
            sender,
            receiver_agent_id=receiver,
            task_id=task_id,
            payload=result,
            **kwargs,
        )
        return self.send(envelope, actor=sender)

    # -- checkpoint / handoff messaging -------------------------------------

    def send_checkpoint(
        self,
        sender: str,
        checkpoint_data: dict[str, Any],
        *,
        receiver: str = "",
        workflow_id: str = "",
        project_id: str = "",
        **kwargs: Any,
    ) -> MessageEnvelope:
        envelope = create_message(
            MessageType.CHECKPOINT.value,
            sender,
            receiver_agent_id=receiver,
            workflow_id=workflow_id,
            project_id=project_id,
            checkpoint=checkpoint_data,
            **kwargs,
        )
        return self.send(envelope, actor=sender)

    def send_handoff(
        self,
        sender: str,
        handoff_data: dict[str, Any],
        *,
        receiver: str = "",
        workflow_id: str = "",
        project_id: str = "",
        **kwargs: Any,
    ) -> MessageEnvelope:
        envelope = create_message(
            MessageType.HANDOFF.value,
            sender,
            receiver_agent_id=receiver,
            workflow_id=workflow_id,
            project_id=project_id,
            handoff=handoff_data,
            **kwargs,
        )
        return self.send(envelope, actor=sender)

    # -- capability negotiation messaging -----------------------------------

    def send_capability_negotiation(
        self,
        sender: str,
        receiver: str,
        offered_capabilities: tuple[str, ...],
        **kwargs: Any,
    ) -> MessageEnvelope:
        envelope = create_message(
            MessageType.CAPABILITY_NEGOTIATION.value,
            sender,
            receiver_agent_id=receiver,
            payload={"offered": list(offered_capabilities)},
            capabilities=offered_capabilities,
            **kwargs,
        )
        return self.send(envelope, actor=sender)

    # -- approval messaging -------------------------------------------------

    def send_approval_request(
        self,
        sender: str,
        receiver: str,
        task_id: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> MessageEnvelope:
        envelope = create_message(
            MessageType.APPROVAL_REQUEST.value,
            sender,
            receiver_agent_id=receiver,
            task_id=task_id,
            payload=payload,
            **kwargs,
        )
        return self.send(envelope, actor=sender)

    def send_approval_response(
        self,
        sender: str,
        receiver: str,
        original_message_id: str,
        approved: bool,
        reason: str = "",
        **kwargs: Any,
    ) -> MessageEnvelope:
        original = self._get(original_message_id)
        envelope = create_message(
            MessageType.APPROVAL_RESPONSE.value,
            sender,
            receiver_agent_id=receiver,
            task_id=original.task_id,
            payload={"approved": approved, "reason": reason},
            correlation_id=original.correlation_id,
            acknowledgement_id=original_message_id,
            **kwargs,
        )
        return self.send(envelope, actor=sender)

    # -- error messaging ----------------------------------------------------

    def send_error(
        self,
        sender: str,
        receiver: str,
        error_code: str,
        error_detail: str,
        *,
        original_message_id: str = "",
        **kwargs: Any,
    ) -> MessageEnvelope:
        ack_id = original_message_id
        corr = ""
        if ack_id:
            try:
                original = self._get(ack_id)
                corr = original.correlation_id
            except UnknownMessageError:
                pass
        envelope = create_message(
            MessageType.ERROR.value,
            sender,
            receiver_agent_id=receiver,
            payload={"error_code": error_code, "error_detail": error_detail},
            correlation_id=corr,
            acknowledgement_id=ack_id,
            **kwargs,
        )
        return self.send(envelope, actor=sender)

    # -- heartbeat messaging ------------------------------------------------

    def send_heartbeat(
        self,
        sender: str,
        *,
        status_detail: str = "",
        **kwargs: Any,
    ) -> MessageEnvelope:
        envelope = create_message(
            MessageType.HEARTBEAT.value,
            sender,
            payload={"status": "alive", "detail": status_detail},
            ttl_seconds=60,
            priority=1,
            **kwargs,
        )
        return self.send(envelope, actor=sender)

    # -- workflow event messaging -------------------------------------------

    def send_workflow_event(
        self,
        sender: str,
        workflow_id: str,
        event_type: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> MessageEnvelope:
        envelope = create_message(
            MessageType.WORKFLOW_EVENT.value,
            sender,
            workflow_id=workflow_id,
            payload={**payload, "event_type": event_type},
            **kwargs,
        )
        return self.send(envelope, actor=sender)

    # -- context / handoff / checkpoint propagation -------------------------

    def context_propagation(self, message_id: str) -> dict[str, Any]:
        envelope = self._get(message_id)
        return {
            "message_id": envelope.message_id,
            "correlation_id": envelope.correlation_id,
            "request_id": envelope.request_id,
            "workflow_id": envelope.workflow_id,
            "task_id": envelope.task_id,
            "project_id": envelope.project_id,
            "execution_id": envelope.execution_id,
            "context": dict(envelope.context),
        }

    def handoff_propagation(self, message_id: str) -> dict[str, Any]:
        envelope = self._get(message_id)
        return {
            "message_id": envelope.message_id,
            "handoff": dict(envelope.handoff),
            "correlation_id": envelope.correlation_id,
            "request_id": envelope.request_id,
        }

    def checkpoint_propagation(self, message_id: str) -> dict[str, Any]:
        envelope = self._get(message_id)
        return {
            "message_id": envelope.message_id,
            "checkpoint": dict(envelope.checkpoint),
            "correlation_id": envelope.correlation_id,
            "request_id": envelope.request_id,
        }

    def capability_propagation(self, message_id: str) -> dict[str, Any]:
        envelope = self._get(message_id)
        return {
            "message_id": envelope.message_id,
            "capabilities": list(envelope.capabilities),
            "sender": envelope.sender_agent_id,
        }

    # -- queries ------------------------------------------------------------

    def get(self, message_id: str) -> MessageEnvelope:
        return self._get(message_id)

    def get_status(self, message_id: str) -> str:
        return self._get(message_id).status

    def list_messages(
        self,
        *,
        sender: str | None = None,
        receiver: str | None = None,
        message_type: str | None = None,
        status: str | None = None,
        workflow_id: str | None = None,
        task_id: str | None = None,
        project_id: str | None = None,
    ) -> list[MessageEnvelope]:
        results: list[MessageEnvelope] = []
        for envelope in self._messages.values():
            if sender and envelope.sender_agent_id != sender:
                continue
            if receiver and envelope.receiver_agent_id != receiver:
                continue
            if message_type and envelope.message_type != message_type:
                continue
            if status and envelope.status != status:
                continue
            if workflow_id and envelope.workflow_id != workflow_id:
                continue
            if task_id and envelope.task_id != task_id:
                continue
            if project_id and envelope.project_id != project_id:
                continue
            results.append(envelope)
        results.sort(key=lambda m: (m.timestamp, m.message_id))
        return results

    def count(self, **filters: Any) -> int:
        return len(self.list_messages(**filters))

    # -- delivery retry / timeout helpers -----------------------------------

    def detect_delivery_failures(self) -> list[MessageEnvelope]:
        """Detect messages stuck in SENT or QUEUED without delivery confirmation."""
        failures: list[MessageEnvelope] = []
        for mid, envelope in list(self._messages.items()):
            if envelope.status not in (
                MessageStatus.SENT.value,
                MessageStatus.QUEUED.value,
            ):
                continue
            if envelope.delivered_at:
                continue
            failures.append(envelope)
        return failures

    def retry_stuck_messages(self, *, actor: str = "system") -> list[MessageEnvelope]:
        """Auto-retry messages stuck in SENT/QUEUED state."""
        stuck = self.detect_delivery_failures()
        retried: list[MessageEnvelope] = []
        for envelope in stuck:
            if envelope.retry_count < envelope.max_retries:
                updated = envelope.with_(
                    status=MessageStatus.QUEUED.value,
                    retry_count=envelope.retry_count + 1,
                    last_retry_at=_now_iso(),
                )
                self._messages[envelope.message_id] = updated
                self._log("message.retry_stuck", actor, envelope.message_id)
                self._save()
                retried.append(updated)
        return retried

    def handle_unavailable_agent(self, agent_id: str, *, actor: str = "system") -> list[MessageEnvelope]:
        """Cancel all pending messages to an unavailable agent."""
        pending = [
            m for m in self._messages.values()
            if m.receiver_agent_id == agent_id
            and m.status in (
                MessageStatus.QUEUED.value,
                MessageStatus.SENT.value,
            )
        ]
        cancelled: list[MessageEnvelope] = []
        for envelope in pending:
            updated = envelope.with_(
                status=MessageStatus.CANCELLED.value,
                error_detail=f"receiver {agent_id} unavailable",
            )
            self._messages[envelope.message_id] = updated
            self._log("message.cancelled_unavailable", actor, envelope.message_id, detail=agent_id)
            cancelled.append(updated)
        if cancelled:
            self._save()
        return cancelled

    # -- persistence / recovery / audit -------------------------------------

    def recover(self) -> dict[str, Any]:
        """Re-validate messages and recover from partial failures."""
        prior_count = len(self._messages)
        self._load()
        recovered = 0
        stale = self.detect_stale()
        for mid, envelope in list(self._messages.items()):
            ok, _ = envelope.validate()
            if not ok:
                continue
            recovered += 1
        return {
            "ok": True,
            "prior_count": prior_count,
            "recovered_count": recovered,
            "stale_expired": len(stale),
        }

    def audit_trail(self) -> tuple[dict[str, str], ...]:
        return tuple(dict(e) for e in self._events)

    # -- reporting / diagnostics -------------------------------------------

    def report(self) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for envelope in self._messages.values():
            by_status[envelope.status] = by_status.get(envelope.status, 0) + 1
            by_type[envelope.message_type] = by_type.get(envelope.message_type, 0) + 1
        return {
            "total": len(self._messages),
            "by_status": by_status,
            "by_type": by_type,
            "known_agents": len(self._known_agents),
            "audit_events": len(self._events),
        }

    def diagnostics(self) -> dict[str, Any]:
        failed = [m for m in self._messages.values() if m.status == MessageStatus.FAILED.value]
        expired = [m for m in self._messages.values() if m.status == MessageStatus.EXPIRED.value]
        return {
            "path": str(self.path),
            "total_messages": len(self._messages),
            "known_agents": sorted(self._known_agents),
            "failed_count": len(failed),
            "expired_count": len(expired),
            "event_count": len(self._events),
            "integrity": {
                "writable": self.state_dir.is_dir(),
                "events_append_only": True,
            },
        }

    def health_check(self) -> dict[str, Any]:
        failed = sum(1 for m in self._messages.values() if m.status == MessageStatus.FAILED.value)
        return {
            "ok": failed == 0,
            "total": len(self._messages),
            "failed": failed,
            "known_agents": len(self._known_agents),
        }

    # -- CLI / API / MCP interfaces ----------------------------------------

    def cli_payload(self) -> dict[str, Any]:
        return {
            "messages": [m.to_dict() for m in sorted(self._messages.values(), key=lambda x: x.timestamp)],
            "count": len(self._messages),
            "health": self.health_check(),
        }

    def api_payload(self, message_id: str | None = None) -> dict[str, Any]:
        if message_id:
            envelope = self._get(message_id)
            return {
                "message": envelope.to_dict(),
                "context": self.context_propagation(message_id),
            }
        return {
            "count": len(self._messages),
            "messages": [m.to_dict() for m in self.list_messages()],
            "health": self.health_check(),
        }

    def mcp_payload(self) -> dict[str, Any]:
        view = []
        for m in self.list_messages():
            view.append({
                "message_id": m.message_id,
                "type": m.message_type,
                "status": m.status,
                "sender": m.sender_agent_id,
                "receiver": m.receiver_agent_id,
                "priority": m.priority,
                "timestamp": m.timestamp,
            })
        return {"count": len(view), "messages": view, "health": self.health_check()}

    # -- internal -----------------------------------------------------------

    def _get(self, message_id: str) -> MessageEnvelope:
        if message_id not in self._messages:
            raise UnknownMessageError(f"Message {message_id!r} is unknown.")
        return self._messages[message_id]

    def _transition(self, envelope: MessageEnvelope, new_status: str) -> None:
        allowed = _VALID_TRANSITIONS.get(envelope.status, ())
        if new_status not in allowed:
            raise MessageStateError(
                f"Cannot transition from {envelope.status!r} to {new_status!r}."
            )

    def _set_status(self, envelope: MessageEnvelope, new_status: str, actor: str) -> MessageEnvelope:
        self._transition(envelope, new_status)
        updated = envelope.with_(status=new_status)
        self._messages[envelope.message_id] = updated
        self._log(f"message.{new_status}", actor, envelope.message_id)
        return updated

    def events(self) -> tuple[dict[str, str], ...]:
        return tuple(dict(e) for e in self._events)
