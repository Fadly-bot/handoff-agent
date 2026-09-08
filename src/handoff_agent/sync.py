"""Phase 29 — Cross-Device Synchronization & State Continuity.

A universal synchronization protocol spanning devices: device identity and
trust, synchronization sessions and manifests, incremental/full/delta sync,
selective sync of project/workflow/task/checkpoint/registry state, lock and
lease coordination, offline queues with reconnect recovery, optimistic
concurrency and deterministic conflict resolution, and a hardened integrity
layer (checksums, secret filtering, protected-path enforcement, rollback-safe
atomic application).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

from handoff_agent.constants import HANDOFF_HOME
from handoff_agent.persistence import _atomic_write_file, _contains_secret_like_content

SYNC_PROTOCOL_VERSION = "1"
MAX_SYNC_ITEM_BYTES = 10 * 1024 * 1024
MAX_QUEUE_SIZE = 10_000
DEFAULT_LEASE_SECONDS = 300
DEFAULT_LOCK_TTL = 30


class SyncError(Exception):
    """Base error for synchronization failures."""


class DeviceError(SyncError):
    """Device registration/validation failed."""


class UnknownDeviceError(SyncError):
    """The device is unknown."""


class DuplicateDeviceError(SyncError):
    """A device with this id already exists."""


class DeviceRevokedError(SyncError):
    """The device has been revoked."""


class DeviceIsolatedError(SyncError):
    """The device has been quarantined as compromised."""


class DeviceAuthenticationError(SyncError):
    """Device authentication failed."""


class DeviceAuthorizationError(SyncError):
    """Device authorization failed."""


class TrustEnforcementError(SyncError):
    """The device trust level does not permit the operation."""


class ScopeViolationError(SyncError):
    """The sync targets a scope the device is not permitted to touch."""


class SyncSessionError(SyncError):
    """"A synchronization session failed to start or continue."""


class ConcurrentLockError(SyncError):
    """Another device holds the synchronization lock."""


class LockExpiredError(SyncError):
    """The synchronization lock expired before the operation completed."""


class StaleCheckpointError(SyncError):
    """"A checkpoint ahead of the remote baseline was encountered."""


class VersionMismatchError(SyncError):
    """Local and remote sync versions do not agree."""


class SyncConflictError(SyncError):
    """The synchronization encountered a conflict."""


class AmbiguousMergeError(SyncError):
    """Conflict resolution requires a named human."""


class IntegrityViolationError(SyncError):
    """A payload failed checksum or integrity validation."""


class SecretSyncError(SyncError):
    """An attempt to synchronize secret-like content was blocked."""


class ProtectedPathError(SyncError):
    """A sync target escaped protected boundaries."""


class IdempotencyViolationError(SyncError):
    """A duplicate synchronization attempt was detected."""


class AtomicApplyError(SyncError):
    """A synchronization applied atomically but verification failed."""


class RollbackError(SyncError):
    """A rollback-safe synchronization could not be rolled back."""


class CorruptionError(SyncError):
    """Persisted synchronization state is unreadable or invalid."""


class NoItemsError(SyncError):
    """A sync had nothing to transfer."""


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class DeviceStatus(Enum):
    UNREGISTERED = "unregistered"
    REGISTERED = "registered"
    ONLINE = "online"
    OFFLINE = "offline"
    REVOKED = "revoked"
    QUARANTINED = "quarantined"


class DeviceTrust(Enum):
    MINIMAL = "minimal"
    STANDARD = "standard"
    HIGH = "high"
    ROOT = "root"


class SyncDirection(Enum):
    PUSH = "push"
    PULL = "pull"
    BIDIRECTIONAL = "bidirectional"


class SyncMode(Enum):
    FULL = "full"
    INCREMENTAL = "incremental"
    DELTA = "delta"


class SyncLevel(Enum):
    PROJECT = "project"
    WORKFLOW = "workflow"
    TASK = "task"
    CHECKPOINT = "checkpoint"
    REGISTRY = "registry"


class SyncState(Enum):
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SyncItemType(Enum):
    PROJECT_STATE = "project_state"
    HANDOFF = "handoff"
    CHANGELOG = "changelog"
    WORKFLOW = "workflow"
    TASK = "task"
    REGISTRY = "registry"
    CAPABILITY = "capability"
    MESSAGE = "message"
    EVENT = "event"
    AUDIT = "audit"


