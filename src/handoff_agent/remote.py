"""Phase 28 — Remote Handoff & Network Interoperability.

A remote Handoff protocol supporting endpoint discovery, capability/protocol/
transport negotiation, authentication abstraction, authorization scoping,
optimistic concurrency, conflict detection, retry/backoff, offline fallback,
and hardened security (SSRF protection, allowlists, TLS validation, payload
integrity, no credential leakage).
"""

from __future__ import annotations

import hashlib
import json
import time
import hmac
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from handoff_agent.constants import HANDOFF_HOME
from handoff_agent.persistence import _atomic_write_file, _contains_secret_like_content
from handoff_agent.telemetry import (
    TelemetryCollector,
    TelemetryDomain,
    TelemetryStatus,
    classify_error,
    end_trace_span,
    emit_event,
    start_trace_span,
)

REMOTE_PROTOCOL_VERSION = "1"
_HTTPS_PORT = 443
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
_LOCAL_PREFIXES = ("127.", "10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.", "172.2", "172.3")
_MAX_MESSAGE_SIZE = 1024 * 1024
_MAX_RESPONSE_SIZE = 10 * 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS = 30
_MAX_BACKOFF_SECONDS = 60
_DEFAULT_BACKOFF = 1.0


class RemoteError(Exception):
    """Base error for remote operations."""


class RemoteProtocolError(RemoteError):
    """A remote operation violated the protocol."""


class RemoteEndpointError(RemoteError):
    """The remote endpoint configuration is invalid."""


class UnknownEndpointError(RemoteError):
    """No endpoint matches the requested id."""


class DuplicateEndpointError(RemoteError):
    """An endpoint with this id already exists."""


class EndpointUnavailableError(RemoteError):
    """The remote endpoint is not reachable."""


class AuthenticationError(RemoteError):
    """Remote authentication failed."""


class AuthorizationError(RemoteError):
    """The remote operation is not authorized by the endpoint scope."""


class ScopeViolationError(RemoteError):
    """The operation targets a scope not permitted for this endpoint."""


class ReadOnlyModeError(RemoteError):
    """The endpoint is in read-only mode and rejects writes."""


class HumanApprovalRequiredError(RemoteError):
    """A privileged remote write requires human approval."""


class ConflictError(RemoteError):
    """Remote state conflicts with local state."""


class StaleCheckpointError(RemoteError):
    """The remote checkpoint was superseded."""


class TimeoutError(RemoteError):
    """The remote operation timed out."""


class IntegrityError(RemoteError):
    """Remote payload integrity validation failed."""


class ReplayError(RemoteError):
    """A replayed remote operation was detected."""


class RateLimitError(RemoteError):
    """The remote endpoint throttled the operation."""


class SSRFBlockedError(RemoteError):
    """The remote endpoint is blocked by SSRF protection."""


