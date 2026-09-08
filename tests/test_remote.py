"""Phase 28 — Remote Handoff & Network Interoperability tests."""

from pathlib import Path

import pytest

from handoff_agent.remote import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    CorruptionError,
    DuplicateEndpointError,
    EndpointRegistry,
    EndpointStatus,
    EndpointUnavailableError,
    EndpointUnavailableError as EndpointUnavailable,
    HumanApprovalRequiredError,
    IntegrityError,
    RateLimitError,
    ReadOnlyModeError,
    RemoteConflict,
    RemoteConflictManager,
    RemoteEndpoint,
    RemoteError,
    RemoteHandoff,
    RemoteOperation,
    RemoteProtocolError,
    RemoteTransport,
    ReplayError,
    SSRFBlockedError,
    StaleCheckpointError,
    TimeoutError,
    UnknownEndpointError,
    _endpoint_id,
)


def _endpoint(endpoint_id: str = "ep1", **changes) -> RemoteEndpoint:
    fields: dict = {
        "endpoint_id": endpoint_id,
        "url": "https://hub.example.com:443",
        "name": "hub",
        "transport": "https",
        "protocol_version": "1",
        "capabilities": frozenset({"checkpoint.read", "checkpoint.write"}),
    }
    fields.update(changes)
    return RemoteEndpoint(**fields)


def _registry(tmp_path: Path, allowlist=None) -> EndpointRegistry:
    return EndpointRegistry(str(tmp_path / "endpoints"), allowlist=allowlist)


