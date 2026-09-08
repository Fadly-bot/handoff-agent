# Cross-AI Interoperability

Handoff is built around one protocol, shared by every AI platform and tooling
interface. Machine-readable checkpoints in `docs/HANDOFF.md` follow the
`universal-handoff-protocol` (version 1). Whether the actor is Claude,
ChatGPT, Gemini, Perplexity, Grok, DeepSeek, Qwen, Kimi, GLM, Manus,
OpenCode, Cline, a future AI agent, a CLI hook, or an MCP server, the same
rules apply.

## What "interoperable" means

Every platform:

- **uses the same protocol** — one machine-readable schema, one identity
  scheme, one checkpoint lifecycle;
- **has its own capability set** — read-only platforms can never create or
  update checkpoints;
- **never receives permissions beyond its capabilities** — the permission
  boundary is enforced by the adapter, not by trust;
- **never receives Handoff secrets** — adapter state and status output carry
  no key material;
- **cannot perform dangerous Git operations** — Git access is limited to
  read-only inspection;
- **cannot escape project containment** — path traversal, symlink escapes,
  and writes outside the project root are refused.

## The stack

```
Protocol layer        universal-handoff-protocol (schema, identity, validation)
Capability layer      capability catalog + permission boundaries
Adapter layer         generic adapter interface (file / CLI / API)
Platform layer        AI platform declarations + instruction mapping
Interface layer       skill • MCP • CLI • file • API
```

## Interfaces

| Interface   | Reads state | Writes checkpoints | Notes                          |
|-------------|:-----------:|:------------------:|--------------------------------|
| File        | ✓           | ✓                  | direct `docs/HANDOFF.md`        |
| CLI         | ✓           | (read-only)        | `handoff inspect`, `--dry-run` |
| MCP         | ✓           | ✓                  | JSON-RPC tools                 |
| Skill       | ✓           | guided             | portable skill package         |
| API         | ✓           | ✓                  | JSON service (opt-in)          |

## AI platforms

| Platform   | Interfaces                | Writes checkpoints |
|------------|---------------------------|:------------------:|
| Claude     | skill, MCP, CLI, file     | ✓                  |
| ChatGPT    | skill, file               | ✓ (with approval)  |
| Gemini     | skill, MCP                | ✓                  |
| Perplexity | —                         | ✗ (read-only)      |
| Grok       | skill, MCP                | ✓                  |
| DeepSeek   | file                      | ✗ (read-only)      |
| Qwen       | file                      | ✗ (read-only)      |
| Kimi       | file                      | ✗ (read-only)      |
| GLM        | file                      | ✗ (read-only)      |
| Manus      | skill, MCP, file          | ✓                  |
| OpenCode   | skill, MCP, CLI, file     | ✓                  |
| Cline      | MCP, CLI, file            | ✓                  |
| Future AI  | file                      | ✗ (read-only)      |

## Interoperability guarantees

Cross-AI workflows (`AI → Handoff`, `Handoff → AI`, `AI → AI`) rely on three
pure, provider-independent operations in `handoff_agent.interop`:

- `is_stale(expected, current)` — stale-writer detection. A writer that
  observed checkpoint identity `A` must not overwrite a checkpoint that now
  has identity `B`.
- `detect_conflict(base, ours, theirs)` — two-sided divergence detection.
  A fast-forward (one side still equals the base) is not a conflict; two
  divergent, non-base checkpoints are.
- `compare_states(a, b)` — a deterministic structural diff of two state
  dicts (`changed`, `only_in_first`, `only_in_second`).

`CheckpointSnapshot` gives a stable, comparable view of a checkpoint
(identity, sequence, objective, schema) with timestamps excluded, so two
readers always agree on what "current" means.

## Consistency validation

`state_consistency(adapter)` verifies the current checkpoint end-to-end:
protocol validation, schema validation, and identity verification
(`verify_identity`). An adapter whose current checkpoint fails validation is
flagged `consistent: False` — callers must never trust an unverifiable
checkpoint.

## Conformance

Every adapter must satisfy the `Universal Conformance Suite`
(see [CONFORMANCE.md](CONFORMANCE.md)). The matrix is available
programmatically via `interoperability_matrix()`.

## Security invariants (enforced, not declared)

- Secret values are refused both on read and on write (never mixed into
  checkpoints, changelogs, status, or reports).
- Filesystem access is confined to the resolved project root; symlinked
  escapes are rejected.
- Adapters never execute arbitrary shell commands; the CLI adapter runs a
  fixed, whitelisted command.
- Unknown adapter/platform names never silently fall back — configuration
  errors surface loudly.