"""Phase 30 — Advanced Observability, Telemetry & Execution Intelligence.

Universal execution telemetry for Handoff Agent. Provides a canonical
telemetry event envelope, trace/span relationships, structured execution
logs, duration/latency metrics, failure classification, health metrics,
degraded-state detection, execution timelines, diagnostic/execution reports,
secret redaction, retention limits, and head-based sampling.

Security guarantees (boundary preserved from all prior phases):
  - No secrets, API keys, credentials, passwords, or private keys may be
    emitted, logged, persisted, or transported.
  - Sensitive payload fields and protected metadata are redacted at the
    point of emission.
  - This module performs NO filesystem access and NO network access. It only
    records in-memory events/metrics for the caller to persist or serve.
  - Telemetry can be disabled or forced to local-only mode.
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Deque, Iterable, Mapping

# ---------------------------------------------------------------------------
# Canonical domains / statuses / event types
# ---------------------------------------------------------------------------


class TelemetryDomain(str, Enum):
    WORKFLOW = "workflow"
    TASK = "task"
    AGENT = "agent"
    PROVIDER = "provider"
    HANDOFF = "handoff"
    MESSAGE = "message"
    REMOTE = "remote"
    SYNC = "sync"
    SYSTEM = "system"
    POLICY = "policy"
    TOOL = "tool"
    PROJECT = "project"
    DECISION = "decision"
    WORKORDER = "workorder"
    QUALITY = "quality"
    DEPLOYMENT = "deployment"


class TelemetryStatus(str, Enum):
    STARTED = "started"
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    RETRY = "retry"
    DEGRADED = "degraded"
    QUEUED = "queued"
    PROCESSING = "processing"
    BLOCKED = "blocked"


class EventType(str, Enum):
    SPAN_START = "span_start"
    SPAN_END = "span_end"
    METRIC = "metric"
    DIAGNOSTIC = "diagnostic"
    HEALTH = "health"
    LOG = "log"
    AGGREGATE = "aggregate"


_KNOWN_SECRET_KEYS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "api-key",
    "access_token",
    "accesstoken",
    "auth_token",
    "authtoken",
    "authorization",
    "client_secret",
    "clientsecret",
    "password",
    "passwd",
    "pwd",
    "secret",
    "private_key",
    "privatekey",
    "credential",
    "credentials",
    "token",
    "session_token",
    "refresh_token",
)

# Secret-value patterns used to scrub anything before it leaves the collector.
# Mirrors the shape used across the project (key=value / private key blocks)
# so that no secret value can be emitted in any field.
_SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"""(?i)(?:api[_\-]?key|apikey)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-]{16,}"""),
    re.compile(r"""(?i)(?:secret[_\-]?key|secretkey)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-]{16,}"""),
    re.compile(r"""(?i)(?:access[_\-]?token|accesstoken)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-\.]{16,}"""),
    re.compile(r"""(?i)(?:password|passwd|pwd)\s*['"]?\s*[:=]\s*['"]?['"]?[^\s'"'\n]{8,}"""),
    re.compile(r"""(?i)(?:client[_\-]?secret|clientsecret)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-]{16,}"""),
    re.compile(r"""(?i)(?:auth[_\-]?token|authtoken)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-]{16,}"""),
    re.compile(r"""(?i)\bbearer\s+[a-z0-9_\-\.]{8,}"""),
    re.compile(r"""(?i)\bsk-[a-z0-9_\-]{16,}"""),
    re.compile(r"""(?i)\bgh[pousr]_[a-z0-9]{20,}"""),
    re.compile(r"""(?i)\bak-[a-z0-9]{20,}"""),
    re.compile(r"""(?i)xox[baprs]-[a-z0-9\-]{10,}"""),
    re.compile(r"""(?i)\btoken\b\s*['"]?\s*[:=]\s*['"]?[a-z0-9_\-\.]{16,}"""),
    re.compile(r"""(?i)(?:private[_\-]?key)\s*['"]?\s*[:=]"""),
    re.compile(r"""['"](?:ghp_[a-z0-9]{36,}|sk-[a-z0-9]{20,}|xox[bpsar]-[a-z0-9\-]{10,})"""),
    re.compile(r"""-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----.*-----END (?:RSA |EC |DSA )?PRIVATE KEY-----""", re.DOTALL),
)

_REDACTION_MARK = "[redacted]"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_epoch_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    """Generate a random, non-sequential ID (never derived from secrets)."""
    return f"{prefix}-{uuid.uuid4().hex}"


def make_trace_id() -> str:
    return new_id("trace")


def make_span_id() -> str:
    return new_id("span")


def make_event_id() -> str:
    return new_id("evt")


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def _is_secret_key(key: str) -> bool:
    lowered = key.lower().strip()
    for known in _KNOWN_SECRET_KEYS:
        if lowered == known or lowered.endswith(f".{known}") or f".{known}." in lowered:
            return True
    if lowered.endswith("_key") or lowered.endswith("-key"):
        return True
    return False


