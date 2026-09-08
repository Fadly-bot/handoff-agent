# Universal Handoff Protocol — State Schema Reference

This document is a reference for agents using the Universal Handoff Skill.

## Protocol Identity

- **Name:** `universal-handoff-protocol`
- **Version:** `1`
- **Canonical string:** `universal-handoff-protocol/1`

## Canonical State Schema

The canonical state is a JSON object embedded in the `handoff-protocol`
fenced code block inside `docs/HANDOFF.md`.

```jsonc
{
  "protocol": {
    "name": "universal-handoff-protocol",
    "version": 1
  },
  "identity": {
    "id": "64-char lowercase hex SHA-256",
    "generated_at": "ISO-8601 timestamp with seconds precision",
    "sequence": 0
  },
  "metadata": {
    "project": {
      "name": "project name",
      "type": "Python"
    },
    "objective": "Current objective description",
    "agents": ["agent-name"]
  },
  "state": {
    "objective": "Current objective",
    "completed": ["task1", "task2"],
    "in_progress": ["task3"],
    "next_actions": ["task4", "task5"],
    "decisions": ["decision1"],
    "constraints": ["constraint1"]
  },
  "validation": {
    "status": "pending",
    "checks": ["check1", "check2"]
  },
  "git": {
    "branch": "main",
    "head": "abc123...",
    "clean": true,
    "status": "clean"
  },
  "artifacts": ["file1", "file2"],
  "risks": ["risk1"]
}
```

## Identity Hash Computation

The identity hash is a SHA-256 (lowercase hex, 64 characters) derived from:

1. `state.objective`
2. `state.completed` (sorted)
3. `state.in_progress` (sorted)
4. `state.next_actions` (sorted)
5. `state.decisions` (sorted)
6. `state.constraints` (sorted)
7. `metadata.project`

The hash is computed on the canonical JSON of these fields (sorted keys,
compact separators). The hash deliberately EXCLUDES `generated_at` and
`sequence`, so the same logical state always has the same identity
regardless of timestamp or agent.

## Validation Status Values

| Value | Meaning |
| --- | --- |
| `pending` | Validation has not been performed yet |
| `passed` | All validation checks passed |
| `failed` | One or more validation checks failed |

## Git State

The `git` field is optional but recommended. It captures:

- `branch`: Current branch name (string or null if detached)
- `head`: Current HEAD commit hash (string)
- `clean`: Whether the working tree is clean (boolean)
- `status`: Human-readable status string

## Backward Compatibility

- Checkpoints without a `handoff-protocol` block are treated as legacy.
- Legacy checkpoints are still valid for reading and context.
- Agents should alert users when encountering legacy checkpoints and
  recommend creating a proper protocol checkpoint.
