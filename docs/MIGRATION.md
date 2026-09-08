# Migration Guide

Handoff is designed for migration as data — you never lose a checkpoint.

From v0.1.0 (which wrote legacy `docs/HANDOFF.md`) through v0.4.0 (protocol
v1, adapters, platforms, MCP, skill, universal workflow), all checkpoints are
forward-compatible. Every new version reads older files and upgrades them in
place.

## From v0.1.0 (legacy checkpoint)

Legacy `docs/HANDOFF.md` is a plain Markdown file with **no** machine-readable
`handoff-protocol` block.

**What you must do**

1. Upgrade the package (`bash install.sh` over your existing `~/.handoff`).
2. Run `handoff` once. The protocol layer detects the legacy file, and the
   first write emits the current checkpoint as a canonical,
   machine-readable `handoff-protocol` block.
3. (Recommended) Run `handoff inspect` to confirm your project state is
   detected and the checkpoint validates.

**What happens automatically**

- Legacy files remain readable forever; they validate as *legacy* (no
  machine-readable block) until overwritten.
- Config `{"version": "0.1.0"}` is accepted; unknown keys are ignored and
  preserved on upgrade.

## From v0.2.0 (protocol v1 era)

v0.2.0 already writes the canonical `universal-handoff-protocol` v1 block, so
0.2.0 checkpoints are fully interoperable with 0.4.0:

**What you must do**

1. Upgrade the package.
2. Nothing else — your `docs/HANDOFF.md` keeps working.

**What changes**

- Config template version becomes `0.4.0` (your existing config keeps working;
  new keys/providers are additive).
- MCP `serverInfo.version` moves to `0.4.0`.
- New capabilities (`capability.negotiation`, permission boundaries), the
  adapter/platform surfaces, and the universal workflow state machine are
  additive; no existing call is removed.

## Verification

Run the conformance suite against your repository:

```python
from handoff_agent.adapters import create_adapter
from handoff_agent.conformance import run_conformance_suite

adapter = create_adapter("file", project_root=".")
adapter.start()
report = run_conformance_suite(adapter, {"repo": ".", "tmp": "/tmp/ext"})
assert report.clean, report.failed
```

## Rollback

Downgrading to an earlier version is not supported once a checkpoint enters
protocol v1 — the canonical block is forward-only. If you must go back, take a
copy of `docs/HANDOFF.md` (and `docs/CHANGELOG.md`) first; both remain plain
readable Markdown.