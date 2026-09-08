# Handoff Agent

AI-powered project handoff generator. Detects a project, builds a secure
read-only snapshot of its state, and generates `docs/HANDOFF.md` — the current
checkpoint for the next AI (or human) to pick up.

`docs/HANDOFF.md` always holds the **current** checkpoint only. Each time a new
handoff replaces it, the previous checkpoint is archived to `docs/CHANGELOG.md`.

Handoff Agent is built on a **provider-agnostic Universal Handoff Protocol**
with a machine-readable checkpoint schema, a capability & agent contract, a
portable skill adapter, and an MCP adapter — so it works across Claude,
OpenAI-compatible agents, Qwen, DeepSeek, Gemini, Kimi, GLM, Grok, Manus, and
any other agent that supports skills or MCP.

## Features

- Project detection + read-only Git inspection.
- Security-filtered context snapshot (never reads or embeds secrets).
- Deterministic prompt building with a restricted, safe file scope.
- Four pluggable AI providers (Claude, OpenAI, Qwen, DeepSeek).
- Safe atomic persistence into `docs/HANDOFF.md`.
- Optional commit that stages **only** `docs/HANDOFF.md`.
- Dry-run that builds context and prompt without calling any AI or writing files.
- Universal Handoff Protocol v1 (machine-readable checkpoint schema).
- Capability & agent contract (permission boundaries, negotiation).
- Portable Universal Handoff Skill (`skills/universal-handoff`).
- MCP adapter exposing resources + tools over JSON-RPC/stdio.
- Generic adapter layer (file / CLI / API) with a no-fallback registry.
- AI platform adapters for 12 platforms + a future-compatible generic one.
- Cross-AI interoperability helpers + a 21-check conformance suite.
- **Real AI integration validation** — certifies every platform against its
  actual capabilities with an integration report + actual integration matrix.
- **Universal AI-to-AI workflow** — producer → handoff → consumer state
  machine with continuity verification, conflict detection, recovery, and an
  append-only audit trail.
- **Stress & recovery suite** — race, conflict, corruption, and failure-mode
  validation with formal invariants.

## Requirements

- Linux or macOS (bash).
- Python 3.11+.

The runtime uses only the Python standard library — no runtime dependencies.

## Install

```bash
bash install.sh
```

This installs to `~/.handoff` and creates a `handoff` command in
`~/.local/bin`. Ensure that directory is on your `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Uninstall with:

```bash
bash uninstall.sh
```

## Quick start

```bash
handoff --help        # usage
handoff config        # provider status (never prints API keys)
handoff --dry-run     # preview: builds context + prompt, no AI call, no writes
handoff               # generate docs/HANDOFF.md using the default provider
handoff --commit      # generate, then commit ONLY docs/HANDOFF.md
handoff mcp           # run the Handoff MCP server over stdio
```

The first provider used without a configured API key fails safely with:

```
[handoff] error: claude: missing API key
```

## Usage

| Command | Description |
| --- | --- |
| `handoff` | Generate `docs/HANDOFF.md` (previous checkpoint archived to `docs/CHANGELOG.md`) |
| `handoff --dry-run` | Build context + prompt, print them, call no AI, write nothing |
| `handoff --provider openai` | Use a specific provider (default: `config.default_provider`) |
| `handoff --model gpt-4o` | Use a specific model for the selected provider |
| `handoff --commit` | After generating, commit **only** `docs/HANDOFF.md`; never pushes |
| `handoff --path DIR` | Detect project from a specific directory |
| `handoff --output FILE` | Write the handoff to a custom path |
| `handoff --config FILE` | Use an explicit config file |
| `handoff inspect` | Show project + Git inspection (read-only) |
| `handoff config` | Show provider configuration status |
| `handoff mcp` | Run the Handoff MCP server over stdio |

See [docs/MCP.md](docs/MCP.md) for MCP server configuration, [docs/PROTOCOL.md](docs/PROTOCOL.md)
for the Universal Handoff Protocol, and [skills/universal-handoff](skills/universal-handoff/)
for the portable skill package.

### Commit safety

`--commit` stages exactly `docs/HANDOFF.md` (`git add -- docs/HANDOFF.md`) and
commits with the deterministic message `docs: update handoff checkpoint`. It
never stages unrelated modified/untracked files, never unstages anything, and
never pushes.

## Providers

| Provider | Config key | API key environment variable | Default model |
| --- | --- | --- | --- |
| Claude | `claude` | `ANTHROPIC_API_KEY` | `claude-sonnet-4-5` |
| OpenAI | `openai` | `OPENAI_API_KEY` | `gpt-4o` |
| Qwen | `qwen` | `DASHSCOPE_API_KEY` | `qwen-plus` |
| DeepSeek | `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-chat` |

Provider selection precedence (no automatic fallback):

```
--provider X
    ↓
