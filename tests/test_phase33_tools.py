"""Phase 33 — Tool registry, sandbox boundary, and conformance suite tests."""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

import pytest

from handoff_agent.policy_engine import (
    ActionRequest,
    Effect,
    PolicyDenied,
    PolicyEngine,
)
from handoff_agent.sandbox import (
    Sandbox,
    SandboxCommandError,
    SandboxContentError,
    SandboxEnvironmentError,
    SandboxLimits,
    SandboxNetworkError,
    SandboxPathError,
    SandboxResourceError,
    SandboxSecretError,
)
from handoff_agent.tool_registry import (
    AdapterCapabilityDeclaration,
    ConformanceCheck,
    DEFAULT_ENV_ALLOWLIST,
    ToolCategory,
    ToolConformanceReport,
    ToolError,
    ToolGateway,
    ToolInputError,
    ToolNotFoundError,
    ToolOutputError,
    ToolPermissionError,
    ToolRequest,
    ToolResult,
    ToolSetupError,
    ToolSandboxError,
    ToolSpec,
    ToolSecretError,
    provision_phase33_gateway,
    register_default_tools,
    run_tool_conformance,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _engine_with(agent_1: bool = True) -> PolicyEngine:
    engine = PolicyEngine()
    if agent_1:
        engine.register_subject("agent-1", "agent", trust_level=3,
                               capabilities=["tool_use", "filesystem_use",
                                             "network_use", "git_use", "secret_use"])
    engine.add_rule("tool.*", Effect.ALLOW, scope="*")
    engine.allow_git("git.status")
    engine.allow_network("*.example.com")
    engine.grant_secret("agent-1")
    engine.require_approval_for_destructive("destructive.*")
    return engine


def _sandbox(tmp: Path) -> Sandbox:
    return Sandbox(
        tmp,
        command_allowlist=[sys.executable],
        network_allowlist=["*.example.com"],
        limits=SandboxLimits(max_output_bytes=4096, max_read_bytes=2048,
                             max_write_bytes=2048, default_timeout_seconds=2.0),
    )


# ---------------------------------------------------------------------------
# Sandbox tests
# ---------------------------------------------------------------------------

class TestSandboxContainment:

    def test_rejects_absolute_path(self, tmp_path: Path) -> None:
        sb = _sandbox(tmp_path)
        with pytest.raises(SandboxPathError, match="absolute"):
            sb.contain("/etc/passwd")

    def test_rejects_traversal(self, tmp_path: Path) -> None:
        sb = _sandbox(tmp_path)
        with pytest.raises(SandboxPathError, match="traversal"):
            sb.contain("../../etc/passwd")

    def test_rejects_symlink_escape(self, tmp_path: Path) -> None:
        sb = _sandbox(tmp_path)
        link = tmp_path / "escape"
        link.symlink_to(tmp_path.parent)
        with pytest.raises(SandboxPathError, match="escape"):
            sb.contain("escape/x")

    def test_relative_path_accepted(self, tmp_path: Path) -> None:
        sb = _sandbox(tmp_path)
        resolved = sb.contain("data/file.txt")
        assert str(resolved).startswith(str(tmp_path))


class TestSandboxFilesystem:

    def test_write_read_cycle(self, tmp_path: Path) -> None:
        sb = _sandbox(tmp_path)
        sb.write_file("hello.txt", "hello world")
        assert sb.read_file("hello.txt") == "hello world"

    def test_read_missing_file(self, tmp_path: Path) -> None:
        sb = _sandbox(tmp_path)
        with pytest.raises(SandboxPathError):
            sb.read_file("nope.txt")

    def test_read_secret_content(self, tmp_path: Path) -> None:
        sb = _sandbox(tmp_path)
        secret_path = tmp_path / "secret.txt"
        secret_path.write_text('access_token = "thisisasecretvalue1"\n', encoding="utf-8")
        with pytest.raises(SandboxSecretError):
            sb.read_file("secret.txt")

    def test_write_secret_content(self, tmp_path: Path) -> None:
        sb = _sandbox(tmp_path)
        with pytest.raises(SandboxSecretError):
            sb.write_file("secret.txt", 'access_token = "thisisasecretvalue1"')

    def test_write_limit(self, tmp_path: Path) -> None:
        sb = _sandbox(tmp_path)
        with pytest.raises(SandboxResourceError, match="limit"):
            sb.write_file("big.txt", "x" * 5000)

    def test_delete_file(self, tmp_path: Path) -> None:
        sb = _sandbox(tmp_path)
        sb.write_file("del.txt", "bye")
        assert sb.delete_file("del.txt")
        assert "del.txt" not in sb.list_all()

    def test_list_dir(self, tmp_path: Path) -> None:
        sb = _sandbox(tmp_path)
        sb.write_file("a/b.txt", "b")
        sb.write_file("a/c.txt", "c")
        entries = sb.list_all()
        assert "a/b.txt" in entries
        assert "a/c.txt" in entries


class TestSandboxProcess:

    def test_unknown_command_rejected(self, tmp_path: Path) -> None:
        sb = _sandbox(tmp_path)
        with pytest.raises(SandboxCommandError, match="sandbox allowlist"):
            sb.run_command("rm", ("-rf", "."))

    def test_allowed_command_runs(self, tmp_path: Path) -> None:
        sb = _sandbox(tmp_path)
        result = sb.run_command(sys.executable, ("-c", "print('hi')"))
        assert result["returncode"] == 0
        assert "hi" in result["stdout"]

    def test_command_timeout(self, tmp_path: Path) -> None:
        sb = _sandbox(tmp_path)
        with pytest.raises(SandboxCommandError, match="timeout"):
            sb.run_command(sys.executable, ("-c", "import time; time.sleep(5)"),
                           timeout=0.1)


class TestSandboxNetwork:

    def test_allowed_host(self, tmp_path: Path) -> None:
        sb = _sandbox(tmp_path)
        assert sb.check_network("svc.example.com") == "svc.example.com"

    def test_disallowed_host(self, tmp_path: Path) -> None:
        sb = _sandbox(tmp_path)
        with pytest.raises(SandboxNetworkError):
            sb.check_network("evil.other.com")


class TestSandboxEnv:

    def test_env_isolation(self, tmp_path: Path) -> None:
        sb = _sandbox(tmp_path)
        env = sb.env_names()
        assert set(env) == set(DEFAULT_ENV_ALLOWLIST)
        assert all(k.isupper() for k in env)
        with pytest.raises(SandboxEnvironmentError):
            sb.env("SECRET_SOMETHING")


# ---------------------------------------------------------------------------
# ToolSpec / ToolRegistry tests
# ---------------------------------------------------------------------------

class TestToolCategory:
    def test_values(self) -> None:
        assert ToolCategory("secret").value == "secret"
        assert ToolCategory("destructive").value == "destructive"
        with pytest.raises(ValueError):
            ToolCategory("unknown")

    def test_audit_friendly_string(self) -> None:
        # Ensure the enum value "secret" doesn't trip the SECRET_ASSIGNMENT
        # regex: functional Enum avoids `secret\s*=\s*['"]`.
        assert ToolCategory.SECRET.value == "secret"
        # Verify the class-level `SECRET` member exists.
        assert hasattr(ToolCategory, "SECRET")


class TestToolSpec:

    def test_default_action(self) -> None:
        spec = ToolSpec(name="mytool", category=ToolCategory.READ,
                        description="a read tool", handler=lambda a, s: None)
        assert spec.action == "tool.mytool"

    def test_custom_policy_action(self) -> None:
        spec = ToolSpec(name="mytool", category=ToolCategory.READ,
                        description="a read tool", policy_action="custom.read",
                        handler=lambda a, s: None)
        assert spec.action == "custom.read"

    def test_arg_keys(self) -> None:
        spec = ToolSpec(name="x", category=ToolCategory.READ, description="x tool",
                        required_args=(("a", "str"),), optional_args=(("b", "int"),),
                        handler=lambda a, s: None)
        assert spec.arg_keys == ("a", "b")


class TestToolRegistry:

    def _spec(self, name: str, category: ToolCategory = ToolCategory.READ) -> ToolSpec:
        return ToolSpec(name=name, category=category, description=f"{name} tool",
                        handler=lambda a, s: None)

    def test_register_and_list(self) -> None:
        from handoff_agent.tool_registry import ToolRegistry
        reg = ToolRegistry()
        s = self._spec("alpha")
        reg.register(s)
        assert len(reg.list()) == 1
        assert reg.get("alpha") is s

    def test_duplicate_register(self) -> None:
        from handoff_agent.tool_registry import ToolRegistry
        reg = ToolRegistry()
        s = self._spec("dup")
        reg.register(s)
        with pytest.raises(ToolSetupError, match="already registered"):
            reg.register(s)

    def test_unknown_get(self) -> None:
        from handoff_agent.tool_registry import ToolRegistry
        reg = ToolRegistry()
        with pytest.raises(ToolNotFoundError):
            reg.get("nope")

    def test_discover(self) -> None:
        from handoff_agent.tool_registry import ToolRegistry
        reg = ToolRegistry()
        reg.register(self._spec("a"))
        reg.register(self._spec("b", ToolCategory.SECRET))
        assert len(reg.discover()) == 2
        assert len(reg.discover("secret")) == 1
        assert reg.discover("secret")[0].name == "b"

    def test_snapshot(self) -> None:
        from handoff_agent.tool_registry import ToolRegistry
        reg = ToolRegistry()
        reg.register(self._spec("s"))
        snap = reg.snapshot()
        assert snap["count"] == 1
        assert snap["tools"][0]["name"] == "s"


# ---------------------------------------------------------------------------
# ToolGateway tests
# ---------------------------------------------------------------------------

class TestToolGateway:

    def test_register_tool(self, tmp_path: Path) -> None:
        eng = _engine_with()
        sb = _sandbox(tmp_path)
        gw = ToolGateway(eng, sb)
        spec = ToolSpec(name="x", category=ToolCategory.READ, description="test tool",
                        handler=lambda a, s: None)
        gw.register_tool(spec)
        assert gw.registry.get("x") is spec

    def test_invoke_happy_path(self, tmp_path: Path) -> None:
        eng = _engine_with()
        sb = _sandbox(tmp_path)
        gw = ToolGateway(eng, sb)
        def handler(args, sandbox):
            return {"ok": True}
        spec = ToolSpec(name="x", category=ToolCategory.READ, description="test tool",
                        required_args=(), handler=handler)
        gw.register_tool(spec)
        result = gw.invoke(ToolRequest("agent-1", "x"))
        assert result.ok
        assert result.output == {"ok": True}

    def test_policy_deny(self, tmp_path: Path) -> None:
        eng = PolicyEngine()  # no subjects
        sb = _sandbox(tmp_path)
        gw = ToolGateway(eng, sb)
        spec = ToolSpec(name="x", category=ToolCategory.READ, description="test tool",
                        handler=lambda a, s: None)
        gw.register_tool(spec)
        with pytest.raises(ToolPermissionError):
            gw.invoke(ToolRequest("unknown", "x"))

    def test_missing_required_arg(self, tmp_path: Path) -> None:
        eng = _engine_with()
        sb = _sandbox(tmp_path)
        gw = ToolGateway(eng, sb)
        spec = ToolSpec(name="x", category=ToolCategory.READ, description="test tool",
                        required_args=(("key", "str"),),
                        handler=lambda a, s: None)
        gw.register_tool(spec)
        with pytest.raises(ToolInputError, match="missing required"):
            gw.invoke(ToolRequest("agent-1", "x"))

    def test_secret_output_rejected(self, tmp_path: Path) -> None:
        eng = _engine_with()
        sb = _sandbox(tmp_path)
        gw = ToolGateway(eng, sb)
        def leaky(args, sandbox):
            return {"token": "access_token = thisisasecretvalue1"}
        spec = ToolSpec(name="leaky", category=ToolCategory.WRITE, description="test tool",
                        handler=leaky)
        gw.register_tool(spec)
        with pytest.raises(ToolSecretError, match="secret-like"):
            gw.invoke(ToolRequest("agent-1", "leaky"))

    def test_filesystem_gate_rejects_escape(self, tmp_path: Path) -> None:
        eng = _engine_with()
        sb = _sandbox(tmp_path)
        gw = ToolGateway(eng, sb)
        def handler(args, sandbox):
            return {"path": args.get("path")}
        spec = ToolSpec(name="fs", category=ToolCategory.FILESYSTEM, description="test tool",
                        required_args=(("path", "str"),), handler=handler)
        gw.register_tool(spec)
        with pytest.raises(ToolPermissionError):
            gw.invoke(ToolRequest("agent-1", "fs", {"path": "../../etc/passwd"}))

    def test_network_gate_blocks_off_list(self, tmp_path: Path) -> None:
        eng = _engine_with()
        sb = _sandbox(tmp_path)
        gw = ToolGateway(eng, sb)
        def handler(args, sandbox):
            return {"host": args.get("host")}
        spec = ToolSpec(name="net", category=ToolCategory.NETWORK, description="test tool",
                        required_args=(("host", "str"),), handler=handler)
        gw.register_tool(spec)
        with pytest.raises(ToolPermissionError):
            gw.invoke(ToolRequest("agent-1", "net", {"host": "evil.zzz"}))

    def test_git_not_allowlisted(self, tmp_path: Path) -> None:
        eng = _engine_with()
        sb = _sandbox(tmp_path)
        gw = ToolGateway(eng, sb)
        def handler(args, sandbox):
            return {"cmd": args.get("command")}
        spec = ToolSpec(name="g", category=ToolCategory.GIT, description="test tool",
                        required_args=(("command", "str"),), handler=handler)
        gw.register_tool(spec)
        with pytest.raises(ToolPermissionError, match="denied"):
            gw.invoke(ToolRequest("agent-1", "g", {"command": "push"}))

    def test_destructive_requires_approval(self, tmp_path: Path) -> None:
        eng = _engine_with()
        sb = _sandbox(tmp_path)
        gw = ToolGateway(eng, sb)
        spec = ToolSpec(name="destructive", category=ToolCategory.DESTRUCTIVE,
                        description="test tool", optional_args=(("path", "str"),),
                        handler=lambda a, s: {"done": True})
        gw.register_tool(spec)
        with pytest.raises(ToolPermissionError, match="denied"):
            gw.invoke(ToolRequest("agent-1", "destructive", {"path": "tmp/x"}))

    def test_destructive_with_valid_ticket(self, tmp_path: Path) -> None:
        eng = _engine_with()
        sb = _sandbox(tmp_path)
        gw = ToolGateway(eng, sb)
        spec = ToolSpec(name="destructive", category=ToolCategory.DESTRUCTIVE,
                        description="test tool", optional_args=(("path", "str"),),
                        handler=lambda a, s: {"done": True})
        gw.register_tool(spec)
        req = ActionRequest("agent-1", "destructive.invoke", resource="tmp/x")
        ticket = eng.request_approval(req)
        eng.grant_approval(ticket, approver="reviewer")
        result = gw.invoke(ToolRequest("agent-1", "destructive", {"path": "tmp/x"}),
                           approval_ticket=ticket)
        assert result.ok

    def test_retry_on_failure(self, tmp_path: Path) -> None:
        eng = _engine_with()
        sb = _sandbox(tmp_path)
        gw = ToolGateway(eng, sb)
        gw._backoff_base_ms = 1
        attempts = [0]
        def flaky(args, sandbox):
            attempts[0] += 1
            if attempts[0] < 3:
                raise ToolError("transient")
            return {"recovered": True}
        spec = ToolSpec(name="flaky", category=ToolCategory.READ, description="test tool",
                        retryable=True, handler=flaky)
        gw.register_tool(spec)
        result = gw.invoke(ToolRequest("agent-1", "flaky"))
        assert result.ok
        assert result.attempts == 3

    def test_retry_bounded(self, tmp_path: Path) -> None:
        eng = _engine_with()
        sb = _sandbox(tmp_path)
        gw = ToolGateway(eng, sb)
        gw._backoff_base_ms = 1
        def always_fail(args, sandbox):
            raise ToolError("always")
        spec = ToolSpec(name="fail", category=ToolCategory.READ, description="test tool",
                        retryable=True, handler=always_fail)
        gw.register_tool(spec)
        with pytest.raises(ToolError):
            gw.invoke(ToolRequest("agent-1", "fail"))
        last = [e for e in gw.audit_trail() if e["tool"] == "fail"][-1]
        assert last["attempts"] == 3

    def test_audit_trail_recorded(self, tmp_path: Path) -> None:
        eng = _engine_with()
        sb = _sandbox(tmp_path)
        gw = ToolGateway(eng, sb)
        spec = ToolSpec(name="x", category=ToolCategory.READ, description="test tool",
                        handler=lambda a, s: "ok")
        gw.register_tool(spec)
        gw.invoke(ToolRequest("agent-1", "x"))
        assert len(gw.audit_trail()) == 1
        assert gw.audit_trail()[0]["tool"] == "x"

    def test_timeout_enforced(self, tmp_path: Path) -> None:
        eng = _engine_with()
        sb = _sandbox(tmp_path)
        gw = ToolGateway(eng, sb)
        spec = ToolSpec(name="slow", category=ToolCategory.READ, description="test tool",
                        command=sys.executable,
                        args=("-c", "import time; time.sleep(10)"),
                        timeout_seconds=0.1)
        gw.register_tool(spec)
        with pytest.raises((ToolSandboxError, ToolError)):
            gw.invoke(ToolRequest("agent-1", "slow"))

    def test_capability_required_for_tool(self, tmp_path: Path) -> None:
        eng = _engine_with()
        eng.register_subject("writer", "agent", trust_level=2,
                             capabilities=["tool_use", "write_reports"])
        sb = _sandbox(tmp_path)
        gw = ToolGateway(eng, sb)
        gw.register_tool(ToolSpec(
            name="export", category=ToolCategory.WRITE, description="export tool",
            capability="write_reports", handler=lambda a, s: {"ok": True}))
        # granted capability passes
        assert gw.invoke(ToolRequest("writer", "export")).ok
        # adapter-subject without the capability is safe-denied
        with pytest.raises(ToolPermissionError, match="denied"):
            gw.invoke(ToolRequest("agent-1", "export"))

    def test_adapter_gets_only_declared_capabilities(self, tmp_path: Path) -> None:
        declared = frozenset(["read_only"])
        dec = AdapterCapabilityDeclaration(
            adapter_kind="mcp", adapter_name="reader", capabilities=declared)
        eng = PolicyEngine()
        eng.register_subject(dec.adapter_name, dec.adapter_kind,
                             trust_level=1, capabilities=dec.capabilities)
        eng.add_rule("tool.*", Effect.ALLOW, scope="*")
        assert eng.subject("reader").capabilities == set(declared)
        sb = _sandbox(tmp_path)
        gw = ToolGateway(eng, sb)
        spec = ToolSpec(name="probe", category=ToolCategory.READ,
                        description="probe tool", capability="read_only",
                        handler=lambda a, s: {"ok": True})
        gw.register_tool(spec)
        result = gw.invoke(ToolRequest("reader", "probe"))
        assert result.ok
        # Declared capability set is authoritative and excludes undeclared ones.
        assert "write_only" not in dec.capabilities
        # Gate layer 1 denies undeclared capability.
        d = eng.evaluate(ActionRequest("reader", "capability.write_only"))
        assert d.allowed is False

    def test_result_to_dict(self, tmp_path: Path) -> None:
        eng = _engine_with()
        sb = _sandbox(tmp_path)
        gw = ToolGateway(eng, sb)
        spec = ToolSpec(name="x", category=ToolCategory.READ, description="test tool",
                        handler=lambda a, s: "ok")
        gw.register_tool(spec)
        result = gw.invoke(ToolRequest("agent-1", "x"))
        d = result.to_dict()
        assert d["tool"] == "x"
        assert d["ok"] is True


# ---------------------------------------------------------------------------
# Adapter capability declaration
# ---------------------------------------------------------------------------

class TestAdapterCapabilityDeclaration:

    def test_valid_adapter_kinds(self) -> None:
        for kind in ("mcp", "cli", "skill", "generic"):
            dec = AdapterCapabilityDeclaration(adapter_kind=kind, adapter_name="test",
                                                capabilities=frozenset(["read"]))
            assert dec.adapter_kind == kind

    def test_invalid_adapter_kind(self) -> None:
        with pytest.raises(ToolSetupError, match="unsupported adapter kind"):
            AdapterCapabilityDeclaration(adapter_kind="ssh", adapter_name="test")


# ---------------------------------------------------------------------------
# Provision / conformance
# ---------------------------------------------------------------------------

class TestProvisionPhase33Gateway:

    def test_provision_returns_gateway(self) -> None:
        gw = provision_phase33_gateway()
        assert isinstance(gw, ToolGateway)
        assert len(gw.registry.list()) == 10

    def test_provision_with_explicit_root(self, tmp_path: Path) -> None:
        root = tmp_path / "custom"
        gw = provision_phase33_gateway(project_root=str(root))
        assert gw.sandbox.root == root.resolve()


class TestRunToolConformance:

    @pytest.mark.parametrize("adapter_kind", ["mcp", "cli", "skill", "generic"])
    def test_all_adapter_kinds_pass(self, adapter_kind: str) -> None:
        report = run_tool_conformance(adapter_kind=adapter_kind)
        assert isinstance(report, ToolConformanceReport)
        assert report.adapter_kind == adapter_kind
        failures = [c for c in report.checks if not c.passed]
        assert not failures, failures

    def test_invalid_adapter_kind(self) -> None:
        with pytest.raises(ToolSetupError, match="unsupported adapter kind"):
            run_tool_conformance(adapter_kind="ssh")

    def test_report_to_dict(self) -> None:
        report = run_tool_conformance(adapter_kind="generic")
        d = report.to_dict()
        assert d["adapter_kind"] == "generic"
        assert d["passed"] is True
        assert len(d["checks"]) > 0
