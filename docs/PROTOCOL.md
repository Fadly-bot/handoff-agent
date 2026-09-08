# Universal Handoff Protocol v1 — Specification

Provider-agnostic, machine-readable checkpoint protocol for Handoff Agent.

## 1. Overview

The Universal Handoff Protocol defines a canonical, versioned checkpoint
model that any AI agent (Claude, OpenAI-compatible, Qwen, DeepSeek, Gemini,
Kimi, GLM, Grok, Manus, or any other that supports skills / MCP / instruction
injection) can read and write — independent of the specific provider that
generated the checkpoint.

**Principles**

- **Provider-independent** — no Claude / OpenAI / Qwen concepts in the schema.
- **Backward compatible** — existing `docs/HANDOFF.md` files without protocol
  metadata are still accepted (and treated as legacy markdown).
- **Human + machine readable** — `docs/HANDOFF.md` stays markdown; the
  machine-readable state is embedded in a well-known fenced JSON block.
- **Deterministic identity** — the checkpoint identity is derived from the
  *state*, not from timestamps, so the same state always has the same id
  across agents and can be diffed.

## 2. Protocol versioning

| Field | Value |
| --- | --- |
| `protocol.name` | `universal-handoff-protocol` |
| `protocol.version` | `1` |
| Canonical version string | `universal-handoff-protocol/1` |

The protocol follows semantic versioning of the *schema*. `version` is an
integer that increments only on breaking changes. Consumers must refuse
checkpoints with an unsupported version rather than risk misreading state.

Implemented in `src/handoff_agent/protocol.py`:

- `PROTOCOL_NAME` / `PROTOCOL_VERSION` / `PROTOCOL_VERSIONS_SUPPORTED`
- `protocol_version_string()`
- `is_supported_version(v)`

## 3. Canonical checkpoint model (state schema)

A checkpoint is a strict JSON object. The full draft-2020-12 JSON schema is
returned by `state_schema()`. Top-level shape:

```jsonc
{
  "protocol": { "name": "universal-handoff-protocol", "version": 1 },
  "identity": { "id": "<sha256 hex, 64>", "generated_at": "<ISO time>", "sequence": 0 },
  "metadata": {
    "project": { "name": "...", "type": "..." },
    "objective": "...",
    "agents": [ "..." ]
  },
  "state": {
    "objective": "...",
    "completed":   [ "..." ],
    "in_progress": [ "..." ],
    "next_actions":[ "..." ],
    "decisions":   [ "..." ],
    "constraints": [ "..." ]
  },
  "validation": { "status": "pending|passed|failed", "checks": [ "..." ] },
  "git": { "branch": "...", "head": "...", "clean": true, "status": "..." },
  "artifacts": [ "..." ],
  "risks": [ "..." ]
}
```

### Required fields

- `protocol` (name + version)
- `identity` (id + generated_at + sequence)
- `metadata` (project.name + objective)
- `state` (completed + in_progress + next_actions)

All list fields default to empty; all optional fields may be omitted by
consumers that only care about state.

## 4. Checkpoint identity

`identity.id` is a lowercase hex SHA-256 (64 chars) derived deterministically
from:

- `state.objective`
- `state.completed` (sorted)
- `state.in_progress` (sorted)
- `state.next_actions` (sorted)
- `state.decisions` (sorted)
- `state.constraints` (sorted)
- `metadata.project`

The identity intentionally excludes timestamps and sequence numbers, so the
same logical state maps to the same id regardless of when/who wrote it.
`generated_at` and `sequence` provide provenance and ordering for history.

`verify_identity(checkpoint)` recomputes and compares the embedded id.

## 5. Human-readable compatibility

`docs/HANDOFF.md` remains a human-readable markdown checkpoint. The
machine-readable state is embedded in a fenced JSON block whose info-string is
`handoff-protocol`:

````markdown
# Project Handoff

... human-readable content ...

```handoff-protocol
{ ... canonical checkpoint JSON ... }
```
````

- `render_state_block(checkpoint)` produces the block.
- `extract_state_block(text)` extracts the block.
- `parse_handoff_document(text)` returns a `Checkpoint` or `None` (legacy).

A legacy markdown HANDOFF.md without a block is still valid (returns `None`
from `parse_handoff_document`), preserving backward compatibility.

## 6. Validation

`validate_checkpoint_data()` / `validate_checkpoint()` validate against the
canonical schema and return a list of error strings. `load_checkpoint()` parses
and validates, raising:

- `ProtocolVersionError` — unsupported protocol version
- `ProtocolValidationError` — schema violations

## 7. Continuity / diffing

`diff_checkpoints(before, after)` produces a structural diff:

- `same_identity` — whether the state identity is unchanged
- `completed_added` / `in_progress_added` / `next_actions_added`
- `identical` — whether the full serialized checkpoints are equal

This enables auditing continuity across agent switches.

## 8. Security

The protocol layer:

- performs **no** filesystem access
- performs **no** Git operations
- does **not** read secrets or the environment
- never logs or returns secret values

Never include secrets in any field of a checkpoint. Persistence continues to
secret-scan generated content before writing (see `persistence.py`).
