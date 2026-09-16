"""Phase 22 release audits.

Contract/introspection audits on top of the structural suites in
test_release.py (secrets, deps, git safety, install/uninstall). These pin the
release-time contracts: version mirroring, protocol, capability & adapter,
workflow state machine, error taxonomy, credential isolation, MCP/API/skill
security, persistence hygiene, and package integrity.
"""

from __future__ import annotations

import importlib
import io
import json
import pkgutil
import re
import threading
from pathlib import Path

import pytest

from handoff_agent import __version__, constants
from handoff_agent.adapters import create_adapter
from handoff_agent.adapters import api
from handoff_agent.adapters.api import _validate_url
from handoff_agent.adapters.base import (
    AdapterConfigError,
    AdapterError,
    AdapterUnsupportedError,
)
from handoff_agent.adapters.platforms import PLATFORM_SPECS
from handoff_agent.capability import (
    ALL_CAPABILITIES,
    CAP_CHECKPOINT_CREATE,
    CAP_CHECKPOINT_READ,
    CAP_CHECKPOINT_UPDATE,
    AgentIdentity,
    CapabilityDeniedError,
    PermissionBoundary,
    is_known_capability,
)
from handoff_agent.integration import run_integration_suite, resolve_credentials
from conftest import init_repo
from handoff_agent.mcp.adapter import HandoffMCPAdapter
from handoff_agent.mcp.server import MCPServer
from handoff_agent.persistence import HandoffPersistenceError, _atomic_write_file
from handoff_agent.protocol import (
    PROTOCOL_BLOCK_KEY,
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    PROTOCOL_VERSIONS_SUPPORTED,
    build_checkpoint,
    checkpoint_identity,
    parse_handoff_document,
    render_state_block,
    validate_checkpoint_data,
    verify_identity,
)
from handoff_agent.providers.base import ProviderError
from handoff_agent.providers.claude import ClaudeProvider
from handoff_agent.providers.factory import UnknownProviderError, create_provider
from handoff_agent.skill import validate_skill
from handoff_agent.workflow import TERMINAL_STATES, WorkflowManager, WorkflowState, _MACHINE

RELEASE_VERSION = "0.4.0"
REPO_ROOT = Path(__file__).resolve().parents[1]
SECRET_LIKE = "sk-thisisasecretvalue-0123456789abcdef"


# ---------------------------------------------------------------------------
# Version mirroring
# ---------------------------------------------------------------------------


class TestVersionMirrorAudit:
    def test_package_and_constants(self) -> None:
        assert __version__ == RELEASE_VERSION
        assert constants.VERSION == RELEASE_VERSION

    def test_mcp_handshake_version(self) -> None:
        import handoff_agent.mcp.server as mcp_server

        src = io.StringIO(importlib.import_module("inspect").getsource(mcp_server))
        assert re.search(r'"version":\s*"' + re.escape(RELEASE_VERSION) + r'"', src.getvalue())

    def test_adapter_versions_are_release(self) -> None:
        instances = [
            create_adapter("file"),
            create_adapter("cli"),
            create_adapter("api", base_url="https://127.0.0.1:8000/handoff"),
        ]
        for adapter in instances:
            assert adapter.adapter_version == RELEASE_VERSION

    def test_installer_readme_mcp_docs_version(self) -> None:
        install = (REPO_ROOT / "install.sh").read_text()
        readme = (REPO_ROOT / "README.md").read_text()
        mcp_doc = (REPO_ROOT / "docs" / "MCP.md").read_text()
        assert f'"version": "{RELEASE_VERSION}"' in install
        assert f'"version": "{RELEASE_VERSION}"' in readme
        assert f'"serverInfo":{{"name":"handoff-mcp-server","version":"{RELEASE_VERSION}"}}' in mcp_doc


# ---------------------------------------------------------------------------
# Protocol audit
# ---------------------------------------------------------------------------


