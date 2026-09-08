"""Handoff MCP Adapter — bridges MCP protocol to Handoff Agent.

All business logic for the Handoff MCP server lives here. The adapter
manages resources (checkpoint, project-state, changelog) and tools
(get_current_handoff, get_project_state, get_changelog, create_checkpoint,
validate_checkpoint).

Security model:
  - All operations are contained within the detected project root.
  - Symlink escapes are rejected.
  - Content is scanned for secrets before returning to the client.
  - Read/write boundaries are enforced: write tools require the client to
    declare write capabilities via the MCP capability contract.
  - No arbitrary shell or Git operations — only existing read-only
    helpers and the narrowly-scoped persistence layer.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from handoff_agent.capability import (
    ALL_CAPABILITIES,
    CAP_CHANGELOG_READ,
    CAP_CHECKPOINT_CREATE,
    CAP_CHECKPOINT_READ,
    CAP_CHECKPOINT_UPDATE,
    CAP_GIT_INSPECTION,
    CAP_PROJECT_INSPECTION,
    CAP_VALIDATION,
    AgentContract,
    AgentIdentity,
    CapabilityDeniedError,
    NegotiationResult,
    PermissionBoundary,
    SecurityConstraints,
    build_contract,
    capability_catalog,
    negotiate_capabilities,
)
from handoff_agent.context_builder import ContextBuilder
from handoff_agent.detector import NotARepositoryError, find_repo_root
from handoff_agent.git_inspector import inspect_repository
from handoff_agent.persistence import (
    CheckpointManager,
    HandoffContentError,
    HandoffPathError,
    HandoffWriteError,
    ChangelogError,
)
from handoff_agent.protocol import (
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    Checkpoint,
    CheckpointState,
    GitState,
    ProtocolValidationError,
    ValidationState,
    build_checkpoint,
    load_checkpoint,
    render_state_block,
    state_schema,
    validate_checkpoint,
)

logger = logging.getLogger(__name__)

# Secret-like patterns reused to scan content before returning to MCP client.
_CONTENT_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"""(?i)(?:api[_\-]?key|apikey)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-]{16,}"""),
    re.compile(r"""(?i)(?:secret[_\-]?key|secretkey)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-]{16,}"""),
    re.compile(r"""(?i)(?:access[_\-]?token|accesstoken)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-\.]{16,}"""),
    re.compile(r"""(?i)(?:private[_\-]?key)\s*['"]?\s*[:=]"""),
    re.compile(r"""-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----"""),
]


