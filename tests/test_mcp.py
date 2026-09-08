"""Tests for Phase 14 — MCP Adapter.

Covers the MCP server layer (JSON-RPC over stdio), the MCP adapter
resources and tools, protocol integration, capability discovery and
negotiation, permission enforcement, read/write boundaries, project
containment, symlink safety, secret filtering, safe error handling, and
lifecycle handling.

All MCP server tests use an in-process transport (``server.handle_line``)
rather than spawning a subprocess, so they are fast and deterministic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import commit_all, init_repo, write_file

from handoff_agent.mcp.adapter import (
    MCPError,
    HandoffMCPAdapter,
    _resolve_within_root,
)
from handoff_agent.mcp.server import MCPServer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json(msg: str) -> dict:
    return json.loads(msg)


def _request(method: str, params: dict, rid: int = 1) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})


def _reply_ok(response: str) -> dict:
    """Assert a response is a success and return its result."""
    data = _json(response)
    assert "result" in data
    assert "error" not in data
    return data["result"]


def _reply_error(response: str) -> dict:
    data = _json(response)
    assert "error" in data
    assert "result" not in data
    return data["error"]


class TestServerLifecycle:
    def test_initialize(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        server = MCPServer(HandoffMCPAdapter(project_root=str(repo)))
        result = _reply_ok(server.handle_line(_request("initialize", {})))
        assert result["protocolVersion"]
        assert result["capabilities"]["tools"] is not None
        assert result["capabilities"]["resources"] is not None

    def test_ping(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        server = MCPServer(HandoffMCPAdapter(project_root=str(repo)))
        result = _reply_ok(server.handle_line(_request("ping", {})))
        assert result == {}

    def test_notification_no_response(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        server = MCPServer(HandoffMCPAdapter(project_root=str(repo)))
        response = server.handle_line(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )
        assert response is None

    def test_parse_error(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        server = MCPServer(HandoffMCPAdapter(project_root=str(repo)))
        response = server.handle_line("not json{{")
        err = _reply_error(response)
        assert err["code"] == -32700

    def test_method_not_found(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        server = MCPServer(HandoffMCPAdapter(project_root=str(repo)))
        response = server.handle_line(_request("bogus.method", {}))
        err = _reply_error(response)
        assert err["code"] == -32601


class TestResources:
    def test_list_resources(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        server = MCPServer(HandoffMCPAdapter(project_root=str(repo)))
        result = _reply_ok(server.handle_line(_request("resources/list", {})))
        uris = [r["uri"] for r in result["resources"]]
        assert "handoff://checkpoint" in uris
        assert "handoff://project-state" in uris
        assert "handoff://changelog" in uris
        assert "handoff://capabilities" in uris

    def test_read_capabilities_resource(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        server = MCPServer(HandoffMCPAdapter(project_root=str(repo)))
        result = _reply_ok(
            server.handle_line(_request("resources/read", {"uri": "handoff://capabilities"}))
        )
        data = json.loads(result["contents"][0]["text"])
        assert "catalog" in data
        assert "read_only" in data

    def test_read_checkpoint_missing(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        server = MCPServer(HandoffMCPAdapter(project_root=str(repo)))
        result = _reply_ok(
            server.handle_line(_request("resources/read", {"uri": "handoff://checkpoint"}))
        )
        assert result["contents"][0]["text"] == ""

    def test_read_checkpoint_present(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        write_file(repo, "docs/HANDOFF.md", "# Handoff\n\nContent")
        commit_all(repo, "init")
        server = MCPServer(HandoffMCPAdapter(project_root=str(repo)))
        result = _reply_ok(
            server.handle_line(_request("resources/read", {"uri": "handoff://checkpoint"}))
        )
        assert "Content" in result["contents"][0]["text"]

    def test_read_unknown_resource(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        server = MCPServer(HandoffMCPAdapter(project_root=str(repo)))
        response = server.handle_line(_request("resources/read", {"uri": "bogus://x"}))
        err = _reply_error(response)
        assert err["code"] == -32602

    def test_read_project_state(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        write_file(repo, "app.py", "pass")
        commit_all(repo, "init")
        server = MCPServer(HandoffMCPAdapter(project_root=str(repo)))
        result = _reply_ok(
            server.handle_line(_request("resources/read", {"uri": "handoff://project-state"}))
        )
        text = result["contents"][0]["text"]
        data = json.loads(text)
        assert data["protocol"]["name"] == "universal-handoff-protocol"
        assert "project" in data


class TestTools:
    def test_list_tools(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        server = MCPServer(HandoffMCPAdapter(project_root=str(repo)))
        result = _reply_ok(server.handle_line(_request("tools/list", {})))
        names = [t["name"] for t in result["tools"]]
        assert "get_current_handoff" in names
        assert "get_project_state" in names
        assert "get_changelog" in names
        assert "validate_checkpoint" in names
        assert "create_checkpoint" in names
        assert "get_capabilities" in names

    def test_list_tools_read_only(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        server = MCPServer(HandoffMCPAdapter(project_root=str(repo), read_only=True))
        result = _reply_ok(server.handle_line(_request("tools/list", {})))
        names = [t["name"] for t in result["tools"]]
        assert "create_checkpoint" not in names
        assert "get_capabilities" in names

    def test_get_capabilities_tool(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        server = MCPServer(HandoffMCPAdapter(project_root=str(repo)))
        result = _reply_ok(
            server.handle_line(
                _request("tools/call", {"name": "get_capabilities", "arguments": {}})
            )
        )
        data = json.loads(result["content"][0]["text"])
        assert "checkpoint.read" in data["granted"]
        assert data["read_only"] is False

    def test_get_current_handoff_missing(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        server = MCPServer(HandoffMCPAdapter(project_root=str(repo)))
        result = _reply_ok(
            server.handle_line(
                _request("tools/call", {"name": "get_current_handoff", "arguments": {}})
            )
        )
        assert "No checkpoint found" in result["content"][0]["text"]

    def test_get_project_state_tool(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        write_file(repo, "package.json", "{}")
        commit_all(repo, "init")
        server = MCPServer(HandoffMCPAdapter(project_root=str(repo)))
        result = _reply_ok(
            server.handle_line(
                _request("tools/call", {"name": "get_project_state", "arguments": {}})
            )
        )
        text = result["content"][0]["text"]
        assert "Node.js" in text

    def test_unknown_tool(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        server = MCPServer(HandoffMCPAdapter(project_root=str(repo)))
        response = server.handle_line(
            _request("tools/call", {"name": "nope", "arguments": {}})
        )
        assert _reply_error(response)["code"] in (-32601, -32602, -32603)

    def test_create_checkpoint_tool(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        write_file(repo, "app.py", "print('hello')")
        commit_all(repo, "init")
        server = MCPServer(HandoffMCPAdapter(project_root=str(repo)))

        result = _reply_ok(
            server.handle_line(
                _request(
                    "tools/call",
                    {
                        "name": "create_checkpoint",
                        "arguments": {
                            "objective": "Build the protocol",
                            "completed": ["defined schema"],
                            "in_progress": ["adapter"],
                            "next_actions": ["MCP server"],
                        },
                    },
                )
            )
        )
        assert "Checkpoint created" in result["content"][0]["text"]
        handoff = (repo / "docs" / "HANDOFF.md")
        assert handoff.is_file()
        content = handoff.read_text(encoding="utf-8")
        assert "handoff-protocol" in content
        assert "Build the protocol" in content

    def test_create_checkpoint_denied_read_only(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        server = MCPServer(HandoffMCPAdapter(project_root=str(repo), read_only=True))
        response = server.handle_line(
            _request(
                "tools/call",
                {"name": "create_checkpoint", "arguments": {"objective": "x"}},
            )
        )
        err = _reply_error(response)
        assert err["code"] == -32001
        assert "checkpoint.create" in err["message"]

    def test_validate_checkpoint_missing(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        server = MCPServer(HandoffMCPAdapter(project_root=str(repo)))
        result = _reply_ok(
            server.handle_line(
                _request("tools/call", {"name": "validate_checkpoint", "arguments": {}})
            )
        )
        assert "No checkpoint" in result["content"][0]["text"]

    def test_validate_checkpoint_present(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        # Write a valid checkpoint with a protocol block
        from handoff_agent.protocol import build_checkpoint, render_state_block
        cp = build_checkpoint(objective="Test", project_name="repo")
        write_file(repo, "docs/HANDOFF.md", "# Handoff\n\n" + render_state_block(cp))
        commit_all(repo, "init")
        server = MCPServer(HandoffMCPAdapter(project_root=str(repo)))
        result = _reply_ok(
            server.handle_line(
                _request("tools/call", {"name": "validate_checkpoint", "arguments": {}})
            )
        )
        data = json.loads(result["content"][0]["text"])
        assert "valid" in data


class TestSecretFiltering:
    def test_read_checkpoint_with_secret_refused(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        write_file(repo, "docs/HANDOFF.md", "api_key = \"sk-supersecretvalue1234\"")
        commit_all(repo, "init")
        server = MCPServer(HandoffMCPAdapter(project_root=str(repo)))
        response = server.handle_line(
            _request("resources/read", {"uri": "handoff://checkpoint"})
        )
        assert _reply_error(response)


class TestContainment:
    def test_resolve_within_root_rejects_traversal(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        with pytest.raises(MCPError):
            _resolve_within_root(root, "../escape")

    def test_resolve_within_root_rejects_absolute(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        with pytest.raises(MCPError):
            _resolve_within_root(root, str(tmp_path / "x"))

    def test_resolve_within_root_symlink_escape(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        (external / "secret.txt").write_text("s")
        (root / "link").symlink_to(external)
        with pytest.raises(MCPError):
            _resolve_within_root(root, "link")

    def test_resolve_within_root_valid(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        resolved = _resolve_within_root(root, "docs/HANDOFF.md")
        assert str(resolved).startswith(str(root))


class TestNoShellExec:
    def test_no_shell_commands_in_adapter(self, tmp_path: Path) -> None:
        """Ensure the adapter never executes arbitrary shell commands."""
        import inspect
        from handoff_agent.mcp import adapter as adapter_mod

        source = inspect.getsource(adapter_mod)
        assert "subprocess" not in source
        assert "os.system" not in source
        assert "shell=True" not in source


class TestMCPServerStdio:
    def test_serve_roundtrip(self, tmp_path: Path) -> None:
        """Test the stdio loop with StringIO to verify full transport works."""
        import io
        repo = tmp_path / "repo"
        init_repo(repo)
        adapter = HandoffMCPAdapter(project_root=str(repo))
        server = MCPServer(adapter)
        stdin = io.StringIO(_request("ping", {}, rid=1) + "\n")
        stdout = io.StringIO()
        server.serve(stdin=stdin, stdout=stdout)
        output = stdout.getvalue().strip()
        assert "jsonrpc" in output
