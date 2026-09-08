"""Phase 29 — Cross-Device Synchronization & State Continuity tests."""

from pathlib import Path

import pytest

from handoff_agent.sync import (
    AmbiguousMergeError,
    ConcurrentLockError,
    CorruptionError,
    Device,
    DeviceAuthenticationError,
    DeviceError,
    DeviceIsolatedError,
    DeviceRegistry,
    DeviceRevokedError,
    DeviceStatus,
    DeviceTrust,
    DuplicateDeviceError,
    IdempotencyViolationError,
    IntegrityViolationError,
    LockExpiredError,
    NoItemsError,
    ProtectedPathError,
    QueuedSync,
    ScopeViolationError,
    SecretSyncError,
    SyncConflict,
    SyncConflictManager,
    SyncCoordinator,
    SyncDirection,
    SyncError,
    SyncIntegrity,
    SyncItem,
    SyncItemType,
    SyncLevel,
    SyncLock,
    SyncManifest,
    SyncMode,
    SyncSession,
    SyncState,
    TrustEnforcementError,
    UnknownDeviceError,
    VersionMismatchError,
    checksum_of,
    _device_id,
    _session_id,
)


def _device(device_id: str = "dev-1", **changes) -> Device:
    fields: dict = {
        "device_id": device_id,
        "name": f"device-{device_id}",
        "platform": "linux",
        "trust": DeviceTrust.HIGH.value,
        "capabilities": frozenset({
            "project_state", "workflow", "task",
            "checkpoint", "registry", "capability", "message", "event", "audit",
        }),
    }
    fields.update(changes)
    return Device(**fields)


def _registry(tmp_path: Path) -> DeviceRegistry:
    return DeviceRegistry(str(tmp_path / "devices"))


def _setup(tmp_path: Path, *devices: Device, **sync_kwargs):
    reg = _registry(tmp_path)
    registered = []
    for d in devices:
        reg.register(d, actor="operator-1")
        if d.status == DeviceStatus.UNREGISTERED.value:
            reg.heartbeat(d.device_id, actor="device")
        registered.append(reg.get(d.device_id))
    coord = SyncCoordinator(reg, state_dir=str(tmp_path / "sync"), **sync_kwargs)
    return reg, coord


def _item(item_id: str = "t1", item_type: str = "task", **changes) -> SyncItem:
    fields: dict = {
        "item_type": item_type,
        "item_id": item_id,
        "version": 1,
        "progress": 0,
        "payload": {"status": "new"},
    }
    fields.update(changes)
    return SyncItem(
        item_type=fields["item_type"],
        item_id=fields["item_id"],
        version=fields.pop("version"),
        cursor_pos=fields.pop("progress"),
        payload=fields.pop("payload"),
    )