class TestEndpointConfiguration:
    def test_creates_remote_endpoint(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        ep = _endpoint()
        returned = reg.register(ep, actor="admin-1")
        assert returned.endpoint_id == "ep1"
        assert returned.url == "https://hub.example.com:443"
        assert returned.protocol_version == "1"

    def test_endpoint_identity_derived_from_url(self) -> None:
        a = _endpoint_id("https://hub.example.com")
        b = _endpoint_id("https://other.example.com")
        assert a.startswith("end-")
        assert a != b

    def test_duplicate_endpoint_rejected(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        with pytest.raises(DuplicateEndpointError):
            reg.register(_endpoint())

    def test_unknown_endpoint(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        with pytest.raises(UnknownEndpointError):
            reg.get("nope")

    def test_validation_rejects_empty_id(self) -> None:
        ok, errors = _endpoint(endpoint_id="").validate()
        assert not ok
        assert any("endpoint_id" in e for e in errors)

    def test_validation_rejects_bad_transport(self) -> None:
        ok, errors = _endpoint(transport="gopher").validate()
        assert not ok
        assert any("transport" in e for e in errors)

    def test_validation_rejects_secret_like_values(self) -> None:
        ok, errors = _endpoint(name="api_key=sk-abcdefghij123456789").validate()
        assert not ok

    def test_endpoint_update(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        updated = reg.update("ep1", timeout_seconds=15)
        assert updated.timeout_seconds == 15

    def test_endpoint_unregister(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        assert reg.unregister("ep1").endpoint_id == "ep1"
        with pytest.raises(UnknownEndpointError):
            reg.get("ep1")

    def test_endpoint_to_dict_roundtrip(self) -> None:
        ep = _endpoint(capabilities=frozenset({"a", "b"}), allowed_projects=frozenset({"x"}))
        restored = RemoteEndpoint.from_dict(ep.to_dict())
        assert restored.to_dict() == ep.to_dict()


class TestEndpointDiscovery:
    def test_list_endpoints(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint("a"))
        reg.register(_endpoint("b"))
        assert [e.endpoint_id for e in reg.list()] == ["a", "b"]

    def test_discovery_sorted(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint("zzz", name="z"))
        reg.register(_endpoint("aaa", name="a"))
        names = [e.name for e in reg.list()]
        assert names == ["a", "z"]

    def test_health_report_counts(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint("a"))
        reg.register(_endpoint("b", status=EndpointStatus.UNAVAILABLE.value))
        report = reg.health_report()
        assert report["total"] == 2
        assert report["by_status"][EndpointStatus.UNAVAILABLE.value] == 1
        assert report["ok"] is False

    def test_diagnostics(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        diag = reg.diagnostics()
        assert diag["total"] == 1
        assert "endpoints.json" in diag["path"]

    def test_audit_trail(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint(), actor="admin-1")
        events = reg.audit_trail()
        assert any(e["action"] == "endpoint.registered" for e in events)
        assert all(e["actor"] == "admin-1" for e in events)


class TestPersistence:
    def test_reload_endpoints(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        reg2 = _registry(tmp_path)
        assert reg2.get("ep1").url == "https://hub.example.com:443"

    def test_corrupt_state_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "endpoints" / "endpoints.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not-json", encoding="utf-8")
        with pytest.raises(CorruptionError):
            _registry(tmp_path)

    def test_invalid_persisted_endpoint_raises(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        import json
        path = tmp_path / "endpoints" / "endpoints.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["endpoints"]["ep1"]["transport"] = "bogus"
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(CorruptionError):
            _registry(tmp_path)


class TestProtocol:
    def test_protocol_version_negotiation(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint(protocol_version="1"))
        client = RemoteHandoff(reg)
        resp = client.discover_capabilities("ep1")
        assert resp["protocol_version"] == "1"

    def test_capability_declaration(self) -> None:
        ep = _endpoint(capabilities=frozenset({"checkpoint.read"}))
        assert "checkpoint.read" in ep.capabilities

    def test_transport_negotiation_local(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint(transport="local", url="file:///tmp/x"))
        client = RemoteHandoff(reg)
        resp = client.read_project_state("ep1", project_id="p1")
        assert resp["transport"] == "local"

    def test_transport_https_default(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        client = RemoteHandoff(reg)
        resp = client.discover_capabilities("ep1")
        assert resp["protocol_version"] == "1"


class TestRemoteAccess:
    def test_remote_project_state_access(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        resp = RemoteHandoff(reg).read_project_state("ep1", project_id="p1")
        assert resp["ok"] is True
        assert resp["payload"]["project_id"] == "p1"

    def test_remote_checkpoint_access(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        resp = RemoteHandoff(reg).read_checkpoint("ep1", checkpoint_id="ckpt-1")
        assert resp["payload"]["checkpoint_id"] == "ckpt-1"

    def test_remote_workflow_access(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        resp = RemoteHandoff(reg).read_workflow("ep1", workflow_id="w1")
        assert resp["payload"]["workflow_id"] == "w1"

    def test_remote_task_access(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        resp = RemoteHandoff(reg).read_task("ep1", task_id="t1")
        assert resp["payload"]["task_id"] == "t1"

    def test_remote_agent_access(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        resp = RemoteHandoff(reg).read_agent("ep1", agent_id="agent-1")
        assert resp["payload"]["agent_id"] == "agent-1"

    def test_remote_registry_access(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        resp = RemoteHandoff(reg).read_registry("ep1")
        assert resp["ok"] is True

    def test_remote_messaging(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        resp = RemoteHandoff(reg).send_message("ep1", message={"topic": "t1"})
        assert resp["payload"]["message"]["topic"] == "t1"

    def test_capability_discovery(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        resp = RemoteHandoff(reg).discover_capabilities("ep1")
        assert resp["payload"]["action"] == "discover_capabilities"

    def test_remote_validation(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        resp = RemoteHandoff(reg).validate_remote("ep1", payload={"k": "v"})
        assert resp["payload"]["payload"]["k"] == "v"


class TestRemoteWrites:
    def test_remote_checkpoint_creation(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        resp = RemoteHandoff(reg).write_checkpoint(
            "ep1", checkpoint={"state": 1}, version=1
        )
        assert resp["payload"]["version"] == 1

    def test_remote_checkpoint_update_version(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        resp = RemoteHandoff(reg).write_checkpoint(
            "ep1", checkpoint={"state": 2}, version=2
        )
        assert resp["payload"]["version"] == 2

    def test_remote_sync(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        resp = RemoteHandoff(reg).sync_remote(
            "ep1", local_state={"x": 1}, project_id="p1"
        )
        assert resp["payload"]["local_state"]["x"] == 1


class TestReadOnlyMode:
    def test_client_read_only_rejects_write(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        client = RemoteHandoff(reg, read_only=True)
        with pytest.raises(ReadOnlyModeError):
            client.write_checkpoint("ep1", checkpoint={"a": 1})

    def test_endpoint_read_only_rejects_write(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint(read_only=True))
        client = RemoteHandoff(reg)
        with pytest.raises(ReadOnlyModeError):
            client.write_checkpoint("ep1", checkpoint={"a": 1})

    def test_read_only_allows_reads(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        client = RemoteHandoff(reg, read_only=True)
        resp = client.read_project_state("ep1")
        assert resp["ok"] is True


class TestHumanApproval:
    def test_privileged_write_requires_human(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        client = RemoteHandoff(reg, require_human_approval=True)
        with pytest.raises(HumanApprovalRequiredError):
            client.write_checkpoint("ep1", checkpoint={"a": 1}, privileged=True)

    def test_privileged_write_with_human(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        client = RemoteHandoff(reg, require_human_approval=True)
        resp = client.write_checkpoint(
            "ep1", checkpoint={"a": 1}, privileged=True, human="alice"
        )
        assert resp["ok"] is True

    def test_unprivileged_write_needs_no_human(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        client = RemoteHandoff(reg, require_human_approval=True)
        resp = client.write_checkpoint("ep1", checkpoint={"a": 1})
        assert resp["ok"] is True


class TestAuthorizationScope:
    def test_project_scope_enforced(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint(allowed_projects=frozenset({"proj-a"})))
        client = RemoteHandoff(reg)
        with pytest.raises(RemoteError):
            client.read_project_state("ep1", project_id="proj-b")

    def test_project_scope_allowed(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint(allowed_projects=frozenset({"proj-a"})))
        resp = RemoteHandoff(reg).read_project_state("ep1", project_id="proj-a")
        assert resp["ok"] is True

    def test_task_scope_enforced(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint(allowed_tasks=frozenset({"t1"})))
        client = RemoteHandoff(reg)
        with pytest.raises(RemoteError):
            client.read_task("ep1", task_id="t2")

    def test_agent_scope_enforced(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint(allowed_agents=frozenset({"agent-1"})))
        client = RemoteHandoff(reg)
        with pytest.raises(RemoteError):
            client.read_agent("ep1", agent_id="agent-2")

    def test_workflow_scope_enforced(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint(allowed_workflows=frozenset({"w1"})))
        client = RemoteHandoff(reg)
        with pytest.raises(RemoteError):
            client.read_workflow("ep1", workflow_id="w2")


class TestNetworkHandling:
    class FlakyTransport(RemoteTransport):
        def __init__(self, fail_first: int = 1) -> None:
            super().__init__()
            self.calls = 0
            self.fail_first = fail_first

        def request(self, endpoint, operation, payload=None, **kwargs):
            self.calls += 1
            if self.calls <= self.fail_first:
                raise ConnectionError("network down")
            return {
                "ok": True,
                "operation": operation,
                "payload": dict(payload or {}),
                "protocol_version": endpoint.protocol_version,
                "integrity_digest": kwargs.get("integrity_digest", ""),
            }

    def test_retry_on_transient_failure(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        client = RemoteHandoff(reg, transport=self.FlakyTransport(fail_first=1))
        resp = client.read_project_state("ep1")
        assert resp["ok"] is True

    def test_exponential_backoff_attempts(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint(max_retries=2, backoff_base_seconds=0))
        client = RemoteHandoff(reg, transport=self.FlakyTransport(fail_first=3))
        with pytest.raises(EndpointUnavailableError):
            client.read_project_state("ep1")

    def test_unavailable_endpoint_raises(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        class AlwaysDown(RemoteTransport):
            def request(self, *a, **k):
                raise ConnectionError("down")
        client = RemoteHandoff(reg, transport=AlwaysDown())
        with pytest.raises(EndpointUnavailableError):
            client.read_project_state("ep1")

    def test_rate_limit_retried(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint(backoff_base_seconds=0))
        class Throttled(RemoteTransport):
            def __init__(self):
                super().__init__()
                self.calls = 0
            def request(self, *a, **k):
                self.calls += 1
                if self.calls == 1:
                    return {"ok": False, "rate_limited": True, "payload": {}}
                return {"ok": True, "payload": {}, "protocol_version": "1",
                        "integrity_digest": k.get("integrity_digest", "")}
        client = RemoteHandoff(reg, transport=Throttled())
        resp = client.read_project_state("ep1")
        assert resp["ok"] is True

    def test_mark_unavailable_after_exhaustion(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        class AlwaysDown(RemoteTransport):
            def request(self, *a, **k):
                raise ConnectionError("down")
        client = RemoteHandoff(reg, transport=AlwaysDown())
        with pytest.raises(EndpointUnavailableError):
            client.read_project_state("ep1")
        assert reg.get("ep1").status == EndpointStatus.UNAVAILABLE.value


class TestIdempotencyReplay:
    def test_replay_detected(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        client = RemoteHandoff(reg)
        client.read_project_state("ep1", request_id="req-1")
        with pytest.raises(ReplayError):
            client.read_project_state("ep1", request_id="req-1")

    def test_correlation_propagation(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        client = RemoteHandoff(reg)
        resp = client.read_project_state("ep1", correlation_id="corr-7")
        assert resp["correlation_id"] == "corr-7"

    def test_request_id_propagation(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        client = RemoteHandoff(reg)
        resp = client.read_project_state("ep1", request_id="req-abc")
        assert resp["request_id"] == "req-abc"

    def test_auto_request_id(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        client = RemoteHandoff(reg)
        resp = client.read_project_state("ep1")
        assert resp["request_id"].startswith("req-")


class TestSecurity:
    def test_ssrf_private_host_blocked(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint("ep2", url="https://10.0.0.1:8443"))
        client = RemoteHandoff(reg)
        with pytest.raises(SSRFBlockedError):
            client.read_project_state("ep2")

    def test_ssrf_allowlist_enforced(self, tmp_path: Path) -> None:
        allow = {"https://good.example.com"}
        reg = EndpointRegistry(str(tmp_path / "r"), allowlist=frozenset(allow))
        with pytest.raises(SSRFBlockedError):
            reg.register(_endpoint("bad", url="https://bad.example.com"))

    def test_ssrf_allowlist_allows_good(self, tmp_path: Path) -> None:
        allow = {"https://good.example.com"}
        reg = EndpointRegistry(str(tmp_path / "r2"), allowlist=frozenset(allow))
        reg.register(_endpoint(url="https://good.example.com"))
        resp = RemoteHandoff(reg).read_project_state("ep1")
        assert resp["ok"] is True

    def test_tls_validation_enforced(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint(trusted_tls=False))
        client = RemoteHandoff(reg)
        with pytest.raises(RemoteError):
            client.read_project_state("ep1")

    def test_payload_integrity(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        class Tampering(RemoteTransport):
            def request(self, *a, **k):
                return {"ok": True, "payload": {"tampered": "yes"}, "protocol_version": "1"}
        client = RemoteHandoff(reg, transport=Tampering())
        with pytest.raises(IntegrityError):
            client.read_project_state("ep1")

    def test_message_size_limit(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        client = RemoteHandoff(reg, transport=RemoteTransport(max_message_size=10))
        with pytest.raises(RemoteError):
            client.execute("ep1", "read_state", {"big": "value" * 100})

    def test_response_size_limit(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        class Huge(RemoteTransport):
            def request(self, *a, **k):
                return {"ok": True, "payload": {"data": "x" * 10000}, "protocol_version": "1"}
        client = RemoteHandoff(reg, transport=Huge(max_response_size=10))
        with pytest.raises(RemoteError):
            client.read_project_state("ep1")

    def test_no_credential_logging(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        client = RemoteHandoff(reg)
        client.read_project_state("ep1")
        for entry in client.operation_log():
            assert "sk-" not in entry.get("detail", "")
            assert "secret" not in entry.get("detail", "").lower()

    def test_no_api_key_in_registry_events(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        for event in reg.audit_trail():
            assert "api_key" not in event.get("detail", "").lower()

    def test_no_arbitrary_remote_command(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        client = RemoteHandoff(reg)
        with pytest.raises(RemoteError):
            client.execute("ep1", "run_arbitrary_command", {"cmd": "rm -rf /"})

    def test_no_unrestricted_fs_access(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        client = RemoteHandoff(reg)
        with pytest.raises(RemoteError):
            client.execute("ep1", "unrestricted_fs_operation", {"path": "~/*"})

    def test_no_unrestricted_git(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        client = RemoteHandoff(reg)
        with pytest.raises(RemoteError):
            client.execute("ep1", "unrestricted_git_operation", {"rev": "^.*"})

    def test_secret_filtering_on_log(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        client = RemoteHandoff(reg)
        client.execute(
            "ep1", "sync", {"local_state": {"password": "hunter2"}},
            human="alice", privileged_write=False,
        )
        repr_str = repr(client.operation_log())
        assert "hunter2" not in repr_str


class TestAuthentication:
    def test_credential_resolution(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("REMOTE_TOKEN", "tok-123")
        reg = _registry(tmp_path)
        reg.register(_endpoint(auth_method="bearer", credential_env="REMOTE_TOKEN"))
        client = RemoteHandoff(reg)
        resp = client.read_project_state("ep1")
        assert resp["ok"] is True

    def test_credential_isolation_no_env(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("REMOTE_TOKEN", raising=False)
        reg = _registry(tmp_path)
        reg.register(_endpoint(auth_method="bearer", credential_env="REMOTE_TOKEN"))
        client = RemoteHandoff(reg)
        with pytest.raises(AuthenticationError):
            client.read_project_state("ep1")

    def test_credential_rotation_support(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("REMOTE_TOKEN", "tok-old")
        reg = _registry(tmp_path)
        reg.register(_endpoint(auth_method="bearer", credential_env="REMOTE_TOKEN"))
        client = RemoteHandoff(reg)
        client.read_project_state("ep1")
        updated = client.rotate_credential("ep1", new_env="REMOTE_TOKEN_NEW")
        assert updated.credential_env == "REMOTE_TOKEN_NEW"

    def test_token_expiration_handling(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("REMOTE_TOKEN", "expired:xyz")
        reg = _registry(tmp_path)
        reg.register(_endpoint(auth_method="bearer", credential_env="REMOTE_TOKEN"))
        client = RemoteHandoff(reg)
        with pytest.raises(AuthenticationError):
            client.read_project_state("ep1")

    def test_auth_failure_marks_degraded(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("REMOTE_TOKEN", raising=False)
        reg = _registry(tmp_path)
        reg.register(_endpoint(auth_method="bearer", credential_env="REMOTE_TOKEN"))
        client = RemoteHandoff(reg)
        with pytest.raises(AuthenticationError):
            client.read_project_state("ep1")
        assert reg.get("ep1").status == EndpointStatus.DEGRADED.value


class TestHealth:
    def test_health_check_ok(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        report = RemoteHandoff(reg).check_health("ep1")
        assert report["ok"] is True
        assert report["status"] == EndpointStatus.HEALTHY.value

    def test_availability_check(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        client = RemoteHandoff(reg)
        client.check_health("ep1")
        avail = client.check_availability("ep1")
        assert avail["status"] == EndpointStatus.HEALTHY.value
        assert avail["last_health_check"] != ""

    def test_unavailable_endpoint_marked(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        class Down(RemoteTransport):
            def request(self, *a, **k):
                raise ConnectionError("down")
        client = RemoteHandoff(reg, transport=Down())
        report = client.check_health("ep1")
        assert report["ok"] is False
        assert report["status"] == EndpointStatus.UNAVAILABLE.value

    def test_health_report_endpoint_status(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        client = RemoteHandoff(reg)
        report = client.health_report()
        assert report["total"] == 1


class TestDiagnostics:
    def test_remote_diagnostics(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        client = RemoteHandoff(reg)
        client.read_project_state("ep1")
        diag = client.remote_diagnostics()
        assert diag["endpoints"] == 1
        assert diag["operations_logged"] == 1

    def test_operation_log(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        client = RemoteHandoff(reg)
        client.read_project_state("ep1")
        log = client.operation_log()
        assert len(log) >= 1
        assert log[0]["operation"] == RemoteOperation.READ_STATE.value
        assert log[0]["status"] == "ok"


class TestOfflineFallback:
    def test_offline_fallback_returns_local(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        client = RemoteHandoff(reg)
        fallback = client.offline_fallback(local_state={"k": "v"})
        assert fallback["offline_fallback"] is True
        assert fallback["local_state"]["k"] == "v"

    def test_remote_failure_recovery(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        client = RemoteHandoff(reg)
        result = client.remote_failure_recovery("ep1")
        assert "recovered" in result


class TestConflict:
    def test_no_conflict_when_identical(self, tmp_path: Path) -> None:
        cm = RemoteConflictManager(str(tmp_path / "conflicts"))
        conflict = cm.detect(
            endpoint_id="ep1", item_type="checkpoint", item_id="c1",
            local_version=1, remote_version=1,
            local_payload={"a": 1}, remote_payload={"a": 1},
        )
        assert conflict is None

    def test_conflict_detected(self, tmp_path: Path) -> None:
        cm = RemoteConflictManager(str(tmp_path / "conflicts"))
        conflict = cm.detect(
            endpoint_id="ep1", item_type="checkpoint", item_id="c1",
            local_version=2, remote_version=1,
            local_payload={"a": 1}, remote_payload={"a": 2},
        )
        assert conflict is not None
        assert conflict.conflict_id.startswith("cf-")
        assert conflict.item_id == "c1"

    def test_stale_checkpoint_detection(self, tmp_path: Path) -> None:
        cm = RemoteConflictManager(str(tmp_path / "conflicts"))
        assert cm.detect_stale_checkpoint(checkpoint_id="c1", local_version=3, remote_version=2) is True
        assert cm.detect_stale_checkpoint(checkpoint_id="c1", local_version=2, remote_version=3) is False

    def test_conflict_classification(self, tmp_path: Path) -> None:
        cm = RemoteConflictManager(str(tmp_path / "conflicts"))
        stale = cm.detect(
            endpoint_id="ep1", item_type="checkpoint", item_id="c1",
            local_version=3, remote_version=2,
            local_payload={"a": 1}, remote_payload={"a": 2},
        )
        assert cm.classify(stale) == "stale-remote"

    def test_conflict_metadata(self, tmp_path: Path) -> None:
        cm = RemoteConflictManager(str(tmp_path / "conflicts"))
        conflict = cm.detect(
            endpoint_id="ep1", item_type="checkpoint", item_id="c1",
            local_version=2, remote_version=1,
            local_payload={"a": 1}, remote_payload={"a": 2},
        )
        meta = conflict.to_dict()
        assert meta["local_version"] == "2"
        assert meta["remote_version"] == "1"
        assert meta["local_hash"] != meta["remote_hash"]

    def test_automatic_safe_merge(self, tmp_path: Path) -> None:
        cm = RemoteConflictManager(str(tmp_path / "conflicts"))
        conflict = cm.detect(
            endpoint_id="ep1", item_type="checkpoint", item_id="c1",
            local_version=2, remote_version=1,
            local_payload={"a": 1, "b": 2}, remote_payload={"a": 2, "c": 3},
        )
        merged = cm.resolve_automatic_safe_merge(
            conflict,
            local_payload={"a": 1, "b": 2}, remote_payload={"a": 2, "c": 3},
        )
        assert set(merged["merged"]) == {"a", "b", "c"}

    def test_manual_resolution_requires_human(self, tmp_path: Path) -> None:
        cm = RemoteConflictManager(str(tmp_path / "conflicts"))
        conflict = cm.detect(
            endpoint_id="ep1", item_type="checkpoint", item_id="c1",
            local_version=2, remote_version=1,
            local_payload={"a": 1}, remote_payload={"a": 2},
        )
        with pytest.raises(HumanApprovalRequiredError):
            cm.resolve_manual(conflict.conflict_id, chosen="local", human="")

    def test_manual_resolution(self, tmp_path: Path) -> None:
        cm = RemoteConflictManager(str(tmp_path / "conflicts"))
        conflict = cm.detect(
            endpoint_id="ep1", item_type="checkpoint", item_id="c1",
            local_version=2, remote_version=1,
            local_payload={"a": 1}, remote_payload={"a": 2},
        )
        resolved = cm.resolve_manual(conflict.conflict_id, chosen="remote", human="alice")
        assert resolved.resolution == "manual:remote:alice"
        assert "alice" in resolved.resolution

    def test_optimistic_concurrency_requires_approval(self, tmp_path: Path) -> None:
        cm = RemoteConflictManager(str(tmp_path / "conflicts"))
        conflict = cm.detect(
            endpoint_id="ep1", item_type="checkpoint", item_id="c1",
            local_version=2, remote_version=1,
            local_payload={"a": 1}, remote_payload={"a": 2},
        )
        assert cm.requires_human_approval(conflict) is True

    def test_conflicts_persist_and_reload(self, tmp_path: Path) -> None:
        cm = RemoteConflictManager(str(tmp_path / "conflicts"))
        cm.detect(
            endpoint_id="ep1", item_type="checkpoint", item_id="c1",
            local_version=2, remote_version=1,
            local_payload={"a": 1}, remote_payload={"a": 2},
        )
        cm2 = RemoteConflictManager(str(tmp_path / "conflicts"))
        conflicts = cm2.list_conflicts()
        assert len(conflicts) == 1


class TestRemotePropagation:
    def test_remote_state_sync(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        resp = RemoteHandoff(reg).sync_remote("ep1", local_state={"state": "ok"})
        assert resp["payload"]["local_state"]["state"] == "ok"

    def test_remote_changelog_sync(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        resp = RemoteHandoff(reg).execute(
            "ep1", "sync", {"changelog": {"v1": "note"}}
        )
        assert resp["ok"] is True

    def test_remote_event_propagation(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        resp = RemoteHandoff(reg).send_message("ep1", message={"type": "event"})
        assert resp["payload"]["message"]["type"] == "event"

    def test_remote_task_delegation(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        resp = RemoteHandoff(reg).execute(
            "ep1", "sync", {"task_delegation": {"task_id": "t1"}}
        )
        assert resp["ok"] is True

    def test_remote_task_result_propagation(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        resp = RemoteHandoff(reg).execute(
            "ep1", "sync", {"task_result": {"task_id": "t1", "status": "done"}}
        )
        assert resp["ok"] is True

    def test_remote_agent_routing(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        resp = RemoteHandoff(reg).execute(
            "ep1", "sync", {"agent_routing": {"agent_id": "agent-1"}}
        )
        assert resp["ok"] is True

    def test_remote_workflow_execution(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        resp = RemoteHandoff(reg).execute(
            "ep1", "sync", {"workflow_execution": {"workflow_id": "w1"}}
        )
        assert resp["ok"] is True


class TestInterruptedTransfer:
    def test_interrupted_transfer_recovery(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint())
        class Intermittent(RemoteTransport):
            def __init__(self):
                super().__init__()
                self.calls = 0
            def request(self, *a, **k):
                self.calls += 1
                if self.calls == 1:
                    raise ConnectionError("interrupted")
                return {"ok": True, "payload": {}, "protocol_version": "1",
                        "integrity_digest": k.get("integrity_digest", "")}
        client = RemoteHandoff(reg, transport=Intermittent())
        resp = client.read_project_state("ep1")
        assert resp["ok"] is True


class TestEndpointWriteAuthorization:
    def test_write_authorization_enforced(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint(allowed_projects=frozenset({"proj-a"})))
        client = RemoteHandoff(reg)
        with pytest.raises(RemoteError):
            client.write_checkpoint(
                "ep1", checkpoint={"a": 1}, project_id="proj-b"
            )

    def test_write_authorization_allowed(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        reg.register(_endpoint(allowed_projects=frozenset({"proj-a"})))
        resp = RemoteHandoff(reg).write_checkpoint(
            "ep1", checkpoint={"a": 1}, project_id="proj-a"
        )
        assert resp["ok"] is True