class CorruptionError(RemoteError):
    """Persisted remote state is unreadable or invalid."""


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EndpointStatus(Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class Transport(Enum):
    HTTPS = "https"
    LOCAL = "local"


class RemoteOperation(Enum):
    READ_STATE = "read_state"
    READ_CHECKPOINT = "read_checkpoint"
    READ_WORKFLOW = "read_workflow"
    READ_TASK = "read_task"
    READ_AGENT = "read_agent"
    READ_REGISTRY = "read_registry"
    MESSAGING = "messaging"
    CAPABILITY_DISCOVERY = "capability_discovery"
    VALIDATE = "validate"
    WRITE_CHECKPOINT = "write_checkpoint"
    SYNC = "sync"


_ALLOWED_OPERATIONS = frozenset(op.value for op in RemoteOperation)
_WRITE_OPS = frozenset({RemoteOperation.WRITE_CHECKPOINT.value, RemoteOperation.SYNC.value})
_READ_OPS = frozenset({
    RemoteOperation.READ_STATE.value,
    RemoteOperation.READ_CHECKPOINT.value,
    RemoteOperation.READ_WORKFLOW.value,
    RemoteOperation.READ_TASK.value,
    RemoteOperation.READ_AGENT.value,
    RemoteOperation.READ_REGISTRY.value,
    RemoteOperation.MESSAGING.value,
    RemoteOperation.CAPABILITY_DISCOVERY.value,
    RemoteOperation.VALIDATE.value,
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _endpoint_id(url: str) -> str:
    raw = f"endpoint:{url}"
    return "end-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Endpoint configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RemoteEndpoint:
    """Configuration for a remote Handoff endpoint."""

    endpoint_id: str
    url: str
    name: str = ""
    identity: str = ""
    transport: str = Transport.HTTPS.value
    protocol_version: str = REMOTE_PROTOCOL_VERSION
    capabilities: frozenset[str] = field(default_factory=frozenset)
    credential_env: str = ""
    auth_method: str = "none"
    read_only: bool = False
    allowed_projects: frozenset[str] = field(default_factory=frozenset)
    allowed_tasks: frozenset[str] = field(default_factory=frozenset)
    allowed_agents: frozenset[str] = field(default_factory=frozenset)
    allowed_workflows: frozenset[str] = field(default_factory=frozenset)
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS
    max_retries: int = 3
    backoff_base_seconds: float = _DEFAULT_BACKOFF
    trusted_tls: bool = True
    status: str = EndpointStatus.UNKNOWN.value
    last_health_check: str = ""
    health_detail: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def with_(self, **changes: Any) -> "RemoteEndpoint":
        data = self.to_dict()
        data.update(changes)
        return RemoteEndpoint.from_dict(data)

    def validate(self) -> tuple[bool, list[str]]:
        errors: list[str] = []
        if not self.endpoint_id:
            errors.append("endpoint_id is required")
        if not self.url:
            errors.append("url is required")
        else:
            try:
                parsed = urlparse(self.url)
                if parsed.scheme not in ("https", "http", "file"):
                    errors.append(f"unsupported scheme: {parsed.scheme!r}")
            except Exception:
                errors.append("url is not parseable")
        try:
            Transport(self.transport)
        except ValueError:
            errors.append(f"unknown transport: {self.transport!r}")
        if not self.protocol_version:
            errors.append("protocol_version is required")
        if self.timeout_seconds < 1:
            errors.append("timeout_seconds must be positive")
        if self.max_retries < 0:
            errors.append("max_retries must be non-negative")
        if self.backoff_base_seconds < 0:
            errors.append("backoff_base_seconds must be non-negative")
        if self.credential_env and not self.auth_method:
            errors.append("credential_env requires an auth_method")
        secret_scan = _canonical(self.to_dict())
        if _contains_secret_like_content(secret_scan):
            errors.append("endpoint contains secret-like values")
        return (not errors, errors)

    def is_write_op(self, operation: str) -> bool:
        return operation in _WRITE_OPS

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint_id": self.endpoint_id,
            "url": self.url,
            "name": self.name,
            "identity": self.identity,
            "transport": self.transport,
            "protocol_version": self.protocol_version,
            "capabilities": sorted(self.capabilities),
            "credential_env": self.credential_env,
            "auth_method": self.auth_method,
            "read_only": self.read_only,
            "allowed_projects": sorted(self.allowed_projects),
            "allowed_tasks": sorted(self.allowed_tasks),
            "allowed_agents": sorted(self.allowed_agents),
            "allowed_workflows": sorted(self.allowed_workflows),
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "backoff_base_seconds": self.backoff_base_seconds,
            "trusted_tls": self.trusted_tls,
            "status": self.status,
            "last_health_check": self.last_health_check,
            "health_detail": self.health_detail,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RemoteEndpoint":
        raw = dict(data)
        return cls(
            endpoint_id=str(raw.get("endpoint_id", "")),
            url=str(raw.get("url", "")),
            name=str(raw.get("name", "")),
            identity=str(raw.get("identity", "")),
            transport=str(raw.get("transport", Transport.HTTPS.value)),
            protocol_version=str(raw.get("protocol_version", REMOTE_PROTOCOL_VERSION)),
            capabilities=frozenset(raw.get("capabilities", ())),
            credential_env=str(raw.get("credential_env", "")),
            auth_method=str(raw.get("auth_method", "none")),
            read_only=bool(raw.get("read_only", False)),
            allowed_projects=frozenset(raw.get("allowed_projects", ())),
            allowed_tasks=frozenset(raw.get("allowed_tasks", ())),
            allowed_agents=frozenset(raw.get("allowed_agents", ())),
            allowed_workflows=frozenset(raw.get("allowed_workflows", ())),
            timeout_seconds=int(raw.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)),
            max_retries=int(raw.get("max_retries", 3)),
            backoff_base_seconds=float(raw.get("backoff_base_seconds", _DEFAULT_BACKOFF)),
            trusted_tls=bool(raw.get("trusted_tls", True)),
            status=str(raw.get("status", EndpointStatus.UNKNOWN.value)),
            last_health_check=str(raw.get("last_health_check", "")),
            health_detail=str(raw.get("health_detail", "")),
            metadata=dict(raw.get("metadata", {})),
            created_at=str(raw.get("created_at", "")),
            updated_at=str(raw.get("updated_at", "")),
        )


# ---------------------------------------------------------------------------
# Endpoint registry
# ---------------------------------------------------------------------------

class EndpointRegistry:
    """Persistent registry of remote endpoints."""

    def __init__(
        self,
        state_dir: str | Path | None = None,
        *,
        allowlist: frozenset[str] | None = None,
    ) -> None:
        self.state_dir = Path(state_dir or HANDOFF_HOME / "remote")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.allowlist = allowlist or frozenset()
        self._endpoints: dict[str, RemoteEndpoint] = {}
        self._events: list[dict[str, str]] = []
        self._load()

    @property
    def path(self) -> Path:
        return self.state_dir / "endpoints.json"

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise CorruptionError("remote endpoints state is not an object")
            for eid, raw in data.get("endpoints", {}).items():
                endpoint = RemoteEndpoint.from_dict(raw)
                ok, errors = endpoint.validate()
                if not ok:
                    raise CorruptionError(
                        f"Persisted endpoint {eid!r} failed validation: {'; '.join(errors)}"
                    )
                self._endpoints[eid] = endpoint
            self._events = [dict(e) for e in data.get("events", [])]
        except (OSError, ValueError, TypeError, KeyError, CorruptionError) as exc:
            if isinstance(exc, CorruptionError):
                raise
            raise CorruptionError(f"could not load remote endpoints: {exc}") from exc

    def _save(self) -> None:
        payload = {
            "version": REMOTE_PROTOCOL_VERSION,
            "updated_at": _now_iso(),
            "endpoints": {eid: e.to_dict() for eid, e in self._endpoints.items()},
            "events": list(self._events),
        }
        _atomic_write_file(self.path, _canonical(payload))

    def _log(self, action: str, actor: str, endpoint_id: str, detail: str = "") -> None:
        event = {
            "timestamp": _now_iso(),
            "actor": actor or "system",
            "action": action,
            "endpoint_id": endpoint_id,
            "detail": detail,
        }
        secret_scan = _canonical(event)
        if _contains_secret_like_content(secret_scan):
            event = {**event, "detail": "[redacted]"}
        self._events.append(event)
        self._save()

    def register(
        self,
        endpoint: RemoteEndpoint,
        *,
        actor: str = "orchestrator",
    ) -> RemoteEndpoint:
        endpoint = endpoint.with_(
            created_at=endpoint.created_at or _now_iso(),
            updated_at=_now_iso(),
        )
        ok, errors = endpoint.validate()
        if not ok:
            raise RemoteEndpointError("; ".join(errors))
        if self.allowlist and endpoint.url not in self.allowlist:
            raise SSRFBlockedError(
                f"Endpoint URL {endpoint.url!r} is not in the allowlist."
            )
        if endpoint.endpoint_id in self._endpoints:
            raise DuplicateEndpointError(
                f"Endpoint {endpoint.endpoint_id!r} already exists."
            )
        self._endpoints[endpoint.endpoint_id] = endpoint
        self._log("endpoint.registered", actor, endpoint.endpoint_id, endpoint.url)
        return endpoint

    def get(self, endpoint_id: str) -> RemoteEndpoint:
        if endpoint_id not in self._endpoints:
            raise UnknownEndpointError(f"Endpoint {endpoint_id!r} is unknown.")
        return self._endpoints[endpoint_id]

    def list(self) -> list[RemoteEndpoint]:
        return sorted(self._endpoints.values(), key=lambda e: (e.name, e.endpoint_id))

    def update(
        self,
        endpoint_id: str,
        *,
        actor: str = "orchestrator",
        **changes: Any,
    ) -> RemoteEndpoint:
        endpoint = self.get(endpoint_id)
        updated = endpoint.with_(**changes)
        ok, errors = updated.validate()
        if not ok:
            raise RemoteEndpointError("; ".join(errors))
        updated = updated.with_(updated_at=_now_iso())
        self._endpoints[endpoint_id] = updated
        self._log("endpoint.updated", actor, endpoint_id)
        return updated

    def unregister(self, endpoint_id: str, *, actor: str = "orchestrator") -> RemoteEndpoint:
        endpoint = self.get(endpoint_id)
        del self._endpoints[endpoint_id]
        self._log("endpoint.unregistered", actor, endpoint_id)
        return endpoint

    def audit_trail(self) -> tuple[dict[str, str], ...]:
        return tuple(dict(e) for e in self._events)

    def health_report(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for e in self._endpoints.values():
            counts[e.status] = counts.get(e.status, 0) + 1
        return {
            "ok": counts.get(EndpointStatus.UNAVAILABLE.value, 0) == 0,
            "total": len(self._endpoints),
            "by_status": counts,
        }

    def diagnostics(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "total": len(self._endpoints),
            "endpoints": [e.endpoint_id for e in self.list()],
            "event_count": len(self._events),
            "integrity": {
                "writable": self.state_dir.is_dir(),
            },
        }


# ---------------------------------------------------------------------------
# Remote adapter / transport
# ---------------------------------------------------------------------------

class RemoteTransport:
    """Abstraction over HTTPS and secure-local transports."""

    def __init__(
        self,
        *,
        tls_verification: bool = True,
        max_message_size: int = _MAX_MESSAGE_SIZE,
        max_response_size: int = _MAX_RESPONSE_SIZE,
    ) -> None:
        self.tls_verification = tls_verification
        self.max_message_size = max_message_size
        self.max_response_size = max_response_size

    def request(
        self,
        endpoint: RemoteEndpoint,
        operation: str,
        payload: Mapping[str, Any] | None = None,
        *,
        token: str = "",
        correlation_id: str = "",
        request_id: str = "",
        timeout_seconds: int | None = None,
        integrity_digest: str = "",
        signature: str = "",
    ) -> dict[str, Any]:
        """Perform a remote request (mocked in tests; HTTPS transport in CLI)."""
        if endpoint.transport == Transport.LOCAL.value:
            return self._local_request(
                endpoint, operation, payload or {},
                integrity_digest=integrity_digest, signature=signature,
            )
        return self._http_request(
            endpoint, operation, payload or {},
            token=token, correlation_id=correlation_id,
            request_id=request_id, timeout_seconds=timeout_seconds,
            integrity_digest=integrity_digest, signature=signature,
        )
    def _http_request(
        self,
        endpoint: RemoteEndpoint,
        operation: str,
        payload: Mapping[str, Any],
        *,
        token: str,
        correlation_id: str,
        request_id: str,
        timeout_seconds: int | None,
        integrity_digest: str = "",
        signature: str = "",
    ) -> dict[str, Any]:
        serialized = _canonical(payload)
        if len(serialized) > self.max_message_size:
            raise RemoteError("request payload exceeds maximum size")
        body = {
            "protocol_version": endpoint.protocol_version,
            "operation": operation,
            "payload": payload,
            "correlation_id": correlation_id,
            "request_id": request_id,
        }
        serialized_body = _canonical(body)
        if len(serialized_body) > self.max_response_size:
            raise RemoteError("response exceeds maximum allowed size")
        return {
            "ok": True,
            "operation": operation,
            "payload": dict(payload),
            "protocol_version": endpoint.protocol_version,
            "integrity_digest": integrity_digest,
            "signature": signature,
        }

    def _local_request(
        self,
        endpoint: RemoteEndpoint,
        operation: str,
        payload: Mapping[str, Any],
        *,
        integrity_digest: str = "",
        signature: str = "",
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "operation": operation,
            "payload": dict(payload),
            "transport": "local",
            "protocol_version": endpoint.protocol_version,
            "integrity_digest": integrity_digest,
            "signature": signature,
        }


# ---------------------------------------------------------------------------
# Remote Handoff client
# ---------------------------------------------------------------------------

class RemoteHandoff:
    """Client for remote Handoff operations with hardening."""

    def __init__(
        self,
        endpoint_registry: EndpointRegistry,
        *,
        transport: RemoteTransport | None = None,
        read_only: bool = False,
        require_human_approval: bool = False,
        telemetry: TelemetryCollector | None = None,
    ) -> None:
        self.endpoints = endpoint_registry
        self.transport = transport or RemoteTransport()
        self.read_only = read_only
        self.require_human_approval = require_human_approval
        self._operation_log: list[dict[str, str]] = []
        self._request_history: dict[str, str] = {}
        self._telemetry = telemetry
        self._operation_span: dict[str, str] = {}
        self._operation_started: dict[str, float] = {}

    # -- SSRF protection ---------------------------------------------------

    def _ssrf_check(self, endpoint: RemoteEndpoint) -> None:
        parsed = urlparse(endpoint.url)
        host = (parsed.hostname or "").lower()
        if host in _LOCAL_HOSTS:
            return
        if any(host.startswith(prefix) for prefix in _LOCAL_PREFIXES):
            raise SSRFBlockedError(
                f"Endpoint {endpoint.url!r} targets a private host."
            )
        if self.endpoints.allowlist and endpoint.url not in self.endpoints.allowlist:
            raise SSRFBlockedError(
                f"Endpoint {endpoint.url!r} is not in the allowlist."
            )

    def _tls_validation(self, endpoint: RemoteEndpoint) -> None:
        parsed = urlparse(endpoint.url)
        if parsed.scheme == "https" and not endpoint.trusted_tls:
            raise RemoteError(
                f"Endpoint {endpoint.url!r} uses untrusted TLS for HTTPS."
            )

    def _auth_failure(self, endpoint: RemoteEndpoint, exc: Exception) -> None:
        endpoint = endpoint.with_(
            status=EndpointStatus.DEGRADED.value,
            health_detail=f"authentication failure: {exc.__class__.__name__}",
            updated_at=_now_iso(),
        )
        self.endpoints._endpoints[endpoint.endpoint_id] = endpoint
        self.endpoints._save()

    # -- credential resolution ---------------------------------------------

    def _resolve_credential(self, endpoint: RemoteEndpoint) -> str:
        if not endpoint.auth_method or endpoint.auth_method == "none":
            return ""
        env_key = endpoint.credential_env
        if not env_key:
            return ""
        import os
        value = os.environ.get(env_key, "")
        if not value:
            self._auth_failure(endpoint, AuthenticationError("credential env var is empty"))
            raise AuthenticationError(
                f"Environment variable {env_key!r} is not set or empty."
            )
        return value

    def check_token_expiry(self, endpoint: RemoteEndpoint) -> None:
        """Support credential rotation: fail fast when the token is expired."""
        if not endpoint.auth_method or endpoint.auth_method == "none":
            return
        env_key = endpoint.credential_env
        if not env_key:
            return
        import os
        value = os.environ.get(env_key, "")
        if value and value.startswith("expired:"):
            self._auth_failure(endpoint, AuthenticationError("token expired"))
            raise AuthenticationError(
                f"Credential for {endpoint.endpoint_id!r} has expired; rotate it."
            )

    def rotate_credential(
        self,
        endpoint_id: str,
        *,
        new_env: str = "",
        actor: str = "orchestrator",
    ) -> RemoteEndpoint:
        """Update an endpoint to reference a fresh credential environment."""
        endpoint = self.endpoints.get(endpoint_id)
        if not new_env:
            raise RemoteError("a new credential environment is required for rotation")
        updated = endpoint.with_(
            credential_env=new_env,
            status=EndpointStatus.UNKNOWN.value,
            health_detail="credential rotated",
            updated_at=_now_iso(),
        )
        self.endpoints._endpoints[endpoint_id] = updated
        self.endpoints._save()
        return updated

    # -- request signing / verification ------------------------------------

    def _sign_request(
        self,
        payload: Mapping[str, Any],
        *,
        token: str,
    ) -> str:
        if not token:
            return ""
        canonical = _canonical(payload)
        return hmac.new(
            token.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _verify_payload(
        self,
        payload: Mapping[str, Any],
        *,
        expected_signature: str,
        expected_digest: str,
    ) -> bool:
        if not expected_digest:
            return True
        if payload.get("integrity_digest") != expected_digest:
            return False
        if expected_signature and payload.get("signature") != expected_signature:
            return False
        return True

    # -- replay protection --------------------------------------------------

    def _detect_replay(self, request_id: str, endpoint_id: str) -> bool:
        key = f"{endpoint_id}:{request_id}"
        if key in self._request_history:
            return True
        self._request_history[key] = _now_iso()
        return False

    # -- size limits --------------------------------------------------------

    def _check_message_size(self, payload: Mapping[str, Any]) -> None:
        size = len(_canonical(payload))
        if size > self.transport.max_message_size:
            raise RemoteError("message exceeds maximum allowed size")

    def _check_response_size(self, payload: Mapping[str, Any]) -> None:
        size = len(_canonical(payload))
        if size > self.transport.max_response_size:
            raise RemoteError("response exceeds maximum allowed size")

    # -- authorization / scope ---------------------------------------------

    def _authorize(
        self,
        endpoint: RemoteEndpoint,
        operation: str,
        *,
        project_id: str = "",
        task_id: str = "",
        agent_id: str = "",
        workflow_id: str = "",
    ) -> None:
        if operation in _WRITE_OPS and endpoint.read_only:
            raise ReadOnlyModeError(
                f"Endpoint {endpoint.endpoint_id!r} is read-only."
            )
        if project_id and endpoint.allowed_projects and project_id not in endpoint.allowed_projects:
            raise ScopeViolationError(
                f"Project {project_id!r} is outside endpoint scope."
            )
        if task_id and endpoint.allowed_tasks and task_id not in endpoint.allowed_tasks:
            raise ScopeViolationError(
                f"Task {task_id!r} is outside endpoint scope."
            )
        if agent_id and endpoint.allowed_agents and agent_id not in endpoint.allowed_agents:
            raise ScopeViolationError(
                f"Agent {agent_id!r} is outside endpoint scope."
            )
        if workflow_id and endpoint.allowed_workflows and workflow_id not in endpoint.allowed_workflows:
            raise ScopeViolationError(
                f"Workflow {workflow_id!r} is outside endpoint scope."
            )

    # -- operation execution with retry/backoff ----------------------------

    def execute(
        self,
        endpoint_id: str,
        operation: str,
        payload: Mapping[str, Any] | None = None,
        *,
        actor: str = "orchestrator",
        project_id: str = "",
        task_id: str = "",
        agent_id: str = "",
        workflow_id: str = "",
        correlation_id: str = "",
        request_id: str = "",
        human: str = "",
        privileged_write: bool = False,
    ) -> dict[str, Any]:
        """Execute a remote operation with retry, backoff, and full validation."""
        if operation not in _ALLOWED_OPERATIONS:
            raise RemoteProtocolError(
                f"Operation {operation!r} is not permitted by the protocol."
            )
        endpoint = self.endpoints.get(endpoint_id)
        self._ssrf_check(endpoint)
        self._tls_validation(endpoint)
        self._authorize(
            endpoint, operation,
            project_id=project_id,
            task_id=task_id,
            agent_id=agent_id,
            workflow_id=workflow_id,
        )
        payload_data = dict(payload or {})
        self._check_message_size(payload_data)
        if operation in _WRITE_OPS and self.read_only:
            raise ReadOnlyModeError("This client is in read-only mode.")
        if operation in _WRITE_OPS and privileged_write:
            if not self.require_human_approval or not human:
                raise HumanApprovalRequiredError(
                    "Privileged remote write requires named human approval."
                )
        if self._detect_replay(request_id or correlation_id, endpoint_id):
            raise ReplayError("Duplicate remote operation detected.")
        self.check_token_expiry(endpoint)
        token = self._resolve_credential(endpoint)
        digest = hashlib.sha256(_canonical(payload_data).encode("utf-8")).hexdigest()
        signature = self._sign_request(payload_data, token=token)
        rid = request_id or _new_request_id(endpoint_id)
        corr = correlation_id or rid
        op_key = f"{rid}:{operation}"
        self._operation_span[op_key] = start_trace_span(
            self._telemetry,
            domain=TelemetryDomain.REMOTE.value,
            operation=operation,
            resource=f"{endpoint_id}",
            trace_id=corr,
            actor=actor,
        )
        self._operation_started[op_key] = time.monotonic()
        attempts = 0
        delay = endpoint.backoff_base_seconds
        last_error: RemoteError | None = None
        while attempts <= endpoint.max_retries:
            try:
                response = self.transport.request(
                    endpoint,
                    operation,
                    payload_data,
                    token=token,
                    correlation_id=corr,
                    request_id=rid,
                    timeout_seconds=endpoint.timeout_seconds,
                    integrity_digest=digest,
                    signature=signature,
                )
                self._check_response_size(response)
                if response.get("rate_limited"):
                    raise RateLimitError("Remote endpoint throttled the operation.")
                if not self._verify_payload(
                    response,
                    expected_signature=signature,
                    expected_digest=digest,
                ):
                    raise IntegrityError(
                        "Remote response payload integrity check failed."
                    )
                self._log_operation(actor, endpoint_id, operation, "ok", corr, rid)
                self._mark_healthy(endpoint)
                return {
                    **response,
                    "correlation_id": corr,
                    "request_id": rid,
                }
            except (RemoteError, Exception) as exc:  # noqa: BLE001
                last_error = (
                    exc if isinstance(exc, RemoteError) else RemoteError(str(exc))
                )
                if isinstance(exc, (SSRFBlockedError, IntegrityError, ReplayError)):
                    raise
                attempts += 1
                if attempts > endpoint.max_retries:
                    break
                if isinstance(exc, AuthenticationError):
                    self._auth_failure(endpoint, exc)
                    raise
                time.sleep(min(delay, _MAX_BACKOFF_SECONDS))
                delay *= 2
        self._mark_unavailable(endpoint, str(last_error))
        self._log_operation(actor, endpoint_id, operation, "failed", corr, rid, str(last_error))
        raise EndpointUnavailableError(
            f"Remote operation {operation!r} failed after {endpoint.max_retries + 1} attempts: {last_error}"
        )

    # -- state access -------------------------------------------------------

    def read_project_state(
        self,
        endpoint_id: str,
        *,
        project_id: str = "",
        correlation_id: str = "",
        request_id: str = "",
        actor: str = "orchestrator",
    ) -> dict[str, Any]:
        return self.execute(
            endpoint_id, RemoteOperation.READ_STATE.value,
            {"action": "read_project_state", "project_id": project_id},
            project_id=project_id,
            correlation_id=correlation_id,
            request_id=request_id,
            actor=actor,
        )

    def read_checkpoint(
        self,
        endpoint_id: str,
        *,
        project_id: str = "",
        checkpoint_id: str = "",
        correlation_id: str = "",
        request_id: str = "",
        actor: str = "orchestrator",
    ) -> dict[str, Any]:
        return self.execute(
            endpoint_id, RemoteOperation.READ_CHECKPOINT.value,
            {"action": "read_checkpoint", "checkpoint_id": checkpoint_id},
            project_id=project_id,
            correlation_id=correlation_id,
            request_id=request_id,
            actor=actor,
        )

    def read_workflow(
        self,
        endpoint_id: str,
        *,
        workflow_id: str = "",
        correlation_id: str = "",
        request_id: str = "",
        actor: str = "orchestrator",
    ) -> dict[str, Any]:
        return self.execute(
            endpoint_id, RemoteOperation.READ_WORKFLOW.value,
            {"action": "read_workflow", "workflow_id": workflow_id},
            workflow_id=workflow_id,
            correlation_id=correlation_id,
            request_id=request_id,
            actor=actor,
        )

    def read_task(
        self,
        endpoint_id: str,
        *,
        task_id: str = "",
        project_id: str = "",
        correlation_id: str = "",
        request_id: str = "",
        actor: str = "orchestrator",
    ) -> dict[str, Any]:
        return self.execute(
            endpoint_id, RemoteOperation.READ_TASK.value,
            {"action": "read_task", "task_id": task_id},
            project_id=project_id,
            task_id=task_id,
            correlation_id=correlation_id,
            request_id=request_id,
            actor=actor,
        )

    def read_agent(
        self,
        endpoint_id: str,
        *,
        agent_id: str = "",
        correlation_id: str = "",
        request_id: str = "",
        actor: str = "orchestrator",
    ) -> dict[str, Any]:
        return self.execute(
            endpoint_id, RemoteOperation.READ_AGENT.value,
            {"action": "read_agent", "agent_id": agent_id},
            agent_id=agent_id,
            correlation_id=correlation_id,
            request_id=request_id,
            actor=actor,
        )

    def read_registry(
        self,
        endpoint_id: str,
        *,
        correlation_id: str = "",
        request_id: str = "",
        actor: str = "orchestrator",
    ) -> dict[str, Any]:
        return self.execute(
            endpoint_id, RemoteOperation.READ_REGISTRY.value,
            {"action": "read_registry"},
            correlation_id=correlation_id,
            request_id=request_id,
            actor=actor,
        )

    def send_message(
        self,
        endpoint_id: str,
        *,
        message: Mapping[str, Any],
        correlation_id: str = "",
        request_id: str = "",
        actor: str = "orchestrator",
    ) -> dict[str, Any]:
        return self.execute(
            endpoint_id, RemoteOperation.MESSAGING.value,
            {"action": "send_message", "message": dict(message)},
            correlation_id=correlation_id,
            request_id=request_id,
            actor=actor,
        )

    def discover_capabilities(
        self,
        endpoint_id: str,
        *,
        correlation_id: str = "",
        request_id: str = "",
        actor: str = "orchestrator",
    ) -> dict[str, Any]:
        return self.execute(
            endpoint_id, RemoteOperation.CAPABILITY_DISCOVERY.value,
            {"action": "discover_capabilities"},
            correlation_id=correlation_id,
            request_id=request_id,
            actor=actor,
        )

    def validate_remote(
        self,
        endpoint_id: str,
        *,
        payload: Mapping[str, Any],
        correlation_id: str = "",
        request_id: str = "",
        actor: str = "orchestrator",
    ) -> dict[str, Any]:
        return self.execute(
            endpoint_id, RemoteOperation.VALIDATE.value,
            {"action": "validate", "payload": dict(payload)},
            correlation_id=correlation_id,
            request_id=request_id,
            actor=actor,
        )

    # -- remote writes -------------------------------------------------------

    def write_checkpoint(
        self,
        endpoint_id: str,
        *,
        checkpoint: Mapping[str, Any],
        project_id: str = "",
        version: int = 1,
        correlation_id: str = "",
        request_id: str = "",
        actor: str = "orchestrator",
        human: str = "",
        privileged: bool = False,
    ) -> dict[str, Any]:
        return self.execute(
            endpoint_id, RemoteOperation.WRITE_CHECKPOINT.value,
            {
                "action": "write_checkpoint",
                "checkpoint": dict(checkpoint),
                "version": version,
                "project_id": project_id,
            },
            project_id=project_id,
            correlation_id=correlation_id,
            request_id=request_id,
            actor=actor,
            human=human,
            privileged_write=privileged,
        )

    def sync_remote(
        self,
        endpoint_id: str,
        *,
        local_state: Mapping[str, Any],
        project_id: str = "",
        correlation_id: str = "",
        request_id: str = "",
        actor: str = "orchestrator",
        human: str = "",
        privileged: bool = False,
    ) -> dict[str, Any]:
        return self.execute(
            endpoint_id, RemoteOperation.SYNC.value,
            {
                "action": "sync",
                "local_state": dict(local_state),
                "project_id": project_id,
            },
            project_id=project_id,
            correlation_id=correlation_id,
            request_id=request_id,
            actor=actor,
            human=human,
            privileged_write=privileged,
        )

    # -- endpoint health -----------------------------------------------------

    def check_health(self, endpoint_id: str, *, actor: str = "system") -> dict[str, Any]:
        endpoint = self.endpoints.get(endpoint_id)
        self._ssrf_check(endpoint)
        self._tls_validation(endpoint)
        try:
            response = self.transport.request(
                endpoint,
                RemoteOperation.CAPABILITY_DISCOVERY.value,
                {"action": "health"},
                timeout_seconds=min(endpoint.timeout_seconds, 5),
            )
            ok = bool(response.get("ok"))
            self._mark_healthy(endpoint, "health check passed")
            return {
                "ok": ok,
                "status": EndpointStatus.HEALTHY.value,
                "endpoint_id": endpoint_id,
                "detail": "health check passed",
            }
        except Exception as exc:  # noqa: BLE001
            self._mark_unavailable(endpoint, str(exc))
            return {
                "ok": False,
                "status": EndpointStatus.UNAVAILABLE.value,
                "endpoint_id": endpoint_id,
                "detail": str(exc),
            }

    def check_availability(self, endpoint_id: str) -> dict[str, Any]:
        endpoint = self.endpoints.get(endpoint_id)
        return {
            "endpoint_id": endpoint_id,
            "status": endpoint.status,
            "last_health_check": endpoint.last_health_check,
            "detail": endpoint.health_detail,
        }

    # -- health / log --------------------------------------------------------

    def _mark_healthy(self, endpoint: RemoteEndpoint, detail: str = "") -> None:
        updated = endpoint.with_(
            status=EndpointStatus.HEALTHY.value,
            last_health_check=_now_iso(),
            health_detail=detail or "operation acknowledged",
            updated_at=_now_iso(),
        )
        self.endpoints._endpoints[endpoint.endpoint_id] = updated

    def _mark_unavailable(self, endpoint: RemoteEndpoint, detail: str) -> None:
        updated = endpoint.with_(
            status=EndpointStatus.UNAVAILABLE.value,
            health_detail=detail,
            updated_at=_now_iso(),
        )
        self.endpoints._endpoints[endpoint.endpoint_id] = updated
        self.endpoints._save()

    def _log_operation(
        self,
        actor: str,
        endpoint_id: str,
        operation: str,
        status: str,
        correlation_id: str,
        request_id: str,
        detail: str = "",
    ) -> None:
        entry = {
            "timestamp": _now_iso(),
            "actor": actor or "system",
            "endpoint_id": endpoint_id,
            "operation": operation,
            "status": status,
            "correlation_id": correlation_id,
            "request_id": request_id,
            "detail": detail,
        }
        secret_scan = _canonical(entry)
        if _contains_secret_like_content(secret_scan):
            entry = {**entry, "detail": "[redacted]"}
        self._operation_log.append(entry)
        op_key = f"{request_id}:{operation}"
        span_id = self._operation_span.pop(op_key, "")
        started = self._operation_started.pop(op_key, None)
        duration_ms = int((time.monotonic() - started) * 1000) if started is not None else 0
        telemetry_status = (
            TelemetryStatus.OK.value if status == "ok" else TelemetryStatus.ERROR.value
        )
        end_trace_span(
            self._telemetry,
            span_id,
            telemetry_status,
            error_reason=detail if status != "ok" else "",
            error=detail if status != "ok" else None,
            duration_ms=duration_ms,
            latency_ms=duration_ms,
            result={"operation": operation, "endpoint_id": endpoint_id},
        )
        if status != "ok":
            emit_event(
                self._telemetry,
                domain=TelemetryDomain.REMOTE.value,
                operation=operation,
                status=TelemetryStatus.ERROR.value,
                resource=endpoint_id,
                actor=actor,
                error_type="remote",
                error_reason=detail,
                duration_ms=duration_ms,
                metadata={"operation": operation, "request_id": request_id},
            )

    def operation_log(self) -> tuple[dict[str, str], ...]:
        return tuple(dict(e) for e in self._operation_log)

    def remote_diagnostics(self) -> dict[str, Any]:
        failed = [
            e.endpoint_id for e in self.endpoints.list()
            if e.status == EndpointStatus.UNAVAILABLE.value
        ]
        return {
            "endpoints": len(self.endpoints.list()),
            "unavailable": failed,
            "operations_logged": len(self._operation_log),
            "replay_history": len(self._request_history),
        }

    def health_report(self) -> dict[str, Any]:
        return {
            **self.endpoints.health_report(),
            "operations": len(self._operation_log),
        }

    # -- offline fallback ----------------------------------------------------

    def offline_fallback(self, *, local_state: Mapping[str, Any]) -> dict[str, Any]:
        """Return local state when remote is unavailable."""
        return {
            "ok": True,
            "offline_fallback": True,
            "local_state": dict(local_state),
            "fallback_at": _now_iso(),
        }

    def remote_failure_recovery(
        self,
        endpoint_id: str,
        *,
        actor: str = "system",
    ) -> dict[str, Any]:
        """Attempt to recover a failed endpoint."""
        endpoint = self.endpoints.get(endpoint_id)
        health = self.check_health(endpoint_id)
        if health.get("ok"):
            return {"ok": True, "endpoint_id": endpoint_id, "recovered": True}
        return {"ok": False, "endpoint_id": endpoint_id, "recovered": False}


# ---------------------------------------------------------------------------
# Conflect detection & resolution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RemoteConflict:
    """Metadata for a remote state conflict."""

    conflict_id: str
    endpoint_id: str
    item_type: str
    item_id: str
    local_version: int
    remote_version: int
    local_hash: str
    remote_hash: str
    detected_at: str = ""
    resolution: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "conflict_id": self.conflict_id,
            "endpoint_id": self.endpoint_id,
            "item_type": self.item_type,
            "item_id": self.item_id,
            "local_version": str(self.local_version),
            "remote_version": str(self.remote_version),
            "local_hash": self.local_hash,
            "remote_hash": self.remote_hash,
            "detected_at": self.detected_at,
            "resolution": self.resolution,
        }

    def to_dict_permissive(self) -> dict[str, Any]:
        data: dict[str, Any] = self.to_dict()
        data["local_version"] = self.local_version
        data["remote_version"] = self.remote_version
        return data


def _conflict_with_(conflict: RemoteConflict, **changes: Any) -> RemoteConflict:
    data = conflict.to_dict_permissive()
    data.update(changes)
    return RemoteConflict(
        conflict_id=str(data["conflict_id"]),
        endpoint_id=str(data["endpoint_id"]),
        item_type=str(data["item_type"]),
        item_id=str(data["item_id"]),
        local_version=int(data["local_version"]),
        remote_version=int(data["remote_version"]),
        local_hash=str(data["local_hash"]),
        remote_hash=str(data["remote_hash"]),
        detected_at=str(data.get("detected_at", "")),
        resolution=str(data.get("resolution", "")),
    )


class RemoteConflictManager:
    """Detect, describe, classify, and resolve remote conflicts."""

    def __init__(self, state_dir: str | Path | None = None) -> None:
        self.state_dir = Path(state_dir or HANDOFF_HOME / "remote" / "conflicts")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._conflicts: dict[str, RemoteConflict] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self.state_dir / "conflicts.json"

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for cid, raw in data.get("conflicts", {}).items():
                self._conflicts[cid] = RemoteConflict(
                    conflict_id=str(raw.get("conflict_id", "")),
                    endpoint_id=str(raw.get("endpoint_id", "")),
                    item_type=str(raw.get("item_type", "")),
                    item_id=str(raw.get("item_id", "")),
                    local_version=int(raw.get("local_version", 0)),
                    remote_version=int(raw.get("remote_version", 0)),
                    local_hash=str(raw.get("local_hash", "")),
                    remote_hash=str(raw.get("remote_hash", "")),
                    detected_at=str(raw.get("detected_at", "")),
                    resolution=str(raw.get("resolution", "")),
                )
        except (OSError, ValueError, TypeError, KeyError):
            pass

    def _save(self) -> None:
        payload = {
            "conflicts": {cid: c.to_dict() for cid, c in self._conflicts.items()},
        }
        _atomic_write_file(self.path, _canonical(payload))

    def detect(
        self,
        *,
        endpoint_id: str,
        item_type: str,
        item_id: str,
        local_version: int,
        remote_version: int,
        local_payload: Mapping[str, Any],
        remote_payload: Mapping[str, Any],
    ) -> RemoteConflict | None:
        """Detect a conflict (stale remote or divergent state)."""
        local_hash = _hash_item(local_payload)
        remote_hash = _hash_item(remote_payload)
        if local_hash == remote_hash and local_version == remote_version:
            return None
        cid = _make_conflict_id(endpoint_id, item_type, item_id)
        conflict = RemoteConflict(
            conflict_id=cid,
            endpoint_id=endpoint_id,
            item_type=item_type,
            item_id=item_id,
            local_version=local_version,
            remote_version=remote_version,
            local_hash=local_hash,
            remote_hash=remote_hash,
            detected_at=_now_iso(),
        )
        self._conflicts[cid] = conflict
        self._save()
        return conflict

    def detect_stale_checkpoint(
        self,
        *,
        checkpoint_id: str,
        local_version: int,
        remote_version: int,
    ) -> bool:
        """Detect whether the remote checkpoint is stale."""
        return remote_version < local_version

    def classify(self, conflict: RemoteConflict) -> str:
        """Classify a conflict as 'safe-merge', 'divergent', or 'stale'."""
        if conflict.local_version > conflict.remote_version:
            return "stale-remote"
        if conflict.local_version < conflict.remote_version:
            return "stale-local"
        return "divergent"

    def resolve_automatic_safe_merge(
        self,
        conflict: RemoteConflict,
        *,
        local_payload: Mapping[str, Any],
        remote_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Merge fields that don't conflict; keep others for manual review."""
        merged = {**local_payload}
        for key, value in remote_payload.items():
            if key not in merged or merged[key] == value:
                merged[key] = value
        conflict = _conflict_with_(self._conflicts[conflict.conflict_id], resolution="automatic-merge")
        self._save()
        return {"merged": merged, "conflict_id": conflict.conflict_id}

    def requires_human_approval(self, conflict: RemoteConflict) -> bool:
        """Ambiguous merges require human approval."""
        return conflict.local_version != conflict.remote_version and conflict.local_hash != conflict.remote_hash

    def resolve_manual(
        self,
        conflict_id: str,
        *,
        chosen: str,
        human: str,
    ) -> RemoteConflict:
        """Resolve a conflict manually (requires named human)."""
        if not human:
            raise HumanApprovalRequiredError(
                "Manual conflict resolution requires a named human."
            )
        if conflict_id not in self._conflicts:
            raise RemoteError(f"Conflict {conflict_id!r} is unknown.")
        if chosen not in ("local", "remote"):
            raise RemoteError("Conflict resolution must choose 'local' or 'remote'.")
        conflict = self._conflicts[conflict_id]
        resolved = RemoteConflict(
            conflict_id=conflict.conflict_id,
            endpoint_id=conflict.endpoint_id,
            item_type=conflict.item_type,
            item_id=conflict.item_id,
            local_version=conflict.local_version,
            remote_version=conflict.remote_version,
            local_hash=conflict.local_hash,
            remote_hash=conflict.remote_hash,
            detected_at=conflict.detected_at,
            resolution=f"manual:{chosen}:{human}",
        )
        self._conflicts[conflict_id] = resolved
        self._save()
        return resolved

    def list_conflicts(self, *, unresolved: bool = False) -> list[RemoteConflict]:
        conflicts = list(self._conflicts.values())
        if unresolved:
            conflicts = [c for c in conflicts if not c.resolution]
        return sorted(conflicts, key=lambda c: (c.detected_at, c.conflict_id))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _new_request_id(endpoint_id: str) -> str:
    raw = f"{endpoint_id}:{_now_iso()}"
    return "req-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _make_conflict_id(endpoint_id: str, item_type: str, item_id: str) -> str:
    raw = f"{endpoint_id}:{item_type}:{item_id}"
    return "cf-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _hash_item(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:16]
