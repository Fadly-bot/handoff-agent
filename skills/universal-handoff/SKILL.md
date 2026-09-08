# Universal Handoff Skill

Portable, provider-independent instructions for AI agents to work with the
Universal Handoff Protocol.

## Overview

This skill teaches any AI agent — Claude, OpenAI-compatible, Qwen, DeepSeek,
Gemini, Kimi, GLM, Grok, Manus, or any other — how to read, verify, create,
and maintain Handoff checkpoints in `docs/HANDOFF.md`.

The skill is provider-agnostic: it does not reference any specific AI provider,
API key, or model. It is self-contained and can be installed on any agent that
supports skills, instruction injection, or system prompts.

## Skill Identity

- **Name:** `universal-handoff`
- **Version:** 1.0.0
- **Protocol:** `universal-handoff-protocol/1`
- **License:** MIT

## Read-Before-Continue Workflow

Before starting any work in a project that has a `docs/HANDOFF.md`:

1. Read `docs/HANDOFF.md` completely.
2. Parse the `handoff-protocol` JSON block if present.
3. Identify the `state.objective`, `state.completed`, `state.in_progress`,
   and `state.next_actions`.
4. Confirm the `identity.id` matches the recomputed identity from the state.
5. If the file has no protocol block (legacy), treat the entire content as
   context — do not assume structured fields.
6. Report any validation errors to the user before proceeding.

Do NOT proceed with work until the checkpoint has been read and validated.

## Verify-Before-Trust Workflow

Never trust a checkpoint blindly:

1. Verify `protocol.name` equals `universal-handoff-protocol`.
2. Verify `protocol.version` is supported (currently `1`).
3. Verify `identity.id` by recomputing the SHA-256 from the state fields.
4. Check `validation.status` — if `"failed"`, report the issues.
5. Check `git.head` against the current repository HEAD — if they differ, the
   checkpoint may be stale; alert the user.
6. Check `metadata.project.name` matches the current project.

If any check fails, stop and ask the user how to proceed. Do NOT silently
ignore verification failures.

## Milestone Checkpoints Workflow

At meaningful milestones in your work, create a new checkpoint:

1. Gather the current state:
   - `objective`: what you are working on
   - `completed`: tasks finished in this session
   - `in_progress`: tasks currently being worked on
   - `next_actions`: recommended next steps
   - `decisions`: architectural or design decisions made
   - `constraints`: limitations or rules observed
2. Build the canonical JSON with the correct protocol envelope.
3. Compute the identity hash deterministically from the state.
4. Serialize as a `handoff-protocol` fenced block inside `docs/HANDOFF.md`.
5. Preserve any human-readable sections above/below the protocol block.
6. Write atomically — the file must never be partially written.

When creating a milestone checkpoint, prefer frequency over size: small,
frequent checkpoints are better than large, infrequent ones.

## AI Switching Workflow

When a different AI agent takes over a project:

1. The new agent MUST execute the **Read-Before-Continue** workflow above.
2. The new agent MUST execute the **Verify-Before-Trust** workflow above.
3. If the checkpoint was written by a different agent, the new agent should:
   - Report which agent wrote it (`metadata.agents`).
   - Compare `git.head` with the current HEAD.
   - If stale, alert the user and offer to refresh.
4. Continue work from `state.next_actions`.
5. If objectives have changed, update `state.objective` at the next milestone.

Never assume the previous agent's work is complete without verification.

## HANDOFF.md Interpretation

`docs/HANDOFF.md` is the canonical checkpoint file.

### Structure

The file contains two parts:

1. **Human-readable markdown** — project context, prose descriptions, notes.
2. **Machine-readable protocol block** — a fenced JSON block:

```
```handoff-protocol
{ "protocol": ..., "identity": ..., "metadata": ..., "state": ..., ... }
``` _(closing fence)_
```

### Parsing Rules

- The protocol block is optional for backward compatibility.
- If present, it MUST be the first fenced code block with info-string
  `handoff-protocol`.
- The JSON inside must conform to the Canonical State Schema (see protocol
  reference).
- If no block is present, the file is a legacy checkpoint: read it as context
  but do not rely on structured fields.

### Persistence Rules

- `docs/HANDOFF.md` holds the **current** checkpoint only.
- When a new checkpoint replaces a different one, the previous content is
  archived to `docs/CHANGELOG.md` before the overwrite.
- NEVER append to `docs/HANDOFF.md`.
- NEVER modify any file other than `docs/HANDOFF.md`.

## CHANGELOG.md Interpretation

`docs/CHANGELOG.md` is the append-only history of replaced checkpoints.

### Structure

```
# Handoff Checkpoint History

## Checkpoint 2025-01-01T00:00:00+00:00

- project: my-project
- branch: main
- commit: abc123

<archived content>
---
```

