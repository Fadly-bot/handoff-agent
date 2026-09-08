"""Phase 24 — Universal Agent Registry & Discovery.

A persistent, security-validated registry of agents and their capabilities,
interfaces, transports, and routing metadata. Registry records never store
credentials; secret-like values are rejected at registration.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from handoff_agent.capability import is_known_capability
from handoff_agent.constants import HANDOFF_HOME
from handoff_agent.persistence import _atomic_write_file, _contains_secret_like_content

_SUPPORTED_AGENT_TYPES = {"ai", "human", "tool", "service"}
DEFAULT_PROTOCOL_VERSION = "universal-handoff-protocol/1"


class AvailabilityStatus(Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class RegistryError(Exception):
    """Base error for the agent registry."""


class DuplicateAgentError(RegistryError):
    """An agent with this id is already registered."""


class UnknownAgentError(RegistryError):
    """No agent matches the requested id."""


class InvalidAgentError(RegistryError):
    """The agent record failed validation."""


class UnauthorizedRegistryOperationError(RegistryError):
    """The caller is not authorized for the requested operation."""


class CorruptRegistryError(RegistryError):
    """The persisted registry cannot be parsed or validated."""


class RegistryStateError(RegistryError):
    """A registry-level state transition is illegal."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_valid_actor(actor: str | None) -> bool:
    return bool(actor) and actor not in ("anonymous", "system")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


# ---------------------------------------------------------------------------
# Agent record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentRecord:
    """Declared identity, capabilities, and routing metadata for one agent."""

    agent_id: str
    name: str
    agent_type: str = "ai"
    version: str = "1"
    platform: str = ""
    provider: str = ""
    model: str = ""
    endpoint: str = ""
    adapter: str = ""
    protocol_version: str = DEFAULT_PROTOCOL_VERSION
    capabilities: frozenset[str] = field(default_factory=frozenset)
    interfaces: tuple[str, ...] = ()
    transports: tuple[str, ...] = ()
    auth_methods: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)
    tags: frozenset[str] = field(default_factory=frozenset)
    priority: int = 5
    trust_level: int = 1
    permission_scope: frozenset[str] = field(default_factory=frozenset)
    project_scope: frozenset[str] = field(default_factory=frozenset)
    task_scope: frozenset[str] = field(default_factory=frozenset)
    status: AvailabilityStatus = AvailabilityStatus.OFFLINE
    last_heartbeat: str = ""
    health_detail: str = ""
    registered_at: str = ""
    updated_at: str = ""

    def with_(self, **changes: Any) -> "AgentRecord":
        """Return an updated copy, preserving frozen-ness."""
        data = self.to_dict()
        for key, value in changes.items():
            if key == "status" and isinstance(value, str):
                value = AvailabilityStatus(value)
            data[key] = value
        return AgentRecord.from_dict(data)

    def routing_metadata(self) -> dict[str, Any]:
        """Transport/interface discovery payload (never credentials)."""
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "endpoint": self.endpoint,
            "adapter": self.adapter,
            "interfaces": tuple(self.interfaces),
            "transports": tuple(self.transports),
            "auth_methods": tuple(self.auth_methods),
            "protocol_version": self.protocol_version,
        }

    def heartbeat_age_seconds(self, now: float | None = None) -> float | None:
        """Seconds since the last heartbeat, or ``None`` if never beaten."""
        now = now if now is not None else time.time()
        if not self.last_heartbeat:
            return None
        try:
            stamp = datetime.fromisoformat(self.last_heartbeat).timestamp()
        except ValueError:
            return None
        return max(0.0, now - stamp)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "agent_type": self.agent_type,
            "version": self.version,
            "platform": self.platform,
            "provider": self.provider,
            "model": self.model,
            "endpoint": self.endpoint,
            "adapter": self.adapter,
            "protocol_version": self.protocol_version,
            "capabilities": sorted(self.capabilities),
            "interfaces": list(self.interfaces),
            "transports": list(self.transports),
            "auth_methods": list(self.auth_methods),
            "metadata": self.metadata,
            "tags": sorted(self.tags),
            "priority": self.priority,
            "trust_level": self.trust_level,
            "permission_scope": sorted(self.permission_scope),
            "project_scope": sorted(self.project_scope),
            "task_scope": sorted(self.task_scope),
            "status": self.status.value,
            "last_heartbeat": self.last_heartbeat,
            "health_detail": self.health_detail,
            "registered_at": self.registered_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentRecord":
        raw = dict(data)
        capabilities = frozenset(raw.get("capabilities", ()))
        permission_scope = frozenset(raw.get("permission_scope", ()))
        status = raw.get("status", AvailabilityStatus.OFFLINE.value)
        return cls(
            agent_id=str(raw.get("agent_id", "")),
            name=str(raw.get("name", "")),
            agent_type=str(raw.get("agent_type", "ai")),
            version=str(raw.get("version", "1")),
            platform=str(raw.get("platform", "")),
            provider=str(raw.get("provider", "")),
            model=str(raw.get("model", "")),
            endpoint=str(raw.get("endpoint", "")),
            adapter=str(raw.get("adapter", "")),
            protocol_version=str(raw.get("protocol_version", DEFAULT_PROTOCOL_VERSION)),
            capabilities=capabilities,
            interfaces=tuple(raw.get("interfaces", ())),
            transports=tuple(raw.get("transports", ())),
            auth_methods=tuple(raw.get("auth_methods", ())),
            metadata=dict(raw.get("metadata", {})),
            tags=frozenset(raw.get("tags", ())),
            priority=int(raw.get("priority", 5)),
            trust_level=int(raw.get("trust_level", 1)),
            permission_scope=permission_scope,
            project_scope=frozenset(raw.get("project_scope", ())),
            task_scope=frozenset(raw.get("task_scope", ())),
            status=AvailabilityStatus(status),
            last_heartbeat=str(raw.get("last_heartbeat", "")),
            health_detail=str(raw.get("health_detail", "")),
            registered_at=str(raw.get("registered_at", "")),
            updated_at=str(raw.get("updated_at", "")),
        )


