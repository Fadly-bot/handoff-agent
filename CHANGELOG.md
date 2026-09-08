# Changelog

All notable changes to the Handoff Agent are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [v0.4.0] — 2026-09-08 — Universal Handoff production release

### Added

- **Real AI Integration Validation** (`src/handoff_agent/integration.py`)
  - Certifies every AI platform (Claude, ChatGPT/OpenAI, Gemini, Perplexity,
    Grok, DeepSeek, Qwen, Kimi, GLM, Manus, OpenCode, Cline + generic
    fallback) against its ACTUAL capabilities and interfaces.
  - Provider authentication validation and environment-based credential
    loading with isolation-first handling (env-var *name* only, never values).
  - Model-override validation, protocol version negotiation, adapter
    capability realization, real HANDOFF.md consumption, checkpoint
    create/update/validate, CHANGELOG lifecycle, Git-state verification,
    limitation detection, unsupported-interface handling, safe provider
    failure, and **no automatic fallback**.
  - Mock mode is the CI default; live API tests are opt-in only
    (`HANDOFF_LIVE_TESTS=1` + credential present).
  - Comprehensive integration report and actual integration matrix.
- **Universal AI-to-AI Workflow** (`src/handoff_agent/workflow.py`)
  - Explicit state machine: idle → working → checkpointed →
    handoff_requested → handoff_accepted → completed (abandon/fail/recover).
  - Agent / session / project identity, checkpoint ownership, producer and
    consumer roles, handoff request/acceptance/completion, predecessor and
    successor tracking, AI-to-AI transition metadata.
  - Seven-field continuity verification (task, context, constraints,
    decisions, validation, artifacts, Git) before continuation.
  - Checkpoint verification before continuation, stale-checkpoint rejection,
    conflict detection, recovery from failed/abandoned/interrupted workflows,
    duplicate-detection and idempotent checkpoint operations.
  - Explicit handoff acknowledgement with token + human-approval boundary.
  - Append-only audit trail and workflow visualization (state diagram / flow).
- **Stress, Conflict & Recovery suite** (`tests/test_stress.py`)
  - Sequential/parallel agents, simultaneous reads, stale-write races,
    checkpoint collisions, conflicting state/objectives/decisions.
  - Concurrent Git mutations (dirty tree, staged unrelated, untracked,
    deleted, renamed, branch/HEAD changes, external writes).
  - Corruption (malformed block, invalid schema, unsupported version, partial
    checkpoint, interrupted write), provider failure, MCP/CLI interruption,
    filesystem/disk failure injection, recovery, fuzz-style protocol
    validation, large checkpoint/repository, long-running 60-step workflow.
- **Docs**: `docs/WORKFLOW.md`, `docs/RECOVERY.md`,
  `docs/CONFLICT_RESOLUTION.md`, `docs/SECURITY.md`, `docs/CLI_GUIDE.md`,
  `docs/TROUBLESHOOTING.md`, `docs/RELEASE_NOTES.md`, integration matrix in
  `docs/AI_INTEGRATION.md`.
- **Audit tests** (`tests/test_audit.py`): protocol/capability/adapter/workflow
  contract, concurrency/recovery, credential isolation, MCP/Skill security,
  API transport, package integrity, temp-file/cache/log/secret-output.

### Changed

- Version bumped to `0.4.0` across `__init__.py`, `constants.py`, the MCP
  server handshake, every adapter, the installer config template, the README,
  and MCP docs.
- The migration guide, compatibility matrix, and versioning policy now
  reference v0.4.0.

### Security

- Credential isolation is verified end-to-end: no API-key value can appear in
  an integration report, workflow audit, or error path.
- STRESS INVARIANTS formalized: no silent overwrite, no silent fallback, no
  destructive recovery, no user-change loss, no secret exposure, no project
  escape, deterministic errors, audit preservation.

### Unchanged

- Protocol: still `universal-handoff-protocol` version `1`, backward
  compatible with legacy `docs/HANDOFF.md` and all prior versions.
- Runtime remains Python 3.11+ and standard-library only.

