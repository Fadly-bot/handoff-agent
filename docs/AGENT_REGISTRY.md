# Universal Agent Registry & Discovery

`handoff_agent.registry` (Phase 24) is a persistent registry of agents and the
discovery surface that routes work to them: identity, capability declarations,
interfaces, transports, authentication methods, availability, filtering,
ranking, and selection. Records are stored under
`~/.handoff/registry/agents.json` (`HANDOFF_HOME` overrides the base) and
written atomically.

## Agent record

| field | purpose |
| --- | --- |
| `agent_id`, `name` | required identity |
| `agent_type` | `ai` / `human` / `tool` / `service` |
| `version`, `platform`, `provider`, `model` | provenance |
| `endpoint`, `adapter`, `protocol_version` | routing metadata |
| `capabilities` | declared capability ids (validated) |
| `interfaces`, `transports`, `auth_methods` | interface/transport discovery |
| `metadata`, `tags` | free-form (secret-scanned) |
| `priority`, `trust_level` | ranking inputs (priority ascending, trust descending) |
| `permission_scope` | capability ids this agent may actually exercise |
| `project_scope`, `task_scope` | project/task restrictions (empty = unrestricted) |
| `status`, `last_heartbeat`, `health_detail` | availability |

`AgentRecord` is frozen and immutable; updates go through `with_`.

## Availability

| status | meaning |
| --- | --- |
| `online` | reporting healthy heartbeats |
| `offline` | registered but silent |
| `busy` | working; cannot be unregistered |
| `degraded` | recovering after unavailability |
| `unavailable` | heartbeat expired past `heartbeat_timeout_seconds` |

`detect_stale()` marks expired heartbeats `unavailable`. `health_check(agent_id)`
returns a derived view (`ok`, `fresh`, heartbeat age, detail). A heartbeat after
`unavailable` transitions the agent to `degraded` until further proof of health.

## Registration rules

Registration never stores credentials. `AgentRecord`s are rejected when they
contain secret-like values (API keys, tokens, passwords in metadata or fields),
reference unknown capability ids, use an unsupported `agent_type`, or declare a
`permission_scope` that is not a subset of `capabilities`. Registration,
modification, unregistration, heartbeats, and status changes all require an
authorized actor (`UnauthorizedRegistryOperationError` otherwise).

```python
from handoff_agent.registry import AgentRegistry, AgentRecord

registry = AgentRegistry()
registry.register(
    AgentRecord(
        agent_id="claude-1", name="Claude", platform="claude",
        provider="anthropic", model="op-4",
        capabilities={"checkpoint.read", "checkpoint.create"},
        permission_scope={"checkpoint.read"},
        interfaces=("mcp",), transports=("stdio",), auth_methods=("handshake",),
    ),
    actor="admin-1",
)
registry.heartbeat("claude-1", actor="claude-1")
```

## Discovery, filtering, selection

`filter(...)` supports capability, permission, platform, provider, model,
protocol, transport, trust, availability, task scope, and project scope
filtering. Unscoped agents remain eligible for any task/project.

Ranking is deterministic: `sort by (priority, -trust_level, name, agent_id)`.
`select(count=...)` returns the top matches; `pick()` returns the best.
`routing_metadata()` exposes only non-sensitive routing fields.

```python
agent = registry.pick(
    capabilities=["checkpoint.create"],
    provider="anthropic",
    trust_min=2,
)
```

Empty results signal "no available agent", which delegation turns into
escalation, not a bypass.

## Query interfaces

- **CLI**: `handoff registry` (summary) and `handoff registry --registry-action list`.
- **API/MCP**: `cli_payload()`, `api_payload(agent_id=None)`, and `mcp_payload()`
  return serializable dictionary views. None of the payloads ever expose
  credentials — there are none to expose.

## Persistence, audit, recovery

- Registry writes are atomic (temp file + rename).
- Loads validate structure, per-record validity, and duplicate ids;
  malformed data raises `CorruptRegistryError`.
- Every mutation appends to an append-only audit trail
  (`audit_trail()` / `events()`).
- `duplicates()` flags records with the same canonical identity.
- `backup(label)` snapshots the registry; `restore(path)` validates and
  atomically replaces it.
- `recover()` re-loads, re-validates, and re-detects stale agents; it reports
  (and isolates) agents lost during recovery.
- `health_report()` and `diagnostics()` summarize availability and integrity.

## Security invariants

- No credential storage and no API key exposure (secret-scanning at
  registration and on every persisted payload).
- Unauthorized registration/modification/removal is rejected.
- Permission scope can never exceed declared capabilities.
- The registry performs no Git operations, no shell, and no code execution.