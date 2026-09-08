"""Phase 24 — Universal Agent Registry & Discovery tests."""

import time
from pathlib import Path

import pytest

from handoff_agent.registry import (
    AgentRecord,
    AgentRegistry,
    AvailabilityStatus,
    CorruptRegistryError,
    DuplicateAgentError,
    InvalidAgentError,
    RegistryStateError,
    UnknownAgentError,
    UnauthorizedRegistryOperationError,
    validate_record,
)


def _record(agent_id: str = "a1", **changes) -> AgentRecord:
    fields: dict = {
        "agent_id": agent_id,
        "name": f"agent-{agent_id}",
        "agent_type": "ai",
        "platform": "claude",
        "provider": "anthropic",
        "model": "op-4",
        "capabilities": {"checkpoint.read", "checkpoint.create"},
        "permission_scope": {"checkpoint.read"},
    }
    fields.update(changes)
    return AgentRecord(**fields)


def _registry(tmp_path: Path, **kwargs) -> AgentRegistry:
    return AgentRegistry(tmp_path / "registry", **kwargs)


def _register(reg: AgentRegistry, *ids: str, actor: str = "admin-1") -> None:
    for agent_id in ids:
        reg.register(_record(agent_id), actor=actor)


class TestRegistration:
    def test_register_persists(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        stored = reg.register(_record("a1"), actor="admin-1")
        assert reg.get("a1").agent_id == "a1"
        assert stored.status == AvailabilityStatus.OFFLINE

    def test_duplicate_rejected(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        _register(reg, "a1")
        with pytest.raises(DuplicateAgentError):
            reg.register(_record("a1"), actor="admin-1")

    def test_unauthorized_registration_rejected(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        with pytest.raises(UnauthorizedRegistryOperationError):
            reg.register(_record("a1"), actor="")
        with pytest.raises(UnauthorizedRegistryOperationError):
            reg.register(_record("a1"), actor="anonymous")

    def test_unregister_and_unknown(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        _register(reg, "a1")
        removed = reg.unregister("a1", actor="admin-1")
        assert removed.agent_id == "a1"
        with pytest.raises(UnknownAgentError):
            reg.get("a1")

    def test_unregister_requires_actor(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        _register(reg, "a1")
        with pytest.raises(UnauthorizedRegistryOperationError):
            reg.unregister("a1", actor=None)

    def test_busy_agent_cannot_be_removed(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(
            _record("a1", status=AvailabilityStatus.BUSY), actor="admin-1"
        )
        with pytest.raises(RegistryStateError):
            reg.unregister("a1", actor="admin-1")


class TestValidation:
    def test_missing_id_and_name_rejected(self) -> None:
        ok, errors = validate_record(AgentRecord(agent_id="", name="x"))
        assert not ok
        ok2, errors2 = validate_record(AgentRecord(agent_id="a", name=""))
        assert not ok2

    def test_unknown_capability_rejected(self) -> None:
        ok, errors = validate_record(
            _record(capabilities={"checkpoint.read", "does.not.exist"})
        )
        assert not ok
        assert any("does.not.exist" in e for e in errors)

    def test_permission_scope_beyond_capabilities_rejected(self) -> None:
        ok, errors = validate_record(
            _record(
                capabilities={"checkpoint.read"},
                permission_scope={"checkpoint.read", "checkpoint.create"},
            )
        )
        assert not ok
        assert any("permission_scope" in e for e in errors)

    def test_secret_like_values_rejected(self) -> None:
        ok, errors = validate_record(
            _record(metadata={"api_key": "thisisasecrettokenvalue2"})
        )
        assert not ok

    def test_unsupported_agent_type_rejected(self) -> None:
        ok, errors = validate_record(_record(agent_type="robot"))
        assert not ok


class TestAvailability:
    def test_online_offline_busy_degraded_unavailable(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_record("o", status=AvailabilityStatus.ONLINE), actor="admin-1")
        reg.register(_record("b", status=AvailabilityStatus.BUSY), actor="admin-1")
        reg.register(_record("d", status=AvailabilityStatus.DEGRADED), actor="admin-1")
        reg.register(_record("u", status=AvailabilityStatus.UNAVAILABLE), actor="admin-1")
        assert reg.get("o").status == AvailabilityStatus.ONLINE
        assert reg.get("b").status == AvailabilityStatus.BUSY
        assert reg.get("d").status == AvailabilityStatus.DEGRADED
        assert reg.get("u").status == AvailabilityStatus.UNAVAILABLE
        assert reg.get("o").status.value == "online"

    def test_heartbeat_marks_online(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        _register(reg, "a1")
        updated = reg.heartbeat("a1", actor="a1")
        assert updated.status == AvailabilityStatus.ONLINE
        assert updated.last_heartbeat

    def test_heartbeat_requires_actor(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        _register(reg, "a1")
        with pytest.raises(UnauthorizedRegistryOperationError):
            reg.heartbeat("a1", actor=None)

    def test_stale_agent_detected(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path, heartbeat_timeout_seconds=1)
        _register(reg, "a1")
        reg.register(
            _record("a2", last_heartbeat="2020-01-01T00:00:00+00:00"), actor="admin-1"
        )
        stale = reg.detect_stale()
        assert [a.agent_id for a in stale] == ["a2"]
        assert reg.get("a2").status == AvailabilityStatus.UNAVAILABLE

    def test_unavailable_agent_marked_degraded_on_recovery_heartbeat(
        self, tmp_path: Path
    ) -> None:
        reg = _registry(tmp_path)
        reg.register(
            _record("a1", status=AvailabilityStatus.UNAVAILABLE), actor="admin-1"
        )
        updated = reg.heartbeat("a1", actor="a1")
        assert updated.status == AvailabilityStatus.DEGRADED

    def test_health_check(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(
            _record("a1", last_heartbeat="2020-01-01T00:00:00+00:00"), actor="admin-1"
        )
        report = reg.health_check("a1")
        assert report["agent_id"] == "a1"
        assert report["ok"] is False

    def test_status_change(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        _register(reg, "a1")
        reg.set_status("a1", AvailabilityStatus.BUSY, actor="admin-1", detail="deploy")
        assert reg.get("a1").status == AvailabilityStatus.BUSY
        assert reg.get("a1").health_detail == "deploy"


class TestDiscoveryFiltering:
    def _populate(self, tmp_path: Path) -> AgentRegistry:
        reg = _registry(tmp_path)
        reg.register(
            _record(
                "claude", platform="claude", provider="anthropic", model="op-4",
                capabilities={"checkpoint.read", "checkpoint.create", "checkpoint.update"},
                permission_scope={"checkpoint.read", "checkpoint.create", "checkpoint.update"},
                status=AvailabilityStatus.ONLINE,
            ),
            actor="admin-1",
        )
        reg.register(
            _record(
                "gemini", platform="gemini", provider="google", model="gemini-2",
                capabilities={"checkpoint.read"},
                permission_scope={"checkpoint.read"},
                status=AvailabilityStatus.ONLINE,
            ),
            actor="admin-1",
        )
        reg.register(
            _record(
                "deepseek", platform="deepseek", provider="deepseek", model="deepseek-r1",
                capabilities={"checkpoint.read"},
                permission_scope={"checkpoint.read"},
                status=AvailabilityStatus.OFFLINE,
            ),
            actor="admin-1",
        )
        return reg

    def test_capability_filtering(self, tmp_path: Path) -> None:
        reg = self._populate(tmp_path)
        matches = reg.filter(capabilities=["checkpoint.create"])
        assert [a.agent_id for a in matches] == ["claude"]

    def test_platform_provider_model_filtering(self, tmp_path: Path) -> None:
        reg = self._populate(tmp_path)
        assert [a.agent_id for a in reg.filter(provider="google")] == ["gemini"]
        assert [a.agent_id for a in reg.filter(model="op-4")] == ["claude"]
        assert [a.agent_id for a in reg.filter(platform="gemini")] == ["gemini"]

    def test_permission_filtering(self, tmp_path: Path) -> None:
        reg = self._populate(tmp_path)
        assert [
            a.agent_id
            for a in reg.filter(permission=["checkpoint.update"])
        ] == ["claude"]

    def test_trust_filtering(self, tmp_path: Path) -> None:
        reg = self._populate(tmp_path)
        reg.update("claude", actor="admin-1", trust_level=3)
        assert [a.agent_id for a in reg.filter(trust_min=2)] == ["claude"]
        assert len(reg.filter(trust_min=5)) == 0

    def test_availability_excludes_offline(self, tmp_path: Path) -> None:
        reg = self._populate(tmp_path)
        available = reg.filter(capabilities=["checkpoint.read"])
        assert "deepseek" not in [a.agent_id for a in available]

    def test_protocol_and_transport_filtering(self, tmp_path: Path) -> None:
        reg = self._populate(tmp_path)
        reg.update(
            "claude", actor="admin-1",
            interfaces=("mcp",), transports=("stdio",), auth_methods=("handshake",),
        )
        assert reg.get("claude").transports == ("stdio",)
        assert [
            a.agent_id for a in reg.filter(transport="stdio")
        ] == ["claude"]
        assert [
            a.agent_id
            for a in reg.filter(protocol_version="universal-handoff-protocol/1")
        ] == ["claude", "gemini"]

    def test_task_and_project_scope_filtering(self, tmp_path: Path) -> None:
        reg = self._populate(tmp_path)
        reg.update("claude", actor="admin-1", task_scope={"codegen"}, project_scope={"proj-1"})
        assert [a.agent_id for a in reg.filter(task_type="codegen")] == [
            "claude", "gemini",
        ]
        assert {a.agent_id for a in reg.filter(task_type="docs")} == {"gemini"}
        assert {a.agent_id for a in reg.filter(project_id="proj-1")} == {
            "claude", "gemini",
        }
        assert {a.agent_id for a in reg.filter(project_id="other")} == {"gemini"}


class TestSelectionAndRanking:
    def test_deterministic_selection(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_record("z", priority=5, status=AvailabilityStatus.ONLINE), actor="admin-1")
        reg.register(_record("a", priority=1, status=AvailabilityStatus.ONLINE), actor="admin-1")
        reg.register(_record("m", priority=1, status=AvailabilityStatus.ONLINE), actor="admin-1")
        selected = reg.select(capabilities=["checkpoint.read"], count=2)
        assert [a.agent_id for a in selected] == ["a", "m"]
        again = reg.select(capabilities=["checkpoint.read"], count=2)
        assert [a.agent_id for a in again] == [a.agent_id for a in selected]

    def test_rank_orders_by_priority_then_trust(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_record("low", priority=9, status=AvailabilityStatus.ONLINE), actor="admin-1")
        reg.register(_record("high", priority=1, status=AvailabilityStatus.ONLINE), actor="admin-1")
        ranked = reg.rank(reg.list())
        assert [a.agent_id for a in ranked] == ["high", "low"]

    def test_pick_returns_best(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_record("best", priority=1, status=AvailabilityStatus.ONLINE), actor="admin-1")
        reg.register(_record("ok", priority=5, status=AvailabilityStatus.ONLINE), actor="admin-1")
        picked = reg.pick(capabilities=["checkpoint.read"])
        assert picked is not None and picked.agent_id == "best"

    def test_select_unavailable_excluded(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_record("off", priority=1, status=AvailabilityStatus.OFFLINE), actor="admin-1")
        reg.register(_record("on", priority=5, status=AvailabilityStatus.ONLINE), actor="admin-1")
        assert reg.pick(capabilities=["checkpoint.read"]).agent_id == "on"

    def test_routing_metadata(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(
            _record("a1", endpoint="https://x", adapter="file",
                    interfaces=("mcp",), transports=("stdio",), auth_methods=("handshake",)),
            actor="admin-1",
        )
        meta = reg.get("a1").routing_metadata()
        assert meta["endpoint"] == "https://x"
        assert meta["adapter"] == "file"
        assert meta["interfaces"] == ("mcp",)
        assert "api_key" not in _canonical(meta)


def _canonical(value) -> str:
    import json
    return json.dumps(value, sort_keys=True, default=str)


class TestPersistenceAndSecurity:
    def test_reload_from_disk(self, tmp_path: Path) -> None:
        state = tmp_path / "registry"
        reg = AgentRegistry(state)
        _register(reg, "a1", "a2")
        del reg
        reloaded = AgentRegistry(state)
        assert {a.agent_id for a in reloaded.list()} == {"a1", "a2"}
        assert reloaded.get("a1").name == "agent-a1"

    def test_corrupt_registry_raises(self, tmp_path: Path) -> None:
        state = tmp_path / "registry"
        reg = AgentRegistry(state)
        _register(reg, "a1")
        state.joinpath("agents.json").write_text('{"broken":', encoding="utf-8")
        with pytest.raises(CorruptRegistryError):
            AgentRegistry(state)

    def test_duplicate_detection(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_record("one", name="same-identity"), actor="admin-1")
        reg.register(_record("two", name="same-identity"), actor="admin-1")
        assert len(reg.duplicates()) == 1

    def test_security_validation(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        with pytest.raises(InvalidAgentError):
            reg.register(
                _record("leak", metadata={"password": "thisisasecretpassword1"}),
                actor="admin-1",
            )

    def test_audit_trail_append_only(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        _register(reg, "a1")
        reg.heartbeat("a1", actor="a1")
        trail = reg.audit_trail()
        actions = [e["action"] for e in trail]
        assert "agent.registered" in actions
        assert "agent.heartbeat" in actions
        assert reg.audit_trail() == trail


class TestRecoveryBackupRestore:
    def test_backup_restore_roundtrip(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        _register(reg, "a1", "a2")
        backup = reg.backup("pre-upgrade")
        reg.register(_record("a3"), actor="admin-1")
        restored = reg.restore(backup)
        assert restored == 2
        with pytest.raises(UnknownAgentError):
            reg.get("a3")
        assert {a.agent_id for a in reg.list()} == {"a1", "a2"}

    def test_restore_rejects_corrupt_backup(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        bad = tmp_path / "bad.json"
        bad.write_text('{nope', encoding="utf-8")
        with pytest.raises(CorruptRegistryError):
            reg.restore(bad)

    def test_recover_remaps_missing(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        _register(reg, "a1")
        report = reg.recover()
        assert report["ok"] is True
        assert report["count"] == 1

    def test_health_report_and_diagnostics(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_record("a1", status=AvailabilityStatus.UNAVAILABLE), actor="admin-1")
        report = reg.health_report()
        assert report["total"] == 1
        assert report["by_status"]["unavailable"] == 1
        diag = reg.diagnostics()
        assert diag["unavailable"] == ["a1"]
        assert diag["integrity"]["writable"] is True
        assert diag["event_count"] >= 1


class TestQueryPayloads:
    def test_cli_api_mcp_payloads(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(
            _record("a1", interfaces=("mcp",), transports=("stdio",), auth_methods=("handshake",)),
            actor="admin-1",
        )
        cli = reg.cli_payload()
        assert cli["count"] == 1
        api = reg.api_payload("a1")
        assert api["agent"]["agent_id"] == "a1"
        api_all = reg.api_payload()
        assert api_all["count"] == 1
        mcp = reg.mcp_payload()
        assert mcp["agents"][0]["routing"]["endpoint"] == ""
        assert "auth_methods" not in mcp["agents"][0]
        assert "capabilities" in mcp["agents"][0]

    def test_payloads_never_expose_credentials(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_record("a1"), actor="admin-1")
        for payload in (reg.cli_payload(), reg.api_payload(), reg.mcp_payload()):
            assert _canonical(payload).find("token") == -1
            assert _canonical(payload).find("secret") == -1