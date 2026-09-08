# Handoff MCP Adapter — Configuration Guide

The Handoff Agent exposes its checkpoint capabilities through the **Model
Context Protocol (MCP)**. Any MCP-compatible client (Claude, IDEs, agent
frameworks) can read the current handoff, project state, and changelog, and —
when granted write capability — create checkpoints.

## Transport

The MCP server speaks **JSON-RPC 2.0 over stdio** using the standard library
only. It reads JSON-RPC messages from stdin and writes responses to stdout.
No external MCP SDK is required.

## Running the server

```bash
# From inside a Git repository (reads/writes that repository)
handoff mcp

# Point at a specific project
handoff --path /path/to/repo mcp

# Read-only mode (disables create_checkpoint)
HANDOFF_MCP_READ_ONLY=1 handoff mcp
```

When run without `--path`, the server auto-detects the Git repository root
from the current working directory.

## Lifecycle

1. Client sends `initialize` → server returns the MCP protocol version and
   the capabilities it supports (`resources`, `tools`).
2. Client may send `notifications/initialized` (a notification, no response).
3. Client calls `tools/list`, `tools/call`, `resources/list`,
   `resources/read`, and `ping`.

## Resources

| URI | Name | Description | MIME |
| --- | --- | --- | --- |
| `handoff://checkpoint` | Current Checkpoint | `docs/HANDOFF.md` content | `text/markdown` |
| `handoff://project-state` | Project State | Project metadata, Git state, security summary | `application/json` |
| `handoff://changelog` | Changelog | Archived checkpoints from `docs/CHANGELOG.md` | `text/markdown` |
| `handoff://capabilities` | Capabilities | Capability catalog, granted set, read-only mode | `application/json` |

## Tools

Read-only tools available in all modes:

| Tool | Capability | Description |
| --- | --- | --- |
| `get_current_handoff` | `checkpoint.read` | Read the current `docs/HANDOFF.md` |
| `get_project_state` | `project.inspection` | Get project metadata + Git state |
| `get_changelog` | `changelog.read` | Read the changelog history |
| `validate_checkpoint` | `validation` | Validate the current checkpoint against the protocol schema |
| `get_capabilities` | `validation` | Discover the capability catalog and granted capabilities |

Write-only tool available only when NOT read-only:

| Tool | Capability | Description |
| --- | --- | --- |
| `create_checkpoint` | `checkpoint.create` | Create a new checkpoint in `docs/HANDOFF.md` |

### `create_checkpoint` arguments

| Argument | Type | Required | Description |
| --- | --- | --- | --- |
| `objective` | string | no | Current objective |
| `completed` | string[] | no | Completed tasks |
| `in_progress` | string[] | no | Tasks in progress |
| `next_actions` | string[] | no | Recommended next steps |
| `decisions` | string[] | no | Decisions made |
| `constraints` | string[] | no | Constraints observed |

## Security model

The MCP server enforces the same security boundaries as the rest of Handoff
Agent:

- **Project containment** — every file/symlink is resolved and verified to
  stay inside the detected project root. Path traversal (`..`) and symlink
  escapes are rejected.
- **Secret filtering** — content returned to the client is scanned for
  secret-like patterns; secret-tainted content is refused, never exposed.
- **Read/write boundaries** — `create_checkpoint` is disabled in read-only
  mode and denied when the client lacks the `checkpoint.create` capability.
- **No arbitrary shell** — the server never executes shell commands.
- **No unrestricted Git** — the server uses only the existing read-only Git
  inspection helpers and the narrowly-scoped `CheckpointManager` for writes.
- **No filesystem access** — tools/resource handlers touch only the
  checkpoint/changelog paths and the read-only context builder.

## Do not

The MCP server will not, and must not:

- execute arbitrary shell/OS commands;
- run unrestricted Git operations (`push`, `reset`, `clean`, `checkout`,
  `merge`, `rebase`, etc.);
- read `.env`, credentials, or secrets;
- write arbitrary paths;
- expose API keys or secret values.

## Example session (JSON-RPC over stdio)

Request:
```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}
```
Response:
```json
{"jsonrpc":"2.0","id":1,"result":{
  "protocolVersion":"2024-11-05",
  "capabilities":{"resources":{"listChanged":false},"tools":{"listChanged":false}},
  "serverInfo":{"name":"handoff-mcp-server","version":"0.4.0"}
}}
```

Request:
```json
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
```

Request:
```json
{"jsonrpc":"2.0","id":3,"method":"tools/call",
 "params":{"name":"get_current_handoff","arguments":{}}}
```
