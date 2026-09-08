# Universal Handoff Skill — Installation Guide

The portable skill package lives in `skills/universal-handoff/`. It teaches
any skill-capable AI agent to interact with Handoff correctly — same protocol,
same verification rules, no agent-specific code.

## What the skill provides

- `SKILL.md` — the portable skill instructions loaded by the agent.
- `PROTOCOL.md` — the protocol reference embedded for the agent.

The skill guides agents through the invariant workflow:

1. **Read first** — open `docs/HANDOFF.md`, parse the `handoff-protocol`
   block.
2. **Verify before trust** — validate the schema and verify the checkpoint
   identity.
3. **Continue, then checkpoint** — record progress as a new checkpoint with
   the machine-readable block, archiving the previous one.

## Installing on a skill-capable agent

Give the agent pointer to `skills/universal-handoff/SKILL.md` (a webserver
URL, a packaged skill, or a local path):

- **Claude / Claude Code** — `claude` installs skills from a
  `skills/` directory; copy the folder in and reload.
- **Gemini (AI Studio)** — attach `SKILL.md` (and optionally `PROTOCOL.md`)
  as a prompt reference.
- **ChatGPT (custom actions / files)** — attach the skill package as a file
  or knowledge source; grant file access to the repository for write
  operations.
- **Grok / Manus** — provide the skill text in the session/context as
  instructions.
- **Generic agents** — paste the contents of `SKILL.md` verbatim into the
  system prompt; no syntax or language is required beyond the invariant.

## Verifying the install

After install, ask the agent to open the checkpoint and read the protocol
block in `docs/HANDOFF.md`. The agent should respond by paraphring the
*current objective* from the block — not by guessing. Then run
`handoff inspect` to confirm the project state it sees matches.

## Testing with the conformance suite

Set up file access for the agent and run
`run_conformance_suite(create_adapter("file", project_root="."), ...)`. A
`clean` report means the agent sees consistent, valid state.

## Note

The skill is guidance, not magic: it cannot grant capabilities the platform
lacks (a read-only platform stays read-only). It exists to make the *workflow*
identical across platforms.