class TestProtocolAudit:
    def test_protocol_constants_final(self) -> None:
        assert PROTOCOL_NAME == "universal-handoff-protocol"
        assert PROTOCOL_VERSION == 1
        assert PROTOCOL_VERSIONS_SUPPORTED == (1,)
        assert PROTOCOL_BLOCK_KEY == "handoff-protocol"

    def test_build_validate_identity_round_trip(self) -> None:
        cp = build_checkpoint(
            objective="audit objective",
            completed=("done",),
            in_progress=("next",),
            next_actions=("run",),
            decisions=("decision",),
            constraints=("none",),
            project_name="proj",
            agents=("alice", "bob"),
            validation_status="passed",
            validation_checks=("schema", "identity"),
            sequence=3,
        )
        assert validate_checkpoint_data(cp.to_dict()) == []
        assert verify_identity(cp) is True
        parsed = parse_handoff_document(render_state_block(cp))
        assert parsed is not None
        assert parsed.identity.id == cp.identity.id
        assert parsed.state.objective == cp.state.objective

    def test_identity_is_timestamp_independent(self) -> None:
        cp1 = build_checkpoint(objective="x", project_name="p", agents=("a",), sequence=1)
        cp1.metadata["generated"] = "t-1"
        cp2 = build_checkpoint(objective="x", project_name="p", agents=("a",), sequence=1)
        cp2.metadata["generated"] = "t-2"
        assert checkpoint_identity(cp1.state, cp1.metadata) == checkpoint_identity(
            cp2.state, cp2.metadata
        )

    def test_prior_versions_are_protocol_v1(self) -> None:
        cp = build_checkpoint(
            objective="v0.2/v0.3 style",
            completed=(),
            in_progress=(),
            next_actions=(),
            decisions=(),
            constraints=(),
            project_name="legacy",
            agents=("carol",),
            sequence=1,
        )
        parsed = parse_handoff_document(render_state_block(cp))
        assert parsed.state.objective == "v0.2/v0.3 style"

    def test_legacy_no_block_readable_null(self) -> None:
        assert parse_handoff_document("# Handoff\n\nplain markdown") is None


# ---------------------------------------------------------------------------
# Capability & adapter contract audit
# ---------------------------------------------------------------------------


class TestCapabilityAudit:
    def test_capability_catalog_stable(self) -> None:
        expected = {
            CAP_CHECKPOINT_READ,
            CAP_CHECKPOINT_CREATE,
            CAP_CHECKPOINT_UPDATE,
            "changelog.read",
            "validation",
            "project.inspection",
            "git.inspection",
        }
        assert expected <= ALL_CAPABILITIES
        assert all(is_known_capability(c) for c in ALL_CAPABILITIES)

    def test_platform_grants_are_known_and_consistent(self) -> None:
        for name, spec in PLATFORM_SPECS.items():
            for cap in spec.capability_grant:
                assert cap in ALL_CAPABILITIES, f"{name} grants unknown {cap}"
            assert (CAP_CHECKPOINT_CREATE in spec.capability_grant) is not spec.read_only

    def test_permission_boundary_enforces(self) -> None:
        boundary = PermissionBoundary(frozenset({CAP_CHECKPOINT_READ}), name="readonly")
        boundary.check(CAP_CHECKPOINT_READ)
        with pytest.raises(CapabilityDeniedError):
            boundary.check(CAP_CHECKPOINT_CREATE)

    def test_no_fallback_anywhere(self) -> None:
        with pytest.raises(AdapterConfigError):
            create_adapter("does-not-exist-anywhere")
        with pytest.raises(UnknownProviderError):
            create_provider("does-not-exist-anywhere", {})


# ---------------------------------------------------------------------------
# Workflow state machine audit
# ---------------------------------------------------------------------------


