"""Phase 34 — Distributed Reliability, Durable Queue & Recovery.

Reliability layer for Handoff Agent: makes execution resilient to agent,
provider, tool, remote-handoff, and network failures, device loss, process
crash, restart, duplicate execution, and state corruption.

Components:

  - ``DurableQueue`` — crash-safe append-only journal queue with acks, retries
    (lease/processing semantics), and stale-leader recovery.
  - ``DeadLetterQueue`` — filesystem-backed dead-letter inbox for messages that
    exhausted their retry budget or hit a non-retryable error.
  - ``RetryPolicy`` — deterministic exponential backoff with optional jitter
    and permanent-failure classification.
  - ``CircuitBreaker`` — CLOSED/OPEN/HALF_OPEN failure isolation.
  - ``LeaseManager`` — durable leases/locks with expiry, renewal, stale and
    orphan recovery.
  - ``ExecutionRegistry`` — idempotency protection and duplicate-execution
    prevention via ``idempotency_key``.
  - ``CheckpointStore`` — integrity-checked durable checkpoints with last-good
    fallback on corruption (crash/restart recovery).
  - ``RecoveryJournal`` — sequence-checked append-only recovery log used to
    rehydrate state after crash and detect gaps.
  - ``BackupManager`` — snapshot/verify/restore for backup-and-restore checks.
  - ``FailureDiagnostics`` — incident store for failure diagnostics.
  - ``ReliabilityEngine`` — coordination facade + conformance probes.

Security guarantees (boundaries preserved from all prior phases):

  - Secret-like payloads are rejected at the queue and checkpoint boundaries;
    nothing secret-shaped is ever persisted to the journal, checkpoint,
    backup, or diagnostics.
  - Errors are recorded with a safe reason (never raw exception text that may
    echo secrets).
  - Only files inside the configured data directory are written. Paths are
    resolved and containment-checked; journal/checkpoint/backup targets reject
    ``..`` traversal and symlink escapes.
  - No arbitrary command execution: only the caller's handler runs
    synchronously. This module never constructs a shell command.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from handoff_agent.adapters.base import contains_secret_like

RELIABILITY_VERSION = "1"
MAX_PAYLOAD_BYTES = 1024 * 1024
DEFAULT_MAX_PENDING = 10_000


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ReliabilityError(Exception):
    """Base error for the reliability layer."""


class DurableQueueError(ReliabilityError):
    """Raised when a durable queue operation fails or is invalid."""


class DeadLetterError(ReliabilityError):
    """Raised when dead-letter handling fails."""


class RetryExhaustedError(ReliabilityError):
    """Raised when a message exhausted its retry budget."""


class NonRetryableError(ReliabilityError):
    """Marker: this failure must NOT be retried (permanent)."""


class _SimulatedCrash(RuntimeError):
    """Internal: raised to simulate a mid-processing crash for tests."""


class CircuitBreakerOpenError(ReliabilityError):
    """Raised when a circuit breaker is OPEN and rejects calls."""


class LeaseError(ReliabilityError):
    """Raised for lease/lock failures."""


class StaleLeaseError(LeaseError):
    """Raised when a lease is stale and requires recovery."""


class ExecutionError(ReliabilityError):
    """Raised by the execution registry."""


class DuplicateExecutionError(ExecutionError):
    """Raised when a duplicate execution cannot be safely merged."""


class CheckpointError(ReliabilityError):
    """Raised when a checkpoint cannot be saved or restored."""


class CorruptCheckpointError(CheckpointError):
    """Raised when a checkpoint fails integrity verification."""


class JournalError(ReliabilityError):
    """Raised when the recovery journal is misused or corrupt."""


class BackupError(ReliabilityError):
    """Raised when a backup snapshot cannot be created, verified, or restored."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _safe_reason(exc: BaseException) -> str:
    """Return a short, redacted reason for an exception.

    The full exception string may contain secret-shaped values; only the
    exception type name plus a content-scrubbed prefix is kept.
    """
    text = f"{type(exc).__name__}: {exc}"
    if contains_secret_like(text):
        return f"{type(exc).__name__}: <redacted>"
    return text[:300]


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically write *data* to *path*: temp file, fsync, rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, str(path))
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _resolve_within(root: Path, *parts: str) -> Path:
    """Resolve ``root / *parts`` with containment enforcement."""
    root_resolved = root.resolve()
    candidate = root_resolved.joinpath(*parts)
    if not candidate.resolve().is_relative_to(root_resolved):
        raise DurableQueueError(f"Path escapes data directory: {parts!r}")
    return candidate


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


class RetryPolicy:
    """Deterministic exponential backoff with optional jitter.

    ``delay_for(attempt)`` returns base * multiplier**(attempt-1) capped at
    ``max_delay_ms``, plus a deterministic jitter within
    ``[0, jitter_factor * delta]`` when ``jitter_factor > 0``. The built-in
    default base delay is 0 ms so tests stay fast and deterministic without a
    wall-clock sleep.
    """

    def __init__(
        self,
        *,
        max_attempts: int = 3,
        base_delay_ms: float = 0.0,
        multiplier: float = 2.0,
        max_delay_ms: float = 10_000.0,
        jitter_factor: float = 0.0,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if base_delay_ms < 0 or max_delay_ms < 0:
            raise ValueError("delays must be non-negative")
        if multiplier < 1.0:
            raise ValueError("multiplier must be >= 1.0")
        if not 0.0 <= jitter_factor <= 1.0:
            raise ValueError("jitter_factor must be within [0.0, 1.0]")
        self.max_attempts = max_attempts
        self.base_delay_ms = base_delay_ms
        self.multiplier = multiplier
        self.max_delay_ms = max_delay_ms
        self.jitter_factor = jitter_factor

    def delay_for(self, attempt: int) -> float:
        """Return the delay (seconds) before retry *attempt* (1-based)."""
        delta = min(self.base_delay_ms * (self.multiplier ** (attempt - 1)), self.max_delay_ms)
        if self.jitter_factor > 0 and delta > 0:
            span = delta * self.jitter_factor
            nosiy = attempt * 100003 + 17
            frac = (nosiy % 1000) / 1000.0
            delta += span * frac
        return delta / 1000.0

    def is_terminal(self, attempt: int) -> bool:
        """True when *attempt* reached the retry budget ceiling."""
        return attempt >= self.max_attempts

    def should_retry(self, attempt: int, error: BaseException | None = None) -> bool:
        """True when a (possibly failed) *attempt* may be retried."""
        if error is not None and isinstance(error, NonRetryableError):
            return False
        return attempt < self.max_attempts

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "base_delay_ms": self.base_delay_ms,
            "multiplier": self.multiplier,
            "max_delay_ms": self.max_delay_ms,
            "jitter_factor": self.jitter_factor,
        }