config.default_provider
```

API keys are read from the environment at request time only. They are never
stored in config, written to files, printed, logged, or included in errors.

## Configuration

Config lives at `~/.handoff/config.json` (created automatically):

```json
{
  "version": "0.4.0",
  "default_provider": "claude",
  "output": "docs/HANDOFF.md",
  "auto_commit": false,
  "auto_push": false,
  "providers": {
    "claude": { "api_key_env": "ANTHROPIC_API_KEY", "model": "" },
    "openai": { "api_key_env": "OPENAI_API_KEY", "model": "" },
    "qwen": { "api_key_env": "DASHSCOPE_API_KEY", "model": "" },
    "deepseek": { "api_key_env": "DEEPSEEK_API_KEY", "model": "" }
  }
}
```

An empty `"model"` uses the provider's default. Override per-run with
`--model`. Endpoints are fixed per provider; custom endpoints must be HTTPS
and are validated before any network request.

## Security model

- Sources are read only and passed through a security filter; `.env`, keys,
  tokens, passwords, and credentials are excluded from context and prompts.
- Providers receive only a prepared `FullContext`; they cannot access the
  filesystem, run Git commands, or enumerate the environment.
- Only providers may open network connections; custom endpoints must be HTTPS.
- Persistence is atomic, path-contained, and secret-scanning.
- Git operations are narrowed to `git add -- docs/HANDOFF.md` /
  `git commit -m <message>`. Push, fetch, pull, reset, clean, checkout,
  restore, merge, rebase, stash, and remote manipulation are not available to
  the tool.
- Dry-run performs zero writes and zero network calls.

### MCP adapter security

The MCP server ([docs/MCP.md](docs/MCP.md)) enforces the same boundaries:

- All operations stay inside the detected project root (path traversal and
  symlink escapes rejected).
- Returned content is secret-scanned; secret-tainted content is refused.
- `create_checkpoint` is disabled in read-only mode and gated on the
  `checkpoint.create` capability.
- No arbitrary shell execution, no unrestricted Git operations, no `push`.

## Protocol, capability, skill, MCP

- **Universal Handoff Protocol** (`src/handoff_agent/protocol.py`,
  [docs/PROTOCOL.md](docs/PROTOCOL.md)) — versioned, machine-readable
  checkpoint schema with deterministic identity, validation, and backward
  compatibility with legacy `docs/HANDOFF.md`.
- **Capability & Agent Contract** (`src/handoff_agent/capability.py`) —
  agent identity, capability discovery, negotiation, permission boundaries,
  and security constraints.
- **Universal Skill Adapter** (`skills/universal-handoff/SKILL.md`,
  `src/handoff_agent/skill.py`) — a portable, provider-independent skill
  package that teaches any agent to read, verify, and create checkpoints.
- **MCP Adapter** (`src/handoff_agent/mcp/`) — MCP server exposing
  `get_current_handoff`, `get_project_state`, `get_changelog`,
  `create_checkpoint`, and `validate_checkpoint` over JSON-RPC/stdio.

## Generic & platform adapters

The [adapter layer](docs/ADAPTERS.md) (`src/handoff_agent/adapters/`) is the
universal integration surface, with one capability-gated interface backed by:

- **file adapter** (`file`) — direct, secret-scanned, containment-enforced
  access to `docs/HANDOFF.md` / `docs/CHANGELOG.md`;
- **CLI adapter** (`cli`) — read-only, whitelisted `handoff inspect`;
- **API adapter** (`api`) — opt-in HTTPS JSON integration with env-var Bearer
  auth (default `HANDOFF_API_KEY`);
- **adapter registry** — deterministic registration/discovery with **no
  automatic fallback**.

AI [platform adapters](docs/AI_INTEGRATION.md)
(`src/handoff_agent/adapters/platforms.py`) declare capability/interface
mappings for Claude, ChatGPT/OpenAI, Gemini, Perplexity, Grok, DeepSeek, Qwen,
Kimi, GLM, Manus, OpenCode, Cline, and a read-only generic fallback:

```python
from handoff_agent.adapters import create_platform_adapter

