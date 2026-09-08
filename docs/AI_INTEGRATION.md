# AI Integration Guides

How each family of platforms integrates with Handoff. Every integration is
**provider-independent** — the core never imports a platform SDK, never stores
a credential, and never opens a network connection on its own.

## The invariant on all platforms

> Read the machine-readable checkpoint in `docs/HANDOFF.md`, verify it, then
> continue the work. Persist progress as a new checkpoint using the
> `handoff-protocol` block — never granting yourself capabilities you do not
> hold.

## Local, file/shell capable agents

**OpenCode, Cline, Claude Code (local checkout).** Highest-fidelity
integration: install the skill package for lifecycle guidance
([SKILL_GUIDE.md](SKILL_GUIDE.md)), connect the MCP server for structured
tools ([MCP.md](MCP.md)), or operate directly through the file adapter.
These agents hold write capabilities and can persist checkpoints.

## Skill-capable assistants

**Claude, ChatGPT, Gemini, Grok, Manus.** Provide the portable
`skill-installer` instructions from `skills/universal-handoff/SKILL.md`. The
skill teaches the read-before-continue, verify-before-trust, and checkpoint
workflow without any agent-specific code.

## MCP-capable clients

**Claude, Gemini, Grok, Manus, OpenCode, Cline.** Register the Handoff MCP
server (see [MCP.md](MCP.md)). Tools exposed: `get_current_handoff`,
`get_project_state`, `get_changelog`, `validate_checkpoint`,
`create_checkpoint`, `get_capabilities`; resource `handoff://capabilities`.
Read-only mode via `HANDOFF_MCP_READ_ONLY=1`.

## Read-only platforms

**Perplexity** (no machine interface), **DeepSeek, Qwen, Kimi, GLM** (file
only). These are research/writing assistants: give them read-only access to
the checkpoint. They can summarize and recommend but are denied
`checkpoint.create`/`checkpoint.update` by contract — a boundary enforced by
the platform adapter, not by hope.

## Future AI agents

Use the generic fallback adapter (read-only by default) and the conformance
suite: run `run_conformance_suite(adapter, ...)` against your adapter; a
`clean` report certifies Universal Handoff conformance.

## API-based integration (opt-in)

See [ADAPTERS.md](ADAPTERS.md) for the API adapter contract:

- HTTPS only; loopback allowed for local services.
- Base URL path `""`, `/api`, or `/handoff`.
- `Authorization: Bearer <HANDOFF_API_KEY>` read from the environment at
  request time.
- 8 MB response cap; safe, sanitized errors.

## Authentication boundary

Every platform expresses its authentication needs as an environment-variable
name — never a value:

| Platform | Auth env |
|---|---|
| Claude | `ANTHROPIC_API_KEY` |
| ChatGPT / OpenAI | `OPENAI_API_KEY` |
| Gemini | `GOOGLE_API_KEY` |
| Perplexity | `PERPLEXITY_API_KEY` |
| Grok | `XAI_API_KEY` |
| DeepSeek | `DEEPSEEK_API_KEY` |
| Qwen | `DASHSCOPE_API_KEY` |
| Kimi | `MOONSHOT_API_KEY` |
| GLM | `ZHIPU_API_KEY` |
| Manus | `MANUS_API_KEY` |
| OpenCode / Cline | handled by the client |

API keys are read at request time and never logged, stored, or emitted.

## Integration matrix (actual capabilities)

Computed by `integration.actual_integration_matrix()` from the real platform
specs — never assumed. `MCP` / `SKILL` / `CLI` mark the machine interfaces a
platform genuinely supports (a "no" means no native interface exists and no
integration is claimed):

| Platform | READ | WRITE | MCP | SKILL | CLI |
|---|---|---|---|---|---|
| Claude | YES | YES | YES | YES | YES |
| ChatGPT / OpenAI | YES | YES | no | YES | no |
| Gemini | YES | YES | YES | YES | no |
| Perplexity | YES | no | no | no | no |
| Grok | YES | YES | YES | YES | no |
| DeepSeek | YES | no | no | no | no |
| Qwen | YES | no | no | no | no |
| Kimi | YES | no | no | no | no |
| GLM | YES | no | no | no | no |
| Manus | YES | YES | YES | YES | no |
| OpenCode | YES | YES | YES | YES | YES |
| Cline | YES | YES | YES | no | YES |
| Generic (fallback) | YES | no | no | no | no |

The full UI of a platform's participation (interface availability, model
defaults, limitations, exact auth env) is declared in `PLATFORM_SPECS`
(`adapters/platforms.py`). Run `run_integration_suite(...)` for the
machine-readable certification of every platform in this matrix.