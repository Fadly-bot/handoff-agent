# Adapters

The adapter layer (`src/handoff_agent/adapters/`) is the universal interface
between any actor and the Handoff state. It turns five operations into a
capability-gated, security-enforcing contract that every adapter (file, CLI,
API, and future ones) honors identically.

## Universal adapter interface

Every adapter implements:

| Operation             | Method                   | Capability gating                           |
|-----------------------|--------------------------|---------------------------------------------|
| Read handoff          | `read_handoff()`         | `checkpoint.read`                           |
| Write checkpoint      | `write_checkpoint(content, expected_base=None)` | `checkpoint.create` / `checkpoint.update` |
| Project state         | `project_state()`        | `project.inspection`, `git.inspection`      |
| Validation            | `validate_checkpoint()`  | `validation`                                |
| Changelog             | `read_changelog()`       | `changelog.read`                            |

Plus lifecycle (`start`/`stop`/`status`), capability negotiation
(`negotiate`), and grant checks (`can`, `assert_can`).

A write observed a base identity? Pass `expected_base=` — if the checkpoint
moved on since that observation, the write is refused. That is the stale-writer
guard.

## The three built-in adapters

### File adapter (`file`)

Direct, safe access to `docs/HANDOFF.md` and `docs/CHANGELOG.md` inside a
project root. Recommended for local, single-writer workflows and for agents
that have local repository access (OpenCode, Cline, Claude Code with a local
checkout, …).

- Writes go through the `CheckpointManager` lifecycle (atomic, prev archived).
- Secret-looking content is refused on both read and write.
- All paths are resolved inside the project root; traversal and symlink
  escapes are rejected.
- Git access is read-only (`inspect_repository`).

### CLI adapter (`cli`)

Read-only CLI integration. Runs a fixed command
(`python -m handoff_agent inspect`) and never opens a shell. Whitelisted,
deterministic — nothing arbitrary runs. Use for environments where only a CLI
binary is reachable.

### API adapter (`api`)

Optional JSON integration (opt-in). HTTPS only, except loopback
(`localhost` / `127.0.0.1` / `::1`); base path must be `""`, `/api`, or
`/handoff`. Authentication is a Bearer token read from the environment at
request time (`HANDOFF_API_KEY` by default) — never stored or logged. Response
size is capped (8 MB). See [AI_INTEGRATION.md](AI_INTEGRATION.md).

## Capability boundaries

- Read-only adapters hold `checkpoint.create`/`checkpoint.update` removed —
  writes are refused by the boundary, not by convention.
- Negotiation is deterministic: `negotiate(declared)` returns only the
  intersection with the adapter's boundary, and unknown capabilities raise.
- No adapter silently falls back to another type; an unknown name is a
  configuration error.

## Security invariants

- No secret propagation: status, state, and reports never include key values.
- No arbitrary filesystem access: containment + symlink protection.
- No unrestricted Git operations: read-only inspection only.
- Adapter failure isolation: one adapter's error never affects the registry or
  other adapters.

## Generic integration guide (hands-on)

```python
from handoff_agent.adapters import create_adapter

adapter = create_adapter("file", project_root=".")
adapter.start()

state = adapter.project_state()          # read-only project + Git snapshot
content = adapter.read_handoff()         # None if no checkpoint yet
result = adapter.read_changelog()

# Write only if you hold the capability, and pass expected_base to
# guard against stale writers:
adapter.write_checkpoint(new_markdown, expected_base=observed_identity)

validation = adapter.validate_checkpoint()  # protocol + schema + identity
```

## Registering a new adapter

```python
from handoff_agent.adapters import register_adapter, list_adapters, create_adapter

register_adapter("my", MyAdapter)        # duplicates are rejected
create_adapter("my", ...)                # no automatic fallback
list_adapters()                          # -> ('api', 'cli', 'file', 'my')
```

## Conformance

Any adapter — built-in or custom — must pass the
[Universal Conformance Suite](CONFORMANCE.md). Run it with
`run_conformance_suite(adapter, {"repo": ".", "tmp": "/tmp/x"})`; a `clean`
report is the release gate.