class TestWorkflowContractAudit:
    def test_machine_is_exactly_the_final_contract(self) -> None:
        assert set(WorkflowState) == {
            WorkflowState.IDLE,
            WorkflowState.WORKING,
            WorkflowState.CHECKPOINTED,
            WorkflowState.HANDOFF_REQUESTED,
            WorkflowState.HANDOFF_ACCEPTED,
            WorkflowState.COMPLETED,
            WorkflowState.ABANDONED,
            WorkflowState.FAILED,
        }
        expected = {
            WorkflowState.IDLE: {WorkflowState.WORKING, WorkflowState.ABANDONED},
            WorkflowState.WORKING: {
                WorkflowState.CHECKPOINTED,
                WorkflowState.ABANDONED,
                WorkflowState.FAILED,
            },
            WorkflowState.CHECKPOINTED: {
                WorkflowState.WORKING,
                WorkflowState.HANDOFF_REQUESTED,
                WorkflowState.COMPLETED,
                WorkflowState.ABANDONED,
                WorkflowState.FAILED,
            },
            WorkflowState.HANDOFF_REQUESTED: {
                WorkflowState.HANDOFF_ACCEPTED,
                WorkflowState.ABANDONED,
                WorkflowState.FAILED,
                WorkflowState.WORKING,
            },
            WorkflowState.HANDOFF_ACCEPTED: {
                WorkflowState.COMPLETED,
                WorkflowState.WORKING,
                WorkflowState.ABANDONED,
                WorkflowState.FAILED,
            },
            WorkflowState.COMPLETED: set(),
            WorkflowState.ABANDONED: {WorkflowState.WORKING},
            WorkflowState.FAILED: {WorkflowState.WORKING},
        }
        assert {k: set(v) for k, v in _MACHINE.items()} == expected
        assert set(TERMINAL_STATES) == {
            WorkflowState.COMPLETED,
            WorkflowState.ABANDONED,
            WorkflowState.FAILED,
        }

    def test_concurrent_checkpoints_never_corrupt_audit(self) -> None:
        mgr = WorkflowManager()
        alice = AgentIdentity(name="alice", version="1.0", kind="claude")
        bob = AgentIdentity(name="bob", version="1.0", kind="opencode")
        record = mgr.begin(alice, bob, "/tmp/audit-concurrency-project")
        producer = record.producer
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def worker(i: int) -> None:
            try:
                barrier.wait(timeout=10)
                mgr.checkpoint(
                    record,
                    objective=f"objective-{i}",
                    completed=(f"step-{i}",),
                    in_progress=(),
                    next_actions=(),
                    decisions=(),
                    constraints=(),
                    actor=producer,
                )
            except BaseException as exc:  # pragma: no cover - defensive
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == [], [str(e) for e in errors]
        assert mgr.export_audit(record), "audit must never be empty"
        assert mgr.get(record.workflow_id).current_identity


# ---------------------------------------------------------------------------
# Error taxonomy & safe errors
# ---------------------------------------------------------------------------


class TestErrorTaxonomyAudit:
    def test_hierarchy_importable_and_derived(self) -> None:
        from handoff_agent.adapters.base import AdapterError
        from handoff_agent.capability import CapabilityError
        from handoff_agent.persistence import HandoffPersistenceError
        from handoff_agent.protocol import ProtocolError
        from handoff_agent.providers.base import ProviderError
        from handoff_agent.workflow import WorkflowError

        for exc in (
            AdapterError,
            CapabilityError,
            HandoffPersistenceError,
            ProtocolError,
            ProviderError,
            WorkflowError,
        ):
            assert issubclass(exc, Exception)

    def test_missing_key_error_names_env_only(self, monkeypatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        provider = ClaudeProvider(config={"api_key_env": "ANTHROPIC_API_KEY", "model": ""})
        with pytest.raises(ProviderError) as exc_info:
            provider.generate({}, "prompt")
        text = str(exc_info.value)
        assert "ANTHROPIC_API_KEY" in text
        assert SECRET_LIKE not in text

    def test_integration_suite_never_serializes_secret_values(self, tmp_path: Path) -> None:
        env = {"ANTHROPIC_API_KEY": SECRET_LIKE, "OPENAI_API_KEY": SECRET_LIKE}
        cred = resolve_credentials(next(s for s in PLATFORM_SPECS.values() if s.auth_env == "ANTHROPIC_API_KEY"), env)
        assert cred.env_var == "ANTHROPIC_API_KEY"
        assert cred.present is True
        repo = tmp_path / "repo"
        init_repo(repo)
        report = run_integration_suite(
            mode="mock", env=env, project_root=str(repo)
        )
        blob = json.dumps(report)
        assert SECRET_LIKE not in blob
        assert "sk-" not in blob
        assert report["clean"] is True


# ---------------------------------------------------------------------------
# MCP, API, skill security audits
# ---------------------------------------------------------------------------


class TestMCPAudit:
    def test_read_only_maps_to_mcp_capability_error(self) -> None:
        server = MCPServer(HandoffMCPAdapter(project_root=str(REPO_ROOT), read_only=True))
        response = json.loads(
            server.handle_line(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "create_checkpoint", "arguments": {}},
                    }
                )
            )
        )
        assert response["error"]["code"] == -32001

    def test_uri_escape_refused(self) -> None:
        server = MCPServer(HandoffMCPAdapter(project_root=str(REPO_ROOT)))
        response = json.loads(
            server.handle_line(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "resources/read",
                        "params": {"uri": "handoff://file/../escape"},
                    }
                )
            )
        )
        assert "error" in response