def validate_record(record: AgentRecord) -> tuple[bool, list[str]]:
    """Validate identity, capabilities, and permission scoping."""
    errors: list[str] = []
    if not record.agent_id:
        errors.append("agent_id is required")
    if not record.name:
        errors.append("name is required")
    if record.agent_type not in _SUPPORTED_AGENT_TYPES:
        errors.append(f"agent_type {record.agent_type!r} is unsupported")
    if not record.protocol_version:
        errors.append("protocol_version is required")
    unknown = sorted(c for c in record.capabilities if not is_known_capability(c))
    if unknown:
        errors.append(
            "capabilities reference unknown capability ids: "
            + ", ".join(unknown)
        )
    beyond = sorted(record.permission_scope - record.capabilities)
    if beyond:
        errors.append(
            "permission_scope must be a subset of declared capabilities; "
            "missing: " + ", ".join(beyond)
        )
    _secret_scan = record.to_dict()
    if _contains_secret_like_content(_canonical(_secret_scan)):
        errors.append("record contains secret-like values")
    _secret_scan.setdefault("metadata", {})
    return (not errors, errors)


def _validate_authorized(actor: str | None, action: str) -> None:
    if not _is_valid_actor(actor):
        raise UnauthorizedRegistryOperationError(
            f"{action} requires an authorized actor."
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class AgentRegistry:
    """Persistent, append-audited registry of agents (Phase 24)."""

    def __init__(
        self,
        state_dir: str | Path | None = None,
        *,
        heartbeat_timeout_seconds: int = 300,
    ) -> None:
        self.state_dir = Path(state_dir or HANDOFF_HOME / "registry")
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._agents: dict[str, AgentRecord] = {}
        self._events: list[dict[str, str]] = []
        self._load()

    # -- persistence --------------------------------------------------------

    @property
    def path(self) -> Path:
        return self.state_dir / "agents.json"

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            raw_agents = data if isinstance(data, list) else data.get("agents", [])
            events = data.get("events", []) if isinstance(data, dict) else []
            loaded: dict[str, AgentRecord] = {}
            for raw in raw_agents:
                record = AgentRecord.from_dict(raw)
                ok, errors = validate_record(record)
                if not ok:
                    raise CorruptRegistryError(
                        f"Persisted agent {record.agent_id!r} failed validation: "
                        + "; ".join(errors)
                    )
                if record.agent_id in loaded:
                    raise CorruptRegistryError(
                        f"Duplicate agent {record.agent_id!r} in persisted registry."
                    )
                loaded[record.agent_id] = record
        except (OSError, ValueError, TypeError, KeyError, CorruptRegistryError) as exc:
            if isinstance(exc, CorruptRegistryError):
                raise
            raise CorruptRegistryError(
                f"Could not parse persisted registry: {exc}"
            ) from exc
        self._agents = loaded
        self._events = [dict(e) for e in events]

    def _save(self) -> None:
        payload = {
            "version": "1",
            "updated_at": _now_iso(),
            "agents": [a.to_dict() for a in self._agents.values()],
            "events": list(self._events),
        }
        _atomic_write_file(self.path, _canonical(payload))

    def _log(self, action: str, actor: str, detail: str = "") -> None:
        self._events.append(
            {
                "timestamp": _now_iso(),
                "actor": actor or "system",
                "action": action,
                "detail": detail,
            }
        )

    # -- registration -------------------------------------------------------

    def register(
        self,
        record: AgentRecord,
        *,
        actor: str | None = None,
        force: bool = False,
    ) -> AgentRecord:
        """Register a new agent. Raises for duplicates, invalid, unauthorized."""
        if record.agent_id in self._agents and not force:
            raise DuplicateAgentError(f"Agent {record.agent_id!r} is already registered.")
        ok, errors = validate_record(record)
        if not ok:
            raise InvalidAgentError("; ".join(errors))
        _validate_authorized(actor, "registration")
        now = _now_iso()
        stored = record.with_(
            status=record.status,
            registered_at=record.registered_at or now,
            updated_at=now,
        )
        self._agents[stored.agent_id] = stored
        self._log("agent.registered", actor, stored.agent_id)
        self._save()
        return stored

    def unregister(self, agent_id: str, *, actor: str | None = None) -> AgentRecord:
        _validate_authorized(actor, "unregistration")
        record = self.get(agent_id)
        if record.status == AvailabilityStatus.BUSY:
            raise RegistryStateError(
                f"Agent {agent_id!r} is busy and cannot be removed."
            )
        del self._agents[agent_id]
        self._log("agent.unregistered", actor, agent_id)
        self._save()
        return record

    def get(self, agent_id: str) -> AgentRecord:
        if agent_id not in self._agents:
            raise UnknownAgentError(f"Agent {agent_id!r} is not registered.")
        return self._agents[agent_id]

    def get_many(self, agent_ids: Iterable[str]) -> list[AgentRecord]:
        missing = [i for i in set(agent_ids) if i not in self._agents]
        if missing:
            raise UnknownAgentError(
                "Unknown agents: " + ", ".join(sorted(missing))
            )
        return [self._agents[i] for i in self._agents if i in set(agent_ids)]

    def list(self) -> list[AgentRecord]:
        """Deterministically ordered snapshot of the registry."""
        return sorted(
            self._agents.values(),
            key=lambda a: (a.priority, a.agent_type, a.name, a.agent_id),
        )

    def update(
        self,
        agent_id: str,
        *,
        actor: str | None = None,
        **changes: Any,
    ) -> AgentRecord:
        _validate_authorized(actor, "modification")
        record = self.get(agent_id)
        merged = record.with_(**changes)
        ok, errors = validate_record(merged)
        if not ok:
            raise InvalidAgentError("; ".join(errors))
        merged = merged.with_(updated_at=_now_iso())
        self._agents[agent_id] = merged
        self._log("agent.updated", actor, agent_id)
        self._save()
        return merged

    # -- availability -------------------------------------------------------

    def heartbeat(self, agent_id: str, *, actor: str | None = None) -> AgentRecord:
        _validate_authorized(actor, "heartbeat")
        record = self.get(agent_id)
        recovering = record.status == AvailabilityStatus.UNAVAILABLE
        updated = record.with_(
            status=(
                AvailabilityStatus.DEGRADED if recovering else AvailabilityStatus.ONLINE
            ),
            last_heartbeat=_now_iso(),
            health_detail=(
                "recovering after unavailability" if recovering else "heartbeat acknowledged"
            ),
        )
        self._agents[agent_id] = updated
        self._log("agent.heartbeat", actor, agent_id)
        self._save()
        return updated

    def set_status(
        self,
        agent_id: str,
        status: AvailabilityStatus | str,
        *,
        actor: str | None = None,
        detail: str = "",
    ) -> AgentRecord:
        _validate_authorized(actor, "status change")
        record = self.get(agent_id)
        if isinstance(status, str):
            status = AvailabilityStatus(status)
        updated = record.with_(status=status, health_detail=detail)
        self._agents[agent_id] = updated
        self._log("agent.status", actor, f"{agent_id}={status.value} {detail}".strip())
        self._save()
        return updated

    def detect_stale(self) -> list[AgentRecord]:
        """Mark agents whose heartbeat expired as UNAVAILABLE."""
        stale: list[AgentRecord] = []
        if not self.heartbeat_timeout_seconds:
            return stale
        now = time.time()
        for agent_id, record in self._agents.items():
            age = record.heartbeat_age_seconds(now)
            if age is not None and age > self.heartbeat_timeout_seconds:
                if record.status != AvailabilityStatus.UNAVAILABLE:
                    updated = record.with_(
                        status=AvailabilityStatus.UNAVAILABLE,
                        health_detail=f"heartbeat older than {self.heartbeat_timeout_seconds}s",
                    )
                    self._agents[agent_id] = updated
                    self._log("agent.stale", "system", agent_id)
                    stale.append(updated)
        if stale:
            self._save()
        return stale

    def duplicates(self) -> list[str]:
        """Flag canonical duplicates: same canonical identity, multiple records."""
        seen: dict[str, str] = {}
        dups: list[str] = []
        for record in self._agents.values():
            key = _canonical(
                {
                    "name": record.name,
                    "provider": record.provider,
                    "model": record.model,
                    "platform": record.platform,
                }
            )
            if key in seen:
                dups.append(f"{record.agent_id} duplicates {seen[key]}")
            else:
                seen[key] = record.agent_id
        return dups

    def health_check(self, agent_id: str) -> dict[str, Any]:
        """Derived health view: online/stale/degraded with detail."""
        record = self.get(agent_id)
        age = record.heartbeat_age_seconds()
        fresh = age is not None and age <= self.heartbeat_timeout_seconds
        return {
            "agent_id": agent_id,
            "status": record.status.value,
            "heartbeat_age_seconds": age,
            "fresh": fresh,
            "ok": record.status in (AvailabilityStatus.ONLINE, AvailabilityStatus.BUSY)
            and fresh,
            "detail": record.health_detail,
        }

    # -- discovery and filtering -------------------------------------------

    def filter(
        self,
        *,
        capabilities: Iterable[str] | None = None,
        platform: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        protocol_version: str | None = None,
        transport: str | None = None,
        permission: Iterable[str] | None = None,
        trust_min: int | None = None,
        agent_type: str | None = None,
        statuses: Iterable[AvailabilityStatus | str] | None = None,
        task_type: str | None = None,
        project_id: str | None = None,
        available_only: bool = True,
    ) -> list[AgentRecord]:
        """Apply deterministic capability/platform/provider/filtering."""
        cap_req = set(capabilities or ())
        perm_req = set(permission or ())
        want_statuses = {s.value if isinstance(s, AvailabilityStatus) else s for s in (statuses or ())}
        results = []
        for record in self._agents.values():
            if cap_req and not cap_req.issubset(record.capabilities):
                continue
            if perm_req and not perm_req.issubset(record.permission_scope):
                continue
            if platform and platform != record.platform:
                continue
            if provider and provider != record.provider:
                continue
            if model and model != record.model:
                continue
            if protocol_version and protocol_version != record.protocol_version:
                continue
            if transport and transport not in record.transports:
                continue
            if trust_min is not None and record.trust_level < trust_min:
                continue
            if agent_type and agent_type != record.agent_type:
                continue
            if want_statuses and record.status.value not in want_statuses:
                continue
            if available_only and record.status in (
                AvailabilityStatus.OFFLINE,
                AvailabilityStatus.UNAVAILABLE,
            ):
                continue
            if task_type and record.task_scope and task_type not in record.task_scope:
                continue
            if project_id and record.project_scope and project_id not in record.project_scope:
                continue
            results.append(record)
        results.sort(key=lambda a: (a.priority, -a.trust_level, a.name, a.agent_id))
        return results

    # -- selection and ranking ---------------------------------------------

    def rank(self, candidates: Iterable[AgentRecord]) -> list[AgentRecord]:
        """Stable, deterministic ranking of agents."""
        return sorted(
            candidates,
            key=lambda a: (a.priority, -a.trust_level, a.name, a.agent_id),
        )

    def select(
        self,
        *,  # for api/MCP helpers, expose selected count instead of unwrap
        count: int = 1,
        **filters: Any,
    ) -> list[AgentRecord]:
        """Deterministically select the top ``count`` matches."""
        return self.filter(**filters)[: max(0, count)]

    def pick(self, **filters: Any) -> AgentRecord | None:
        ranked = self.filter(**filters)
        return ranked[0] if ranked else None

    # -- query payloads ----------------------------------------------------

    def cli_payload(self) -> dict[str, Any]:
        return {
            "agents": [a.to_dict() for a in self.list()],
            "count": len(self._agents),
        }

    def api_payload(self, agent_id: str | None = None) -> dict[str, Any]:
        if agent_id is not None:
            return {"agent": self.get(agent_id).to_dict()}
        return {
            "count": len(self._agents),
            "agents": [a.to_dict() for a in self.list()],
        }

    def mcp_payload(self) -> dict[str, Any]:
        view = []
        for a in self.list():
            view.append(
                {
                    "agent_id": a.agent_id,
                    "name": a.name,
                    "status": a.status.value,
                    "capabilities": sorted(a.capabilities),
                    "platform": a.platform,
                    "provider": a.provider,
                    "model": a.model,
                    "routing": a.routing_metadata(),
                }
            )
        return {"count": len(view), "agents": view}

    # -- audit --------------------------------------------------------------

    def audit_trail(self) -> tuple[dict[str, str], ...]:
        return tuple(dict(e) for e in self._events)

    # -- recovery / backup / health ----------------------------------------

    def backup(self, label: str = "manual") -> Path:
        destination = self.state_dir / f"agents.backup-{label}-{int(time.time())}.json"
        _atomic_write_file(
            destination,
            _canonical(
                {
                    "backup": True,
                    "agents": [a.to_dict() for a in self._agents.values()],
                    "events": list(self._events),
                }
            ),
        )
        self._log("registry.backup", "system", destination.name)
        self._save()
        return destination

    def restore(self, backup_path: str | Path) -> int:
        path = Path(backup_path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            raw_agents = data.get("agents", [])
            events = data.get("events", [])
        except (OSError, ValueError, TypeError) as exc:
            raise CorruptRegistryError(f"Backup {path} cannot be read: {exc}") from exc
        restored: dict[str, AgentRecord] = {}
        for raw in raw_agents:
            record = AgentRecord.from_dict(raw)
            ok, errors = validate_record(record)
            if not ok:
                raise CorruptRegistryError(
                    f"Backup agent {record.agent_id!r} failed validation: "
                    + "; ".join(errors)
                )
            restored[record.agent_id] = record
        self._agents = restored
        self._events = [dict(e) for e in events]
        self._log("registry.restore", "system", path.name)
        self._save()
        return len(restored)

    def recover(self) -> dict[str, Any]:
        """Re-validate, re-sync, and detect stale/unavailable agents."""
        prior = dict(self._agents)
        self._load()
        reported: list[str] = []
        for agent_id in set(prior) - set(self._agents):
            reported.append(f"{agent_id} lost during recovery")
        stale = self.detect_stale()
        if stale:
            reported.append(f"{len(stale)} agents marked unavailable")
        return {
            "ok": not reported,
            "count": len(self._agents),
            "reported": reported,
            "stale": [a.agent_id for a in stale],
        }

    def health_report(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for record in self._agents.values():
            counts[record.status.value] = counts.get(record.status.value, 0) + 1
        return {
            "ok": self._agents == {} or counts.get(
                AvailabilityStatus.UNAVAILABLE.value, 0
            ) == 0,
            "total": len(self._agents),
            "by_status": counts,
        }

    def diagnostics(self) -> dict[str, Any]:
        problematic = [
            a.agent_id
            for a in self._agents.values()
            if a.status == AvailabilityStatus.UNAVAILABLE
        ]
        return {
            "path": str(self.path),
            "total": len(self._agents),
            "duplicates": self.duplicates(),
            "unavailable": problematic,
            "event_count": len(self._events),
            "integrity": {
                "writable": self.state_dir.is_dir(),
                "events_append_only": len(self._events)
                == len({e.get("timestamp") for e in self._events}),
            },
        }

    def events(self) -> tuple[dict[str, str], ...]:
        return self.audit_trail()