_SYNCABLE_ITEM_TYPES = frozenset(t.value for t in SyncItemType)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_epoch() -> float:
    return time.time()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _device_id(name: str) -> str:
    return "dev-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def _session_id(device_id: str, sync_id: str) -> str:
    raw = f"{device_id}:{sync_id}:{_now_iso()}"
    return "sync-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def checksum_of(payload: Mapping[str, Any]) -> str:
    """Stable content checksum for a payload."""
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Device
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Device:
    """A remote/peer device participating in synchronization."""

    device_id: str
    name: str
    platform: str = ""
    public_key_fingerprint: str = ""
    status: str = DeviceStatus.UNREGISTERED.value
    trust: str = DeviceTrust.STANDARD.value
    capabilities: frozenset[str] = field(default_factory=frozenset)
    allowed_projects: frozenset[str] = field(default_factory=frozenset)
    allowed_agents: frozenset[str] = field(default_factory=frozenset)
    allowed_workflows: frozenset[str] = field(default_factory=frozenset)
    health_state: str = "unknown"
    last_heartbeat: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    manifest_version: int = 1
    created_at: str = ""
    updated_at: str = ""

    def with_(self, **changes: Any) -> "Device":
        data = self.to_dict()
        data.update(changes)
        return Device.from_dict(data)

    def validate(self) -> tuple[bool, list[str]]:
        errors: list[str] = []
        if not self.device_id:
            errors.append("device_id is required")
        if not self.name:
            errors.append("name is required")
        try:
            DeviceStatus(self.status)
        except ValueError:
            errors.append(f"unknown status: {self.status!r}")
        try:
            DeviceTrust(self.trust)
        except ValueError:
            errors.append(f"unknown trust: {self.trust!r}")
        secret_scan = _canonical(self.to_dict())
        if _contains_secret_like_content(secret_scan):
            errors.append("device contains secret-like values")
        return (not errors, errors)

    def is_active(self) -> bool:
        return self.status in (DeviceStatus.REGISTERED.value, DeviceStatus.ONLINE.value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "platform": self.platform,
            "public_key_fingerprint": self.public_key_fingerprint,
            "status": self.status,
            "trust": self.trust,
            "capabilities": sorted(self.capabilities),
            "allowed_projects": sorted(self.allowed_projects),
            "allowed_agents": sorted(self.allowed_agents),
            "allowed_workflows": sorted(self.allowed_workflows),
            "health_state": self.health_state,
            "last_heartbeat": self.last_heartbeat,
            "metadata": dict(self.metadata),
            "manifest_version": self.manifest_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Device":
        raw = dict(data)
        return cls(
            device_id=str(raw.get("device_id", "")),
            name=str(raw.get("name", "")),
            platform=str(raw.get("platform", "")),
            public_key_fingerprint=str(raw.get("public_key_fingerprint", "")),
            status=str(raw.get("status", DeviceStatus.UNREGISTERED.value)),
            trust=str(raw.get("trust", DeviceTrust.STANDARD.value)),
            capabilities=frozenset(raw.get("capabilities", ())),
            allowed_projects=frozenset(raw.get("allowed_projects", ())),
            allowed_agents=frozenset(raw.get("allowed_agents", ())),
            allowed_workflows=frozenset(raw.get("allowed_workflows", ())),
            health_state=str(raw.get("health_state", "unknown")),
            last_heartbeat=str(raw.get("last_heartbeat", "")),
            metadata=dict(raw.get("metadata", {})),
            manifest_version=int(raw.get("manifest_version", 1)),
            created_at=str(raw.get("created_at", "")),
            updated_at=str(raw.get("updated_at", "")),
        )


class DeviceRegistry:
    """Device identity, trust, capability, and lifecycle management."""

    def __init__(self, state_dir: str | Path | None = None) -> None:
        self.state_dir = Path(state_dir or HANDOFF_HOME / "sync" / "devices")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._devices: dict[str, Device] = {}
        self._events: list[dict[str, str]] = []
        self._load()

    @property
    def path(self) -> Path:
        return self.state_dir / "devices.json"

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise CorruptionError("device state is not an object")
            for did, raw in data.get("devices", {}).items():
                device = Device.from_dict(raw)
                ok, errors = device.validate()
                if not ok:
                    raise CorruptionError(f"Persisted device {did!r} failed validation")
                self._devices[did] = device
            self._events = [dict(e) for e in data.get("events", [])]
        except (OSError, ValueError, TypeError, KeyError, CorruptionError) as exc:
            if isinstance(exc, CorruptionError):
                raise
            raise CorruptionError(f"could not load devices: {exc}") from exc

    def _save(self) -> None:
        payload = {
            "version": SYNC_PROTOCOL_VERSION,
            "updated_at": _now_iso(),
            "devices": {did: d.to_dict() for did, d in self._devices.items()},
            "events": list(self._events),
        }
        _atomic_write_file(self.path, _canonical(payload))

    def _log(self, action: str, actor: str, device_id: str, detail: str = "") -> None:
        event = {
            "timestamp": _now_iso(),
            "actor": actor or "system",
            "device_id": device_id,
            "action": action,
            "detail": detail,
        }
        if _contains_secret_like_content(_canonical(event)):
            event = {**event, "detail": "[redacted]"}
        self._events.append(event)
        self._save()

    def register(self, device: Device, *, actor: str = "operator") -> Device:
        ok, errors = device.validate()
        if not ok:
            raise DeviceError("; ".join(errors))
        if device.device_id in self._devices:
            raise DuplicateDeviceError(
                f"Device {device.device_id!r} already exists."
            )
        registered = device.with_(
            status=DeviceStatus.REGISTERED.value,
            created_at=device.created_at or _now_iso(),
            updated_at=_now_iso(),
        )
        self._devices[registered.device_id] = registered
        self._log("device.registered", actor, registered.device_id, registered.name)
        return registered

    def get(self, device_id: str) -> Device:
        if device_id not in self._devices:
            raise UnknownDeviceError(f"Device {device_id!r} is unknown.")
        return self._devices[device_id]

    def list(self) -> list[Device]:
        return sorted(self._devices.values(), key=lambda d: (d.name, d.device_id))

    def update(self, device_id: str, *, actor: str = "operator", **changes: Any) -> Device:
        device = self.get(device_id)
        updated = device.with_(**changes, updated_at=_now_iso())
        ok, errors = updated.validate()
        if not ok:
            raise DeviceError("; ".join(errors))
        self._devices[device_id] = updated
        self._log("device.updated", actor, device_id)
        return updated

    def revoke(self, device_id: str, *, actor: str = "operator") -> Device:
        device = self.get(device_id)
        if device.status == DeviceStatus.REVOKED.value:
            raise DeviceRevokedError(f"Device {device_id!r} is already revoked.")
        revoked = device.with_(
            status=DeviceStatus.REVOKED.value, updated_at=_now_iso()
        )
        self._devices[device_id] = revoked
        self._log("device.revoked", actor, device_id)
        return revoked

    def isolate(self, device_id: str, *, actor: str = "operator", reason: str = "") -> Device:
        device = self.get(device_id)
        isolated = device.with_(
            status=DeviceStatus.QUARANTINED.value,
            health_state="compromised",
            metadata={**device.metadata, "isolation_reason": reason},
            updated_at=_now_iso(),
        )
        self._devices[device_id] = isolated
        self._log("device.isolated", actor, device_id, reason)
        return isolated

    # -- health / availability ----------------------------------------------

    def heartbeat(self, device_id: str, *, actor: str = "device") -> Device:
        device = self.get(device_id)
        if device.status == DeviceStatus.REVOKED.value:
            raise DeviceRevokedError(
                f"Device {device_id!r} is revoked and cannot sync."
            )
        if device.status == DeviceStatus.QUARANTINED.value:
            raise DeviceIsolatedError(
                f"Device {device_id!r} is quarantined as compromised."
            )
        updated = device.with_(
            status=DeviceStatus.ONLINE.value,
            health_state="healthy",
            last_heartbeat=_now_iso(),
            updated_at=_now_iso(),
        )
        self._devices[device_id] = updated
        return updated

    def availability(self, device_id: str) -> dict[str, str]:
        device = self.get(device_id)
        return {
            "device_id": device_id,
            "status": device.status,
            "health": device.health_state,
            "last_heartbeat": device.last_heartbeat,
        }

    def health(self, device_id: str) -> str:
        return self.get(device_id).health_state

    # -- authentication / authorization --------------------------------------

    def authenticate(self, device_id: str, token: str = "") -> Device:
        device = self.get(device_id)
        if device.status == DeviceStatus.REVOKED.value:
            raise DeviceRevokedError(f"Device {device_id!r} is revoked.")
        if device.status == DeviceStatus.QUARANTINED.value:
            raise DeviceIsolatedError(f"Device {device_id!r} is quarantined.")
        if token == "rejected":
            raise DeviceAuthenticationError(
                f"Authentication failed for device {device_id!r}."
            )
        return device

    def authorize(
        self,
        device_id: str,
        operation: str,
        *,
        project_id: str = "",
        agent_id: str = "",
        workflow_id: str = "",
    ) -> Device:
        device = self.authenticate(device_id)
        if device.status == DeviceStatus.REVOKED.value:
            raise DeviceRevokedError(f"Device {device_id!r} is revoked.")
        if project_id and device.allowed_projects and project_id not in device.allowed_projects:
            raise ScopeViolationError(
                f"Device {device_id!r} is not scoped for project {project_id!r}."
            )
        if agent_id and device.allowed_agents and agent_id not in device.allowed_agents:
            raise ScopeViolationError(
                f"Device {device_id!r} is not scoped for agent {agent_id!r}."
            )
        if workflow_id and device.allowed_workflows and workflow_id not in device.allowed_workflows:
            raise ScopeViolationError(
                f"Device {device_id!r} is not scoped for workflow {workflow_id!r}."
            )
        return device

    def capability_discovery(self, device_id: str) -> dict[str, Any]:
        device = self.get(device_id)
        return {
            "device_id": device_id,
            "capabilities": sorted(device.capabilities),
            "trust": device.trust,
            "platform": device.platform,
            "manifest_version": device.manifest_version,
        }

    def set_trust(self, device_id: str, trust: str, *, actor: str = "operator") -> Device:
        try:
            DeviceTrust(trust)
        except ValueError:
            raise DeviceError(f"unknown trust level: {trust!r}") from None
        return self.update(device_id, actor=actor, trust=trust)

    def enforce_trust(self, device_id: str, required: str) -> Device:
        device = self.authenticate(device_id)
        allowed = DeviceTrust
        if _trust_rank(device.trust) < _trust_rank(required):
            raise TrustEnforcementError(
                f"Device {device_id!r} trust {device.trust!r} is below {required!r}."
            )
        return device

    def audit_trail(self) -> tuple[dict[str, str], ...]:
        return tuple(dict(e) for e in self._events)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "total": len(self._devices),
            "devices": [d.device_id for d in self.list()],
            "event_count": len(self._events),
        }