class TestAPITransportAudit:
    def test_plain_http_refused_outside_loopback(self) -> None:
        with pytest.raises(AdapterConfigError):
            _validate_url("http://example.com/handoff")

    def test_loopback_http_allowed(self) -> None:
        assert _validate_url("http://127.0.0.1:8000/handoff") is not None

    def test_size_limit_enforced(self) -> None:
        assert api._MAX_RESPONSE_BYTES == 8 * 1024 * 1024

    def test_bad_scheme_refused(self) -> None:
        with pytest.raises(AdapterConfigError):
            _validate_url("ftp://example.com/handoff")


class TestSkillSecurityAudit:
    def test_skill_package_validates(self) -> None:
        report = validate_skill(REPO_ROOT / "skills" / "universal-handoff")
        assert report.ok is True, [i.to_dict() for i in report.issues]

    def test_skill_payload_has_no_secrets_or_shell(self) -> None:
        text = (REPO_ROOT / "skills" / "universal-handoff" / "SKILL.md").read_text()
        assert "curl" not in text.lower()
        assert not re.search(r"sk-[A-Za-z0-9]{16,}", text)
        assert not re.search(r"(?:api[_-]?key|token|secret)[^\\n]*[:=]\\s*\\S+", text, flags=re.IGNORECASE)


# ---------------------------------------------------------------------------
# Persistence hygiene audits
# ---------------------------------------------------------------------------


class TestPersistenceHygieneAudit:
    def test_failed_atomic_write_leaves_previous_checkpoint(self, tmp_path: Path) -> None:
        target = tmp_path / "docs" / "HANDOFF.md"
        target.parent.mkdir(parents=True)
        target.write_text("previous checkpoint")
        with pytest.raises((TypeError, HandoffPersistenceError, AttributeError)):
            _atomic_write_file(target, None)  # type: ignore[arg-type]
        assert target.read_text() == "previous checkpoint"
        assert not list(REPO_ROOT.rglob("*.tmp"))

    def test_successful_atomic_write_replaces(self, tmp_path: Path) -> None:
        target = tmp_path / "docs" / "CHANGELOG.md"
        target.parent.mkdir(parents=True)
        _atomic_write_file(target, "new checkpoint")
        assert target.read_text() == "new checkpoint"


# ---------------------------------------------------------------------------
# Package integrity audit
# ---------------------------------------------------------------------------


class TestPackageIntegrityAudit:
    def test_every_module_imports(self) -> None:
        import handoff_agent

        imported: list[str] = []
        for mod in pkgutil.walk_packages(handoff_agent.__path__, "handoff_agent."):
            importlib.import_module(mod.name)
            imported.append(mod.name)
        assert "handoff_agent.workflow" in imported
        assert "handoff_agent.integration" in imported

    def test_no_stray_runtime_artifacts_in_repo(self) -> None:
        for pattern in ("*.tmp", "*.log"):
            assert not list(REPO_ROOT.rglob(pattern)), f"stray {pattern} in repo"