### Reading Rules

- Entries are ordered oldest-first.
- Each entry starts with `## Checkpoint <ISO timestamp>`.
- Metadata lines are project, branch, and commit.
- The body is the archived `docs/HANDOFF.md` content from that point.
- If the changelog has no entries, it is empty (or does not exist).

## State Schema Interpretation

The canonical state schema defines these top-level fields:

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `protocol` | object | yes | Protocol name and version |
| `identity` | object | yes | Deterministic id, timestamp, sequence |
| `metadata` | object | yes | Project info, objective, agents |
| `state` | object | yes | Completed, in-progress, next actions, decisions, constraints |
| `validation` | object | no | Validation status and checks |
| `git` | object | no | Branch, HEAD, clean status |
| `artifacts` | array | no | List of artifacts produced |
| `risks` | array | no | List of known risks |

### Identity Fields

| Field | Description |
| --- | --- |
| `identity.id` | SHA-256 hex (64 chars) derived deterministically from state |
| `identity.generated_at` | ISO-8601 timestamp of checkpoint creation |
| `identity.sequence` | Monotonic sequence number within the session |

### State Fields

| Field | Type | Description |
| --- | --- | --- |
| `state.objective` | string | Current objective |
| `state.completed` | string[] | Tasks completed in this session |
| `state.in_progress` | string[] | Tasks currently in progress |
| `state.next_actions` | string[] | Recommended next steps |
| `state.decisions` | string[] | Decisions made during this session |
| `state.constraints` | string[] | Constraints observed |

## Capability Awareness

Agents interacting with Handoff must be aware of their capabilities:

### Read Capabilities

| Capability | Description |
| --- | --- |
| `project.inspection` | Read project metadata and type detection |
| `git.inspection` | Read Git repository state |
| `checkpoint.read` | Read the current Handoff checkpoint |
| `changelog.read` | Read the changelog history |

### Write Capabilities

| Capability | Description |
| --- | --- |
| `checkpoint.create` | Create a new checkpoint |
| `checkpoint.update` | Update an existing checkpoint |

### Validation Capability

| Capability | Description |
| --- | --- |
| `validation` | Validate a checkpoint against the protocol |

An agent MUST NOT perform an action for which it lacks the corresponding
capability. If a required capability is missing, report the limitation and
request the capability from the system.

## Safety Rules

These rules are mandatory and cannot be overridden:

1. **No secrets.** Never read, write, log, or include API keys, tokens,
   passwords, private keys, `.env` contents, or credentials in any
   checkpoint, prompt, or communication.

2. **No filesystem escape.** Never read or write files outside the detected
   project root. Path traversal (`..`) and symlink escapes must be rejected.

3. **No unvalidated writes.** Never write to `docs/HANDOFF.md` without
   running the checkpoint validation first. Refuse to persist checkpoints
   that contain secrets or fail schema validation.

4. **No destructive Git operations.** Never run `push`, `reset`, `clean`,
   `checkout`, `restore`, `merge`, `rebase`, `stash`, `fetch`, `pull`,
   `filter-branch`, or any mutating Git command except `add` and `commit`
   scoped to `docs/HANDOFF.md`.

5. **No arbitrary shell execution.** Never execute shell commands that are
   not strictly required for the approved operations.

6. **Read before continue.** Always read and validate the current checkpoint
   before starting work in a project.

7. **Verify before trust.** Always verify the identity hash and staleness of
   a checkpoint before trusting its contents.

8. **Atomic writes.** All checkpoint writes must be atomic (write to temp,
   then rename) to prevent partial/corrupt files.

9. **Error transparency.** Report all validation errors, capability denials,
   and safety violations clearly — never suppress or hide them.

10. **Provider independence.** Never embed provider-specific configuration,
    API endpoints, or model identifiers in checkpoints or skill instructions.

## Provider-Independent Instructions

This skill does NOT reference:

- Any specific AI provider (Claude, OpenAI, Qwen, DeepSeek, Gemini, etc.)
- Any API key environment variable or secret
- Any specific model identifier
- Any provider-specific API endpoint or SDK

The skill works through generic system-prompt injection. It does not require
any specific integration mechanism beyond the ability to receive and follow
text-based instructions.

## Portable Skill Package

This skill package is designed to be portable across agents:

- All instructions are in Markdown (human and machine readable).
- No binary files.
- No external dependencies.
- No embedded secrets.
- File paths are relative to the skill root.
- The package can be copied, symlinked, or distributed as-is.

### Skill Package Structure

```
skills/universal-handoff/
  SKILL.md       # This file — main instructions
  PROTOCOL.md    # State schema reference
```