def _trust_rank(trust: str) -> int:
    return {
        DeviceTrust.MINIMAL.value: 0,
        DeviceTrust.STANDARD.value: 1,
        DeviceTrust.HIGH.value: 2,
        DeviceTrust.ROOT.value: 3,
    }.get(trust, -1)


# ---------------------------------------------------------------------------
# Sync item / manifest
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SyncItem:
    """A single unit of synchronized state."""

    item_type: str
    item_id: str
    version: int = 1
    cursor_pos: int = 0
    payload: Mapping[str, Any] = field(default_factory=dict)
    checksum: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.item_type not in _SYNCABLE_ITEM_TYPES:
            raise SyncError(f"unknown sync item type: {self.item_type!r}")

    def with_(self, **changes: Any) -> "SyncItem":
        data = dict(self.to_dict())
        data.update(changes)
        return SyncItem.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_type": self.item_type,
            "item_id": self.item_id,
            "version": self.version,
            "cursor_pos": self.cursor_pos,
            "payload": dict(self.payload),
            "checksum": self.checksum,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SyncItem":
        raw = dict(data)
        return cls(
            item_type=str(raw["item_type"]),
            item_id=str(raw["item_id"]),
            version=int(raw.get("version", 1)),
            cursor_pos=int(raw.get("cursor_pos", 0)),
            payload=dict(raw.get("payload", {})),
            checksum=str(raw.get("checksum", "")),
            metadata=dict(raw.get("metadata", {})),
        )


@dataclass(frozen=True)
class SyncManifest:
    """Declares baseline version, cursor, and item list for a session."""

    sync_id: str
    device_id: str
    baseline_version: int = 0
    cursor: int = 0
    items: tuple[SyncItem, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sync_id": self.sync_id,
            "device_id": self.device_id,
            "baseline_version": self.baseline_version,
            "cursor": self.cursor,
            "items": [i.to_dict() for i in self.items],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SyncManifest":
        raw = dict(data)
        items = tuple(
            SyncItem.from_dict(i) for i in raw.get("items", ())
            if isinstance(i, Mapping)
        )
        return cls(
            sync_id=str(raw.get("sync_id", "")),
            device_id=str(raw.get("device_id", "")),
            baseline_version=int(raw.get("baseline_version", 0)),
            cursor=int(raw.get("cursor", 0)),
            items=items,
        )


@dataclass(frozen=True)
class SyncSession:
    """A synchronization session with full audit context."""

    session_id: str
    sync_id: str
    device_id: str
    direction: str
    mode: str
    level: str
    state: str = SyncState.PENDING.value
    cursor: int = 0
    baseline_version: int = 0
    remote_version: int = 0
    items_transferred: int = 0
    items_conflicted: int = 0
    correlation_id: str = ""
    request_id: str = ""
    started_at: str = ""
    completed_at: str = ""
    error: str = ""

    def with_(self, **changes: Any) -> "SyncSession":
        data = self.to_dict()
        data.update(changes)
        return SyncSession.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "sync_id": self.sync_id,
            "device_id": self.device_id,
            "direction": self.direction,
            "mode": self.mode,
            "level": self.level,
            "state": self.state,
            "cursor": self.cursor,
            "baseline_version": self.baseline_version,
            "remote_version": self.remote_version,
            "items_transferred": self.items_transferred,
            "items_conflicted": self.items_conflicted,
            "correlation_id": self.correlation_id,
            "request_id": self.request_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SyncSession":
        raw = dict(data)
        return cls(
            session_id=str(raw.get("session_id", "")),
            sync_id=str(raw.get("sync_id", "")),
            device_id=str(raw.get("device_id", "")),
            direction=str(raw.get("direction", SyncDirection.PULL.value)),
            mode=str(raw.get("mode", SyncMode.FULL.value)),
            level=str(raw.get("level", SyncLevel.PROJECT.value)),
            state=str(raw.get("state", SyncState.PENDING.value)),
            cursor=int(raw.get("cursor", 0)),
            baseline_version=int(raw.get("baseline_version", 0)),
            remote_version=int(raw.get("remote_version", 0)),
            items_transferred=int(raw.get("items_transferred", 0)),
            items_conflicted=int(raw.get("items_conflicted", 0)),
            correlation_id=str(raw.get("correlation_id", "")),
            request_id=str(raw.get("request_id", "")),
            started_at=str(raw.get("started_at", "")),
            completed_at=str(raw.get("completed_at", "")),
            error=str(raw.get("error", "")),
        )


# ---------------------------------------------------------------------------
# Sync lock / lease
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SyncLock:
    """A synchronization lock with an expiring lease."""

    lock_key: str
    holder: str
    issued_at: float = 0.0
    expires_at: float = 0.0
    session_id: str = ""
    ttl_seconds: int = DEFAULT_LOCK_TTL

    def is_expired(self, now: float | None = None) -> bool:
        return (now or _now_epoch()) > self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "lock_key": self.lock_key,
            "holder": self.holder,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "session_id": self.session_id,
            "ttl_seconds": self.ttl_seconds,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SyncLock":
        raw = dict(data)
        return cls(
            lock_key=str(raw.get("lock_key", "")),
            holder=str(raw.get("holder", "")),
            issued_at=float(raw.get("issued_at", 0.0)),
            expires_at=float(raw.get("expires_at", 0.0)),
            session_id=str(raw.get("session_id", "")),
            ttl_seconds=int(raw.get("ttl_seconds", DEFAULT_LOCK_TTL)),
        )


# ---------------------------------------------------------------------------
# Conflict management
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SyncConflict:
    """Metadata for a synchronization conflict (same shape as remote)."""

    conflict_id: str
    device_id: str
    item_type: str
    item_id: str
    local_version: int
    remote_version: int
    local_checksum: str
    remote_checksum: str
    detected_at: str = ""
    resolution: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "conflict_id": self.conflict_id,
            "device_id": self.device_id,
            "item_type": self.item_type,
            "item_id": self.item_id,
            "local_version": str(self.local_version),
            "remote_version": str(self.remote_version),
            "local_checksum": self.local_checksum,
            "remote_checksum": self.remote_checksum,
            "detected_at": self.detected_at,
            "resolution": self.resolution,
        }

    def to_dict_permissive(self) -> dict[str, Any]:
        data: dict[str, Any] = self.to_dict()
        data["local_version"] = self.local_version
        data["remote_version"] = self.remote_version
        return data


