# Development F — Security Architecture

Status: **PASS** (Phase 2 security architecture foundation).

Security is an **independent security boundary** in Development F — distinct
from the Quality Guardian. The Quality Guardian audits quality; the Security
Gate verifies security and holds release-blocking authority. This document
defines the security layers, the Security Gate, the scanner evidence model, the
security principle, and the human approval boundary. It is consistent with the
existing hardening in `src/handoff_agent/` (`security.py`, `sandbox.py`,
`policy_engine.py`, `remote.py`, `quality_gate.py`) and the Company F flow.

## SECURITY LAYERS

### 1. Repository Security

- Secret scanning: scan source, tests, fixtures, docs, logs, config, generated
  output, staged files, and git diff/history for API keys, tokens, passwords,
  private keys, bearer credentials, hardcoded credentials, and sensitive
  environment values.
- Git history scanning: audit commit history for leaked secrets on a
  redacted/need-to-know basis.
- Sensitive files: `.env`, keystores, and secret-like content are excluded by
  the secret filter (`SecurityFilter`) before entering context or prompts.
- `.gitignore`: build caches (`__pycache__`, virtualenvs) and tooling artifacts
  are ignored; user-owned operating documents are never staged or committed.
- Repository permissions: least-privilege role-based capabilities; Git
  mutation restricted to allowlisted commands (`ALLOWED_GIT_COMMANDS`); push
  and destructive reset are forbidden from sandboxed surfaces.

### 2. Dependency Security

- Dependency audit: check dependencies for known vulnerabilities before release.
- Lockfile: release readiness requires a lockfile/version-pinned manifest.
- Known vulnerabilities: a dependency vulnerability with critical severity
  blocks release.
- Suspicious dependencies: non-stdlib imports are audited; source must remain
  stdlib-only except explicit, reviewed boundaries.

### 3. Application Security

Minimal coverage across the application surface (CLI, API, MCP, adapters,
providers, workflow, sync, remote, messaging, tool registry):

- Authentication: identity and role claims verified per action.
- Authorization: deny-by-default; most-restrictive-wins
  (REQUIRE_APPROVAL > DENY > ALLOW); capability checks per action.
- Input validation: structured input validated; invalid input rejected.
- Output handling: outputs bounded (max output bytes) and secret-scanned.
- Injection: no dynamic `eval`/`exec`; shell is never invoked
  (`shell=True` forbidden); subprocess bounded to allowlisted commands.
- XSS / CSRF: not applicable in the CLI/agent core; web-facing surfaces are
  out of Development F scope unless delegated to a reviewed adapter.
- SSRF: endpoint allowlist enforced; private/local hosts blocked on unlistened
  endpoints; registry rejects non-allowlisted endpoints.
- Path traversal: sandbox resolves all paths under its root; traversal out of
  root rejected (`SandboxPathError`).
- File upload: adoption/checkpoint ingestion is filtered by the secret filter
  and rejects symlink escapes.
- Rate limiting: bounded retries and deterministic timeouts on outbound calls.
- Session/token security: approval tickets bind to the exact request signature;
  token reuse/forgery rejected.

### 4. Infrastructure Security

- Environment variables: only allowlisted env vars are exposed to processes.
- Secrets: never logged, never echoed in CLI output, never placed in
  checkpoints, reports, or telemetry.
- Database access: project state persisted to an allowlisted, git-backed
  checkpoint store; no external DB in the core.
- Storage: filesystem access sandboxed to project root.
- API exposure: transport enforces TLS verification
  (`tls_verification=True`); insecure TLS contexts are forbidden.
- HTTPS: outbound requests are HTTPS; unverified/`NONE` verification modes are
  rejected.
- Security headers: delegated to reviewed web adapters (out of core scope).
- Deployment configuration: release requires Quality PASS, Security Gate PASS,
  Deployment Check PASS, and satisfied human approval; dry-run is zero-write /
  zero-network.

### 5. Agent Security

- Shell permissions: no shell execution; allowlisted commands only.
- File permissions: sandboxed root; no unrestricted filesystem access.
- Network permissions: allowlisted hosts only; `SandboxNetworkError` on exit
  to non-allowlisted destinations.
- Secret exposure: secret-like content filtered from agent context and prompts.
- Prompt injection: static context is built from verified project state;
  tool output is bounded and scanned; agent output is never blindly trusted.
- Agent-to-agent trust: every handoff is verified (checkpoint validity, Git
  state, non-staleness, ownership) before acceptance.
- Destructive commands: destructive actions require human approval; approval
  binds to the exact request signature.
