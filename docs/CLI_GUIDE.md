# CLI Guide

The `handoff` command-line interface works on the project root you run it in.

## Commands

| command | purpose |
| --- | --- |
| `handoff` | Generate a checkpoint into `docs/HANDOFF.md`; the previous checkpoint is archived to `docs/CHANGELOG.md` |
| `handoff inspect` | Read-only project + Git state inspection |
| `handoff config` | Show provider configuration status |
| `handoff mcp` | Run the Handoff MCP server over stdio |
| `handoff --version` | Print the installed version |

## Options

| option | effect |
| --- | --- |
| `--path DIR` | directory to operate on (default: current directory) |
| `--provider NAME` | AI provider (default: from config; no automatic fallback) |
| `--model NAME` | model for the provider (validated against the provider's catalog) |
| `--output PATH` | output path (default: `docs/HANDOFF.md`) |
| `--config PATH` | config file (default: `~/.handoff/config.json`) |
| `--dry-run` | build context + prompt and print, without calling AI or writing |
| `--commit` | after generation, commit **only** `docs/HANDOFF.md` (never pushes) |

## Examples

```bash
# inspect a project read-only
handoff inspect --path .

# preview without side effects
handoff --dry-run --provider claude

# target a specific provider and model
handoff --provider openai --model gpt-4o

# generate and commit only the handoff document
handoff --commit
```

## Exit behavior

Errors are deterministic and safe:

- unknown provider/model → validation error, nothing written;
- secret-like content → refused (never persisted, never printed);
- unsupported operation → capability/permission error;
- no network, no hidden fallback, no destructive Git operations.

## Integration surfaces

- **MCP**: `handoff mcp` serves JSON-RPC over stdio
  (`get_current_handoff`, `get_project_state`, `get_changelog`,
  `create_checkpoint`, `validate_checkpoint`, `get_capabilities`).
- **Skill**: install `skills/universal-handoff` into your agent's skill
  directory (see docs/SKILL_GUIDE.md).
- **Workflow**: the universal AI-to-AI workflow state machine is available as
  a Python API (see docs/WORKFLOW.md).