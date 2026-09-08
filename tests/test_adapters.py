"""Tests for Phase 15 — Generic Adapter & Interoperability Layer.

Covers the universal adapter interface, adapter registry/discovery/lifecycle,
capability negotiation and permission enforcement, adapter isolation, the
file-based adapter (read/write/validate/project-state/changelog + safety),
the CLI adapter, the API adapter, and safe error handling.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from conftest import commit_all, init_repo, write_file

from handoff_agent.capability import (
    CAP_CHECKPOINT_UPDATE,
    PermissionBoundary,
    build_contract,
)
from handoff_agent.adapters import (
    AdapterConfigError,
    AdapterError,
    AdapterPermissionError,
    AdapterUnsupportedError,
    create_adapter,
    discover_adapters,
    is_registered,
    list_adapters,
    map_agent_identity,
    register_adapter,
    unregister_adapter,
)
from handoff_agent.adapters.base import (
    AdapterStateError,
    BaseAdapter,
    resolve_within_root,
)
from handoff_agent.adapters.cli import CliAdapter
from handoff_agent.adapters.filesystem import FileAdapter
from handoff_agent.adapters.api import ApiAdapter
from handoff_agent.protocol import (
    build_checkpoint,
    parse_handoff_document,
    render_state_block,
)


def checkpoint_markdown(objective: str = "Objective", project: str = "repo", **state) -> str:
    cp = build_checkpoint(
        objective=objective,
        completed=state.get("completed", ["a"]),
        in_progress=state.get("in_progress", []),
        next_actions=state.get("next_actions", ["b"]),
        project_name=project,
    )
    return f"# Project: {project}\n\nObjective: {objective}\n\n" + render_state_block(cp)


class TestRegistry:
    def test_builtins_registered(self) -> None:
        assert {"file", "cli", "api"} <= set(list_adapters())

    def test_discovery_is_deterministic(self) -> None:
        d1 = discover_adapters()
        d2 = discover_adapters()
        assert d1 == d2
        assert set(d1) == {"api", "cli", "file"}

    def test_register_and_create_custom(self) -> None:
        class FakeAdapter(BaseAdapter):
            adapter_name = "fake"
            adapter_version = "9.9.9"

            def _do_read_handoff(self) -> str | None: return None
            def _do_write_checkpoint(self, content: str):
                from handoff_agent.adapters import AdapterWriteResult
                return AdapterWriteResult(rel_path="docs/HANDOFF.md", created=True)
            def _do_project_state(self) -> dict: return {"fake": True}
            def _do_validate_checkpoint(self) -> dict: return {"valid": True, "errors": []}
            def _do_read_changelog(self) -> str | None: return None

        register_adapter("fake", FakeAdapter)
        try:
            assert is_registered("fake")
            adapter = create_adapter("fake")
            assert adapter.adapter_name == "fake"
        finally:
            unregister_adapter("fake")
        assert not is_registered("fake")

    def test_duplicate_registration_rejected(self) -> None:
        from handoff_agent.adapters import FileAdapter as FA
        with pytest.raises(AdapterConfigError):
            register_adapter("file", FA)

    def test_unknown_adapter_no_fallback(self) -> None:
        with pytest.raises(AdapterConfigError, match="Unknown adapter"):
            create_adapter("does-not-exist")


class TestBaseIdentityAndNegotiation:
    def test_identity_mapping_deterministic(self) -> None:
        a = map_agent_identity("claude", version="1.0.0", kind="ai")
        b = map_agent_identity("claude", version="1.0.0", kind="ai")
        assert a == b

    def test_negotiation_grants_all_for_full_contract(self, tmp_path: Path) -> None:
        repo = tmp_path / "r"
        init_repo(repo)
        ad = FileAdapter(project_root=str(repo))
        result = ad.negotiate(
            {
                "checkpoint.read",
                "checkpoint.create",
                "checkpoint.update",
                "validation",
                "project.inspection",
                "changelog.read",
            }
        )
        assert result.ok
        assert not result.unknown

    def test_negotiation_flags_unknown(self, tmp_path: Path) -> None:
        repo = tmp_path / "r"
        init_repo(repo)
        ad = FileAdapter(project_root=str(repo))
        result = ad.negotiate({"bogus.cap"})
        assert not result.ok
        assert "bogus.cap" in result.unknown

    def test_status_never_contains_secrets(self, tmp_path: Path) -> None:
        repo = tmp_path / "r"
        init_repo(repo)
        ad = FileAdapter(project_root=str(repo), config={"note": "x"})
        status = json.dumps(ad.status())
        assert "sk-" not in status
        assert "apikey" not in status.lower()


class TestLifecycle:
    def test_requires_started(self, tmp_path: Path) -> None:
        repo = tmp_path / "r"
        init_repo(repo)
        ad = FileAdapter(project_root=str(repo))
        with pytest.raises(AdapterStateError):
            ad.read_handoff()

    def test_started_status(self, tmp_path: Path) -> None:
        repo = tmp_path / "r"
        init_repo(repo)
        ad = FileAdapter(project_root=str(repo))
        assert ad.started is False
        ad.start()
        assert ad.started is True
        assert ad.status()["started"] is True
        ad.stop()
        assert ad.started is False

    def test_lifecycle_idempotent(self, tmp_path: Path) -> None:
        repo = tmp_path / "r"
        init_repo(repo)
        ad = FileAdapter(project_root=str(repo))
        ad.start()
        ad.start()
        ad.stop()
        ad.stop()
        assert ad.started is False


class TestFileAdapter:
    def test_write_then_read_roundtrip(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        write_file(repo, "app.py", "pass")
        commit_all(repo, "init")
        ad = FileAdapter(project_root=str(repo))
        ad.start()
        body = checkpoint_markdown(project="repo")
        result = ad.write_checkpoint(body)
        assert result.created is True
        assert result.modified is False
        assert result.identity
        assert (repo / "docs" / "HANDOFF.md").is_file()
        content = ad.read_handoff()
        assert content is not None
        assert "handoff-protocol" in content

    def test_update_archives_to_changelog(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        write_file(repo, "app.py", "pass")
        commit_all(repo, "init")
        ad = FileAdapter(project_root=str(repo))
        ad.start()
        ad.write_checkpoint(checkpoint_markdown(objective="one", project="repo"))
        result = ad.write_checkpoint(checkpoint_markdown(objective="two", project="repo"))
        assert result.modified is True
        assert result.history_recorded is True
        changelog = ad.read_changelog()
        assert changelog is not None
        assert "Checkpoint" in changelog
        # The changelog records the PREVIOUS checkpoint content.
        assert "one" in changelog

    def test_unchanged_write_detected(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        ad = FileAdapter(project_root=str(repo))
        ad.start()
        body = checkpoint_markdown(objective="x", project="repo")
        ad.write_checkpoint(body)
        result = ad.write_checkpoint(body)
        assert result.unchanged is True

    def test_validate_checkpoint(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        ad = FileAdapter(project_root=str(repo))
        ad.start()
        ad.write_checkpoint(checkpoint_markdown(project="repo"))
        report = ad.validate_checkpoint()
        assert report["valid"] is True
        assert report["identity_verified"] is True
        assert report["protocol"]["name"] == "universal-handoff-protocol"

    def test_validate_missing_checkpoint(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        ad = FileAdapter(project_root=str(repo))
        ad.start()
        report = ad.validate_checkpoint()
        assert report["valid"] is False

    def test_project_state(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        write_file(repo, "package.json", "{}")
        commit_all(repo, "init")
        ad = FileAdapter(project_root=str(repo))
        ad.start()
        state = ad.project_state()
        assert state["protocol"]["name"] == "universal-handoff-protocol"
        assert state["git"]["branch"] == "main"
        assert state["project"]["root"] == str(repo.resolve())

    def test_read_changelog_missing(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        ad = FileAdapter(project_root=str(repo))
        ad.start()
        assert ad.read_changelog() is None


class TestCapabilityEnforcement:
    def test_read_only_write_denied(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        ad = FileAdapter(project_root=str(repo), read_only=True)
        ad.start()
        with pytest.raises(AdapterPermissionError):
            ad.write_checkpoint(checkpoint_markdown(project="repo"))

    def test_read_only_read_allowed(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        ad = FileAdapter(project_root=str(repo))
        ad.start()
        ad.write_checkpoint(checkpoint_markdown(project="repo"))
        ro = FileAdapter(project_root=str(repo), read_only=True)
        ro.start()
        assert ro.read_handoff() is not None
        with pytest.raises(AdapterPermissionError):
            ro.write_checkpoint("x")
        with pytest.raises(AdapterPermissionError):
            ro.assert_can("checkpoint.create")

    def test_write_idempotent_contract_update_only(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        # Pre-seed an existing checkpoint so an update-only agent may write.
        ad = FileAdapter(project_root=str(repo))
        ad.start()
        ad.write_checkpoint(checkpoint_markdown(objective="base", project="repo"))
        # Contract that may ONLY update (not create).
        contract = build_contract(
            map_agent_identity("updater"),
            {CAP_CHECKPOINT_UPDATE},
            boundary=PermissionBoundary(allowed=frozenset({CAP_CHECKPOINT_UPDATE})),
        )
        updater = FileAdapter(project_root=str(repo), contract=contract)
        updater.start()
        result = updater.write_checkpoint(
            checkpoint_markdown(objective="updated", project="repo")
        )
        assert result.modified is True

    def test_write_create_not_granted_when_only_reader(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        contract = build_contract(
            map_agent_identity("reader"),
            {"checkpoint.read"},
            boundary=PermissionBoundary(allowed=frozenset({"checkpoint.read"})),
        )
        ad = FileAdapter(project_root=str(repo), contract=contract)
        ad.start()
        with pytest.raises(AdapterPermissionError):
            ad.write_checkpoint(checkpoint_markdown(project="repo"))

    def test_unsupported_capability_safe(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        ad = FileAdapter(project_root=str(repo))
        # Negotiating an unknown capability is reported, not raised.
        result = ad.negotiate({"not.a.capability"})
        assert "not.a.capability" in result.unknown
        assert result.ok is False


class TestAdapterIsolation:
    def test_two_adapters_do_not_share_contracts(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        a = FileAdapter(project_root=str(repo), agent=map_agent_identity("alpha"))
        b = FileAdapter(project_root=str(repo), agent=map_agent_identity("beta"))
        assert a.contract.agent != b.contract.agent
        a.negotiate({"checkpoint.create"})
        assert b.contract.granted  # unchanged

    def test_failure_does_not_poison_other_adapter(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        ro = FileAdapter(project_root=str(repo), read_only=True)
        ro.start()
        with pytest.raises(AdapterPermissionError):
            ro.write_checkpoint("x")
        rw = FileAdapter(project_root=str(repo))
        rw.start()
        assert rw.write_checkpoint(checkpoint_markdown(project="repo")).created is True


class TestContainment:
    def test_resolve_rejects_traversal(self, tmp_path: Path) -> None:
        root = tmp_path / "r"
        root.mkdir()
        with pytest.raises(AdapterError):
            resolve_within_root(root, "../escape")

    def test_resolve_rejects_absolute(self, tmp_path: Path) -> None:
        root = tmp_path / "r"
        root.mkdir()
        with pytest.raises(AdapterError):
            resolve_within_root(root, str(tmp_path / "x"))

    def test_resolve_rejects_symlink_escape(self, tmp_path: Path) -> None:
        root = tmp_path / "r"
        root.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        (external / "secret.txt").write_text("s")
        (root / "docs").symlink_to(external, target_is_directory=True)
        with pytest.raises(AdapterError):
            resolve_within_root(root, "docs/HANDOFF.md")

    def test_project_state_contained(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        (repo / "docs").mkdir()
        ad = FileAdapter(project_root=str(repo))
        ad.start()
        state = ad.project_state()
        assert state["project"]["root"].startswith(str(repo.resolve()))


class TestSecretFiltering:
    def test_write_secret_refused(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        ad = FileAdapter(project_root=str(repo))
        ad.start()
        with pytest.raises(AdapterError):
            ad.write_checkpoint('api_key = "sk-supersecretvalue1234"')

    def test_read_secret_refused(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        write_file(repo, "docs/HANDOFF.md", 'access_token = "thisisasecrettokenvalue1"')
        commit_all(repo, "init")
        ad = FileAdapter(project_root=str(repo))
        ad.start()
        with pytest.raises(AdapterError):
            ad.read_handoff()


class TestConcurrentUpdates:
    def test_expected_base_guards_update(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        a = FileAdapter(project_root=str(repo), agent=map_agent_identity("alpha"))
        a.start()
        first = a.write_checkpoint(checkpoint_markdown(objective="v1", project="repo"))
        # A second agent changes the checkpoint.
        b = FileAdapter(project_root=str(repo), agent=map_agent_identity("beta"))
        b.start()
        b.write_checkpoint(checkpoint_markdown(objective="v2", project="repo"))
        # Alpha tries to write based on its stale observation -> refused.
        with pytest.raises(AdapterPermissionError, match="Checkpoint changed"):
            a.write_checkpoint(
                checkpoint_markdown(objective="v3", project="repo"),
                expected_base=first.identity,
            )

    def test_expected_base_matching_succeeds(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        a = FileAdapter(project_root=str(repo))
        a.start()
        first = a.write_checkpoint(checkpoint_markdown(objective="v1", project="repo"))
        result = a.write_checkpoint(
            checkpoint_markdown(objective="v2", project="repo"),
            expected_base=first.identity,
        )
        assert result.modified is True


class TestCliAdapter:
    def test_project_state_invokes_inspect(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        write_file(repo, "package.json", "{}")
        commit_all(repo, "init")
        ad = CliAdapter(project_root=str(repo))
        ad.start()
        state = ad.project_state()
        assert state["safe"] is True
        assert state["returncode"] == 0
        assert "Node.js" in state["stdout"]

    def test_whitelist_enforced(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        ad = CliAdapter(project_root=str(repo))
        ad.start()
        with pytest.raises(AdapterError):
            ad._run(["disallowed-cmd", "--force"])

    def test_unsupported_operations_safe(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        ad = CliAdapter(project_root=str(repo))
        ad.start()
        with pytest.raises(AdapterUnsupportedError):
            ad.read_handoff()
        with pytest.raises(AdapterUnsupportedError):
            ad._do_write_checkpoint("x")
        with pytest.raises(AdapterUnsupportedError):
            ad.validate_checkpoint()
        with pytest.raises(AdapterUnsupportedError):
            ad.read_changelog()

    def test_no_shell_used(self, tmp_path: Path) -> None:
        import inspect
        from handoff_agent.adapters import cli as cli_module
        source = inspect.getsource(cli_module)
        assert 'shell=True' not in source
        assert 'os.system' not in source


class TestApiAdapter:
    def test_config_validation(self, tmp_path: Path) -> None:
        with pytest.raises(AdapterConfigError):
            ApiAdapter(base_url="ftp://example.com")
        with pytest.raises(AdapterConfigError):
            ApiAdapter(base_url="http://example.com")
        with pytest.raises(AdapterConfigError):
            ApiAdapter(base_url="https://example.com/api?x=1")

    def test_requires_base_url(self) -> None:
        with pytest.raises(AdapterConfigError):
            ApiAdapter()

    def test_roundtrip_over_loopback(self, tmp_path: Path, monkeypatch) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        body = checkpoint_markdown(objective="api", project="repo")

        state = {"content": body, "latest": None}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _send(self, payload: dict) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                if self.path.startswith("/handoff"):
                    self._send({"content": state["content"]})
                elif self.path.startswith("/project-state"):
                    self._send({"project": {"name": "api-repo"}, "protocol": {"name": "universal-handoff-protocol", "version": 1}})
                elif self.path.startswith("/validate"):
                    self._send({"valid": True, "errors": [], "protocol": {"name": "universal-handoff-protocol", "version": 1}})
                elif self.path.startswith("/changelog"):
                    self._send({"content": "# Changelog"})
                else:
                    self._send({})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                payload = json.loads(raw.decode("utf-8"))
                state["latest"] = payload.get("content")
                self._send(
                    {
                        "rel_path": "docs/HANDOFF.md",
                        "created": True,
                        "modified": False,
                        "unchanged": False,
                        "history_recorded": False,
                        "identity": "abc" * 20 + "def" * 4,
                    }
                )

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            port = server.server_address[1]
            ad = ApiAdapter(base_url=f"http://127.0.0.1:{port}", auth_env="FAKE_HANDOFF_API_KEY")
            ad.start()
            monkeypatch.setenv("FAKE_HANDOFF_API_KEY", "fake-key-not-a-secret-pattern")
            assert ad.read_handoff() == body
            written = ad._do_write_checkpoint("new body")
            assert written.rel_path == "docs/HANDOFF.md"
            assert state["latest"] == "new body"
            assert ad.project_state()["project"]["name"] == "api-repo"
            assert ad.validate_checkpoint()["valid"] is True
            assert "Changelog" in (ad.read_changelog() or "")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_connection_refused_safe(self, tmp_path: Path) -> None:
        ad = ApiAdapter(base_url="http://127.0.0.1:1")
        ad.start()
        with pytest.raises(AdapterError):
            ad.read_handoff()

    def test_auth_only_from_env(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        sent_auth = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:
                sent_auth.append(self.headers.get("Authorization"))
                raw = b'{"content": "x"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            port = server.server_address[1]
            ad = ApiAdapter(
                base_url=f"http://127.0.0.1:{port}",
                auth_env="MY_TEST_AUTH_TOKEN_ENV",
            )
            ad.start()
            ad.read_handoff()
            assert sent_auth and sent_auth[0] is None
            import os
            os.environ["MY_TEST_AUTH_TOKEN_ENV"] = "t0k3n-v4lue-abcdef"
            try:
                ad.read_handoff()
                assert sent_auth[1] == "Bearer t0k3n-v4lue-abcdef"
            finally:
                os.environ.pop("MY_TEST_AUTH_TOKEN_ENV", None)
            # status must not contain the key value
            assert "t0k3n" not in json.dumps(ad.status())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class TestCrossAdapterCompatibility:
    def test_file_and_api_share_identity(self, tmp_path: Path) -> None:
        """A checkpoint identity is adapter-independent (deterministic)."""
        repo = tmp_path / "repo"
        init_repo(repo)
        write_file(repo, "app.py", "pass")
        commit_all(repo, "init")

        cp = build_checkpoint(
            objective="cross",
            completed=["a"],
            next_actions=["b"],
            project_name="repo",
        )
        identity = cp.identity.id

        ad = FileAdapter(project_root=str(repo))
        ad.start()
        result = ad.write_checkpoint("# Cross\n\n" + render_state_block(cp))
        assert result.identity == identity
        parsed = parse_handoff_document(ad.read_handoff() or "")
        assert parsed is not None
        assert parsed.identity.id == identity