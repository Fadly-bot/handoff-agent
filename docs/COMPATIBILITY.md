# Compatibility Matrix

One protocol, one state representation, many platforms and interfaces.

## Protocol

| Layer | Value |
|---|---|
| Protocol name | `universal-handoff-protocol` |
| Current version | `1` |
| State carrier | `docs/HANDOFF.md` (current) + `docs/CHANGELOG.md` (history) |
| Machine-readable block | fenced `handoff-protocol` block |
| State form | canonical JSON schema (see [docs/PROTOCOL.md](PROTOCOL.md)) |
| Identity | deterministic SHA-256 (independent of timestamps) |

## Interfaces

| Interface | Read state | Write checkpoint | Validation | Notes |
|---|---:|---:|---:|-------|
| File | ✓ | ✓ | ✓ | direct access, recommended |
| CLI | ✓ | ✗ | ✓ | `handoff inspect`, `--dry-run` |
| MCP | ✓ | ✓ | ✓ | JSON-RPC / stdio tools |
| Skill | ✓ | guided | ✓ | portable skill package |
| API | ✓ | ✓ | ✓ | opt-in JSON service |

## Platforms

| Platform   | Skill | MCP | CLI | File | Write cap. |
|------------|:---:|:---:|:---:|:---:|:---:|
| Claude     | ✓ | ✓ | ✓ | ✓ | ✓ |
| ChatGPT / OpenAI | ✓ | ✗ | ✗ | ✓ | ✓ (with approval) |
| Gemini     | ✓ | ✓ | ✗ | ✗ | ✓ |
| Perplexity | ✗ | ✗ | ✗ | ✗ | ✗ |
| Grok       | ✓ | ✓ | ✗ | ✗ | ✓ |
| DeepSeek   | ✗ | ✗ | ✗ | ✓ | ✗ |
| Qwen       | ✗ | ✗ | ✗ | ✓ | ✗ |
| Kimi       | ✗ | ✗ | ✗ | ✓ | ✗ |
| GLM        | ✗ | ✗ | ✗ | ✓ | ✗ |
| Manus      | ✓ | ✓ | ✗ | ✓ | ✓ |
| OpenCode   | ✓ | ✓ | ✓ | ✓ | ✓ |
| Cline      | ✗ | ✓ | ✓ | ✓ | ✓ |
| Future AI agents | — | — | — | ✓ (default) | ✗ (default) |

The generic fallback adapter for future agents is read-only by default; it can
be configured to write only with an explicit, deliberate grant.

## Cross-adapter guarantees

Every combination satisfies:

- same protocol and schema;
- capability enforcement (never more than granted);
- no checkpoint secrecy leaks between platforms;
- read-only Git inspection only;
- project containment (no path or symlink escapes).

## Backward compatibility

- Protocol v1 reads legacy `docs/HANDOFF.md` files (no machine-readable block)
  and marks them as unvalidated legacy checkpoints.
- v0.2.0, v0.3.0, and v0.4.0 checkpoints are all protocol v1; migration is in-place
  (see [MIGRATION.md](MIGRATION.md)).
- Checkpoints written by any v1-compliant tool are readable and writable by
  every v1-compliant adapter.