def redact_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Redact a mapping by secret-like keys then redact remaining values."""
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        if _is_secret_key(str(key)):
            out[str(key)] = _REDACTION_MARK
        else:
            out[str(key)] = redact_value(value)
    return out


def redact_value(value: Any) -> Any:
    """Recursively redact secret values from arbitrary structured data."""
    if isinstance(value, Mapping):
        return redact_mapping(value)
    if isinstance(value, (list, tuple)):
        return [redact_value(v) for v in value]
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8", errors="replace")
        except Exception:
            return _REDACTION_MARK
        return redact_text(text)
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(text: str) -> str:
    """Scrub secret-shaped values from a string without echoing the secret."""
    if not text:
        return text
    out = text
    for pattern in _SECRET_VALUE_PATTERNS:
        out = pattern.sub(_REDACTION_MARK, out)
    return out


def is_sensitive_value(value: Any) -> bool:
    """Return True if *value* contains secret-shaped content."""
    scanned = redact_mapping(value) if isinstance(value, Mapping) else redact_value(value)
    canonical = str(scanned)
    return _REDACTION_MARK in canonical


# ---------------------------------------------------------------------------
# Merkle-style trace correlation
# ---------------------------------------------------------------------------


def _hash_chain(*parts: str) -> str:
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Canonical event envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TelemetryEvent:
    """Canonical telemetry event envelope.

    Every execution activity is emitted as a ``TelemetryEvent``. The envelope
    is immutable and fully serializable. All structured fields are redacted at
    emission time, so the event never carries secrets.
    """

    event_id: str
    trace_id: str
    span_id: str
    parent_span_id: str
    domain: str
    operation: str
    status: str
    event_type: str
    timestamp: str
    actor: str
    resource: str
    event_chain: str
    duration_ms: int
    queue_ms: int
    processing_ms: int
    network_ms: int
    retry_count: int
    failure_count: int
    success_count: int
    timeout_count: int
    cancellation_count: int
    latency_ms: int
    error_type: str
    error_reason: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)
    sampled: bool = False

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "event_id": self.event_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "domain": self.domain,
            "operation": self.operation,
            "status": self.status,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "actor": self.actor,
            "resource": self.resource,
            "event_chain": self.event_chain,
            "duration_ms": self.duration_ms,
            "queue_ms": self.queue_ms,
            "processing_ms": self.processing_ms,
            "network_ms": self.network_ms,
            "retry_count": self.retry_count,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "timeout_count": self.timeout_count,
            "cancellation_count": self.cancellation_count,
            "latency_ms": self.latency_ms,
            "error_type": self.error_type,
            "error_reason": self.error_reason,
            "message": self.message,
            "sampled": self.sampled,
        }
        if redact:
            data["metadata"] = redact_mapping(self.metadata)
            data["error_reason"] = redact_text(self.error_reason)
            data["message"] = redact_text(self.message)
        else:
            data["metadata"] = dict(self.metadata)
        return data

    def matches(self, **filters: Any) -> bool:
        for key, expect in filters.items():
            if key in ("metadata", "message", "error_reason"):
                continue
            if str(getattr(self, key, "")) != str(expect):
                return False
        return True

    def chain(self) -> str:
        return self.event_chain


# ---------------------------------------------------------------------------
# Span / trace
# ---------------------------------------------------------------------------


@dataclass
class Span:
    """A scoped execution span within a trace."""

    span_id: str
    trace_id: str
    parent_span_id: str
    domain: str
    operation: str
    resource: str
    started_at_ms: int
    status: str = TelemetryStatus.STARTED.value
    error_type: str = ""
    error_reason: str = ""
    duration_ms: int = 0
    event_ids: list[str] = field(default_factory=list)

    def end(self, status: str = TelemetryStatus.OK.value) -> "Span":
        self.duration_ms = max(0, _now_epoch_ms() - self.started_at_ms)
        if not self.error_type and status in (
            TelemetryStatus.ERROR.value,
            TelemetryStatus.TIMEOUT.value,
            TelemetryStatus.CANCELLED.value,
        ):
            self.error_type = status
        self.status = status
        return self


@dataclass
class Trace:
    """An execution trace: root event + ordered spans + all events."""

    trace_id: str
    started_at: str
    root_span_id: str
    domain: str
    spans: list[Span] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    status: str = TelemetryStatus.STARTED.value
    completed_at: str = ""
    error_type: str = ""
    error_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "root_span_id": self.root_span_id,
            "domain": self.domain,
            "status": self.status,
            "error_type": self.error_type,
            "error_reason": redact_text(self.error_reason),
            "spans": [
                {
                    "span_id": s.span_id,
                    "parent_span_id": s.parent_span_id,
                    "domain": s.domain,
                    "operation": s.operation,
                    "resource": s.resource,
                    "status": s.status,
                    "error_type": redact_text(s.error_type),
                    "error_reason": redact_text(s.error_reason),
                    "duration_ms": s.duration_ms,
                    "event_count": len(s.event_ids),
                }
                for s in self.spans
            ],
        }


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


_ERROR_REASON_GROUPS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("timeout", "timed out", "timed_out", "deadline"), "timeout"),
    (("cancel", "cancelled", "abort"), "cancelled"),
    (("auth", "authorization", "permission", "403", "401"), "permission"),
    (("not found", "404", "unknown", "unregistered"), "not_found"),
    (("network", "dns", "connection", "socket", "tls", "ssl"), "network"),
    (("http 4", "http 5", "rate", "429", "500", "503"), "http_error"),
    (("invalid", "malformed", "schema", "validation"), "validation"),
    (("secret", "credential", "api key"), "secret"),
    (("resource", "quota", "limit", "exhausted"), "resource_limit"),
    (("crash", "runtime", "unexpected", "internal"), "internal"),
)


def classify_error(error: Exception | str | None) -> tuple[str, str]:
    """Return (error_type, safe_error_reason).

    ``error`` may be an exception or a string. The reason is redacted.
    Classification is deterministic and never echoes secrets.
    """
    if error is None:
        return "", ""
    if isinstance(error, BaseException):
        reason = f"{type(error).__name__}: {error}"
    else:
        reason = str(error)
    reason_lower = reason.lower()
    for keywords, category in _ERROR_REASON_GROUPS:
        for kw in keywords:
            if kw in reason_lower:
                return category, redact_text(reason)
    return "unknown", redact_text(reason)


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


class Sampler:
    """Head-based trace sampler.

    Sampling is deterministic per trace_id so all events of a trace share the
    same decision. Failure/timeout/cancelled events are always retained.
    """

    def __init__(self, rate: float = 1.0) -> None:
        if not 0.0 <= rate <= 1.0:
            raise ValueError("sampling rate must be within [0.0, 1.0]")
        self.rate = rate

    def sample(self, trace_id: str, status: str = TelemetryStatus.OK.value) -> bool:
        if status in (
            TelemetryStatus.ERROR.value,
            TelemetryStatus.TIMEOUT.value,
            TelemetryStatus.CANCELLED.value,
        ):
            return True
        if self.rate >= 1.0:
            return True
        if self.rate <= 0.0:
            return False
        digest = hashlib.sha256(trace_id.encode("utf-8")).hexdigest()
        bucket = int(digest[:8], 16) / (16**8)
        return bucket < self.rate


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetentionPolicy:
    """Telemetry retention / size limits.

    ``max_events`` limits the in-memory event ring buffer.
    ``max_sampled_events`` applies when sampling is enabled.
    ``max_traces`` limits retained trace summaries.
    """

    max_events: int = 10_000
    max_sampled_events: int = 2_000
    max_traces: int = 1_000

    def resolved_events(self, sampling_enabled: bool) -> int:
        if sampling_enabled:
            return self.max_sampled_events
        return self.max_events


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@dataclass
class LatencyStats:
    """Aggregate latency statistics (min/avg/max/count) in milliseconds."""

    count: int = 0
    total_ms: int = 0
    min_ms: int = 0
    max_ms: int = 0

    def record(self, ms: int) -> None:
        self.count += 1
        self.total_ms += ms
        self.min_ms = ms if self.count == 1 else min(self.min_ms, ms)
        self.max_ms = max(self.max_ms, ms)

    @property
    def avg_ms(self) -> float:
        return (self.total_ms / self.count) if self.count else 0.0


@dataclass
class DomainMetrics:
    """Per-domain counters + latency aggregates."""

    domain: str
    success_count: int = 0
    failure_count: int = 0
    retry_count: int = 0
    timeout_count: int = 0
    cancellation_count: int = 0
    queued_count: int = 0
    blocked_count: int = 0
    total_count: int = 0
    latency: LatencyStats = field(default_factory=LatencyStats)
    queue_latency: LatencyStats = field(default_factory=LatencyStats)

    def record(self, event: TelemetryEvent) -> None:
        self.total_count += 1
        status = event.status
        if status == TelemetryStatus.OK.value:
            self.success_count += 1
        elif status == TelemetryStatus.ERROR.value:
            self.failure_count += 1
        elif status == TelemetryStatus.TIMEOUT.value:
            self.timeout_count += 1
        elif status == TelemetryStatus.CANCELLED.value:
            self.cancellation_count += 1
        elif status == TelemetryStatus.RETRY.value:
            self.retry_count += 1
        elif status == TelemetryStatus.QUEUED.value:
            self.queued_count += 1
        elif status == TelemetryStatus.BLOCKED.value:
            self.blocked_count += 1
        if event.latency_ms:
            self.latency.record(event.latency_ms)
        if event.queue_ms:
            self.queue_latency.record(event.queue_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "total_count": self.total_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "retry_count": self.retry_count,
            "timeout_count": self.timeout_count,
            "cancellation_count": self.cancellation_count,
            "queued_count": self.queued_count,
            "blocked_count": self.blocked_count,
            "latency_ms": {
                "count": self.latency.count,
                "min": self.latency.min_ms,
                "avg": round(self.latency.avg_ms, 3),
                "max": self.latency.max_ms,
            },
            "queue_latency_ms": {
                "count": self.queue_latency.count,
                "min": self.queue_latency.min_ms,
                "avg": round(self.queue_latency.avg_ms, 3),
                "max": self.queue_latency.max_ms,
            },
        }


# ---------------------------------------------------------------------------
# Anomaly detection abstraction
# ---------------------------------------------------------------------------


class AnomalyDetector:
    """Abstraction over anomaly detection for telemetry streams."""

    def detect(self, events: Iterable[TelemetryEvent]) -> list[dict[str, Any]]:
        """Return a list of anomaly descriptors. Override in subclasses."""
        raise NotImplementedError


class FailureRateAnomalyDetector(AnomalyDetector):
    """Detect failure-rate spikes above a threshold within a domain."""

    def __init__(self, failure_threshold: float = 0.5, min_events: int = 5) -> None:
        self.failure_threshold = failure_threshold
        self.min_events = min_events

    def detect(self, events: Iterable[TelemetryEvent]) -> list[dict[str, Any]]:
        by_domain: dict[str, list[TelemetryEvent]] = {}
        for evt in events:
            by_domain.setdefault(evt.domain, []).append(evt)
        anomalies: list[dict[str, Any]] = []
        for domain, evts in by_domain.items():
            failures = sum(
                1
                for e in evts
                if e.status in (
                    TelemetryStatus.ERROR.value,
                    TelemetryStatus.TIMEOUT.value,
                )
            )
            if len(evts) >= self.min_events and failures / len(evts) >= self.failure_threshold:
                anomalies.append(
                    {
                        "type": "failure_rate",
                        "domain": domain,
                        "events": len(evts),
                        "failures": failures,
                        "rate": round(failures / len(evts), 4),
                    }
                )
        return anomalies


# ---------------------------------------------------------------------------
# Degraded-state detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DegradedState:
    """A degraded-state report for a domain or resource."""

    key: str
    reason: str
    failure_rate: float
    total: int
    failures: int
    detected_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "reason": self.reason,
            "failure_rate": round(self.failure_rate, 4),
            "total": self.total,
            "failures": self.failures,
            "detected_at": self.detected_at,
        }


def detect_degraded_resources(
    events: Iterable[TelemetryEvent],
    *,
    failure_threshold: float = 0.6,
    min_events: int = 3,
    concurrency_violation_threshold: int = 20,
) -> list[DegradedState]:
    """Detect degraded state at the resource level.

    A resource is degraded when its error/timeout/cancellation rate exceeds
    ``failure_threshold`` over at least ``min_events`` events.
    """
    by_resource: dict[str, dict[str, Any]] = {}
    for evt in events:
        key = f"{evt.domain}:{evt.resource}"
        entry = by_resource.setdefault(
            key, {"total": 0, "failures": 0, "retries": 0, "timeouts": 0, "cancels": 0}
        )
        entry["total"] += 1
        if evt.status in (
            TelemetryStatus.ERROR.value,
            TelemetryStatus.TIMEOUT.value,
        ):
            entry["failures"] += 1
        if evt.status == TelemetryStatus.RETRY.value:
            entry["retries"] += 1
        if evt.status == TelemetryStatus.TIMEOUT.value:
            entry["timeouts"] += 1
        if evt.status == TelemetryStatus.CANCELLED.value:
            entry["cancels"] += 1

    degraded: list[DegradedState] = []
    for key, entry in by_resource.items():
        total = entry["total"]
        failures = entry["failures"]
        rate = failures / total if total else 0.0
        if failures and rate >= failure_threshold and total >= min_events:
            reasons = []
            if entry["timeouts"]:
                reasons.append(f"timeouts={entry['timeouts']}")
            if entry["retries"] > concurrency_violation_threshold // 2:
                reasons.append(f"retries={entry['retries']}")
            if entry["cancels"]:
                reasons.append(f"cancelled={entry['cancels']}")
            degraded.append(
                DegradedState(
                    key=key,
                    reason="; ".join(reasons) or "elevated failure rate",
                    failure_rate=rate,
                    total=total,
                    failures=failures,
                    detected_at=_now_iso(),
                )
            )
    degraded.sort(key=lambda d: (-d.failure_rate, d.key))
    return degraded


# ---------------------------------------------------------------------------
# Telemetry collector
# ---------------------------------------------------------------------------


class TelemetryCollector:
    """In-memory telemetry collector.

    Accepts structured events, maintains spans/traces, aggregates domain
    metrics, applies redaction, retention, and sampling, and produces
    reports (timeline, diagnostic, execution, health).

    The collector performs NO filesystem or network access. When ``enabled``
    is False it silently no-ops. ``local_only`` forces sampling rate to 1.0
    and disables any external delivery hooks.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        local_only: bool = True,
        sampler: Sampler | None = None,
        retention: RetentionPolicy | None = None,
        anomaly_detector: AnomalyDetector | None = None,
    ) -> None:
        self.enabled = enabled
        self.local_only = local_only
        self.sampler = sampler or Sampler(rate=1.0 if local_only else 0.9)
        self.retention = retention or RetentionPolicy()
        self.anomaly_detector = anomaly_detector or FailureRateAnomalyDetector()
        self.reset()

    # -- lifecycle ----------------------------------------------------------

    def reset(self) -> None:
        """Clear all collected events/spans/metrics."""
        self._events: Deque[TelemetryEvent] = deque()
        self._spans: dict[str, Span] = {}
        self._traces: dict[str, Trace] = {}
        self._domain_metrics: dict[str, DomainMetrics] = {}
        self._event_count = 0
        self._dropped_count = 0

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def set_local_only(self, local_only: bool) -> None:
        self.local_only = local_only
        if local_only:
            self.sampler = Sampler(rate=1.0)

    # -- emission ------------------------------------------------------------

    def _next_event(self, event_id: str, trace_id: str, span_id: str) -> str:
        prev = ""
        span = self._spans.get(span_id)
        if span and span.event_ids:
            prev_event_ids = span.event_ids
            prev = prev_event_ids[-1]
        if not prev:
            trace = self._traces.get(trace_id)
            if trace and trace.event_ids:
                prev = trace.event_ids[-1]
        return _hash_chain(trace_id, prev, event_id)

    def emit(
        self,
        *,
        domain: str,
        operation: str,
        status: str = TelemetryStatus.OK.value,
        event_type: str = EventType.LOG.value,
        trace_id: str = "",
        span_id: str = "",
        parent_span_id: str = "",
        actor: str = "system",
        resource: str = "",
        duration_ms: int = 0,
        queue_ms: int = 0,
        processing_ms: int = 0,
        network_ms: int = 0,
        retry_count: int = 0,
        latency_ms: int = 0,
        error_type: str = "",
        error_reason: str = "",
        message: str = "",
        metadata: Mapping[str, Any] | None = None,
        event_id: str = "",
    ) -> TelemetryEvent | None:
        """Emit a telemetry event. Returns the event or None if not sampled."""
        if not self.enabled:
            return None
        trace_id = trace_id or make_trace_id()
        if not span_id:
            span_id = make_span_id()
            self._attach_span(
                span_id=span_id,
                trace_id=trace_id,
                parent_span_id=parent_span_id,
                domain=domain,
                operation=operation,
                resource=resource,
            )
        sampled = self.sampler.sample(trace_id, status)
        event_id = event_id or make_event_id()
        raw_metadata = dict(metadata or {})
        sanitized_metadata = redact_mapping(raw_metadata)
        event = TelemetryEvent(
            event_id=event_id,
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            domain=domain,
            operation=operation,
            status=status,
            event_type=event_type,
            timestamp=_now_iso(),
            actor=actor,
            resource=resource,
            event_chain="",
            duration_ms=max(0, int(duration_ms)),
            queue_ms=max(0, int(queue_ms)),
            processing_ms=max(0, int(processing_ms)),
            network_ms=max(0, int(network_ms)),
            retry_count=max(0, int(retry_count)),
            failure_count=1 if status == TelemetryStatus.ERROR.value else 0,
            success_count=1 if status == TelemetryStatus.OK.value else 0,
            timeout_count=1 if status == TelemetryStatus.TIMEOUT.value else 0,
            cancellation_count=1 if status == TelemetryStatus.CANCELLED.value else 0,
            latency_ms=max(0, int(latency_ms)),
            error_type=error_type,
            error_reason=redact_text(error_reason),
            message=redact_text(message),
            metadata=sanitized_metadata,
            sampled=sampled,
        )
        if not sampled:
            self._event_count += 1
            self._dropped_count += 1
            return None

        chain = self._next_event(event_id, trace_id, span_id)
        event = TelemetryEvent(
            **{**event.__dict__, "event_chain": chain}
        )
        self._events.append(event)
        self._event_count += 1
        self._record_span_event(event)
        self._record_trace_event(event)
        self._domain_metrics.setdefault(
            event.domain, DomainMetrics(domain=event.domain)
        ).record(event)
        self._enforce_retention()
        return event

    def start_span(
        self,
        *,
        domain: str,
        operation: str,
        resource: str = "",
        trace_id: str = "",
        parent_span_id: str = "",
        actor: str = "system",
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        """Start a new span; returns its span_id."""
        if not self.enabled:
            return ""
        if not trace_id:
            if parent_span_id and parent_span_id in self._spans:
                trace_id = self._spans[parent_span_id].trace_id
            else:
                trace_id = make_trace_id()
        span_id = make_span_id()
        self._attach_span(
            span_id=span_id,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            domain=domain,
            operation=operation,
            resource=resource,
        )
        self.emit(
            domain=domain,
            operation=operation,
            status=TelemetryStatus.STARTED.value,
            event_type=EventType.SPAN_START.value,
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            actor=actor,
            resource=resource,
            metadata=metadata,
        )
        return span_id

    def end_span(
        self,
        span_id: str,
        status: str = TelemetryStatus.OK.value,
        *,
        error: Exception | str | None = None,
        error_type: str = "",
        error_reason: str = "",
        result: Mapping[str, Any] | None = None,
    ) -> TelemetryEvent | None:
        """End a span; emits a span_end event with duration."""
        if not self.enabled or not span_id:
            return None
        span = self._spans.get(span_id)
        if span is None:
            return None
        if error is not None:
            err_type, safe_reason = classify_error(error)
            status = (
                TelemetryStatus.TIMEOUT.value
                if err_type == "timeout"
                else (
                    TelemetryStatus.CANCELLED.value
                    if err_type == "cancelled"
                    else TelemetryStatus.ERROR.value
                )
            )
            error_type = error_type or err_type
            error_reason = error_reason or safe_reason
        span.end(status)
        if error_type:
            span.error_type = error_type
        if error_reason:
            span.error_reason = error_reason
        trace = self._traces.get(span.trace_id)
        if trace and status != TelemetryStatus.STARTED.value:
            trace.status = status
            trace.completed_at = _now_iso()
            if error_type:
                trace.error_type = error_type
            if error_reason:
                trace.error_reason = error_reason
        return self.emit(
            domain=span.domain,
            operation=span.operation,
            status=status,
            event_type=EventType.SPAN_END.value,
            trace_id=span.trace_id,
            span_id=span.span_id,
            parent_span_id=span.parent_span_id,
            resource=span.resource,
            duration_ms=span.duration_ms,
            latency_ms=span.duration_ms,
            error_type=error_type or span.error_type,
            error_reason=error_reason or span.error_reason,
            metadata=dict(result or {}),
        )

    # -- internal bookkeeping -------------------------------------------------

    def _attach_span(
        self,
        *,
        span_id: str,
        trace_id: str,
        parent_span_id: str,
        domain: str,
        operation: str,
        resource: str,
    ) -> None:
        span = Span(
            span_id=span_id,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            domain=domain,
            operation=operation,
            resource=resource,
            started_at_ms=_now_epoch_ms(),
        )
        self._spans[span_id] = span
        if trace_id not in self._traces:
            self._traces[trace_id] = Trace(
                trace_id=trace_id,
                started_at=_now_iso(),
                root_span_id=span_id,
                domain=domain,
            )
        if parent_span_id and parent_span_id in self._spans:
            self._spans[parent_span_id].event_ids.append(span_id)
        trace = self._traces[trace_id]
        trace.spans.append(span)
        if len(self._traces) > self.retention.max_traces:
            oldest = sorted(
                self._traces.values(), key=lambda t: (t.started_at, t.trace_id)
            )
            for old in oldest[: len(self._traces) - self.retention.max_traces]:
                self._traces.pop(old.trace_id, None)
                for span in old.spans:
                    self._spans.pop(span.span_id, None)

    def _record_span_event(self, event: TelemetryEvent) -> None:
        span = self._spans.get(event.span_id)
        if span is not None:
            span.event_ids.append(event.event_id)
        parent = self._spans.get(event.parent_span_id)
        if parent is not None and parent is not span:
            parent.event_ids.append(event.event_id)

    def _record_trace_event(self, event: TelemetryEvent) -> None:
        trace = self._traces.get(event.trace_id)
        if trace is not None:
            trace.event_ids.append(event.event_id)

    def _enforce_retention(self) -> None:
        limit = self.retention.resolved_events(
            self.sampler.rate < 1.0
        )
        while len(self._events) > limit:
            self._events.popleft()

    # -- accessors ------------------------------------------------------------

    def events(self) -> tuple[TelemetryEvent, ...]:
        return tuple(self._events)

    def spans(self) -> tuple[Span, ...]:
        return tuple(self._spans.values())

    def traces(self) -> tuple[Trace, ...]:
        return tuple(self._traces.values())

    def metrics(self) -> dict[str, dict[str, Any]]:
        return {
            name: m.to_dict()
            for name, m in sorted(self._domain_metrics.items())
        }

    def counts(self, **filters: Any) -> int:
        return sum(1 for e in self._events if e.matches(**filters))

    def events_by_domain(self, domain: str) -> tuple[TelemetryEvent, ...]:
        return tuple(e for e in self._events if e.domain == domain)

    def events_by_trace(self, trace_id: str) -> tuple[TelemetryEvent, ...]:
        return tuple(e for e in self._events if e.trace_id == trace_id)

    def total_emitted(self) -> int:
        return self._event_count

    def dropped_count(self) -> int:
        return self._dropped_count

    # -- timeline --------------------------------------------------------------

    def timeline(self, trace_id: str) -> dict[str, Any]:
        """Generate an execution timeline for a trace."""
        events = sorted(
            self.events_by_trace(trace_id), key=lambda e: (e.timestamp, e.event_id)
        )
        trace = self._traces.get(trace_id)
        return {
            "trace_id": trace_id,
            "domain": trace.domain if trace else "",
            "status": trace.status if trace else "",
            "started_at": trace.started_at if trace else "",
            "completed_at": trace.completed_at if trace else "",
            "events_total": len(events),
            "events": [
                {
                    "event_id": e.event_id,
                    "timestamp": e.timestamp,
                    "domain": e.domain,
                    "operation": e.operation,
                    "status": e.status,
                    "event_type": e.event_type,
                    "span_id": e.span_id,
                    "parent_span_id": e.parent_span_id,
                    "resource": e.resource,
                    "duration_ms": e.duration_ms,
                    "latency_ms": e.latency_ms,
                    "error_type": e.error_type,
                    "chain": e.event_chain[-8:],
                    "message": e.message,
                }
                for e in events
            ],
        }

    def timelines(self) -> list[dict[str, Any]]:
        return [self.timeline(tid) for tid in self._traces.keys()]

    # -- anomalies / degraded ----------------------------------------------------

    def anomalies(self) -> list[dict[str, Any]]:
        return self.anomaly_detector.detect(self._events)

    def degraded(self, **kwargs: Any) -> list[DegradedState]:
        return detect_degraded_resources(self._events, **kwargs)

    # -- health ----------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        ok_events = self.counts(status=TelemetryStatus.OK.value)
        error_events = self.counts(status=TelemetryStatus.ERROR.value)
        timeout_events = self.counts(status=TelemetryStatus.TIMEOUT.value)
        cancelled_events = self.counts(status=TelemetryStatus.CANCELLED.value)
        total = len(self._events)
        failure_rate = (error_events + timeout_events) / total if total else 0.0
        degraded = self.degraded()
        return {
            "ok": failure_rate < 0.5,
            "total_events": total,
            "success_count": ok_events,
            "failure_count": error_events,
            "timeout_count": timeout_events,
            "cancellation_count": cancelled_events,
            "failure_rate": round(failure_rate, 4),
            "degraded_count": len(degraded),
            "degraded": [d.to_dict() for d in degraded],
            "anomalies": self.anomalies(),
            "sampling_rate": self.sampler.rate,
            "enabled": self.enabled,
            "local_only": self.local_only,
        }

    # -- reports ----------------------------------------------------------------

    def execution_report(self, trace_id: str | None = None) -> dict[str, Any]:
        """Report of an execution (a trace) or the whole collected execution."""
        if trace_id:
            events = self.events_by_trace(trace_id)
            return self._summary(events, trace_id=trace_id)
        return self._summary(self._events)

    def diagnostic_report(self) -> dict[str, Any]:
        """Full diagnostic report over the collected stream."""
        by_domain: dict[str, dict[str, int]] = {}
        by_error: dict[str, int] = {}
        by_status: dict[str, int] = {}
        by_operation: dict[str, int] = {}
        for evt in self._events:
            by_domain.setdefault(evt.domain, {})
            by_domain[evt.domain][evt.status] = (
                by_domain[evt.domain].get(evt.status, 0) + 1
            )
            by_status[evt.status] = by_status.get(evt.status, 0) + 1
            by_operation[f"{evt.domain}.{evt.operation}"] = (
                by_operation.get(f"{evt.domain}.{evt.operation}", 0) + 1
            )
            if evt.error_type:
                by_error[evt.error_type] = by_error.get(evt.error_type, 0) + 1
        raw_events = [e.to_dict(redact=False) for e in self._events]
        redacted_events = [e.to_dict(redact=True) for e in self._events]
        return {
            "health": self.health(),
            "total_events": len(self._events),
            "emitted_count": self.total_emitted(),
            "dropped_by_sampling": self.dropped_count(),
            "trace_count": len(self._traces),
            "span_count": len(self._spans),
            "by_domain": by_domain,
            "by_status": by_status,
            "by_operation": by_operation,
            "error_classification": by_error,
            "domain_metrics": self.metrics(),
            "anomalies": self.anomalies(),
            "degraded": [d.to_dict() for d in self.degraded()],
            "retention": {
                "max_events": self.retention.max_events,
                "max_traces": self.retention.max_traces,
            },
            "redaction_scan_clean": raw_events == redacted_events,
        }

    def _summary(
        self, events: Iterable[TelemetryEvent], *, trace_id: str = ""
    ) -> dict[str, Any]:
        events = list(events)
        by_status: dict[str, int] = {}
        by_domain: dict[str, int] = {}
        lat = LatencyStats()
        for evt in events:
            by_status[evt.status] = by_status.get(evt.status, 0) + 1
            by_domain[evt.domain] = by_domain.get(evt.domain, 0) + 1
            if evt.latency_ms:
                lat.record(evt.latency_ms)
        return {
            "trace_id": trace_id,
            "events": len(events),
            "by_status": by_status,
            "by_domain": by_domain,
            "latency_ms": {
                "count": lat.count,
                "min": lat.min_ms,
                "avg": round(lat.avg_ms, 3),
                "max": lat.max_ms,
            },
        }

    # -- CLI / API / MCP interfaces ------------------------------------------------

    def cli_payload(self) -> dict[str, Any]:
        """Compact payload for CLI diagnostics (secret-free)."""
        return {"health": self.health(), "counts": self.counts()}

    def api_payload(self, trace_id: str = "") -> dict[str, Any]:
        """Full payload for API diagnostics."""
        if trace_id:
            return self.timeline(trace_id)
        return {
            "health": self.health(),
            "diagnostic": self.diagnostic_report(),
            "timeline_count": len(self._traces),
        }

    def mcp_payload(self) -> dict[str, Any]:
        """Limited payload for MCP diagnostics (no full event dumps)."""
        return {
            "health": self.health(),
            "timeline_count": len(self._traces),
            "span_count": len(self._spans),
        }

    def event_envelope_sample(self, limit: int = 5) -> list[dict[str, Any]]:
        """Return a small, redacted sample of the canonical envelope schema."""
        return [e.to_dict() for e in list(self._events)[:limit]]


# ---------------------------------------------------------------------------
# Trace context manager
# ---------------------------------------------------------------------------


def trace(
    collector: TelemetryCollector,
    *,
    domain: str,
    operation: str,
    resource: str = "",
    trace_id: str = "",
    parent_span_id: str = "",
    actor: str = "system",
):
    """Context manager that starts + ends a span, computing timing metrics.

    Returns a ``_TraceContext`` whose ``.span_id`` and ``.trace_id`` are
    available inside the block. The span end event carries duration, latency,
    and (on exception) a safe classified error.
    """

    class _TraceContext:
        def __init__(self) -> None:
            self.trace_id = ""
            self.span_id = ""
            self.exc: BaseException | None = None

    ctx = _TraceContext()

    import contextlib

    @contextlib.contextmanager
    def _manager():
        if collector.enabled:
            ctx.span_id = collector.start_span(
                domain=domain,
                operation=operation,
                resource=resource,
                trace_id=trace_id,
                parent_span_id=parent_span_id,
                actor=actor,
            )
            span = next((s for s in collector.spans() if s.span_id == ctx.span_id), None)
            ctx.trace_id = span.trace_id if span else trace_id or collector.new_trace_id()
            started = _now_epoch_ms()
            try:
                yield ctx
            except BaseException as exc:
                ctx.exc = exc
                collector.end_span(ctx.span_id, error=exc, error_reason=str(exc))
                raise
            else:
                duration = _now_epoch_ms() - started
                collector.end_span(
                    ctx.span_id,
                    status=TelemetryStatus.OK.value,
                    duration_ms=duration,
                    latency_ms=duration,
                )
        else:
            yield ctx

    return _manager()


# monkeypatched helper on collector for trace ctx
def _collector_new_trace_id(self: TelemetryCollector) -> str:
    return make_trace_id()


TelemetryCollector.new_trace_id = _collector_new_trace_id  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Module-level default collector (local-only, disabled by default)
# ---------------------------------------------------------------------------

_default_collector = TelemetryCollector(enabled=False, local_only=True)


def default_collector() -> TelemetryCollector:
    """Return the process-wide default telemetry collector.

    The default is disabled/local-only until explicitly enabled by the
    application (CLI/MCP). Tests may construct their own collectors.
    """
    return _default_collector


def enable_default() -> TelemetryCollector:
    """Enable the process-wide default collector (local-only)."""
    _default_collector.set_enabled(True)
    _default_collector.set_local_only(True)
    return _default_collector


def reset_default() -> None:
    """Reset the default collector to disabled + empty."""
    _default_collector.reset()
    _default_collector.set_enabled(False)


def emit_event(
    collector: TelemetryCollector | None,
    *,
    domain: str,
    operation: str,
    status: str = TelemetryStatus.OK.value,
    event_type: str = EventType.LOG.value,
    resource: str = "",
    actor: str = "system",
    trace_id: str = "",
    span_id: str = "",
    parent_span_id: str = "",
    duration_ms: int = 0,
    queue_ms: int = 0,
    processing_ms: int = 0,
    network_ms: int = 0,
    retry_count: int = 0,
    latency_ms: int = 0,
    error_type: str = "",
    error_reason: str = "",
    message: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> TelemetryEvent | None:
    """Emit onto *collector* if it is not None; otherwise no-op.

    This is the single safe call site used by the integrated modules. It
    guarantees that telemetry never raises and never emits unredacted data.
    """
    if collector is None:
        return None
    try:
        return collector.emit(
            domain=domain,
            operation=operation,
            status=status,
            event_type=event_type,
            resource=resource,
            actor=actor,
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            duration_ms=duration_ms,
            queue_ms=queue_ms,
            processing_ms=processing_ms,
            network_ms=network_ms,
            retry_count=retry_count,
            latency_ms=latency_ms,
            error_type=error_type,
            error_reason=error_reason,
            message=message,
            metadata=metadata,
        )
    except Exception:
        return None


def start_trace_span(
    collector: TelemetryCollector | None,
    *,
    domain: str,
    operation: str,
    resource: str = "",
    trace_id: str = "",
    parent_span_id: str = "",
    actor: str = "system",
) -> str:
    """Start a span on *collector*; returns the span_id or ``""`` when None."""
    if collector is None or not collector.enabled:
        return ""
    try:
        return collector.start_span(
            domain=domain,
            operation=operation,
            resource=resource,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            actor=actor,
        )
    except Exception:
        return ""


def end_trace_span(
    collector: TelemetryCollector | None,
    span_id: str,
    status: str = TelemetryStatus.OK.value,
    *,
    error: Exception | str | None = None,
    error_type: str = "",
    error_reason: str = "",
    result: Mapping[str, Any] | None = None,
    duration_ms: int = 0,
    latency_ms: int = 0,
) -> None:
    """End a span on *collector* if present; always safe."""
    if collector is None or not span_id:
        return
    try:
        event = collector.end_span(
            span_id,
            status,
            error=error,
            error_type=error_type,
            error_reason=error_reason,
            result=result,
        )
        if event is not None and duration_ms:
            # duration already recorded; no-op to keep API uniform
            pass
    except Exception:
        return


def render_timeline(collector: TelemetryCollector | None, trace_id: str = "") -> dict[str, Any]:
    """Render a timeline/diagnostic payload without any sensitive content."""
    if collector is None:
        return {"enabled": False, "timeline": [], "health": None}
    try:
        if trace_id:
            return collector.timeline(trace_id)
        return collector.mcp_payload()
    except Exception:
        return {"enabled": True, "timeline": [], "error": "rendering failed"}


# ---------------------------------------------------------------------------
# Convenience factory for tests / integrations
# ---------------------------------------------------------------------------


def new_collector(
    *,
    enabled: bool = True,
    keep_all: bool = True,
    max_events: int = 10_000,
    anomaly_detector: AnomalyDetector | None = None,
) -> TelemetryCollector:
    """Create a telemetry collector for integration use."""
    sampler = Sampler(rate=1.0) if keep_all else Sampler(rate=0.5)
    retention = RetentionPolicy(max_events=max_events)
    return TelemetryCollector(
        enabled=enabled,
        local_only=True,
        sampler=sampler,
        retention=retention,
        anomaly_detector=anomaly_detector,
    )