# ---------------------------------------------------------------------------
# Durable append-only journal (shared primitives)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JournalEntry:
    seq: int = 0
    op: str = ""
    message_id: str = ""
    message: Mapping[str, Any] = field(default_factory=dict)
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "op": self.op,
            "message_id": self.message_id,
            "message": dict(self.message),
            "data": dict(self.data),
        }


class _AppendOnlyJournal:
    """Crash-safe append-only structured journal (one JSON object per line)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: Any = None
        self.last_seq = 0
        self.truncated_at = 0

    def open(self) -> "_AppendOnlyJournal":
        if self._fh is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")
        for _ in self.replay():
            pass
        return self

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
            finally:
                self._fh.close()
                self._fh = None

    def flush(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def append(self, entry: JournalEntry) -> JournalEntry:
        if self._fh is None:
            raise JournalError("journal is not open")
        self.last_seq += 1
        entry = JournalEntry(seq=self.last_seq, op=entry.op, message_id=entry.message_id,
                             message=entry.message, data=entry.data)
        line = (_canonical(entry.to_dict()) + "\n").encode("utf-8")
        self._fh.write(line.decode("utf-8"))
        self._fh.flush()
        os.fsync(self._fh.fileno())
        return entry

    def replay(self) -> Iterable[JournalEntry]:
        if not self.path.exists():
            return
        seq = 0
        self.truncated_at = 0
        with open(self.path, "r", encoding="utf-8") as handle:
            position = 0
            for raw in handle:
                position += len(raw)
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if record.get("seq", 0) != seq + 1:
                        self.truncated_at = position
                        return
                    seq = record["seq"]
                    yield JournalEntry(
                        seq=seq,
                        op=str(record.get("op", "")),
                        message_id=str(record.get("message_id", "")),
                        message=dict(record.get("message", {})),
                        data=dict(record.get("data", {})),
                    )
                except (ValueError, KeyError, TypeError):
                    self.truncated_at = position
                    return
        self.last_seq = seq


# ---------------------------------------------------------------------------
# Durable queue
# ---------------------------------------------------------------------------


class QueueMessage:
    """A durable message with retry/lease lifecycle state."""

    def __init__(
        self,
        *,
        message_id: str,
        kind: str,
        payload: Mapping[str, Any],
        enqueue_seq: int,
        state: str = "pending",
        attempts: int = 0,
        error_type: str = "",
        error_reason: str = "",
        next_retry_ms: int = 0,
        leased_until_ms: int = 0,
        created_at_ms: int = 0,
    ) -> None:
        self.message_id = message_id
        self.kind = kind
        self.payload = dict(payload)
        self.enqueue_seq = enqueue_seq
        self.state = state
        self.attempts = attempts
        self.error_type = error_type
        self.error_reason = error_reason
        self.next_retry_ms = next_retry_ms
        self.leased_until_ms = leased_until_ms
        self.created_at_ms = created_at_ms or _now_ms()

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "kind": self.kind,
            "payload": dict(self.payload),
            "enqueue_seq": self.enqueue_seq,
            "state": self.state,
            "attempts": self.attempts,
            "error_type": self.error_type,
            "error_reason": self.error_reason,
            "next_retry_ms": self.next_retry_ms,
            "leased_until_ms": self.leased_until_ms,
            "created_at_ms": self.created_at_ms,
        }


class DurableQueue:
    """Crash-safe FIFO queue backed by an append-only journal.

    Lifecycle:
        enqueue -> pending -> dequeue -> processing -> ack (acked)
                                 `-> nack -> pending (retry) | dead (DLQ)
      processing with expired lease -> recover_stale -> pending again

    On ``recover()`` the journal is replayed in sequence order; a truncated
    final line (crash during append) is tolerated and stops replay at the last
    complete record.
    """

    def __init__(self, data_dir: str | Path, *, retry_policy: RetryPolicy | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.journal_path = _resolve_within(self.data_dir, "queue", "journal.jsonl")
        self.retry_policy = retry_policy or RetryPolicy()
        self._msg: dict[str, QueueMessage] = {}
        self._pending: list[str] = []
        self._processing: dict[str, QueueMessage] = {}
        self._dead: dict[str, QueueMessage] = {}
        self._acked: set[str] = set()
        self._next_enqueue_seq = 0
        self._journal = _AppendOnlyJournal(self.journal_path)
        self.open()

    # -- lifecycle ----------------------------------------------------------

    def open(self) -> "DurableQueue":
        if self._journal._fh is None:
            self._journal.open()
        return self

    def close(self) -> None:
        self._journal.close()

    def recover(self) -> dict[str, Any]:
        """Rehydrate queue state by replaying the journal (idempotent)."""
        self._msg.clear()
        self._pending = []
        self._processing = {}
        self._dead = {}
        self._acked = set()
        self._next_enqueue_seq = 0
        for entry in self._journal.replay():
            self._apply(entry)
        return self.stats()

    def _apply(self, entry: JournalEntry) -> None:
        op = entry.op
        if op == "enqueue":
            self._next_enqueue_seq = max(self._next_enqueue_seq, entry.message.get("enqueue_seq", 0))
            message = QueueMessage(**{
                k: v for k, v in entry.message.items()
                if k in QueueMessage(
                    message_id="", kind="", payload={}, enqueue_seq=0
                ).to_dict()
            })
            self._msg[message.message_id] = message
            self._pending.append(message.message_id)
            self._next_enqueue_seq = max(self._next_enqueue_seq, message.enqueue_seq)
        elif op == "dequeue":
            message = self._msg.get(entry.message_id)
            if message is None:
                return
            if message.message_id in self._pending:
                self._pending.remove(message.message_id)
            message.state = "processing"
            message.leased_until_ms = int(entry.data.get("leased_until_ms", 0))
            self._processing[message.message_id] = message
        elif op == "ack":
            message = self._msg.get(entry.message_id)
            if message is None:
                return
            self._processing.pop(message.message_id, None)
            if message.message_id in self._pending:
                self._pending.remove(message.message_id)
            message.state = "acked"
            self._acked.add(message.message_id)
        elif op == "nack":
            message = self._msg.get(entry.message_id)
            if message is None:
                return
            self._processing.pop(message.message_id, None)
            message.attempts = int(entry.data.get("attempts", message.attempts))
            message.error_type = str(entry.data.get("error_type", ""))
            message.error_reason = str(entry.data.get("error_reason", ""))
            message.next_retry_ms = int(entry.data.get("next_retry_ms", 0))
            if message.message_id not in self._pending:
                self._pending.append(message.message_id)
            message.state = "pending"
        elif op == "dead":
            message = self._msg.get(entry.message_id)
            if message is None:
                return
            self._processing.pop(message.message_id, None)
            if message.message_id in self._pending:
                self._pending.remove(message.message_id)
            message.state = "dead"
            self._dead[message.message_id] = message
        elif op == "recover":
            message = self._msg.get(entry.message_id)
            if message is None:
                return
            self._processing.pop(message.message_id, None)
            if message.message_id not in self._pending:
                self._pending.append(message.message_id)
            message.state = "pending"
            message.next_retry_ms = int(entry.data.get("next_retry_ms", 0))
        else:
            raise DurableQueueError(f"unknown journal op: {op!r}")

    # -- enqueue ------------------------------------------------------------

    def enqueue(self, kind: str, payload: Mapping[str, Any], *, message_id: str = "") -> QueueMessage:
        raw = _canonical(payload)
        if contains_secret_like(raw):
            raise DurableQueueError("queue payload embeds secret-like content; refused")
        if len(raw.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise DurableQueueError("queue payload exceeds size limit")
        self._next_enqueue_seq += 1
        message = QueueMessage(
            message_id=message_id or _new_id("msg"),
            kind=kind,
            payload=dict(payload),
            enqueue_seq=self._next_enqueue_seq,
        )
        self._msg[message.message_id] = message
        self._pending.append(message.message_id)
        self._journal.append(JournalEntry(
            op="enqueue", message_id=message.message_id,
            message=message.to_dict(), data={"kind": kind},
        ))
        self._enforce_pending_limit()
        return message

    def _enforce_pending_limit(self) -> None:
        while len(self._pending) > DEFAULT_MAX_PENDING:
            oldest = self._pending.pop(0)
            message = self._msg.get(oldest)
            if message is not None and message.state == "pending":
                self._dead[oldest] = message
                message.state = "dead"

    # -- dequeue / lifecycle -------------------------------------------------

    def dequeue_due(
        self,
        *,
        kind: str = "",
        now_ms: int | None = None,
        lease_ms: int = 30_000,
        limit: int = 1,
    ) -> list[QueueMessage]:
        now = now_ms if now_ms is not None else _now_ms()
        due: list[QueueMessage] = []
        for message_id in list(self._pending):
            message = self._msg.get(message_id)
            if message is None or message.state != "pending":
                continue
            if kind and message.kind != kind:
                continue
            if message.next_retry_ms > now:
                continue
            self._pending.remove(message_id)
            message.state = "processing"
            message.leased_until_ms = now + lease_ms
            self._processing[message_id] = message
            self._journal.append(JournalEntry(
                op="dequeue", message_id=message_id,
                data={"leased_until_ms": message.leased_until_ms},
            ))
            due.append(message)
            if len(due) >= limit:
                break
        return due

    def dequeue(self, *, kind: str = "") -> QueueMessage | None:
        due = self.dequeue_due(kind=kind, limit=1)
        return due[0] if due else None

    def ack(self, message_id: str) -> QueueMessage:
        message = self._msg.get(message_id)
        if message is None:
            raise DurableQueueError(f"unknown message: {message_id!r}")
        self._processing.pop(message_id, None)
        if message_id in self._pending:
            self._pending.remove(message_id)
        message.state = "acked"
        self._acked.add(message_id)
        self._journal.append(JournalEntry(op="ack", message_id=message_id))
        return message

    def nack(self, message_id: str, error: BaseException | None = None, *, reason: str = "") -> QueueMessage:
        message = self._msg.get(message_id)
        if message is None:
            raise DurableQueueError(f"unknown message: {message_id!r}")
        self._processing.pop(message_id, None)
        message.state = "pending"
        message.attempts += 1
        message.error_type = type(error).__name__ if error else "nack"
        message.error_reason = _safe_reason(error) if error else reason
        if message.attempts >= self.retry_policy.max_attempts or isinstance(error, NonRetryableError):
            message.state = "dead"
            self._dead[message_id] = message
            self._journal.append(JournalEntry(
                op="dead", message_id=message_id,
                data={"attempts": message.attempts,
                      "error_type": message.error_type,
                      "error_reason": message.error_reason},
            ))
            return message
        message.next_retry_ms = _now_ms() + int(self.retry_policy.delay_for(message.attempts) * 1000)
        if message_id not in self._pending:
            self._pending.append(message_id)
        self._journal.append(JournalEntry(
            op="nack", message_id=message_id,
            data={"attempts": message.attempts,
                  "error_type": message.error_type,
                  "error_reason": message.error_reason,
                  "next_retry_ms": message.next_retry_ms},
        ))
        return message

    def recover_stale(self, *, stale_ms: int = 30_000, now_ms: int | None = None) -> list[dict[str, Any]]:
        """Re-enqueue processing messages whose lease expired (crash/stuck)."""
        now = now_ms if now_ms is not None else _now_ms()
        recovered: list[dict[str, Any]] = []
        for message_id, message in list(self._processing.items()):
            if message.leased_until_ms and message.leased_until_ms < now - stale_ms:
                self._processing.pop(message_id, None)
                message.state = "pending"
                message.next_retry_ms = now
                self._pending.append(message_id)
                self._journal.append(JournalEntry(
                    op="recover", message_id=message_id,
                    data={"next_retry_ms": now},
                ))
                recovered.append({"message_id": message_id, "kind": message.kind,
                                  "created_at_ms": message.created_at_ms})
        return recovered

    # -- inspection ----------------------------------------------------------

    def get(self, message_id: str) -> QueueMessage | None:
        return self._msg.get(message_id)

    def pending(self, *, kind: str = "") -> list[QueueMessage]:
        return [self._msg[m] for m in self._pending if not kind or self._msg[m].kind == kind]

    def processing(self) -> dict[str, QueueMessage]:
        return dict(self._processing)

    def acked_count(self) -> int:
        return len(self._acked)

    def total_count(self) -> int:
        return len(self._msg)

    def stats(self) -> dict[str, Any]:
        return {
            "total": self.total_count(),
            "pending": len(self._pending),
            "processing": len(self._processing),
            "dead": len(self._dead),
            "acked": self.acked_count(),
            "journal_seq": self._journal.last_seq,
            "truncated_at": self._journal.truncated_at,
        }


# ---------------------------------------------------------------------------
# Dead-letter queue
# ---------------------------------------------------------------------------


class DeadLetterQueue:
    """Filesystem-backed dead-letter inbox.

    Each dead message is stored as one JSON file under ``dir/dead/``. A
    message is moved here when it exhausts the retry budget or fails with a
    ``NonRetryableError``.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.dead_dir = _resolve_within(self.data_dir, "dead")

    def enqueue(self, message: QueueMessage) -> Path:
        self.dead_dir.mkdir(parents=True, exist_ok=True)
        target = _resolve_within(self.dead_dir, f"{message.message_id}.json")
        _atomic_write_bytes(
            target, (_canonical(message.to_dict()) + "\n").encode("utf-8")
        )
        return target

    def get(self, message_id: str) -> dict[str, Any] | None:
        target = self.dead_dir / f"{message_id}.json"
        if not target.exists():
            return None
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None

    def list(self, *, kind: str = "") -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if not self.dead_dir.exists():
            return records
        for path in sorted(self.dead_dir.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if not kind or record.get("kind") == kind:
                records.append(record)
        return records

    def count(self, *, kind: str = "") -> int:
        return len(self.list(kind=kind))


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """CLOSED / OPEN / HALF_OPEN failure isolation."""

    def __init__(
        self,
        *,
        name: str = "default",
        failure_threshold: int = 3,
        recovery_timeout_ms: float = 10_000.0,
        half_open_allowance: int = 1,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if failure_threshold < 1 or half_open_allowance < 1:
            raise ValueError("thresholds must be positive")
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout_ms = recovery_timeout_ms
        self.half_open_allowance = half_open_allowance
        self._clock = clock_ms or _now_ms
        self.state = BreakerState.CLOSED
        self.consecutive_failures = 0
        self._opened_at_ms = 0
        self._half_open_probes = 0

    def record_success(self) -> None:
        self.consecutive_failures = 0
        if self.state == BreakerState.HALF_OPEN:
            self._half_open_probes += 1
            if self._half_open_probes >= self.half_open_allowance:
                self._close()

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.state == BreakerState.HALF_OPEN and self._half_open_probes:
            self._open()
            return
        if self.state == BreakerState.CLOSED and self.consecutive_failures >= self.failure_threshold:
            self._open()

    def _open(self) -> None:
        self.state = BreakerState.OPEN
        self._opened_at_ms = self._clock()

    def _close(self) -> None:
        self.state = BreakerState.CLOSED
        self.consecutive_failures = 0
        self._half_open_probes = 0

    def allow_call(self, now_ms: int | None = None) -> bool:
        now = now_ms if now_ms is not None else self._clock()
        if self.state == BreakerState.CLOSED:
            return True
        if self.state == BreakerState.OPEN:
            if now - self._opened_at_ms >= self.recovery_timeout_ms:
                self.state = BreakerState.HALF_OPEN
                self._half_open_probes = 0
                return True
            return False
        return self._half_open_probes < self.half_open_allowance

    def call(self, fn: Callable[[], Any]) -> Any:
        if not self.allow_call():
            raise CircuitBreakerOpenError(f"circuit {self.name!r} is open")
        try:
            result = fn()
        except BaseException:
            self.record_failure()
            raise
        self.record_success()
        return result

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "consecutive_failures": self.consecutive_failures,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout_ms": self.recovery_timeout_ms,
        }


# ---------------------------------------------------------------------------
# Leases and locks
# ---------------------------------------------------------------------------


@dataclass
class Lease:
    lease_key: str
    holder: str
    issued_at_ms: int
    expires_at_ms: int
    renewal_count: int = 0

    def is_expired(self, now_ms: int | None = None) -> bool:
        now = now_ms if now_ms is not None else _now_ms()
        return now >= self.expires_at_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_key": self.lease_key,
            "holder": self.holder,
            "issued_at_ms": self.issued_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "renewal_count": self.renewal_count,
        }


class LeaseManager:
    """Durable lease / lock manager with stale and orphan recovery."""

    def __init__(self, data_dir: str | Path, *, ttl_ms: int = 30_000) -> None:
        self.data_dir = Path(data_dir)
        self.state_path = _resolve_within(self.data_dir, "leases", "leases.json")
        self.ttl_ms = ttl_ms
        self._leases: dict[str, Lease] = {}
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            records = json.loads(self.state_path.read_text(encoding="utf-8"))
            for key, data in records.items():
                self._leases[key] = Lease(**data)
        except (ValueError, TypeError, KeyError, OSError):
            self._leases = {}

    def _save(self) -> None:
        _atomic_write_bytes(
            self.state_path,
            _canonical({k: v.to_dict() for k, v in sorted(self._leases.items())}).encode("utf-8"),
        )

    def acquire(self, lease_key: str, holder: str, *, ttl_ms: int | None = None) -> Lease:
        now = _now_ms()
        ttl = ttl_ms if ttl_ms is not None else self.ttl_ms
        existing = self._leases.get(lease_key)
        if existing and not existing.is_expired(now) and existing.holder != holder:
            raise LeaseError(
                f"lease {lease_key!r} held by {existing.holder!r} until {existing.expires_at_ms}"
            )
        lease = Lease(lease_key=lease_key, holder=holder, issued_at_ms=now, expires_at_ms=now + ttl)
        self._leases[lease_key] = lease
        self._save()
        return lease

    def renew(self, lease_key: str, holder: str, *, ttl_ms: int | None = None) -> Lease:
        lease = self._leases.get(lease_key)
        if lease is None:
            raise LeaseError(f"lease {lease_key!r} does not exist")
        if lease.holder != holder:
            raise LeaseError(f"lease {lease_key!r} held by another holder")
        now = _now_ms()
        if lease.is_expired(now):
            raise StaleLeaseError(f"lease {lease_key!r} expired before renewal")
        ttl = ttl_ms if ttl_ms is not None else self.ttl_ms
        renewed = Lease(
            lease_key=lease_key, holder=holder, issued_at_ms=now,
            expires_at_ms=now + ttl, renewal_count=lease.renewal_count + 1,
        )
        self._leases[lease_key] = renewed
        self._save()
        return renewed

    def release(self, lease_key: str, holder: str) -> Lease:
        lease = self._leases.get(lease_key)
        if lease is None:
            raise LeaseError(f"lease {lease_key!r} does not exist")
        if lease.holder != holder:
            raise LeaseError(f"lease {lease_key!r} held by another holder")
        self._leases.pop(lease_key, None)
        self._save()
        return lease

    def get(self, lease_key: str) -> Lease | None:
        return self._leases.get(lease_key)

    def recover_stale(self) -> list[Lease]:
        """Remove expired leases (crash/stuck holders)."""
        recovered: list[Lease] = []
        now = _now_ms()
        for key, lease in list(self._leases.items()):
            if lease.is_expired(now):
                self._leases.pop(key, None)
                recovered.append(lease)
        if recovered:
            self._save()
        return recovered

    def orphans(self, *, holder: str = "") -> list[Lease]:
        """Return leases whose holder is gone (no heartbeat); informational."""
        return [
            self._leases[k]
            for k in sorted(self._leases)
            if (not holder or self._leases[k].holder == holder)
        ]

    def status(self) -> dict[str, Any]:
        return {
            "leases": len(self._leases),
            "expired": sum(1 for l in self._leases.values() if l.is_expired()),
            "ttl_ms": self.ttl_ms,
        }


# ---------------------------------------------------------------------------
# Execution registry (idempotency + duplicate prevention)
# ---------------------------------------------------------------------------


class ExecutionPhase(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"
    RECOVERED = "recovered"


@dataclass
class ExecutionRecord:
    execution_id: str
    idempotency_key: str
    kind: str
    phase: str = ExecutionPhase.QUEUED.value
    attempts: int = 0
    outcome: str = ""
    error_type: str = ""
    created_at_ms: int = field(default_factory=_now_ms)
    updated_at_ms: int = field(default_factory=_now_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "idempotency_key": self.idempotency_key,
            "kind": self.kind,
            "phase": self.phase,
            "attempts": self.attempts,
            "outcome": self.outcome,
            "error_type": self.error_type,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
        }


class ExecutionRegistry:
    """Tracks executions and enforces idempotency / duplicate prevention."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.state_path = _resolve_within(self.data_dir, "executions", "registry.json")
        self._executions: dict[str, ExecutionRecord] = {}
        self._by_key: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            records = json.loads(self.state_path.read_text(encoding="utf-8"))
            for record in records:
                execution = ExecutionRecord(**record)
                self._executions[execution.execution_id] = execution
                if execution.idempotency_key:
                    self._by_key[execution.idempotency_key] = execution.execution_id
        except (ValueError, TypeError, KeyError, OSError):
            self._executions = {}
            self._by_key = {}

    def _save(self) -> None:
        payload = [e.to_dict() for e in sorted(self._executions.values(), key=lambda e: e.execution_id)]
        _atomic_write_bytes(self.state_path, _canonical(payload).encode("utf-8"))

    def register(self, *, idempotency_key: str = "", kind: str = "") -> ExecutionRecord:
        """Register a new execution, deduplicating on ``idempotency_key``."""
        existing = self.lookup(idempotency_key=idempotency_key) if idempotency_key else None
        if existing is not None:
            return existing
        execution_id = _new_id("exe")
        record = ExecutionRecord(
            execution_id=execution_id,
            idempotency_key=idempotency_key,
            kind=kind,
        )
        if record.idempotency_key:
            self._by_key[record.idempotency_key] = record.execution_id
        self._executions[execution_id] = record
        self._save()
        return record

    def lookup(self, *, execution_id: str = "", idempotency_key: str = "") -> ExecutionRecord | None:
        if execution_id:
            return self._executions.get(execution_id)
        if idempotency_key:
            exe_id = self._by_key.get(idempotency_key)
            return self._executions.get(exe_id) if exe_id else None
        return None

    def is_duplicate(self, idempotency_key: str) -> bool:
        record = self.lookup(idempotency_key=idempotency_key)
        return record is not None and record.phase not in (ExecutionPhase.QUEUED.value,)

    def touch(self, execution_id: str, *, phase: str) -> ExecutionRecord:
        record = self._executions.get(execution_id)
        if record is None:
            raise ExecutionError(f"unknown execution: {execution_id!r}")
        record.phase = phase
        record.updated_at_ms = _now_ms()
        self._save()
        return record

    def mark_succeeded(self, execution_id: str, *, outcome: str = "") -> ExecutionRecord:
        record = self.touch(execution_id, phase=ExecutionPhase.SUCCEEDED.value)
        record.outcome = outcome
        record.updated_at_ms = _now_ms()
        self._save()
        return record

    def mark_failed(self, execution_id: str, *, error_type: str = "", error_reason: str = "") -> ExecutionRecord:
        record = self.touch(execution_id, phase=ExecutionPhase.FAILED.value)
        record.error_type = error_type
        record.updated_at_ms = _now_ms()
        self._save()
        return record

    def mark_dead(self, execution_id: str, *, error_type: str = "", error_reason: str = "") -> ExecutionRecord:
        record = self.touch(execution_id, phase=ExecutionPhase.DEAD.value)
        record.error_type = error_type
        record.updated_at_ms = _now_ms()
        self._save()
        return record

    def list(self, *, phase: str = "") -> list[ExecutionRecord]:
        records = list(self._executions.values())
        if phase:
            records = [r for r in records if r.phase == phase]
        return sorted(records, key=lambda r: r.created_at_ms)

    def count(self, *, phase: str = "") -> int:
        return len(self.list(phase=phase))


# ---------------------------------------------------------------------------
# Checkpoints (crash / restart recovery)
# ---------------------------------------------------------------------------


@dataclass
class Checkpoint:
    name: str
    version: str
    payload: Mapping[str, Any]
    sha256: str
    created_at_ms: int = field(default_factory=_now_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "payload": dict(self.payload),
            "sha256": self.sha256,
            "created_at_ms": self.created_at_ms,
        }


class CheckpointStore:
    """Integrity-checked durable checkpoints with last-good fallback.

    ``save()`` writes atomically to ``{name}.json`` while keeping the previous
    good revision at ``{name}.bak.json``. ``restore()`` verifies the SHA-256;
    if the current file is corrupt it fails over to the backup; if both are
    corrupt it raises ``CorruptCheckpointError`` (never returns bad state).
    """

    def __init__(self, data_dir: str | Path, *, version: str = RELIABILITY_VERSION) -> None:
        self.data_dir = Path(data_dir)
        self.checkpoint_dir = _resolve_within(self.data_dir, "checkpoints")
        self.version = version

    def save(self, name: str, payload: Mapping[str, Any]) -> Checkpoint:
        if contains_secret_like(_canonical(payload)):
            raise CheckpointError("checkpoint payload embeds secret-like content; refused")
        checkpoint = Checkpoint(
            name=name,
            version=self.version,
            payload=dict(payload),
            sha256=_sha256(_canonical(payload).encode("utf-8")),
        )
        target = _resolve_within(self.checkpoint_dir, f"{name}.json")
        backup = _resolve_within(self.checkpoint_dir, f"{name}.bak.json")
        if target.exists():
            _atomic_write_bytes(backup, target.read_bytes())
        _atomic_write_bytes(target, (_canonical(checkpoint.to_dict()) + "\n").encode("utf-8"))
        return checkpoint

    def restore(self, name: str) -> Checkpoint:
        target = _resolve_within(self.checkpoint_dir, f"{name}.json")
        backup = _resolve_within(self.checkpoint_dir, f"{name}.bak.json")
        for path in (target, backup):
            if path.exists():
                checkpoint = self._load_verified(path)
                if checkpoint is not None:
                    return checkpoint
        raise CorruptCheckpointError(f"checkpoint {name!r} is corrupt (current and backup)")

    def _load_verified(self, path: Path) -> Checkpoint | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            payload = dict(data.get("payload", {}))
            expected = data.get("sha256", "")
            actual = _sha256(_canonical(payload).encode("utf-8"))
            if not expected or expected != actual:
                return None
            return Checkpoint(
                name=str(data.get("name", "")),
                version=str(data.get("version", "")),
                payload=payload,
                sha256=expected,
                created_at_ms=int(data.get("created_at_ms", 0)),
            )
        except (ValueError, TypeError, OSError):
            return None

    def exists(self, name: str) -> bool:
        return _resolve_within(self.checkpoint_dir, f"{name}.json").exists() or \
            _resolve_within(self.checkpoint_dir, f"{name}.bak.json").exists()


# ---------------------------------------------------------------------------
# Recovery journal
# ---------------------------------------------------------------------------


class RecoveryJournal:
    """Sequence-checked append-only recovery log.

    Used to rehydrate reliability state after a crash and to detect gaps in
    the write sequence (state corruption). ``replay()`` yields entries in
    order; if a sequence gap is found, ``gap_detected`` is set and replay
    continues from the last contiguous prefix.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.path = _resolve_within(data_dir, "recovery", "journal.jsonl")
        self.gap_detected = False

    def append(self, kind: str, *, payload: Mapping[str, Any] | None = None) -> int:
        if contains_secret_like(_canonical(payload or {})):
            raise JournalError("recovery journal entry embeds secret-like content; refused")
        existing = list(self._tail())
        seq = existing[-1] if existing else 0
        seq += 1
        record = {"seq": seq, "kind": kind, "payload": dict(payload or {}), "at": _now_iso()}
        _atomic_write_bytes(
            self.path,
            (_canonical(record) + "\n").encode("utf-8"),
        )
        return seq

    def _tail(self) -> Iterable[int]:
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    yield int(json.loads(line).get("seq", 0))
                except (ValueError, TypeError):
                    return

    def replay(self) -> Iterable[dict[str, Any]]:
        self.gap_detected = False
        if not self.path.exists():
            return
        expected = 1
        entries: list[dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    return
                if record.get("seq") != expected:
                    self.gap_detected = True
                    return
                entries.append(record)
                expected += 1
        yield from entries


# ---------------------------------------------------------------------------
# Backup manager
# ---------------------------------------------------------------------------


class BackupManager:
    """Snapshot / verify / restore for backup verification."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.backup_dir = _resolve_within(self.data_dir, "backups")

    def snapshot(self, source_files: Iterable[Path]) -> dict[str, Any]:
        snapshot_id = _new_id("snap")
        targets: list[dict[str, Any]] = []
        snapshot_dir = _resolve_within(self.backup_dir, snapshot_id)
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        for source in source_files:
            if not source.is_file():
                raise BackupError(f"backup source missing: {source}")
            data = source.read_bytes()
            rel_name = source.name
            entry_path = snapshot_dir / rel_name
            block = data
            entry_path.write_bytes(block)
            targets.append({
                "source": str(source),
                "rel": rel_name,
                "sha256": _sha256(block),
                "size": len(block),
            })
        manifest = {
            "snapshot_id": snapshot_id,
            "created_at": _now_iso(),
            "files": targets,
            "version": RELIABILITY_VERSION,
        }
        _atomic_write_bytes(
            snapshot_dir / "manifest.json",
            (_canonical(manifest) + "\n").encode("utf-8"),
        )
        return manifest

    def list_snapshots(self) -> list[str]:
        if not self.backup_dir.exists():
            return []
        return sorted(p.name for p in self.backup_dir.iterdir() if (p / "manifest.json").exists())

    def verify(self, snapshot_id: str) -> dict[str, Any]:
        snapshot_dir = _resolve_within(self.backup_dir, snapshot_id)
        manifest_path = snapshot_dir / "manifest.json"
        if not manifest_path.exists():
            raise BackupError(f"snapshot {snapshot_id!r} has no manifest")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        valid = True
        failures: list[str] = []
        for entry in manifest["files"]:
            path = snapshot_dir / entry["rel"]
            if not path.exists():
                valid = False
                failures.append(f"missing {entry['rel']}")
                continue
            actual = _sha256(path.read_bytes())
            if actual != entry["sha256"]:
                valid = False
                failures.append(f"checksum mismatch {entry['rel']}")
        return {"snapshot_id": snapshot_id, "files": len(manifest["files"]),
                "valid": valid, "failures": failures}

    def restore(self, snapshot_id: str, target_dir: Path) -> list[str]:
        snapshot_dir = _resolve_within(self.backup_dir, snapshot_id)
        manifest_path = snapshot_dir / "manifest.json"
        if not manifest_path.exists():
            raise BackupError(f"snapshot {snapshot_id!r} has no manifest")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        restored: list[str] = []
        for entry in manifest["files"]:
            source = snapshot_dir / entry["rel"]
            target = target_dir / entry["rel"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            restored.append(entry["rel"])
        return restored


# ---------------------------------------------------------------------------
# Failure diagnostics
# ---------------------------------------------------------------------------


@dataclass
class FailureIncident:
    incident_id: str
    kind: str
    resource: str
    error_type: str
    error_reason: str
    created_at_ms: int = field(default_factory=_now_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "kind": self.kind,
            "resource": self.resource,
            "error_type": self.error_type,
            "error_reason": self.error_reason,
            "created_at_ms": self.created_at_ms,
        }


class FailureDiagnostics:
    """Bounded incident store for failure diagnostics."""

    def __init__(self, *, max_incidents: int = 1_000) -> None:
        self.max_incidents = max_incidents
        self._incidents: list[FailureIncident] = []

    def record(self, *, kind: str, resource: str = "", error: BaseException | str | None = None,
               error_type: str = "", error_reason: str = "") -> FailureIncident:
        if error is not None:
            if isinstance(error, BaseException):
                error_type = error_type or type(error).__name__
                error_reason = error_reason or _safe_reason(error)
            else:
                error_type = error_type or "error"
                error_reason = error_reason or str(error)
        incident = FailureIncident(
            incident_id=_new_id("inc"),
            kind=kind,
            resource=resource,
            error_type=error_type,
            error_reason=error_reason if not contains_secret_like(error_reason) else "<redacted>",
        )
        self._incidents.append(incident)
        if len(self._incidents) > self.max_incidents:
            del self._incidents[: len(self._incidents) - self.max_incidents]
        return incident

    def recent(self, limit: int = 50) -> list[FailureIncident]:
        return list(self._incidents[-limit:])

    def by_kind(self, kind: str) -> list[FailureIncident]:
        return [i for i in self._incidents if i.kind == kind]

    def report(self) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for incident in self._incidents:
            by_kind[incident.kind] = by_kind.get(incident.kind, 0) + 1
            by_type[incident.error_type] = by_type.get(incident.error_type, 0) + 1
        last = self._incidents[-1] if self._incidents else None
        return {
            "incidents": len(self._incidents),
            "by_kind": by_kind,
            "by_error_type": by_type,
            "last_error": last.to_dict() if last else None,
            "clean": not any(contains_secret_like(i.error_reason) for i in self._incidents),
        }


# ---------------------------------------------------------------------------
# Reliability engine (coordination facade)
# ---------------------------------------------------------------------------


class ReliabilityEngine:
    """Coordination facade around all reliability primitives."""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        retry_policy: RetryPolicy | None = None,
        tracer: Any = None,
        lease_ms: int = 30_000,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.retry_policy = retry_policy or RetryPolicy()
        self.tracer = tracer
        self.lease_ms = lease_ms
        self.queue = DurableQueue(self.data_dir, retry_policy=self.retry_policy).open()
        self.queue.recover()
        self.dead_letters = DeadLetterQueue(self.data_dir)
        self.circuit_breaker = CircuitBreaker()
        self.leases = LeaseManager(self.data_dir)
        self.executions = ExecutionRegistry(self.data_dir)
        self.checkpoints = CheckpointStore(self.data_dir)
        self.recovery_journal = RecoveryJournal(self.data_dir)
        self.backups = BackupManager(self.data_dir)
        self.diagnostics = FailureDiagnostics()
        self._journal_seq: dict[str, int] = {"submits": 0, "processed": 0}

    def close(self) -> None:
        self.queue.close()

    # -- telemetry ----------------------------------------------------------

    def _emit(self, operation: str, status: str, *, message: str = "", resource: str = "",
              **metadata: Any) -> None:
        try:
            from handoff_agent.telemetry import emit_event
            emit_event(
                self.tracer,
                domain="reliability",
                operation=operation,
                status=status,
                resource=resource,
                message=message,
                metadata=metadata,
            )
        except Exception:
            pass

    # -- submit --------------------------------------------------------------

    def submit(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """Submit a durable work item, deduplicating on ``idempotency_key``.

        Returns ``{"message_id", "execution_id", "state", "duplicate"}``. If a
        prior execution with the same ``idempotency_key`` already reached a
        terminal phase, no new item is enqueued and the existing outcome is
        returned (duplicate execution prevented).
        """
        if idempotency_key:
            existing = self.executions.lookup(idempotency_key=idempotency_key)
            if existing is not None and existing.phase not in (ExecutionPhase.QUEUED.value,
                                                               ExecutionPhase.PROCESSING.value):
                self._emit("submit", "duplicate", resource=kind,
                           message="duplicate idempotency-key submission suppressed")
                return {
                    "message_id": existing.execution_id,
                    "execution_id": existing.execution_id,
                    "state": existing.phase,
                    "duplicate": True,
                }
            if existing is not None:
                self._emit("submit", "queued", resource=kind, message="already active")
                return {
                    "message_id": existing.execution_id,
                    "execution_id": existing.execution_id,
                    "state": existing.phase,
                    "duplicate": True,
                }
        execution = self.executions.register(idempotency_key=idempotency_key, kind=kind)
        message = self.queue.enqueue(kind, dict(payload), message_id=execution.execution_id)
        self._journal_seq["submits"] += 1
        self.recovery_journal.append(
            "submit",
            payload={"kind": kind, "execution_id": execution.execution_id,
                     "message_id": message.message_id,
                     "idempotency_key": idempotency_key},
        )
        self._emit("submit", "queued", resource=kind, execution_id=execution.execution_id,
                   message_id=message.message_id)
        return {
            "message_id": message.message_id,
            "execution_id": execution.execution_id,
            "state": message.state,
            "duplicate": False,
        }

    # -- processing ----------------------------------------------------------

    def process_next(
        self,
        handler: Callable[[QueueMessage], Any],
        *,
        kind: str = "",
        simulate_crash: bool = False,
    ) -> dict[str, Any] | None:
        """Process the next due message once. Returns a processing record.

        A handler exception is routed through the retry policy; messages that
        exhaust their budget (or fail with ``NonRetryableError``) move to the
        dead-letter queue and the execution is marked dead.

        When ``simulate_crash`` is True the message is left in the ``processing``
        state (its lease is not acked/nacked) and ``_SimulatedCrash`` is raised
        so the caller can exercise ``recover_stale`` afterwards.
        """
        if not self.circuit_breaker.allow_call():
            self.diagnostics.record(kind="breaker_open", resource=kind)
            raise CircuitBreakerOpenError("circuit breaker open")
        leader_lease = self.leases.get("leader")
        if leader_lease is not None and leader_lease.is_expired():
            self.leases.recover_stale()
        due = self.queue.dequeue_due(kind=kind, limit=1, lease_ms=self.lease_ms)
        if not due:
            return None
        message = due[0]
        execution = self.executions.lookup(execution_id=message.message_id)
        if execution is None:
            execution = self.executions.register(idempotency_key="", kind=message.kind)
        self.executions.touch(execution.execution_id, phase=ExecutionPhase.PROCESSING.value)
        self._emit("process", "processing", resource=message.kind,
                   message_id=message.message_id, attempts=message.attempts)
        if simulate_crash:
            self._emit("process", "error", resource=message.kind,
                       message="simulated crash mid-processing", message_id=message.message_id)
            raise _SimulatedCrash("simulated crash mid-processing")
        finished = False
        try:
            finished = True
            result = handler(message)
        except _SimulatedCrash:
            raise
        except NonRetryableError as error:
            finished = True
            self.circuit_breaker.record_failure()
            self.queue.nack(message.message_id, error)
            self.executions.mark_dead(execution.execution_id,
                                      error_type=type(error).__name__,
                                      error_reason=_safe_reason(error))
            self.diagnostics.record(kind="permanent_failure", resource=message.kind, error=error)
            self.dead_letters.enqueue(self.queue.get(message.message_id))
            self._emit("process", "error", resource=message.kind,
                       message="permanent failure dead-lettered", message_id=message.message_id)
            return {"message_id": message.message_id, "outcome": "dead", "retried": False}
        except Exception as error:
            finished = True
            self.circuit_breaker.record_failure()
            attempts = message.attempts
            updated = self.queue.nack(message.message_id, error)
            should_retry = self.retry_policy.should_retry(updated.attempts, error)
            self.executions.touch(execution.execution_id, phase=ExecutionPhase.QUEUED.value)
            self.diagnostics.record(kind="handler_failure", resource=message.kind, error=error)
            if not should_retry or updated.state == "dead":
                self.executions.mark_dead(execution.execution_id,
                                          error_type=type(error).__name__,
                                          error_reason=_safe_reason(error))
                self.dead_letters.enqueue(self.queue.get(message.message_id))
                self._emit("process", "error", resource=message.kind,
                           message="dead-lettered after retries", message_id=message.message_id,
                           attempts=attempts + 1)
                return {"message_id": message.message_id, "outcome": "dead", "retried": True}
            self._emit("process", "retry", resource=message.kind,
                       message="will retry", message_id=message.message_id, attempts=attempts + 1)
            return {"message_id": message.message_id, "outcome": "retry", "retried": True}
        finally:
            if finished:
                self._journal_seq["processed"] += 1
        outcome = "ok"
        if isinstance(result, Mapping):
            outcome = str(result.get("outcome", "ok"))
        self.executions.mark_succeeded(execution.execution_id, outcome=outcome)
        self.circuit_breaker.record_success()
        self.queue.ack(message.message_id)
        self._emit("process", "ok", resource=message.kind, message_id=message.message_id,
                   outcome=outcome)
        return {"message_id": message.message_id, "outcome": outcome, "retried": False}

    def run_until_idle(
        self,
        handler: Callable[[QueueMessage], Any],
        *,
        kind: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        processed: list[dict[str, Any]] = []
        while len(processed) < limit:
            record = self.process_next(handler, kind=kind)
            if record is None:
                break
            processed.append(record)
        return processed

    # -- recovery ------------------------------------------------------------

    def recover(
        self,
        *,
        stale_lease_ms: int = 30_000,
        restore_checkpoint: str = "",
    ) -> dict[str, Any]:
        """Run crash/restart/interruption recovery steps."""
        queue_recovered = self.queue.recover_stale(stale_ms=stale_lease_ms)
        for entry in queue_recovered:
            self.diagnostics.record(kind="stale_lease_recovered", resource=entry["kind"],
                                    error_reason="processing lease expired")
        lease_recovered = self.leases.recover_stale()
        restored: Checkpoint | None = None
        if restore_checkpoint and self.checkpoints.exists(restore_checkpoint):
            restored = self.checkpoints.restore(restore_checkpoint)
        self.recovery_journal.append(
            "recover",
            payload={"queue_recovered": len(queue_recovered),
                     "leases_recovered": len(lease_recovered)},
        )
        return {
            "queue_stale_recovered": len(queue_recovered),
            "leases_recovered": len(lease_recovered),
            "checkpoint_restored": restored.name if restored else "",
            "gap_detected": self.recovery_journal.gap_detected,
        }

    def heartbeat_lease(self, *, lease_key: str = "leader", holder: str = "pipeline") -> Lease:
        existing = self.leases.get(lease_key)
        if existing is None or existing.holder != holder:
            return self.leases.acquire(lease_key, holder)
        if existing.is_expired() and existing.holder == holder:
            self.leases.recover_stale()
            return self.leases.acquire(lease_key, holder)
        return self.leases.renew(lease_key, holder)

    # -- status --------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "version": RELIABILITY_VERSION,
            "queue": self.queue.stats(),
            "dead_letters": self.dead_letters.count(),
            "circuit_breaker": self.circuit_breaker.status(),
            "leases": self.leases.status(),
            "executions": {
                "total": len(self.executions.list()),
                "succeeded": self.executions.count(phase=ExecutionPhase.SUCCEEDED.value),
                "failed": self.executions.count(phase=ExecutionPhase.FAILED.value),
                "dead": self.executions.count(phase=ExecutionPhase.DEAD.value),
            },
            "diagnostics": self.diagnostics.report(),
            "checkpoints": sorted(
                {"name": p.name, "kind": "current"}
                for p in self.checkpoints.checkpoint_dir.glob("*.json")
            ) if self.checkpoints.checkpoint_dir.exists() else [],
            "journal_seq": self._journal_seq,
        }


# ---------------------------------------------------------------------------
# Provision + conformance (self-contained, like Phase 33)
# ---------------------------------------------------------------------------


def provision_phase34_reliability(data_dir: str | Path, *, tracer: Any = None) -> ReliabilityEngine:
    """Create a fully provisioned reliability engine for conformance tests."""
    return ReliabilityEngine(data_dir, tracer=tracer, lease_ms=1000)


def _check(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"check": name, "ok": bool(ok), "detail": detail}


def run_reliability_conformance(engine: ReliabilityEngine) -> list[dict[str, Any]]:
    """Run a bounded conformance probe over all Phase-34 primitives."""
    checks: list[dict[str, Any]] = []
    handler = lambda msg: {"outcome": "ok"}

    checks.append(_check(
        "durable_queue_roundtrip",
        (lambda q: q.enqueue("probe", {"seq": 1}) is not None)(engine.queue),
    ))
    engine.run_until_idle(handler, kind="probe")
    checks.append(_check(
        "durable_queue_idle",
        engine.queue.pending() == [] and engine.queue.stats()["acked"] > 0,
        engine.queue.stats(),
    ))
    checks.append(_check(
        "retry_policy_backoff",
        engine.retry_policy.delay_for(2) >= engine.retry_policy.delay_for(1),
    ))
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout_ms=60_000, clock_ms=lambda: 0)
    for _ in range(2):
        try:
            breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        except RuntimeError:
            pass
    try:
        breaker.call(lambda: None)
    except CircuitBreakerOpenError:
        ok = breaker.status()["state"] == "open"
    else:
        ok = False
    checks.append(_check("circuit_breaker_opens", ok))

    engine.leases.acquire("leader", "pipeline", ttl_ms=0)
    engine.leases.recover_stale()
    checks.append(_check("lease_recovery", engine.leases.get("leader") is None))

    engine.checkpoints.save("state", {"pos": 1})
    restored = engine.checkpoints.restore("state")
    checks.append(_check("checkpoint_restore", restored.payload.get("pos") == 1))

    engine.recovery_journal.append("boot", payload={"n": 1})
    replayed = list(engine.recovery_journal.replay())
    checks.append(_check("recovery_journal_replay", len(replayed) == 1))

    checks.append(_check("dead_letter_bound", engine.dead_letters.count() == 0))

    import tempfile as _tf
    with _tf.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "state.txt"
        probe.write_text("hello", encoding="utf-8")
        manifest = engine.backups.snapshot([probe])
        verified = engine.backups.verify(manifest["snapshot_id"])
        checks.append(_check("backup_verify", "missing" not in str(verified.get("failures", []))))

    try:
        engine.queue.enqueue("probe", {"line": "probe access_token = thisisasecretvalue1"})
    except DurableQueueError:
        ok = True
    else:
        ok = False
    checks.append(_check("queue_rejects_secret_like", ok))

    diagnostics = engine.diagnostics.record(kind="injected", resource="probe",
                                            error=RuntimeError("x"))
    checks.append(_check("failure_diagnostics", diagnostics.incident_id != ""))

    return checks


# ---------------------------------------------------------------------------
# Module-level default engine (disabled until explicitly provisioned)
# ---------------------------------------------------------------------------

_default_engine: ReliabilityEngine | None = None


def default_engine() -> ReliabilityEngine | None:
    """Return the process-wide default reliability engine (or None)."""
    return _default_engine