# Handoff Agent

AI-powered project handoff generator. Detects a project, builds a secure
read-only snapshot of its state, and generates `docs/HANDOFF.md` — the current
checkpoint for the next AI (or human) to pick up.

`docs/HANDOFF.md` always holds the **current** checkpoint only. Each time a new
handoff replaces it, the previous checkpoint is archived to `docs/CHANGELOG.md`.

## Features

- Project detection + read-only Git inspection.
- Security-filtered context snapshot (never reads or embeds secrets).
- Deterministic prompt building with a restricted, safe file scope.
- Four pluggable AI providers (Claude, OpenAI, Qwen, DeepSeek).
- Safe atomic persistence into `docs/HANDOFF.md`.
- Optional commit that stages **only** `docs/HANDOFF.md`.
- Dry-run that builds context and prompt without calling any AI or writing files.

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
  "version": "0.1.0",
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