- Human approval boundaries: deployment, release, and production approvals are
  human-only and cannot be auto-bypassed by AI.

## SECURITY GATE

```text
Quality Guardian
    ↓
Quality Audit
    ↓
Security Gate
    ↓
Security Decision
```

The Quality Guardian finishes its quality audit first; the Security Gate then
verifies security independently and issues a security decision. The gate
outputs are:

```text
PASS
BLOCK
NEEDS_REVIEW
```

Scanner evidence is reported with an explicit status. Missing or failed scans
are NEVER treated as safe:

```text
PASS
FAIL
ERROR
NOT_APPLICABLE
NOT_SCANNED
```

Rule: `NOT_SCANNED ≠ PASS`. If an expected scan did not run (or errored), the
gate must not issue PASS on that evidence channel; it must be
`BLOCK`/`NEEDS_REVIEW` until the evidence is present and passing.
`ERROR` also blocks release (evidence unavailable is not evidence of safety).

## SECURITY PRINCIPLE

Evidence is higher authority than agent assumption. The loop is:

```text
AI
↓
Analysis

CLI / Scanner
↓
Evidence

AI
↓
Interpretation / Fix Recommendation

Scanner
↓
Verification
```

An agent may propose an interpretation or a fix, but the verification comes
from an actual scanner/execution — the final decision rests on evidence, not
description.

## Implementation mapping (existing code)

| Layer | Existing implementation |
|---|---|
| Repository Security | `handoff_agent.security.SecurityFilter`; `git_helper.GitRunner` allowlist |
| Dependency Security | `quality_gate` release checklist (lockfile, vulnerable dependencies) |
| Application Security | `sandbox.Sandbox`, `policy_engine.PolicyEngine` (deny-by-default, `require_approval_for_destructive`), `remote.EndpointRegistry`/SSRF guard |
| Infrastructure Security | `remote.RemoteTransport` TLS verification; env allowlist; secret-free telemetry/checkpoints |
| Agent Security | handoff `verify_before_continue`, lease/ownership, `HumanApprovalRequiredError` on unapproved acceptance |

---

# PHASE 2 CHECKPOINT — PASS

- Security boundary clear: Security Gate is an independent boundary with
  release-blocking authority, distinct from the Quality Guardian.
- Scanner evidence model clear: PASS / FAIL / ERROR / NOT_APPLICABLE /
  NOT_SCANNED; `NOT_SCANNED ≠ PASS` enforced.
- Quality ≠ Security: distinct gates with distinct responsibilities.
- Agent security covered: shell/file/network/secret/prompt-injection/trust/
  destructive-command/human-approval boundaries all defined.
- Human approval boundary covered: deployment/release/production approvals are
  human-only.
- Release blocking mechanism clear: Quality PASS → Security Gate → Deployment
  Check → Human Release Approval → deploy.

---

# PHASE 2 TEST

### Coverage Test — PASS
All five security layers (Repository, Dependency, Application, Infrastructure,
Agent) plus the Security Gate and evidence model are present.

### Boundary Test — PASS
Quality Guardian = quality audit; Security Gate = security verification +
release-blocking. No role overlap. (Cross-referenced against
`docs/DEVELOPMENT-F.md` Section F.)

### Failure Test — PASS
Documented rule: FAIL, ERROR, and NOT_SCANNED are never automatically treated
as safe; only PASS counts, and only with real scanner evidence.

### Documentation Consistency Test — PASS
- Consistent with `docs/DEVELOPMENT-F.md` (Security Gate appears in the org and
  flow before Deployment Check; human gates preserved).
- Consistent with the existing implementation described above.

### Git Test — PASS
`git diff --check` clean; only `docs/SECURITY-ARCHITECTURE.md` staged.

---

# PHASE 2 FINAL CHECKPOINT

```text
PHASE: 2
STATUS: PASS

DOCUMENT:
docs/SECURITY-ARCHITECTURE.md

CHECKS:
- Security Layers: PASS
- Security Boundary: PASS
- Scanner Evidence Model: PASS
- Agent Security: PASS
- Human Approval: PASS
- Quality/Security Separation: PASS
- Cross-Document Consistency: PASS
- Git Integrity: PASS

FINDINGS:
None.

REPAIRS:
None required.

RETEST:
Coverage, boundary, failure, consistency, and git tests all PASS on first run.

CONCLUSION:
Security architecture foundation established as an independent boundary,
consistent with the hardened implementation and Development F architecture.

NEXT:
PHASE 3
```