adapter = create_platform_adapter("claude").concrete_adapter("file", project_root=".")
```

## Interoperability & conformance

`src/handoff_agent/interop.py` provides snapshot, stale-writer, conflict, and
state-consistency helpers. The [Universal Conformance Suite](docs/CONFORMANCE.md)
(`src/handoff_agent/conformance.py`) runs 21 checks against any adapter and
certifies protocol + security invariants:

```python
from handoff_agent.adapters import create_adapter
from handoff_agent.conformance import run_conformance_suite

adapter = create_adapter("file", project_root="."); adapter.start()
report = run_conformance_suite(adapter, {"repo": ".", "tmp": "/tmp/ext"})
assert report.clean, report.failed
```

The [compatibility matrix](docs/COMPATIBILITY.md) maps every platform, state,
interface, and guarantee; the [interoperability spec](docs/INTEROPERABILITY.md)
explains the cross-AI workflow.

## AI integration certification

`src/handoff_agent/integration.py` certifies every registered platform against
its **actual** capabilities (never assumed): provider authentication,
credential isolation, protocol negotiation, capability realization, checkpoint
lifecycle, changelog archiving, Git-state verification, limitation detection,
unsupported-interface handling, safe provider failure, and **no automatic
fallback**. The [actual integration matrix](docs/AI_INTEGRATION.md) is computed
from the real platform specs; mock mode is the CI default and live API tests
are strictly opt-in (`HANDOFF_LIVE_TESTS=1` + credential present):

```python
from handoff_agent.integration import run_integration_suite, actual_integration_matrix

report = run_integration_suite(vendor_root=".", mode="mock")
assert report.clean, report.failed
```

## Universal AI-to-AI workflow

`src/handoff_agent/workflow.py` drives the producer-handoff-consumer lifecycle
as an explicit [state machine](docs/WORKFLOW.md): checkpointing (idempotent,
duplicate-detected), handoff request/acceptance with explicit token, human
approval, seven-field continuity verification, stale/conflict rejection,
non-destructive recovery, and an append-only audit trail:

```python
from handoff_agent.workflow import WorkflowManager

mgr = WorkflowManager()
mgr.register_agent("agent-a")
atomic_checkpoint = mgr.checkpoint("agent-a", {...})
token = mgr.request_handoff("agent-a")
mgr.register_agent("agent-b")
mgr.accept_handoff("agent-b", token, provenance="agent-a")
```

## Documentation

| Doc | Content |
| --- | --- |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | Protocol, schema, identity, validation |
| [docs/MCP.md](docs/MCP.md) | MCP server setup and tools |
| [docs/ADAPTERS.md](docs/ADAPTERS.md) | Adapter interface, built-ins, generic integration |
| [docs/AI_INTEGRATION.md](docs/AI_INTEGRATION.md) | Per-platform integration guides |
| [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) | Compatibility matrix |
| [docs/INTEROPERABILITY.md](docs/INTEROPERABILITY.md) | Cross-AI interoperability |
| [docs/CONFORMANCE.md](docs/CONFORMANCE.md) | Conformance suite reference |
| [docs/WORKFLOW.md](docs/WORKFLOW.md) | Universal AI-to-AI workflow state machine |
| [docs/RECOVERY.md](docs/RECOVERY.md) | Non-destructive recovery procedures |
| [docs/CONFLICT_RESOLUTION.md](docs/CONFLICT_RESOLUTION.md) | Conflict handling guide |
| [docs/SECURITY.md](docs/SECURITY.md) | Security model, credential isolation, transport |
| [docs/CLI_GUIDE.md](docs/CLI_GUIDE.md) | Command-line reference |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Failure mode diagnosis |
| [docs/SKILL_GUIDE.md](docs/SKILL_GUIDE.md) | Skill installation |
| [docs/VERSIONING.md](docs/VERSIONING.md) | Versioning policies |
| [docs/MIGRATION.md](docs/MIGRATION.md) | Migrating from v0.1 / v0.2 / v0.3 |
| [docs/RELEASE.md](docs/RELEASE.md) | Release checklist, no auto push/release |
| [docs/RELEASE_NOTES.md](docs/RELEASE_NOTES.md) | v0.4.0 release notes |
| [CHANGELOG.md](CHANGELOG.md) | Release history |

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=$PWD/src:$PWD/tests .venv/bin/python -m pytest tests/
```

## Uninstaller

`bash uninstall.sh` removes the symlink and the entire `~/.handoff`
installation directory after confirmation.

## License

MIT — see [LICENSE](LICENSE).