## [v0.3.0] — 2026-09-07 — Universal Handoff release candidate

### Added

- **Generic Adapter Layer** (`src/handoff_agent/adapters/`)
  - Universal adapter interface (`BaseAdapter`) with lifecycle, capability
    negotiation, permission enforcement, and safe errors.
  - File-based adapter (direct `docs/HANDOFF.md` + `docs/CHANGELOG.md` access,
    secret scanning, and filesystem containment).
  - CLI adapter (fixed, whitelisted `handoff inspect` command; no shell).
  - API-compatible adapter (HTTPS except loopback, env-var Bearer auth, size
    limits, safe error handling).
  - Adapter registry, discovery, and factory with **no automatic fallback**.
- **AI Platform Adapters** (`src/handoff_agent/adapters/platforms.py`)
  - Declarative specs for Claude, ChatGPT/OpenAI, Gemini, Perplexity, Grok,
    DeepSeek, Qwen, Kimi, GLM, Manus, OpenCode, Cline, and a generic
    fallback adapter.
  - Capability mapping, instruction mapping, interface support (skill / MCP /
    CLI / file / API), model configuration, and authentication boundaries
    (env-var names only — no hardcoded credentials).
- **Cross-AI Interoperability** (`src/handoff_agent/interop.py`
  and `src/handoff_agent/conformance.py`)
  - Checkpoint snapshots, stale-checkpoint and conflict detection, state
    comparison, and state-consistency validation.
  - Universal Conformance Suite (21 checks) with machine-readable reports and
    an interoperability matrix.
- **Docs**: `docs/INTEROPERABILITY.md`, `docs/CONFORMANCE.md`,
  `docs/ADAPTERS.md`, `docs/COMPATIBILITY.md`, `docs/AI_INTEGRATION.md`,
  `docs/VERSIONING.md`, `docs/MIGRATION.md`, `docs/RELEASE.md`,
  `docs/SKILL_GUIDE.md`.
- **Tests**: adapter conformance, platform adapters, interop, conformance
  suite, and a release/security audit suite.

### Changed

- Version bumped to `0.3.0` (release candidate) across `__init__.py`,
  `constants.py`, the MCP server handshake, the installer config template,
  the README, and MCP docs.
- `install.sh` config template now targets version `0.3.0`.

### Security

- Adapter layer adds secret-value filtering on read and write, path/symlink
  containment, Git read-only inspection, and adapter failure isolation.
- Platform adapters introduce no runtime dependencies and never access the
  network; the API adapter is an explicit, opt-in integration.

### Unchanged

- Protocol: still `universal-handoff-protocol` version `1`, backward
  compatible with legacy `docs/HANDOFF.md`.
- Runtime remains Python 3.11+ and standard-library only.

## [v0.2.0] — 2026-09-06

### Added

- Skill adapter (`skills/universal-handoff`) — portable, provider-independent
  skill package.
- MCP adapter (`src/handoff_agent/mcp/`) — JSON-RPC/stdio server exposing
  `get_current_handoff`, `get_project_state`, `get_changelog`,
  `create_checkpoint`, and `validate_checkpoint`.
- MCP capability error handling: denied capabilities map to JSON-RPC error
  `-32001`; `get_capabilities` tool and `handoff://capabilities` resource.
- Capability & agent contract (`src/handoff_agent/capability.py`) — agent
  identity, capability discovery, negotiation, permission boundaries, and
  security constraints.
- Universal Handoff Protocol v1 (`src/handoff_agent/protocol.py`) —
  machine-readable checkpoint schema with deterministic identity and
  backward compatibility with legacy `docs/HANDOFF.md`.
- Secure provider layer with HTTP client hardening, response limits, and
  HTTPS enforcement.

## [v0.1.0] — 2026-09-04

### Added

- Initial release: project detection, read-only Git inspection,
  security-filtered context snapshots, deterministic prompt building, four
  AI providers (Claude, OpenAI, Qwen, DeepSeek), atomic persistence to
  `docs/HANDOFF.md` with changelog archiving, optional `--commit`, dry-run
  mode, installer and uninstaller.