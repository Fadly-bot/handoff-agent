# Security Model

The Handoff Agent treats the AI response and every provider as **untrusted**.
Security is enforced structurally, never by convention.

## Credentials & isolation

- Providers read API keys **only** from environment variables
  (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`,
  `PERPLEXITY_API_KEY`, `XAI_API_KEY`, `DEEPSEEK_API_KEY`,
  `DASHSCOPE_API_KEY`, `MOONSHOT_API_KEY`, `ZHIPU_API_KEY`, `MANUS_API_KEY`).
- Only the env-var **name** is ever represented in reports, audits, or
  errors — never the value. `credential.isolation` is a checked integration.
- No credential is ever written to `docs/HANDOFF.md`, `docs/CHANGELOG.md`,
  config, logs, or error output.

## Secret filtering

Any content that looks like a hardcoded secret (API keys, tokens, passwords,
private keys) is **rejected on read and write**: adapter reads and writes
refuse secret-like content with a safe error (`AdapterContentError`), and
secret-tainted history is never archived.

## Filesystem containment

- Only the project-relative handoff/changelog paths may be written
  (`docs/HANDOFF.md`, `docs/CHANGELOG.md` by default).
- Final paths are resolved and verified inside the detected project root;
  symlink escapes are rejected.
- All writes are atomic (temp file + rename): a reader never sees a
  half-written checkpoint, and a failed write leaves the previous checkpoint
  intact.

## Git safety

- All Git inspection is **read-only**; network Git commands are forbidden.
- The tool can never reset, clean, checkout, stage, or delete project files.
- The only Git write is an optional `git add -- docs/HANDOFF.md` + commit,
  gated on explicit `--commit`.

## Capability & permission boundaries

- Every adapter/agent holds an explicit capability grant enforced by a
  `PermissionBoundary`; operations beyond the grant raise
  `AdapterPermissionError`.
- Platform grants are the **actual** capabilities, not assumed ones
  (read-only platforms can never create/update checkpoints).
- There is **no automatic fallback**: unknown platforms/providers/adapter
  names raise, they never silently substitute.

## Transport

- The HTTP API adapter is HTTPS-only (loopback is an explicit opt-in), size
  limited, and Bearer-auth via env var.
- The MCP server runs over stdio JSON-RPC; denied capabilities map to
  `-32001`. Skills are static, provider-independent instructions.

## Workflow & audit

- Handoff acceptance requires an explicit token and human approval.
- Continuation requires a validated, non-stale, conflict-free checkpoint.
- Every operation appends to an immutable audit trail; recovery is
  non-destructive and never rewrites project files.