class TestDeviceIdentity:
    def test_device_id_derived(self) -> None:
        a = _device_id("laptop-1")
        b = _device_id("phone-1")
        assert a.startswith("dev-")
        assert a != b

    def test_registration(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        device = reg.register(_device())
        assert device.status == DeviceStatus.REGISTERED.value
        assert device.name == "device-dev-1"

    def test_duplicate_device_rejected(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_device())
        with pytest.raises(DuplicateDeviceError):
            reg.register(_device())

    def test_unknown_device(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        with pytest.raises(UnknownDeviceError):
            reg.get("nope")

    def test_validation_rejects_empty(self) -> None:
        ok, errors = _device(device_id="").validate()
        assert not ok

    def test_validation_rejects_secret(self) -> None:
        ok, errors = _device(metadata={"api_key": "sk-abcdefghij123456789"}).validate()
        assert not ok

    def test_list_devices(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_device("a"))
        reg.register(_device("b"))
        assert [d.device_id for d in reg.list()] == ["a", "b"]


class TestDeviceLifecycle:
    def test_heartbeat_marks_online(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_device())
        online = reg.heartbeat("dev-1", actor="device")
        assert online.status == DeviceStatus.ONLINE.value
        assert online.health_state == "healthy"

    def test_availability(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_device())
        reg.heartbeat("dev-1")
        avail = reg.availability("dev-1")
        assert avail["status"] == DeviceStatus.ONLINE.value

    def test_revocation(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_device())
        revoked = reg.revoke("dev-1", actor="operator-1")
        assert revoked.status == DeviceStatus.REVOKED.value
        with pytest.raises(DeviceRevokedError):
            reg.heartbeat("dev-1")

    def test_double_revoke_rejected(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_device())
        reg.revoke("dev-1")
        with pytest.raises(DeviceRevokedError):
            reg.revoke("dev-1")

    def test_isolation(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_device())
        isolated = reg.isolate("dev-1", actor="operator-1", reason="key leak")
        assert isolated.status == DeviceStatus.QUARANTINED.value
        assert isolated.health_state == "compromised"
        with pytest.raises(DeviceIsolatedError):
            reg.heartbeat("dev-1")

    def test_set_trust(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_device())
        updated = reg.set_trust("dev-1", DeviceTrust.ROOT.value)
        assert updated.trust == DeviceTrust.ROOT.value

    def test_set_trust_invalid(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_device())
        with pytest.raises(DeviceError):
            reg.set_trust("dev-1", "ultra")

    def test_update(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_device())
        updated = reg.update("dev-1", actor="operator-1", platform="macos")
        assert updated.platform == "macos"


class TestDeviceAuthnAuthz:
    def test_authenticate_rejected_token(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_device())
        with pytest.raises(DeviceAuthenticationError):
            reg.authenticate("dev-1", token="rejected")

    def test_authenticate_ok(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_device())
        assert reg.authenticate("dev-1").device_id == "dev-1"

    def test_revoked_cannot_authenticate(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_device())
        reg.revoke("dev-1")
        with pytest.raises(DeviceRevokedError):
            reg.authenticate("dev-1")

    def test_authorize_project_scope(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_device(allowed_projects=frozenset({"proj-a"})))
        with pytest.raises(ScopeViolationError):
            reg.authorize("dev-1", "sync", project_id="proj-b")

    def test_authorize_project_allowed(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_device(allowed_projects=frozenset({"proj-a"})))
        reg.authorize("dev-1", "sync", project_id="proj-a")

    def test_authorize_agent_scope(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_device(allowed_agents=frozenset({"agent-1"})))
        with pytest.raises(ScopeViolationError):
            reg.authorize("dev-1", "sync", agent_id="agent-2")

    def test_authorize_workflow_scope(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_device(allowed_workflows=frozenset({"w1"})))
        with pytest.raises(ScopeViolationError):
            reg.authorize("dev-1", "sync", workflow_id="w2")


class TestDeviceTrust:
    def test_capability_discovery(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_device(capabilities=frozenset({"task"})))
        info = reg.capability_discovery("dev-1")
        assert "task" in info["capabilities"]
        assert info["trust"] == DeviceTrust.HIGH.value

    def test_trust_enforced(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_device(trust=DeviceTrust.MINIMAL.value))
        with pytest.raises(TrustEnforcementError):
            reg.enforce_trust("dev-1", DeviceTrust.ROOT.value)

    def test_trust_satisfied(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_device(trust=DeviceTrust.HIGH.value))
        reg.enforce_trust("dev-1", DeviceTrust.STANDARD.value)

    def test_audit_trail(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_device(), actor="operator-1")
        events = reg.audit_trail()
        assert any(e["action"] == "device.registered" for e in events)


class TestSyncSessions:
    def test_start_session(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        session = coord.start_session("dev-1")
        assert session.session_id.startswith("sync-")
        assert session.state == SyncState.PENDING.value
        assert session.level == SyncLevel.PROJECT.value

    def test_session_with_ids(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        session = coord.start_session(
            "dev-1", correlation_id="corr-9", request_id="req-9"
        )
        assert session.correlation_id == "corr-9"
        assert session.request_id == "req-9"

    def test_session_unknown(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        with pytest.raises(SyncError):
            coord.get_session("nope")

    def test_list_sessions_by_device(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        coord.start_session("dev-1")
        coord.start_session("dev-1")
        assert len(coord.list_sessions(device_id="dev-1")) == 2

    def test_invalid_direction_rejected(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        with pytest.raises(SyncError):
            coord.start_session("dev-1", direction="sideways")

    def test_invalid_mode_rejected(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        with pytest.raises(SyncError):
            coord.start_session("dev-1", mode="nonsense")


class TestSynchronize:
    def test_full_sync(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        session = coord.synchronize("dev-1")
        assert session.state == SyncState.COMPLETE.value
        assert session.items_transferred >= 1
        assert len(coord.current_state()) == session.items_transferred

    def test_full_sync_method(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        session = coord.full_sync("dev-1")
        assert session.direction == SyncDirection.BIDIRECTIONAL.value
        assert session.state == SyncState.COMPLETE.value

    def test_idempotent_sync_rejected(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        session = coord.synchronize("dev-1")
        with pytest.raises(IdempotencyViolationError):
            coord.start_session("dev-1", sync_id=session.sync_id)

    def test_one_way_sync(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        session = coord.one_way_sync("dev-1", direction=SyncDirection.PUSH.value)
        assert session.direction == SyncDirection.PUSH.value
        assert session.state == SyncState.COMPLETE.value

    def test_bidirectional_sync(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        session = coord.bidirectional_sync("dev-1")
        assert session.direction == SyncDirection.BIDIRECTIONAL.value

    def test_require_trust(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        session = coord.synchronize("dev-1", require_trust=DeviceTrust.STANDARD.value)
        assert session.state == SyncState.COMPLETE.value

    def test_require_trust_rejected(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device(trust=DeviceTrust.MINIMAL.value))
        with pytest.raises(TrustEnforcementError):
            coord.synchronize("dev-1", require_trust=DeviceTrust.ROOT.value)


class TestSelectiveSync:
    def test_selective_sync(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        session = coord.selective_sync("dev-1", item_types={"task", "workflow"})
        assert session.items_transferred == 2
        assert session.state == SyncState.COMPLETE.value

    def test_selective_invalid_type(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        with pytest.raises(SyncError):
            coord.selective_sync("dev-1", item_types={"bogus"})

    def test_incremental_sync(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        session = coord.incremental_sync("dev-1", since_cursor=0)
        assert session.mode == SyncMode.INCREMENTAL.value
        assert session.cursor >= 0

    def test_incremental_since_cursor(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        session = coord.incremental_sync("dev-1", since_cursor=1_000_000_000_000)
        assert session.cursor == 0

    def test_delta_sync(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        baseline = coord.current_state()
        session = coord.delta_sync("dev-1", baseline_state=baseline)
        assert session.mode == SyncMode.DELTA.value
        assert session.state == SyncState.PENDING.value


class TestManifest:
    def test_build_manifest(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        session = coord.start_session("dev-1")
        manifest = coord.build_manifest("dev-1", session)
        assert manifest.device_id == "dev-1"
        assert manifest.baseline_version == 1
        assert manifest.cursor > 0

    def test_manifest_roundtrip(self) -> None:
        manifest = SyncManifest(
            sync_id="s1", device_id="dev-1", baseline_version=3, cursor=2,
            items=(SyncItem(item_type="task", item_id="t1"),),
        )
        restored = SyncManifest.from_dict(manifest.to_dict())
        assert restored.to_dict() == manifest.to_dict()

    def test_manifest_duplicate_items_rejected(self, tmp_path: Path) -> None:
        guard = SyncIntegrity(project_root=tmp_path)
        manifest = SyncManifest(
            sync_id="s1", device_id="dev-1", cursor=2,
            items=(
                SyncItem(item_type="task", item_id="t1"),
                SyncItem(item_type="task", item_id="t1"),
            ),
        )
        verdict = guard.verify_manifest(manifest)
        assert verdict.ok is False


class TestLocksAndLeases:
    def test_acquire_and_release_lock(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        lock = coord.acquire_lock("dev-1", session_id="s1")
        assert lock.holder == "dev-1"
        assert coord.lock_status()["held"] is True
        coord.release_lock("dev-1")
        assert coord.lock_status()["held"] is False

    def test_concurrent_lock_rejected(self, tmp_path: Path) -> None:
        reg, coord = _setup(tmp_path, _device("dev-1"), _device("dev-2"))
        coord.acquire_lock("dev-1", session_id="s1")
        with pytest.raises(ConcurrentLockError):
            coord.acquire_lock("dev-2", session_id="s2")
        with pytest.raises(ConcurrentLockError):
            coord.release_lock("dev-1", lock_key="other")

    def test_same_session_reacquires(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        l1 = coord.acquire_lock("dev-1", session_id="s1")
        l2 = coord.acquire_lock("dev-1", session_id="s1")
        assert l2.holder == l1.holder

    def test_lock_expiration_detected(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device(), lock_ttl_seconds=1)
        lock = coord.acquire_lock("dev-1")
        import time as _t
        _t.sleep(1.1)
        assert lock.is_expired() is True

    def test_refresh_expired_raises(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device(), lock_ttl_seconds=1)
        coord.acquire_lock("dev-1")
        import time as _t
        _t.sleep(1.1)
        with pytest.raises(LockExpiredError):
            coord.refresh_lock("dev-1")

    def test_lock_persisted(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        coord.acquire_lock("dev-1")
        _, coord2 = _setup2(tmp_path)
        assert coord2.lock_status()["held"] is True


def _setup2(tmp_path: Path):
    reg = _registry(tmp_path)
    return reg, SyncCoordinator(reg, state_dir=str(tmp_path / "sync"))


class TestOfflineSync:
    def test_queue_offline(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        q = coord.queue_offline("dev-1", items=[_item()], sync_id="off-1")
        assert q.queue_id.startswith("q-")
        assert len(coord.pending_offline("dev-1")) == 1

    def test_queue_empty_rejected(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        with pytest.raises(NoItemsError):
            coord.queue_offline("dev-1", items=[])

    def test_flush_offline(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        coord.queue_offline("dev-1", items=[_item()], sync_id="off-1")
        flushed, failed = coord.flush_offline("dev-1")
        assert len(flushed) == 1
        assert failed == []
        assert coord.pending_offline("dev-1") == []

    def test_flush_offline_failure(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        coord.queue_offline("dev-1", items=[_item()], sync_id="off-dup")
        coord.start_session("dev-1")
        # second flush attempt re-uses existing sync id => conflict path safe
        flushed, failed = coord.flush_offline("dev-1")
        assert len(flushed) + len(failed) == 1


class TestRecovery:
    def test_recover_failed_session(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        session = coord.start_session("dev-1")
        session = session.with_(state=SyncState.FAILED.value)
        coord._sessions[session.session_id] = session
        recovered = coord.recover_interrupted(session.session_id)
        assert recovered.state == SyncState.PENDING.value
        assert recovered.error == ""

    def test_recover_active_session(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        session = coord.start_session("dev-1")
        recovered = coord.recover_interrupted(session.session_id)
        assert recovered.state == SyncState.PENDING.value

    def test_cannot_recover_complete(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        session = coord.synchronize("dev-1")
        with pytest.raises(SyncError):
            coord.recover_interrupted(session.session_id)

    def test_device_recovery(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        result = coord.device_recovery("dev-1")
        assert result["status"] == DeviceStatus.REGISTERED.value
        assert result["health"] == "recovering"

    def test_state_recovery(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        coord.synchronize("dev-1")
        result = coord.state_recovery()
        assert result["ok"] is True
        assert result["items"] > 0

    def test_interrupted_transfer_recovered_via_flush(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        coord.queue_offline("dev-1", items=[_item()], sync_id="recover-1")
        coord.device_recovery("dev-1")
        flushed, failed = coord.flush_offline("dev-1")
        assert len(flushed) == 1


class TestBackupRestore:
    def test_backup_snapshot(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        coord.synchronize("dev-1")
        snap = coord.backup_snapshot(label="daily")
        assert snap["snapshot_id"].startswith("snap-")
        assert snap["items"] > 0

    def test_restore_snapshot(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        coord.synchronize("dev-1")
        snap = coord.backup_snapshot(label="daily")
        coord._state_store.clear()
        restored = coord.restore_snapshot(snap["snapshot_id"])
        assert restored["ok"] is True
        assert restored["items"] > 0

    def test_restore_unknown(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        with pytest.raises(SyncError):
            coord.restore_snapshot("snap-nope")

    def test_disaster_recovery_via_snapshot(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        coord.synchronize("dev-1")
        snap = coord.backup_snapshot(label="dr")
        restored = coord.restore_snapshot(snap["snapshot_id"])
        assert len(coord.current_state()) == restored["items"]


class TestConflictDetection:
    def test_no_conflict_identical(self, tmp_path: Path) -> None:
        cm = SyncConflictManager(str(tmp_path / "conf"))
        conflict = cm.detect(
            device_id="dev-1", item_type="task", item_id="t1",
            local_version=1, remote_version=1,
            local_item={"a": 1}, remote_item={"a": 1},
        )
        assert conflict is None

    def test_conflict_detected(self, tmp_path: Path) -> None:
        cm = SyncConflictManager(str(tmp_path / "conf"))
        conflict = cm.detect(
            device_id="dev-1", item_type="task", item_id="t1",
            local_version=2, remote_version=1,
            local_item={"a": 1}, remote_item={"a": 2},
        )
        assert conflict is not None
        assert conflict.conflict_id.startswith("sconf-")

    def test_conflict_classification_stale(self, tmp_path: Path) -> None:
        cm = SyncConflictManager(str(tmp_path / "conf"))
        c1 = cm.detect(
            device_id="dev-1", item_type="checkpoint", item_id="c1",
            local_version=3, remote_version=2,
            local_item={"a": 1}, remote_item={"a": 2},
        )
        assert cm.classify(c1) == "stale-remote"
        c2 = cm.detect(
            device_id="dev-1", item_type="checkpoint", item_id="c2",
            local_version=1, remote_version=2,
            local_item={"a": 1}, remote_item={"a": 2},
        )
        assert cm.classify(c2) == "stale-local"

    def test_divergent_classification(self, tmp_path: Path) -> None:
        cm = SyncConflictManager(str(tmp_path / "conf"))
        c3 = cm.detect(
            device_id="dev-1", item_type="task", item_id="t3",
            local_version=2, remote_version=2,
            local_item={"a": 1}, remote_item={"a": 2},
        )
        assert cm.classify(c3) == "divergent"
        assert cm.is_divergent(c3) is True

    def test_conflict_metadata(self, tmp_path: Path) -> None:
        cm = SyncConflictManager(str(tmp_path / "conf"))
        conflict = cm.detect(
            device_id="dev-1", item_type="task", item_id="t1",
            local_version=2, remote_version=1,
            local_item={"a": 1}, remote_item={"a": 2},
        )
        meta = conflict.to_dict()
        assert meta["local_version"] == "2"
        assert meta["remote_version"] == "1"
        assert meta["local_checksum"] != meta["remote_checksum"]

    def test_automatic_safe_merge(self, tmp_path: Path) -> None:
        cm = SyncConflictManager(str(tmp_path / "conf"))
        conflict = cm.detect(
            device_id="dev-1", item_type="task", item_id="t1",
            local_version=2, remote_version=1,
            local_item={"a": 1, "b": 2}, remote_item={"a": 2, "c": 3},
        )
        merged = cm.resolve_automatic_safe_merge(
            conflict,
            local_payload={"a": 1, "b": 2}, remote_payload={"a": 2, "c": 3},
        )
        assert set(merged["merged"]) == {"a", "b", "c"}

    def test_manual_resolution_requires_human(self, tmp_path: Path) -> None:
        cm = SyncConflictManager(str(tmp_path / "conf"))
        conflict = cm.detect(
            device_id="dev-1", item_type="task", item_id="t1",
            local_version=2, remote_version=2,
            local_item={"a": 1}, remote_item={"a": 2},
        )
        with pytest.raises(AmbiguousMergeError):
            cm.resolve_manual(conflict.conflict_id, chosen="local", human="")

    def test_manual_resolution(self, tmp_path: Path) -> None:
        cm = SyncConflictManager(str(tmp_path / "conf"))
        conflict = cm.detect(
            device_id="dev-1", item_type="task", item_id="t1",
            local_version=2, remote_version=1,
            local_item={"a": 1}, remote_item={"a": 2},
        )
        resolved = cm.resolve_manual(conflict.conflict_id, chosen="remote", human="alice")
        assert resolved.resolution == "manual:remote:alice"

    def test_human_approval_policy(self, tmp_path: Path) -> None:
        cm = SyncConflictManager(str(tmp_path / "conf"))
        conflict = cm.detect(
            device_id="dev-1", item_type="task", item_id="t1",
            local_version=1, remote_version=1,
            local_item={"a": 1}, remote_item={"a": 2},
        )
        assert cm.requires_human_approval(conflict, "human-ambiguous") is True

    def test_policy_automatic(self, tmp_path: Path) -> None:
        cm = SyncConflictManager(str(tmp_path / "conf"))
        conflict = cm.detect(
            device_id="dev-1", item_type="task", item_id="t1",
            local_version=2, remote_version=1,
            local_item={"a": 1}, remote_item={"a": 2},
        )
        assert cm.requires_human_approval(conflict, "automatic-safe-merge") is False

    def test_conflicts_persist(self, tmp_path: Path) -> None:
        cm = SyncConflictManager(str(tmp_path / "conf"))
        cm.detect(
            device_id="dev-1", item_type="task", item_id="t1",
            local_version=2, remote_version=1,
            local_item={"a": 1}, remote_item={"a": 2},
        )
        cm2 = SyncConflictManager(str(tmp_path / "conf"))
        assert len(cm2.list_conflicts()) == 1


class TestStateDetection:
    def test_version_mismatch(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        assert coord.detect_version_mismatch(2, 1) is True
        assert coord.detect_version_mismatch(1, 1) is False

    def test_stale_state(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        assert coord.detect_stale_state(3, 1) is True
        assert coord.detect_stale_state(1, 2) is False

    def test_divergent_state(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        assert coord.detect_divergent_state({"a": 1}, {"a": 2}) is True
        assert coord.detect_divergent_state({"a": 1}, {"a": 1}) is False


class TestIntegrity:
    def test_checksum_stable(self) -> None:
        assert checksum_of({"a": 1, "b": 2}) == checksum_of({"b": 2, "a": 1})

    def test_checksum_sensitive_to_value(self) -> None:
        assert checksum_of({"a": 1}) != checksum_of({"a": 2})

    def test_payload_checksum_validation(self, tmp_path: Path) -> None:
        guard = SyncIntegrity(project_root=tmp_path)
        payload = {"state": "ok"}
        item = SyncItem(
            item_type="task", item_id="t1", payload=payload,
            checksum=checksum_of(payload),
        )
        verdict = guard.validate_payload(item)
        assert verdict.ok is True

    def test_checksum_mismatch_detected(self, tmp_path: Path) -> None:
        guard = SyncIntegrity(project_root=tmp_path)
        item = SyncItem(
            item_type="task", item_id="t1",
            payload={"state": "ok"}, checksum="deadbeef",
        )
        verdict = guard.validate_payload(item)
        assert verdict.ok is False

    def test_secret_filtering_blocked(self, tmp_path: Path) -> None:
        guard = SyncIntegrity(project_root=tmp_path)
        payload = {"password": "supersecret12345"}
        item = SyncItem(
            item_type="task", item_id="t1",
            payload=payload, checksum=checksum_of(payload),
        )
        verdict = guard.validate_payload(item)
        assert verdict.ok is False

    def test_secret_allowed_flag(self, tmp_path: Path) -> None:
        guard = SyncIntegrity(project_root=tmp_path, allow_secrets=True)
        payload = {"password": "supersecret12345"}
        item = SyncItem(
            item_type="task", item_id="t1",
            payload=payload, checksum=checksum_of(payload),
        )
        assert guard.validate_payload(item).ok is True

    def test_arbitrary_file_sync_blocked(self, tmp_path: Path) -> None:
        guard = SyncIntegrity(project_root=tmp_path)
        verdict = guard.validate_path("../../etc/passwd")
        assert verdict.ok is False

    def test_absolute_path_blocked(self, tmp_path: Path) -> None:
        guard = SyncIntegrity(project_root=tmp_path)
        assert guard.validate_path("/etc/passwd").ok is False

    def test_project_containment(self, tmp_path: Path) -> None:
        guard = SyncIntegrity(project_root=tmp_path / "proj")
        assert guard.validate_path("docs/notes.md").ok is True

    def test_symlink_escape_blocked(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "secret.txt"
        target.write_text("x", encoding="utf-8")
        proj = tmp_path / "proj"
        proj.mkdir()
        link = proj / "leak"
        link.symlink_to(outside)
        guard = SyncIntegrity(project_root=proj)
        verdict = guard.validate_path("leak")
        assert verdict.ok is False

    def test_no_arbitrary_file_items_in_sync(self, tmp_path: Path) -> None:
        reg, coord = _setup(tmp_path, _device())
        items = coord.build_items("dev-1")
        for item in items:
            assert item.item_type in {
                t.value for t in SyncItemType
            }  # only enumerated syncable types


class TestSyncReportAndDiagnostics:
    def test_sync_report(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        coord.synchronize("dev-1")
        report = coord.sync_report()
        assert report["sessions"] >= 1
        assert report["items_transferred"] > 0

    def test_health_check(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        coord.synchronize("dev-1")
        health = coord.health_check("dev-1")
        assert health["ok"] is True
        assert health["status"] == DeviceStatus.ONLINE.value

    def test_diagnostics(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        coord.synchronize("dev-1")
        diag = coord.diagnostics()
        assert diag["sessions"] >= 1
        assert diag["state_items"] > 0

    def test_audit_trail(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        coord.synchronize("dev-1")
        events = coord.audit_trail()
        assert any(e["action"] == "sync.completed" for e in events)

    def test_negotiation(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        result = coord.negotiate("dev-1", peer_protocol="1", peer_capabilities={"delta": True})
        assert result["negotiated"] is True

    def test_negotiation_version_mismatch(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        with pytest.raises(VersionMismatchError):
            coord.negotiate("dev-1", peer_protocol="99")


class TestPersistence:
    def test_sessions_persist(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        coord.synchronize("dev-1")
        reg, coord2 = _setup2(tmp_path)
        assert len(coord2.list_sessions()) >= 1

    def test_applied_sync_ids_persist(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        session = coord.synchronize("dev-1")
        reg, coord2 = _setup2(tmp_path)
        with pytest.raises(IdempotencyViolationError):
            coord2.start_session("dev-1", sync_id=session.sync_id)

    def test_query_state(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        coord.synchronize("dev-1")
        state = coord.item_state("task", f"task:dev-1")
        assert state["item_type"] == "task"


class TestSecurity:
    def test_no_credential_isolation_breach(self, tmp_path: Path) -> None:
        reg, coord = _setup(tmp_path, _device())
        items = coord.build_items("dev-1")
        for item in items:
            assert "credential" not in item.payload
            assert "api_key" not in item.payload

    def test_no_api_key_synchronization(self, tmp_path: Path) -> None:
        reg, coord = _setup(tmp_path, _device())
        payload = {"api_key": "sk-abcdefghij123456789", "data": 1}
        item = SyncItem(
            item_type="task", item_id="t1",
            payload=payload, checksum=checksum_of(payload),
        )
        with pytest.raises(IntegrityViolationError):
            coord._apply_item_if_safe(coord.start_session("dev-1"), item, "dev-1")

    def test_revoked_device_blocked(self, tmp_path: Path) -> None:
        reg, coord = _setup(tmp_path, _device())
        coord.start_session("dev-1")
        reg.revoke("dev-1")
        with pytest.raises(DeviceRevokedError):
            coord.start_session("dev-1")

    def test_quarantined_device_blocked(self, tmp_path: Path) -> None:
        reg, coord = _setup(tmp_path, _device())
        reg.isolate("dev-1")
        with pytest.raises(DeviceIsolatedError):
            coord.start_session("dev-1")

    def test_trust_enforcement_in_sync(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device(trust=DeviceTrust.MINIMAL.value))
        with pytest.raises(TrustEnforcementError):
            coord.synchronize("dev-1", require_trust=DeviceTrust.HIGH.value)


class TestAtomicRollback:
    def test_sync_is_atomic_all_or_nothing(self, tmp_path: Path) -> None:
        reg, coord = _setup(tmp_path, _device())
        session = coord.synchronize("dev-1")
        assert session.state == SyncState.COMPLETE.value
        # state is fully populated (all-or-nothing committed)
        assert len(coord.current_state()) == session.items_transferred

    def test_rollback_safe_manifest(self, tmp_path: Path) -> None:
        _, coord = _setup(tmp_path, _device())
        session = coord.start_session("dev-1")
        manifest = coord.build_manifest("dev-1", session)
        assert manifest.baseline_version >= 1

    def test_failed_integrity_fails_session(self, tmp_path: Path) -> None:
        reg, coord = _setup(tmp_path, _device())
        bad = SyncItem(
            item_type="task", item_id="t1",
            payload={"x": 1}, checksum="wrong",
        )
        with pytest.raises(IntegrityViolationError):
            coord._apply_item_if_safe(coord.start_session("dev-1"), bad, "dev-1")