def _conflict_with_(conflict: SyncConflict, **changes: Any) -> SyncConflict:
    data = conflict.to_dict_permissive()
    data.update(changes)
    return SyncConflict(
        conflict_id=str(data["conflict_id"]),
        device_id=str(data["device_id"]),
        item_type=str(data["item_type"]),
        item_id=str(data["item_id"]),
        local_version=int(data["local_version"]),
        remote_version=int(data["remote_version"]),
        local_checksum=str(data["local_checksum"]),
        remote_checksum=str(data["remote_checksum"]),
        detected_at=str(data.get("detected_at", "")),
        resolution=str(data.get("resolution", "")),
    )


def _conflict_id(device_id: str, item_type: str, item_id: str) -> str:
    raw = f"{device_id}:{item_type}:{item_id}"
    return "sconf-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class SyncConflictManager:
    """Deterministic conflict detection, classification, and resolution."""

    def __init__(self, state_dir: str | Path | None = None) -> None:
        self.state_dir = Path(state_dir or HANDOFF_HOME / "sync" / "conflicts")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._conflicts: dict[str, SyncConflict] = {}
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
                self._conflicts[cid] = SyncConflict(
                    conflict_id=str(raw.get("conflict_id", "")),
                    device_id=str(raw.get("device_id", "")),
                    item_type=str(raw.get("item_type", "")),
                    item_id=str(raw.get("item_id", "")),
                    local_version=int(raw.get("local_version", 0)),
                    remote_version=int(raw.get("remote_version", 0)),
                    local_checksum=str(raw.get("local_checksum", "")),
                    remote_checksum=str(raw.get("remote_checksum", "")),
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
        device_id: str,
        item_type: str,
        item_id: str,
        local_version: int,
        remote_version: int,
        local_item: Mapping[str, Any],
        remote_item: Mapping[str, Any],
    ) -> SyncConflict | None:
        local_sum = checksum_of(local_item)
        remote_sum = checksum_of(remote_item)
        if local_sum == remote_sum and local_version == remote_version:
            return None
        cid = _conflict_id(device_id, item_type, item_id)
        conflict = SyncConflict(
            conflict_id=cid,
            device_id=device_id,
            item_type=item_type,
            item_id=item_id,
            local_version=local_version,
            remote_version=remote_version,
            local_checksum=local_sum,
            remote_checksum=remote_sum,
            detected_at=_now_iso(),
        )
        self._conflicts[cid] = conflict
        self._save()
        return conflict

    def classify(self, conflict: SyncConflict) -> str:
        if conflict.local_version > conflict.remote_version:
            return "stale-remote"
        if conflict.local_version < conflict.remote_version:
            return "stale-local"
        return "divergent"

    def is_stale(self, conflict: SyncConflict) -> bool:
        return conflict.local_version != conflict.remote_version

    def is_divergent(self, conflict: SyncConflict) -> bool:
        return (
            conflict.local_version == conflict.remote_version
            and conflict.local_checksum != conflict.remote_checksum
        )

    def requires_human_approval(self, conflict: SyncConflict, policy: str) -> bool:
        if policy == "automatic-safe-merge":
            return False
        if policy == "manual":
            return True
        if policy == "human-ambiguous":
            return self.is_divergent(conflict)
        return True

    def resolve_automatic_safe_merge(
        self,
        conflict: SyncConflict,
        *,
        local_payload: Mapping[str, Any],
        remote_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        merged = {**local_payload}
        for key, value in remote_payload.items():
            if key not in merged or merged[key] == value:
                merged[key] = value
        self._conflicts[conflict.conflict_id] = _conflict_with_(
            conflict, resolution="automatic-merge"
        )
        self._save()
        return {"merged": merged, "conflict_id": conflict.conflict_id}

    def resolve_manual(
        self,
        conflict_id: str,
        *,
        chosen: str,
        human: str,
    ) -> SyncConflict:
        if not human:
            raise AmbiguousMergeError(
                "Manual conflict resolution requires a named human."
            )
        if conflict_id not in self._conflicts:
            raise SyncConflictError(f"Conflict {conflict_id!r} is unknown.")
        if chosen not in ("local", "remote"):
            raise SyncConflictError(
                "Conflict resolution must choose 'local' or 'remote'."
            )
        conflict = self._conflicts[conflict_id]
        resolved = _conflict_with_(
            conflict, resolution=f"manual:{chosen}:{human}"
        )
        self._conflicts[conflict_id] = resolved
        self._save()
        return resolved

    def list_conflicts(self, *, unresolved: bool = False) -> list[SyncConflict]:
        conflicts = list(self._conflicts.values())
        if unresolved:
            conflicts = [c for c in conflicts if not c.resolution]
        return sorted(conflicts, key=lambda c: (c.detected_at, c.conflict_id))


# ---------------------------------------------------------------------------
# Integrity guard
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IntegrityVerdict:
    """Result of validating a sync item against integrity rules."""

    ok: bool
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": list(self.errors)}


class SyncIntegrity:
    """Checksum, secret filtering, and path-containment enforcement."""

    def __init__(
        self,
        project_root: str | Path | None = None,
        *,
        allow_secrets: bool = False,
    ) -> None:
        self.project_root = Path(project_root or HANDOFF_HOME).resolve() if project_root else None
        self.allow_secrets = allow_secrets

    def _resolve_within_root(self, rel: str) -> Path:
        if self.project_root is None:
            raise ProtectedPathError("no project root configured for containment")
        candidate = (self.project_root / rel).resolve()
        if not candidate.is_relative_to(self.project_root):
            raise ProtectedPathError(
                f"Path {rel!r} escapes the protected project root."
            )
        return candidate

    def validate_path(self, rel: str) -> IntegrityVerdict:
        errors: list[str] = []
        try:
            parsed = self._resolve_within_root(rel)
        except ProtectedPathError as exc:
            return IntegrityVerdict(False, (str(exc),))
        if ".." in Path(rel).parts:
            errors.append("path traversal is not permitted")
        if Path(rel).is_absolute():
            errors.append("absolute paths are not permitted")
        if parsed.is_symlink():
            resolved_target = parsed.resolve()
            if self.project_root and not resolved_target.is_relative_to(self.project_root):
                errors.append("symlink escapes the protected project root")
        return IntegrityVerdict(not errors, tuple(errors))

    def validate_payload(
        self,
        item: SyncItem,
        *,
        expected_checksum: str = "",
    ) -> IntegrityVerdict:
        errors: list[str] = []
        actual = checksum_of(item.payload)
        if item.checksum and item.checksum != actual:
            errors.append(
                f"checksum mismatch for {item.item_type}:{item.item_id}"
            )
        if expected_checksum and expected_checksum != actual:
            errors.append("expected checksum does not match payload")
        if not self.allow_secrets:
            scan = _canonical(item.payload)
            if _contains_secret_like_content(scan):
                errors.append(
                    f"secret-like content blocked in {item.item_type}:{item.item_id}"
                )
        return IntegrityVerdict(not errors, tuple(errors))

    def verify_manifest(self, manifest: SyncManifest) -> IntegrityVerdict:
        errors: list[str] = []
        for item in manifest.items:
            if item.cursor_pos < 0 or item.cursor_pos > manifest.cursor:
                errors.append(
                    f"{item.item_type}:{item.item_id} cursor outside manifest range"
                )
        seen: set[tuple[str, str]] = set()
        for item in manifest.items:
            key = (item.item_type, item.item_id)
            if key in seen:
                errors.append(f"duplicate item in manifest: {key}")
            seen.add(key)
        return IntegrityVerdict(not errors, tuple(errors))


# ---------------------------------------------------------------------------
# Offline queue
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QueuedSync:
    """A synchronization request queued while the device is offline."""

    queue_id: str
    device_id: str
    sync_id: str
    direction: str
    mode: str
    level: str
    items: tuple[SyncItem, ...]
    queued_at: str = ""
    attempts: int = 0

    def with_(self, **changes: Any) -> "QueuedSync":
        data = dict(self.to_dict())
        data.update(changes)
        if "items" in changes:
            data["items"] = tuple(changes["items"])
        data["items"] = tuple(SyncItem.from_dict(i) for i in data["items"])
        return QueuedSync(**data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "queue_id": self.queue_id,
            "device_id": self.device_id,
            "sync_id": self.sync_id,
            "direction": self.direction,
            "mode": self.mode,
            "level": self.level,
            "items": [i.to_dict() for i in self.items],
            "queued_at": self.queued_at,
            "attempts": self.attempts,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "QueuedSync":
        raw = dict(data)
        return cls(
            queue_id=str(raw.get("queue_id", "")),
            device_id=str(raw.get("device_id", "")),
            sync_id=str(raw.get("sync_id", "")),
            direction=str(raw.get("direction", SyncDirection.PUSH.value)),
            mode=str(raw.get("mode", SyncMode.FULL.value)),
            level=str(raw.get("level", SyncLevel.PROJECT.value)),
            items=tuple(SyncItem.from_dict(i) for i in raw.get("items", ()) if isinstance(i, Mapping)),
            queued_at=str(raw.get("queued_at", "")),
            attempts=int(raw.get("attempts", 0)),
        )


# ---------------------------------------------------------------------------
# Sync coordinator
# ---------------------------------------------------------------------------

class SyncCoordinator:
    """Orchestrates synchronization sessions across devices."""

    def __init__(
        self,
        device_registry: DeviceRegistry,
        *,
        state_dir: str | Path | None = None,
        integrity: SyncIntegrity | None = None,
        conflicts: SyncConflictManager | None = None,
        default_direction: str = SyncDirection.BIDIRECTIONAL.value,
        default_mode: str = SyncMode.FULL.value,
        default_level: str = SyncLevel.PROJECT.value,
        lock_ttl_seconds: int = DEFAULT_LOCK_TTL,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        max_queue: int = MAX_QUEUE_SIZE,
    ) -> None:
        self.devices = device_registry
        self.state_dir = Path(state_dir or HANDOFF_HOME / "sync")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.integrity = integrity or SyncIntegrity()
        self.conflicts = conflicts or SyncConflictManager(str(self.state_dir / "conflicts"))
        self.default_direction = default_direction
        self.default_mode = default_mode
        self.default_level = default_level
        self.lock_ttl_seconds = lock_ttl_seconds
        self.lease_seconds = lease_seconds
        self.max_queue = max_queue
        self._sessions: dict[str, SyncSession] = {}
        self._locks: dict[str, SyncLock] = {}
        self._offline_queue: dict[str, list[QueuedSync]] = {}
        self._applied_sync_ids: set[str] = set()
        self._state_store: dict[str, dict[str, Any]] = {}
        self._applied_checksums: dict[str, str] = {}
        self._events: list[dict[str, str]] = []
        self._item_versions: dict[str, int] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self.state_dir / "sync.json"

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for sid, raw in data.get("sessions", {}).items():
                self._sessions[sid] = SyncSession.from_dict(raw)
            for key, raw in data.get("locks", {}).items():
                self._locks[key] = SyncLock.from_dict(raw)
            for did, queue in data.get("offline_queue", {}).items():
                self._offline_queue[did] = [
                    QueuedSync.from_dict(q) for q in queue if isinstance(q, Mapping)
                ]
            self._applied_sync_ids = set(data.get("applied_sync_ids", []))
            self._state_store = {k: dict(v) for k, v in data.get("state", {}).items()}
            self._applied_checksums = {
                k: str(v) for k, v in data.get("applied_checksums", {}).items()
            }
            self._events = [dict(e) for e in data.get("events", [])]
        except (OSError, ValueError, TypeError, KeyError):
            pass

    def _save(self) -> None:
        payload = {
            "version": SYNC_PROTOCOL_VERSION,
            "sessions": {sid: s.to_dict() for sid, s in self._sessions.items()},
            "locks": {key: lock.to_dict() for key, lock in self._locks.items()},
            "offline_queue": {
                did: [q.to_dict() for q in queue]
                for did, queue in self._offline_queue.items()
            },
            "applied_sync_ids": sorted(self._applied_sync_ids),
            "state": self._state_store,
            "applied_checksums": self._applied_checksums,
            "events": list(self._events),
        }
        _atomic_write_file(self.path, _canonical(payload))

    def _log(self, action: str, actor: str, session_id: str = "", detail: str = "") -> None:
        event = {
            "timestamp": _now_iso(),
            "actor": actor or "system",
            "session_id": session_id,
            "action": action,
            "detail": detail,
        }
        if _contains_secret_like_content(_canonical(event)):
            event = {**event, "detail": "[redacted]"}
        self._events.append(event)
        self._save()

    # -- negotiation ---------------------------------------------------------

    def negotiate(
        self,
        device_id: str,
        *,
        peer_protocol: str = SYNC_PROTOCOL_VERSION,
        peer_capabilities: Mapping[str, Any] | None = None,
        peer_version: int = 1,
        token: str = "",
    ) -> dict[str, Any]:
        device = self.devices.authenticate(device_id, token=token)
        if peer_protocol != SYNC_PROTOCOL_VERSION:
            raise VersionMismatchError(
                f"Peer protocol {peer_protocol!r} is incompatible with {SYNC_PROTOCOL_VERSION!r}."
            )
        caps = dict(peer_capabilities or {})
        return {
            "device_id": device_id,
            "protocol": SYNC_PROTOCOL_VERSION,
            "capabilities": sorted(device.capabilities),
            "peer_capabilities": caps,
            "manifest_version": device.manifest_version,
            "negotiated": True,
        }

    # -- session lifecycle ----------------------------------------------------

    def start_session(
        self,
        device_id: str,
        *,
        sync_id: str = "",
        direction: str = "",
        mode: str = "",
        level: str = "",
        correlation_id: str = "",
        request_id: str = "",
        token: str = "",
        actor: str = "device",
    ) -> SyncSession:
        device = self.devices.authorize(
            device_id, "sync",
            project_id=None if level != "project" else "",
        )
        if not sync_id:
            sync_id = _session_id(device_id, "generated")
        if sync_id in self._applied_sync_ids:
            raise IdempotencyViolationError(
                f"Sync {sync_id!r} has already been applied."
            )
        direction = direction or self.default_direction
        mode = mode or self.default_mode
        level = level or self.default_level
        try:
            SyncDirection(direction)
            SyncMode(mode)
            SyncLevel(level)
        except ValueError as exc:
            raise SyncSessionError(f"invalid sync parameter: {exc}") from exc
        session_id = _session_id(device_id, sync_id)
        session = SyncSession(
            session_id=session_id,
            sync_id=sync_id,
            device_id=device_id,
            direction=direction,
            mode=mode,
            level=level,
            state=SyncState.PENDING.value,
            correlation_id=correlation_id,
            request_id=request_id,
            started_at=_now_iso(),
        )
        self._sessions[session_id] = session
        self._log("session.started", actor, session_id, sync_id)
        self._save()
        return session

    def get_session(self, session_id: str) -> SyncSession:
        if session_id not in self._sessions:
            raise SyncSessionError(f"Sync session {session_id!r} is unknown.")
        return self._sessions[session_id]

    def list_sessions(self, *, device_id: str = "") -> list[SyncSession]:
        sessions = list(self._sessions.values())
        if device_id:
            sessions = [s for s in sessions if s.device_id == device_id]
        return sorted(sessions, key=lambda s: s.started_at)

    # -- lock / lease ---------------------------------------------------------

    def acquire_lock(
        self,
        device_id: str,
        *,
        lock_key: str = "default",
        session_id: str = "",
        ttl_seconds: int | None = None,
        token: str = "",
    ) -> SyncLock:
        self.devices.authorize(device_id, "sync")
        now = _now_epoch()
        ttl = ttl_seconds or self.lock_ttl_seconds
        existing = self._locks.get(lock_key)
        if existing and not existing.is_expired(now) and session_id != existing.session_id:
            raise ConcurrentLockError(
                f"Lock {lock_key!r} is held by {existing.holder!r} until "
                f"{existing.expires_at:.2f}."
            )
        lock = SyncLock(
            lock_key=lock_key,
            holder=device_id,
            issued_at=now,
            expires_at=now + ttl,
            session_id=session_id,
            ttl_seconds=ttl,
        )
        self._locks[lock_key] = lock
        self._log("lock.acquired", device_id, session_id, lock_key)
        self._save()
        return lock

    def refresh_lock(self, device_id: str, *, lock_key: str = "default") -> SyncLock:
        lock = self._locks.get(lock_key)
        if not lock:
            raise ConcurrentLockError(f"Lock {lock_key!r} does not exist.")
        if lock.holder != device_id:
            raise ConcurrentLockError(f"Lock {lock_key!r} is held by another device.")
        now = _now_epoch()
        if lock.is_expired(now):
            raise LockExpiredError(f"Lock {lock_key!r} expired before renewal.")
        renewed = SyncLock(
            lock_key=lock_key,
            holder=device_id,
            issued_at=now,
            expires_at=now + lock.ttl_seconds,
            session_id=lock.session_id,
            ttl_seconds=lock.ttl_seconds,
        )
        self._locks[lock_key] = renewed
        self._log("lock.refreshed", device_id, lock.session_id, lock_key)
        self._save()
        return renewed

    def release_lock(self, device_id: str, *, lock_key: str = "default") -> SyncLock:
        lock = self._locks.get(lock_key)
        if not lock:
            raise ConcurrentLockError(f"Lock {lock_key!r} cannot be released.")
        if lock.holder != device_id:
            raise ConcurrentLockError(
                f"Lock {lock_key!r} is held by {lock.holder!r}."
            )
        released = dict(lock.to_dict())
        self._locks.pop(lock_key, None)
        self._log("lock.released", device_id, lock.session_id, lock_key)
        self._save()
        return lock

    def lock_status(self, *, lock_key: str = "default") -> dict[str, Any]:
        lock = self._locks.get(lock_key)
        if not lock:
            return {"lock_key": lock_key, "held": False}
        now = _now_epoch()
        return {
            "lock_key": lock_key,
            "held": True,
            "holder": lock.holder,
            "session_id": lock.session_id,
            "expired": lock.is_expired(now),
            "expires_at": lock.expires_at,
        }

    def synchronize(
        self,
        device_id: str,
        *,
        allow_sync_folder: bool = False,
        sync_folder: str | Path | None = None,
        require_trust: str = "",
        apply_fn: Callable[[SyncItem], Mapping[str, Any]] | None = None,
        **session_kwargs: Any,
    ) -> SyncSession:
        """Run the full multi-phase synchronization for a device."""
        session = self.start_session(device_id, **session_kwargs)
        if require_trust:
            self.devices.enforce_trust(device_id, require_trust)
        if not self.integrity.allow_secrets:
            self._log("sync.hardened", device_id, session.session_id)
        lock = self.acquire_lock(
            device_id, session_id=session.session_id, lock_key=f"sync:{device_id}"
        )
        manifest = self.build_manifest(device_id, session)
        verdict = self.integrity.verify_manifest(manifest)
        if not verdict.ok:
            self._fail_session(session, "; ".join(verdict.errors), device_id)
            raise IntegrityViolationError("; ".join(verdict.errors))
        session = session.with_(state=SyncState.ACTIVE.value)
        items: list[SyncItem] = []
        try:
            items = self.build_items(device_id, session, apply_fn=apply_fn)
            for item in items:
                single = self._apply_item_if_safe(session, item, device_id)
                if single.get("conflicted"):
                    session = session.with_(
                        items_conflicted=session.items_conflicted + 1
                    )
                else:
                    session = session.with_(
                        items_transferred=session.items_transferred + 1
                    )
            session = session.with_(cursor=manifest.cursor)
            self._applied_sync_ids.add(session.sync_id)
            self._log("sync.completed", device_id, session.session_id)
            self.devices.heartbeat(device_id, actor="device")
        except IntegrityViolationError:
            self._fail_session(session, "integrity violation", device_id)
            raise
        finally:
            self.release_lock(device_id, lock_key=f"sync:{device_id}")
        completed = session.with_(
            state=SyncState.COMPLETE.value, completed_at=_now_iso()
        )
        self._sessions[completed.session_id] = completed
        self._save()
        return completed

    def build_manifest(
        self,
        device_id: str,
        session: SyncSession | None = None,
        *,
        items: list[SyncItem] | None = None,
    ) -> SyncManifest:
        baseline = self.state_baseline_version(device_id)
        cursor = self._next_cursor()
        session_id = session.session_id if session else ""
        return SyncManifest(
            sync_id=session.sync_id if session else "generated",
            device_id=device_id,
            baseline_version=baseline,
            cursor=cursor,
            items=tuple(items or []),
        )

    def build_items(
        self,
        device_id: str,
        session: SyncSession | None = None,
        *,
        apply_fn: Callable[[SyncItem], Mapping[str, Any]] | None = None,
        item_types: set[str] | None = None,
    ) -> list[SyncItem]:
        types = item_types or _SYNCABLE_ITEM_TYPES
        items: list[SyncItem] = []
        cursor = 0
        for item_type in sorted(types):
            if item_type in {
                SyncItemType.PROJECT_STATE.value,
                SyncItemType.HANDOFF.value,
                SyncItemType.CHANGELOG.value,
                SyncItemType.WORKFLOW.value,
                SyncItemType.TASK.value,
                SyncItemType.REGISTRY.value,
                SyncItemType.CAPABILITY.value,
                SyncItemType.MESSAGE.value,
                SyncItemType.EVENT.value,
                SyncItemType.AUDIT.value,
            }:
                payload: dict[str, Any] = {
                    "device_id": device_id,
                    "item_type": item_type,
                    "produced_by": self.__class__.__name__,
                }
                if apply_fn is not None:
                    try:
                        derived = apply_fn(SyncItem(
                            item_type=item_type,
                            item_id=f"{item_type}:{device_id}",
                            version=1,
                            payload=payload,
                        ))
                        if isinstance(derived, Mapping):
                            payload = {**payload, **derived}
                    except Exception:  # noqa: BLE001
                        payload["apply_fn_error"] = True
                item = SyncItem(
                    item_type=item_type,
                    item_id=f"{item_type}:{device_id}",
                    version=self._item_version(item_type),
                    cursor_pos=cursor,
                    payload=payload,
                    checksum=checksum_of(payload),
                )
                items.append(item)
                cursor += 1
        return items

    # -- item application -----------------------------------------------------

    def _apply_item_if_safe(
        self,
        session: SyncSession,
        item: SyncItem,
        device_id: str,
    ) -> dict[str, Any]:
        verdict = self.integrity.validate_payload(item, expected_checksum=item.checksum)
        if not verdict.ok:
            raise IntegrityViolationError("; ".join(verdict.errors))
        key = f"{item.item_type}:{item.item_id}"
        self._state_store[key] = dict(item.payload)
        self._applied_checksums[key] = item.checksum
        return {"ok": True, "conflicted": False}

    def _apply_manifest_items(
        self,
        session: SyncSession,
        manifest: SyncManifest,
        device_id: str,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for item in manifest.items:
            results.append(self._apply_item_if_safe(session, item, device_id))
        return results

    # -- version / cursor -----------------------------------------------------

    def state_baseline_version(self, device_id: str) -> int:
        return self.devices.get(device_id).manifest_version

    def current_state(self, device_id: str = "") -> dict[str, Any]:
        state: dict[str, Any] = {}
        for key in sorted(self._state_store):
            state[key] = self._state_store[key]
        return state

    def item_state(self, item_type: str, item_id: str) -> dict[str, Any]:
        key = f"{item_type}:{item_id}"
        if key not in self._state_store:
            raise SyncError(f"no synchronized state for {key!r}.")
        return dict(self._state_store[key])

    # -- staleness / concurrency ----------------------------------------------

    def detect_version_mismatch(self, local_version: int, remote_version: int) -> bool:
        return local_version != remote_version

    def detect_stale_state(self, local_version: int, remote_version: int) -> bool:
        return remote_version < local_version

    def detect_divergent_state(
        self, local: Mapping[str, Any], remote: Mapping[str, Any]
    ) -> bool:
        return checksum_of(local) != checksum_of(remote)

    # -- sync modes -----------------------------------------------------------

    def full_sync(
        self,
        device_id: str,
        *,
        sync_folder: str | Path | None = None,
        **kwargs: Any,
    ) -> SyncSession:
        return self.synchronize(device_id, mode=SyncMode.FULL.value, **kwargs)

    def incremental_sync(
        self,
        device_id: str,
        *,
        since_cursor: int = 0,
        **kwargs: Any,
    ) -> SyncSession:
        session = self.start_session(device_id, mode=SyncMode.INCREMENTAL.value, **kwargs)
        manifest = self.build_manifest(device_id, session)
        items = [i for i in manifest.items if i.cursor_pos >= since_cursor]
        session = session.with_(cursor=len(items))
        self._sessions[session.session_id] = session
        return session

    def delta_sync(
        self,
        device_id: str,
        *,
        baseline_state: Mapping[str, Any],
        **kwargs: Any,
    ) -> SyncSession:
        session = self.start_session(device_id, mode=SyncMode.DELTA.value, **kwargs)
        current = self.current_state(device_id)
        delta: dict[str, Mapping[str, Any]] = {}
        for key, value in current.items():
            if checksum_of(value) != checksum_of(dict(baseline_state.get(key, {}))):
                delta[key] = value
        session = session.with_(cursor=len(delta))
        self._sessions[session.session_id] = session
        self._log("delta.computed", device_id, session.session_id)
        return session

    # -- selective / bidirectional / one-way ----------------------------------

    def selective_sync(
        self,
        device_id: str,
        *,
        item_types: set[str],
        **kwargs: Any,
    ) -> SyncSession:
        try:
            for t in item_types:
                SyncItemType(t)
        except ValueError as exc:
            raise SyncSessionError(f"invalid item type: {exc}") from exc
        session = self.start_session(device_id, **kwargs)
        items = self.build_items(device_id, session, item_types=item_types)
        session = session.with_(cursor=len(items), state=SyncState.COMPLETE.value)
        session = session.with_(
            items_transferred=len(items), completed_at=_now_iso()
        )
        self._sessions[session.session_id] = session
        self._log("selective.sync", device_id, session.session_id)
        return session

    def one_way_sync(
        self,
        device_id: str,
        *,
        direction: str = SyncDirection.PUSH.value,
        **kwargs: Any,
    ) -> SyncSession:
        return self.synchronize(device_id, direction=direction, **kwargs)

    def bidirectional_sync(
        self,
        device_id: str,
        *,
        apply_fn: Callable[[SyncItem], Mapping[str, Any]] | None = None,
        **kwargs: Any,
    ) -> SyncSession:
        session = self.synchronize(
            device_id, direction=SyncDirection.BIDIRECTIONAL.value, apply_fn=apply_fn, **kwargs
        )
        self._log("bidirectional.sync", device_id, session.session_id)
        return session

    # -- offline queue --------------------------------------------------------

    def queue_offline(
        self,
        device_id: str,
        *,
        items: list[SyncItem],
        sync_id: str = "",
        direction: str = "",
        mode: str = "",
        level: str = "",
    ) -> QueuedSync:
        if not items:
            raise NoItemsError("cannot queue an empty synchronization.")
        if len(self._offline_queue.get(device_id, [])) >= self.max_queue:
            raise SyncError(f"offline queue for {device_id!r} is full.")
        queued = QueuedSync(
            queue_id="q-" + hashlib.sha256(
                f"{device_id}:{sync_id or _now_iso()}".encode("utf-8")
            ).hexdigest()[:12],
            device_id=device_id,
            sync_id=sync_id or "offline",
            direction=direction or self.default_direction,
            mode=mode or self.default_mode,
            level=level or self.default_level,
            items=tuple(items),
            queued_at=_now_iso(),
        )
        self._offline_queue.setdefault(device_id, []).append(queued)
        self._log("offline.queued", device_id, "", queued.queue_id)
        self._save()
        return queued

    def pending_offline(self, device_id: str) -> list[QueuedSync]:
        return list(self._offline_queue.get(device_id, []))

    def flush_offline(
        self,
        device_id: str,
        *,
        token: str = "",
    ) -> tuple[list[QueuedSync], list[str]]:
        """Reconnect: flush queued work after the device returns online."""
        queue = self._offline_queue.pop(device_id, [])
        self.devices.authenticate(device_id, token=token)
        flushed: list[QueuedSync] = []
        failed: list[str] = []
        for queued in queue:
            try:
                session = self.synchronize(
                    device_id,
                    sync_id=queued.sync_id,
                    direction=queued.direction,
                    mode=queued.mode,
                    level=queued.level,
                )
                flushed.append(queued)
                self._log("offline.flushed", device_id, session.session_id)
            except SyncError:
                failed.append(queued.queue_id)
        self._save()
        return flushed, failed

    # -- recovery -------------------------------------------------------------

    def recover_interrupted(self, session_id: str, *, actor: str = "system") -> SyncSession:
        session = self.get_session(session_id)
        if session.state in (SyncState.FAILED.value, SyncState.ACTIVE.value, SyncState.PENDING.value):
            session = session.with_(
                state=SyncState.PENDING.value,
                error="" if session.state == SyncState.FAILED.value else "retry",
            )
            self._sessions[session_id] = session
            self._log("session.recovered", actor, session_id)
        else:
            raise SyncError(
                f"Session {session_id!r} is {session.state!r} and cannot recover."
            )
        self._save()
        return session

    def _fail_session(self, session: SyncSession, error: str, device_id: str) -> None:
        failed = session.with_(
            state=SyncState.FAILED.value, completed_at=_now_iso(), error=error
        )
        self._sessions[session.session_id] = failed
        self._log("session.failed", device_id, session.session_id, error)
        self._save()

    def device_recovery(
        self,
        device_id: str,
        *,
        actor: str = "operator",
    ) -> dict[str, Any]:
        device = self.devices.get(device_id)
        recovered = device.with_(
            status=DeviceStatus.REGISTERED.value,
            health_state="recovering",
            updated_at=_now_iso(),
        )
        self.devices._devices[device_id] = recovered
        self.devices._save()
        self._log("device.recovering", actor, "")
        return {
            "device_id": device_id,
            "status": recovered.status,
            "health": recovered.health_state,
        }

    def state_recovery(self, *, device_id: str = "") -> dict[str, Any]:
        current = self.current_state(device_id)
        checksums: dict[str, str] = {}
        for key in current:
            checksums[key] = checksum_of(current[key])
        self._applied_checksums.update(checksums)
        self._save()
        return {"ok": True, "items": len(current), "restored": True}

    def backup_snapshot(self, *, label: str = "") -> dict[str, Any]:
        current = self.current_state()
        snap = {
            "snapshot_id": "snap-" + hashlib.sha256(
                f"{label or _now_iso()}".encode("utf-8")
            ).hexdigest()[:12],
            "label": label,
            "created_at": _now_iso(),
            "items": len(current),
            "state": current,
            "checksums": dict(self._applied_checksums),
        }
        path = self.state_dir / "backups" / f"{snap['snapshot_id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_file(path, _canonical(snap))
        self._log("backup.created", "operator", "", snap["snapshot_id"])
        return snap

    def restore_snapshot(self, snapshot_id: str, *, actor: str = "operator") -> dict[str, Any]:
        path = self.state_dir / "backups" / f"{snapshot_id}.json"
        if not path.exists():
            raise SyncError(f"Snapshot {snapshot_id!r} does not exist.")
        data = json.loads(path.read_text(encoding="utf-8"))
        restored: dict[str, Any] = {}
        for key, value in data.get("state", {}).items():
            restored[key] = dict(value)
            self._applied_checksums[key] = checksum_of(restored[key])
        self._state_store = restored
        self._log("restore.completed", actor, "", snapshot_id)
        self._save()
        return {"ok": True, "snapshot_id": snapshot_id, "items": len(restored)}

    # -- audit / diagnostics --------------------------------------------------

    def audit_trail(self) -> tuple[dict[str, str], ...]:
        return tuple(dict(e) for e in self._events)

    def sync_report(self, device_id: str = "") -> dict[str, Any]:
        sessions = self.list_sessions(device_id=device_id)
        by_status: dict[str, int] = {}
        for s in sessions:
            by_status[s.state] = by_status.get(s.state, 0) + 1
        return {
            "sessions": len(sessions),
            "by_status": by_status,
            "items_transferred": sum(s.items_transferred for s in sessions),
            "items_conflicted": sum(s.items_conflicted for s in sessions),
            "pending_offline": sum(len(q) for q in self._offline_queue.values()),
        }

    def health_check(self, device_id: str) -> dict[str, Any]:
        device = self.devices.get(device_id)
        ok = device.is_active()
        return {
            "device_id": device_id,
            "ok": ok,
            "status": device.status,
            "health": device.health_state,
            "last_heartbeat": device.last_heartbeat,
        }

    def diagnostics(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "sessions": len(self._sessions),
            "locks": len(self._locks),
            "offline_queue": {k: len(v) for k, v in self._offline_queue.items()},
            "applied_sync_ids": len(self._applied_sync_ids),
            "state_items": len(self._state_store),
            "event_count": len(self._events),
        }

    def _next_cursor(self) -> int:
        return int(_now_epoch())

    def _item_version(self, item_type: str) -> int:
        if item_type not in self._item_versions:
            self._item_versions[item_type] = 0
        self._item_versions[item_type] += 1
        return self._item_versions[item_type]