class MCPError(Exception):
    """Error returned to MCP client as a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _contains_secret(content: str) -> bool:
    """Return True if *content* looks like it embeds secret values."""
    for pattern in _CONTENT_SECRET_PATTERNS:
        if pattern.search(content):
            return True
    return False


def _resolve_within_root(root: Path, rel: str) -> Path:
    """Resolve ``rel`` inside ``root``, rejecting escapes and symlinks."""
    root_resolved = root.resolve()
    rel_path = Path(rel)
    if rel_path.is_absolute():
        raise MCPError(-32602, f"Path must be relative, got absolute: {rel}")
    for part in rel_path.parts:
        if part == "..":
            raise MCPError(-32602, "Path must not contain '..' traversal")
    candidate = root_resolved / rel_path
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise MCPError(-32602, "Path escapes the project root")
    return resolved


class HandoffMCPAdapter:
    """Bridges MCP protocol calls to Handoff Agent capabilities.

    Args:
        project_root: Directory to use as project root. If ``None``,
            the current working directory is used and the Git repository
            root is auto-detected.
        read_only: If True, write tools (create_checkpoint) are disabled.
    """

    # Canonical MCP resource URIs
    URI_CHECKPOINT = "handoff://checkpoint"
    URI_PROJECT_STATE = "handoff://project-state"
    URI_CHANGELOG = "handoff://changelog"
    URI_CAPABILITIES = "handoff://capabilities"

    def __init__(
        self,
        project_root: str | Path | None = None,
        *,
        read_only: bool = False,
    ) -> None:
        self._project_root = self._resolve_project_root(project_root)
        self._read_only = read_only
        self._handoff_rel = "docs/HANDOFF.md"
        self._changelog_rel = "docs/CHANGELOG.md"
        # Negotiated contract — defaults to read-only capabilities
        self._contract = self._build_initial_contract()

    @staticmethod
    def _resolve_project_root(project_root: str | Path | None) -> Path:
        if project_root is not None:
            return Path(project_root).resolve()
        try:
            return find_repo_root()
        except NotARepositoryError:
            raise MCPError(-32602, "Not inside a Git repository")

    def _build_initial_contract(self) -> AgentContract:
        agent = AgentIdentity(name="mcp-client", kind="ai")
        if self._read_only:
            boundary = PermissionBoundary(
                allowed=ALL_CAPABILITIES - frozenset({CAP_CHECKPOINT_CREATE, CAP_CHECKPOINT_UPDATE}),
                name="mcp-read-only",
            )
        else:
            boundary = PermissionBoundary(name="mcp-read-write")
        return build_contract(agent, boundary.allowed, boundary=boundary)

    # ------------------------------------------------------------------
    # Capability negotiation (called from initialize handler)
    # ------------------------------------------------------------------

    def negotiate(
        self,
        client_capabilities: frozenset[str] | list[str],
    ) -> NegotiationResult:
        """Negotiate client-declared capabilities against the allowed set."""
        allowed = self._contract.boundary.allowed
        return negotiate_capabilities(client_capabilities, allowed)

    # ------------------------------------------------------------------
    # Resources
    # ------------------------------------------------------------------

    def list_resources(self) -> list[dict[str, Any]]:
        """Return the list of MCP resources."""
        return [
            {
                "uri": self.URI_CHECKPOINT,
                "name": "Current Checkpoint",
                "description": "The current handoff checkpoint (docs/HANDOFF.md).",
                "mimeType": "text/markdown",
            },
            {
                "uri": self.URI_PROJECT_STATE,
                "name": "Project State",
                "description": "Project metadata, Git state, and security summary.",
                "mimeType": "application/json",
            },
            {
                "uri": self.URI_CHANGELOG,
                "name": "Changelog",
                "description": "Archived previous checkpoints (docs/CHANGELOG.md).",
                "mimeType": "text/markdown",
            },
            {
                "uri": self.URI_CAPABILITIES,
                "name": "Capabilities",
                "description": "The capability catalog (agent contract discovery).",
                "mimeType": "application/json",
            },
        ]

    def read_resource(self, uri: str) -> list[dict[str, Any]]:
        """Read a resource by URI. Returns MCP resource contents."""
        if uri == self.URI_CHECKPOINT:
            return self._read_checkpoint_resource()
        if uri == self.URI_PROJECT_STATE:
            return self._read_project_state_resource()
        if uri == self.URI_CHANGELOG:
            return self._read_changelog_resource()
        if uri == self.URI_CAPABILITIES:
            return self._read_capabilities_resource()
        raise MCPError(-32602, f"Unknown resource URI: {uri}")

    def _read_capabilities_resource(self) -> list[dict[str, Any]]:
        payload = {
            "catalog": capability_catalog(),
            "granted": sorted(self._contract.granted),
            "read_only": self._read_only,
            "boundary": self._contract.boundary.to_dict(),
        }
        return [
            {
                "uri": self.URI_CAPABILITIES,
                "mimeType": "application/json",
                "text": json.dumps(payload, indent=2),
            }
        ]

    def _read_checkpoint_resource(self) -> list[dict[str, Any]]:
        resolved = _resolve_within_root(self._project_root, self._handoff_rel)
        if not resolved.is_file():
            return [{"uri": self.URI_CHECKPOINT, "mimeType": "text/markdown", "text": ""}]
        content = resolved.read_text(encoding="utf-8")
        if _contains_secret(content):
            raise MCPError(-32603, "Checkpoint contains secret-like content — refusing to expose")
        return [{"uri": self.URI_CHECKPOINT, "mimeType": "text/markdown", "text": content}]

    def _read_project_state_resource(self) -> list[dict[str, Any]]:
        try:
            ctx = ContextBuilder(project_root=str(self._project_root)).build()
        except NotARepositoryError:
            raise MCPError(-32603, "Not inside a Git repository")
        except Exception as exc:
            raise MCPError(-32603, f"Failed to build project state: {exc}")

        state_dict = ctx.to_dict()
        # Add protocol metadata
        state_dict["protocol"] = {
            "name": PROTOCOL_NAME,
            "version": PROTOCOL_VERSION,
        }
        return [
            {
                "uri": self.URI_PROJECT_STATE,
                "mimeType": "application/json",
                "text": json.dumps(state_dict, indent=2),
            }
        ]

    def _read_changelog_resource(self) -> list[dict[str, Any]]:
        resolved = _resolve_within_root(self._project_root, self._changelog_rel)
        if not resolved.is_file():
            return [{"uri": self.URI_CHANGELOG, "mimeType": "text/markdown", "text": ""}]
        content = resolved.read_text(encoding="utf-8")
        if _contains_secret(content):
            raise MCPError(-32603, "Changelog contains secret-like content — refusing to expose")
        return [{"uri": self.URI_CHANGELOG, "mimeType": "text/markdown", "text": content}]

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        """Return the list of MCP tools."""
        tools = [
            {
                "name": "get_current_handoff",
                "description": "Read the current handoff checkpoint (docs/HANDOFF.md).",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "get_project_state",
                "description": "Get the current project state (metadata, git, security).",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "get_changelog",
                "description": "Read the checkpoint changelog history (docs/CHANGELOG.md).",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "validate_checkpoint",
                "description": "Validate a checkpoint against the Universal Handoff Protocol schema.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "get_capabilities",
                "description": "Discover the capability catalog and granted capabilities.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]
        if not self._read_only:
            tools.append(
                {
                    "name": "create_checkpoint",
                    "description": "Create a new handoff checkpoint in docs/HANDOFF.md.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "objective": {
                                "type": "string",
                                "description": "The objective for this checkpoint.",
                            },
                            "completed": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Completed tasks.",
                            },
                            "in_progress": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Tasks currently in progress.",
                            },
                            "next_actions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Recommended next steps.",
                            },
                            "decisions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Decisions made.",
                            },
                            "constraints": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Constraints observed.",
                            },
                        },
                    },
                }
            )
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a tool call. Returns MCP tool result."""
        try:
            self._contract.assert_can(self._tool_capability(name))
        except CapabilityDeniedError as exc:
            raise MCPError(
                code=-32001,
                message=f"Permission denied: {exc.capability!r} not granted",
                data={"capability": exc.capability},
            )

        dispatch = {
            "get_current_handoff": self._tool_get_current_handoff,
            "get_project_state": self._tool_get_project_state,
            "get_changelog": self._tool_get_changelog,
            "validate_checkpoint": self._tool_validate_checkpoint,
            "create_checkpoint": self._tool_create_checkpoint,
            "get_capabilities": self._tool_get_capabilities,
        }
        handler = dispatch.get(name)
        if handler is None:
            raise MCPError(-32601, f"Unknown tool: {name}")
        return handler(arguments)

    @staticmethod
    def _tool_capability(name: str) -> str:
        """Map a tool name to the capability required to call it."""
        mapping = {
            "get_current_handoff": CAP_CHECKPOINT_READ,
            "get_project_state": CAP_PROJECT_INSPECTION,
            "get_changelog": CAP_CHANGELOG_READ,
            "validate_checkpoint": CAP_VALIDATION,
            "create_checkpoint": CAP_CHECKPOINT_CREATE,
            "get_capabilities": CAP_VALIDATION,
        }
        return mapping.get(name, "unknown.cap")

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _tool_get_current_handoff(self, args: dict[str, Any]) -> dict[str, Any]:
        resolved = _resolve_within_root(self._project_root, self._handoff_rel)
        if not resolved.is_file():
            return {
                "content": [{"type": "text", "text": "No checkpoint found (docs/HANDOFF.md does not exist)."}],
            }
        content = resolved.read_text(encoding="utf-8")
        if _contains_secret(content):
            raise MCPError(-32603, "Checkpoint contains secret-like content — refusing to expose")
        return {
            "content": [{"type": "text", "text": content}],
        }

    def _tool_get_capabilities(self, args: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "catalog": capability_catalog(),
            "granted": sorted(self._contract.granted),
            "read_only": self._read_only,
        }
        return {
            "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
        }

    def _tool_get_project_state(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            ctx = ContextBuilder(project_root=str(self._project_root)).build()
        except NotARepositoryError:
            raise MCPError(-32603, "Not inside a Git repository")
        except Exception as exc:
            raise MCPError(-32603, f"Failed to build project state: {exc}")

        state_dict = ctx.to_dict()
        state_dict["protocol"] = {"name": PROTOCOL_NAME, "version": PROTOCOL_VERSION}
        return {
            "content": [{"type": "text", "text": json.dumps(state_dict, indent=2)}],
        }

    def _tool_get_changelog(self, args: dict[str, Any]) -> dict[str, Any]:
        resolved = _resolve_within_root(self._project_root, self._changelog_rel)
        if not resolved.is_file():
            return {
                "content": [{"type": "text", "text": "No changelog found (docs/CHANGELOG.md does not exist)."}],
            }
        content = resolved.read_text(encoding="utf-8")
        if _contains_secret(content):
            raise MCPError(-32603, "Changelog contains secret-like content — refusing to expose")
        return {
            "content": [{"type": "text", "text": content}],
        }

    def _tool_validate_checkpoint(self, args: dict[str, Any]) -> dict[str, Any]:
        # Read current checkpoint
        resolved = _resolve_within_root(self._project_root, self._handoff_rel)
        if not resolved.is_file():
            return {
                "content": [{"type": "text", "text": "No checkpoint to validate (docs/HANDOFF.md does not exist)."}],
            }
        content = resolved.read_text(encoding="utf-8")
        if _contains_secret(content):
            raise MCPError(-32603, "Checkpoint contains secret-like content — refusing to expose")

        # Parse the protocol block (returns None for legacy markdown)
        from handoff_agent.protocol import parse_handoff_document
        cp = parse_handoff_document(content)

        if cp is None:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "valid": False,
                                "errors": ["No handoff-protocol block found (legacy checkpoint)."],
                                "protocol": {"name": PROTOCOL_NAME, "version": PROTOCOL_VERSION},
                            },
                            indent=2,
                        ),
                    }
                ],
            }

        errors = validate_checkpoint(cp)

        result = {
            "valid": len(errors) == 0,
            "errors": errors,
            "protocol": {"name": PROTOCOL_NAME, "version": PROTOCOL_VERSION},
        }
        if cp:
            result["identity"] = {
                "id": cp.identity.id,
                "generated_at": cp.identity.generated_at,
            }
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
        }

    def _tool_create_checkpoint(self, args: dict[str, Any]) -> dict[str, Any]:
        objective = args.get("objective", "")
        completed = tuple(args.get("completed", []))
        in_progress = tuple(args.get("in_progress", []))
        next_actions = tuple(args.get("next_actions", []))
        decisions = tuple(args.get("decisions", []))
        constraints = tuple(args.get("constraints", []))

        # Get project info for the checkpoint
        try:
            ctx = ContextBuilder(project_root=str(self._project_root)).build()
            project_name = ctx.project.name
            project_type = ctx.project.project_type
            git = GitState(
                branch=ctx.git.branch,
                head=ctx.git.head,
                clean=ctx.git.clean,
                status=ctx.git.status,
            )
        except Exception:
            project_name = self._project_root.name
            project_type = "unknown"
            git = GitState()

        # Build canonical checkpoint
        cp = build_checkpoint(
            objective=objective,
            completed=list(completed),
            in_progress=list(in_progress),
            next_actions=list(next_actions),
            decisions=list(decisions),
            constraints=list(constraints),
            project_name=project_name,
            project_type=project_type,
            git=git,
        )

        # Render as markdown with protocol block
        body = f"# Project: {project_name}\n\nObjective: {objective}\n\n"
        if completed:
            body += "## Completed\n\n" + "\n".join(f"- {c}" for c in completed) + "\n\n"
        if in_progress:
            body += "## In Progress\n\n" + "\n".join(f"- {i}" for i in in_progress) + "\n\n"
        if next_actions:
            body += "## Next Actions\n\n" + "\n".join(f"- {a}" for a in next_actions) + "\n\n"
        if decisions:
            body += "## Decisions\n\n" + "\n".join(f"- {d}" for d in decisions) + "\n\n"
        if constraints:
            body += "## Constraints\n\n" + "\n".join(f"- {c}" for c in constraints) + "\n\n"
        body += render_state_block(cp)

        # Validate before writing
        if _contains_secret(body):
            raise MCPError(-32603, "Generated content contains secret-like patterns — refusing to write")

        # Persist via CheckpointManager
        try:
            manager = CheckpointManager(project_root=self._project_root)
            result = manager.write_checkpoint(
                body,
                metadata={
                    "project": project_name,
                    "branch": git.branch,
                    "commit": git.head,
                },
            )
        except HandoffContentError as exc:
            raise MCPError(-32603, f"Refused to write checkpoint: {exc}")
        except (HandoffPathError, HandoffWriteError, ChangelogError) as exc:
            raise MCPError(-32603, f"Failed to write checkpoint: {exc}")

        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Checkpoint created: {result.rel_path}\n"
                    f"Identity: {cp.identity.id}\n"
                    f"Objective: {objective}",
                }
            ],
        }
