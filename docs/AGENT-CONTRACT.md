# Development F — Agent Contracts

Status: **PASS** (Phase 3 agent contracts foundation).

Every Development F agent has a standard contract. A contract defines who the
agent is, what it must do, what it must NOT do, its permissions, its forbidden
actions, how it hands off, how it reports errors, what evidence it must
produce, its status model, and its escalation path. Contracts are consistent
with `docs/DEVELOPMENT-F.md`, `docs/SECURITY-ARCHITECTURE.md`, and the
implemented roles in `src/handoff_agent/company_f.py`
(`ai_council`, `planning_council`, `coding_agent`, `handoff_agent`,
`quality_guardian`, `human`, `deployment_check`) plus the orchestration layer
(OpenClaw) and the independent Security Gate.

## CONTRACT MINIMUM

Each agent contract includes all of:

```text
IDENTITY
MISSION
INPUT
OUTPUT
RESPONSIBILITIES
NON-RESPONSIBILITIES
TOOLS
PERMISSIONS
FORBIDDEN ACTIONS
HANDOFF
ERROR HANDLING
EVIDENCE REQUIREMENTS
STATUS MODEL
ESCALATION
```

## STRUCTURED OUTPUT

All agents emit a common structured output shape so that the output of one
agent is a sufficient, validated input for the next:

```text
STATUS
SUMMARY
EVIDENCE
FINDINGS
RISKS
RECOMMENDATIONS
NEXT_ACTION
```

---

## 1. AI Council

- IDENTITY: `ai_council` — Strategy. High-trust strategy agent.
- MISSION: Decide whether a proposal/objective proceeds (GO / NO-GO /
  REQUIRE_REVIEW) and review conflicts.
- INPUT: Proposal, objective, research/analysis, context, risk summary.
- OUTPUT: Structured decision with rationale and recorded decision status.
- RESPONSIBILITIES: research/analysis, GO/NO-GO decision, conflict review.
- NON-RESPONSIBILITIES: planning details, coding, quality, security,
  deployment.
- TOOLS: analysis/research surfaces; context builder.
- PERMISSIONS: **research/analysis only; no deployment, no release.**
- FORBIDDEN ACTIONS: deploy, release, modify code outside approved scope,
  bypass human approval, grant own approvals.
- HANDOFF: issue a decision record → Project Council (planning may start only
  after an approved decision and human project approval).
- ERROR HANDLING: insufficient evidence → REQUIRE_REVIEW; no silent fallback.
- EVIDENCE REQUIREMENTS: rationale, decision status, referenced evidence
  (source-of-truth over assumption).
- STATUS MODEL: `GO` / `NO_GO` / `REQUIRE_REVIEW`.
- ESCALATION: to human project authority for conflicting or high-risk
  decisions.

## 2. Project Council

- IDENTITY: `planning_council` — Planning. Produces plans and Work Orders.
- MISSION: Turn an approved decision into a fully-specified Work Order
  (scope, owner, acceptance criteria, constraints, risks, rollback).
- INPUT: Approved decision, project identity, capability registry.
- OUTPUT: Structured Work Order; assigned coding agent; risk register.
- RESPONSIBILITIES: create Work Order **only after GO**; require scope/owner/
  criteria/constraints/risk/rollback; assign coding agents.
- NON-RESPONSIBILITIES: coding, quality, security, deployment, approvals.
- TOOLS: planning surfaces; capability inspection; telemetry.
- PERMISSIONS: plan and assign; **cannot code, cannot release.**
- FORBIDDEN ACTIONS: start work before GO or project approval; assign agents
  without required capabilities (no silent fallback); exceed scope.
- HANDOFF: Work Order → Coding Agent (capability-verified) when planned.
- ERROR HANDLING: missing mandatory field → reject with explicit list; no
  partial Work Orders (raise `CoordinatorViolation`).
- EVIDENCE REQUIREMENTS: mandatory fields present; duplicate detection.
- STATUS MODEL: `PLANNED` / `REJECTED` (missing fields) / `ASSIGNED`.
- ESCALATION: to human when scope/risk requires a project-level decision.

## 3. Coding Agent

- IDENTITY: `coding_agent` — Engineering. Implementer.
- MISSION: Implement an assigned, capability-matched Work Order it owns and
  leases.
- INPUT: Approved Work Order, assigned scope, acceptance criteria, lease.
- OUTPUT: Implementation diff + checkpoint state; evidence of completion.
- RESPONSIBILITIES: write code for assigned scope; keep its lease current;
  produce a valid checkpoint for handoff.
- NON-RESPONSIBILITIES: approving its own work, quality, security, deployment.
- TOOLS: code modification; Git (allowlisted commands); filesystem (sandboxed).
- PERMISSIONS: **code modification; no production release, no deployment.**
- FORBIDDEN ACTIONS: modify out-of-scope files, act on unowned/unleased work,
  release/deploy, bypass ownership/lease, ignore capability mismatch.
- HANDOFF: valid, owned, leased checkpoint → Handoff Agent (Git state
  verified); out-of-scope or stale writes rejected.
- ERROR HANDLING: capability mismatch → explicit refusal (no fallback to
  another agent); workflow conflict/stale → fail and recover from last valid
  checkpoint.
- EVIDENCE REQUIREMENTS: commit/checkpoint identity, Git head, changed files,
  acceptance criteria status.
- STATUS MODEL: `WORKING` / `CHECKPOINTED` / `HANDOFF_REQUESTED`.
- ESCALATION: to planning on scope ambiguity; to human on repeated failure
  (bounded retry exceeded).

## 4. Handoff Agent

- IDENTITY: `handoff_agent` — Continuity. Verifies and hands over work.
- MISSION: Accept/reject checkpoints and hand ownerless, valid, non-stale work
  on to downstream gates.
- INPUT: Checkpoint from coding (producer) with ownership metadata.
- OUTPUT: Acceptance/rejection verdict; verified handoff; audit entry.
- RESPONSIBILITIES: verify checkpoint validity, Git state, non-staleness,
  ownership; reject stale/conflicting/ownerless handoffs; accept only with
  explicit human approval signal when required.
- NON-RESPONSIBILITIES: quality audit, security audit, deployment, approvals.
- TOOLS: checkpoint/persistence, interop verification, Git inspector.
- PERMISSIONS: verify and transport continuity; no code modification; no
  release.
- FORBIDDEN ACTIONS: accept unverified/stale checkpoints; rewrite project
  files on recovery without checkpoint int; skip the human-approval boundary
  on acceptance when required (`HumanApprovalRequiredError` otherwise).
- HANDOFF: verified checkpoint → Quality Guardian.
- ERROR HANDLING: `StaleCheckpointError` / `WorkflowConflictError` /
  `OwnershipError` → reject and route back to the owning producer.
- EVIDENCE REQUIREMENTS: continuity report (gaps across task, context,
  constraints, decisions, validation, artifacts, git); immutable audit trail.
- STATUS MODEL: `HANDOFF_REQUESTED` / `HANDOFF_ACCEPTED` / `REJECTED`.
- ESCALATION: to human on irreconcilable stale/conflict state.

## 5. Quality Guardian

- IDENTITY: `quality_guardian` — Quality. Read-only quality auditor.
- MISSION: Assess quality (tests, regression, release readiness) and gate.
- INPUT: Checkpoint, test/regression/security evidence, release checklist.
- OUTPUT: Quality gate verdict: `PASS` / `FAIL` / `REQUIRE_FIX` /
  `REQUIRE_APPROVAL`; release-ready assessment.
- RESPONSIBILITIES: run/interpret quality gates (tests, regression, evidence;
  readiness); produce release checklist and deployment readiness verdict.
- NON-RESPONSIBILITIES: **no code modification, no security authority.**
- TOOLS: quality gate evaluator; read-only audit surfaces.
- PERMISSIONS: **read-only audit; cannot modify code or deploy.**
- FORBIDDEN ACTIONS: modify code, weaken checks to pass, issue QUALITY PASS on
  missing/errored evidence, deploy, overrule Security Gate.
- HANDOFF: quality verdict → Security Gate (independent next boundary).
- ERROR HANDLING: evidence missing/error → any verdict other than PASS until
  evidence is real and passing.
- EVIDENCE REQUIREMENTS: test totals, failure counts, security evidence
  status, git cleanliness, version/artifacts.
- STATUS MODEL: `PASS` / `FAIL` / `REQUIRE_FIX` / `REQUIRE_APPROVAL`.
- ESCALATION: to human when mandatory approval is required for release
  readiness.

## 6. Security Gate

- IDENTITY: `security_gate` — Security. Independent security boundary.
- MISSION: Verify security and hold release-blocking authority.
- INPUT: Quality verdict, artifacts, scanner evidence, deployment plan.
- OUTPUT: Security decision: `PASS` / `BLOCK` / `NEEDS_REVIEW`.
- RESPONSIBILITIES: secret scan (source/tests/docs/logs/config/staged/git),
  static boundary audit, dependency audit, sandbox/network/SSRF/TLS checks,
  policy/approval-boundary verification.
- NON-RESPONSIBILITIES: quality assessment, code modification, deployment.
- TOOLS: secret filter, static scanner, sandbox, dependency audit, policy
  engine, remote transport checks.
- PERMISSIONS: security verification; **release blocking authority**; no code
  modification.
- FORBIDDEN ACTIONS: mark PASS on FAIL/ERROR/NOT_SCANNED evidence; treat
  `NOT_SCANNED = PASS`; disable a security control to achieve PASS; bypass
  human approval boundaries.
- HANDOFF: security decision → Deployment Check.
- ERROR HANDLING: any unverified channel → not PASS (`BLOCK`/`NEEDS_REVIEW`);
  report with scanner evidence status.
- EVIDENCE REQUIREMENTS: per-layer scanner status (`PASS`/`FAIL`/`ERROR`/
  `NOT_APPLICABLE`/`NOT_SCANNED`) and a security decision.
- STATUS MODEL: `PASS` / `BLOCK` / `NEEDS_REVIEW`.
- ESCALATION: to human for `NEEDS_REVIEW` and for any `BLOCK`.

## 7. Deployment Check

- IDENTITY: `deployment_check` — Operations. Deployment gatekeeper and
  executor.
- MISSION: Assess deployment readiness and, only after all prior gates plus
  human release approval, execute release; own rollback and monitoring.
- INPUT: Quality PASS, Security PASS, deployment plan, rollback plan,
  approval status.
- OUTPUT: Deployment gate verdict; dry-run plan; release/rollback report.
- RESPONSIBILITIES: verify Quality + Security + required approvals; produce a
  zero-write/zero-network dry-run; require a rollback plan; block release
  without approval; verify post-deploy state.
- NON-RESPONSIBILITIES: quality, security, approvals, coding.
- TOOLS: deployment gate evaluator, dry-run, rollback readiness, telemetry/
  ops reporting.
- PERMISSIONS: deployment execution **only after approval**; rollback.
- FORBIDDEN ACTIONS: deploy without Quality PASS / Security PASS / satisfied
  human approval; treat dry-run as real deploy; release without rollback.
- HANDOFF: release report → Monitoring / post-deploy verification → human.
- ERROR HANDLING: deployment failure → ROLLBACK → INVESTIGATION; never leave
  presumed-deployed state unverified.
- EVIDENCE REQUIREMENTS: verdict, approval satisfied, rollback ready, dry-run
  zero-write/zero-network, post-deploy verification result.
- STATUS MODEL: `REQUIRE_APPROVAL` / `DEPLOY_READY` / `DEPLOYED` / `ROLLED_BACK`.
- ESCALATION: to human for any deployment decision; a failure that survives
  bounded retry must reach the human.

## 8. OpenClaw

- IDENTITY: `openclaw` — Orchestration. Pipeline coordinator.
- MISSION: Orchestrate the Development F pipeline deterministically.
- INPUT: Agent structured outputs and statuses.
- OUTPUT: Coordination decisions, next-step routing, checkpoint updates.
- RESPONSIBILITIES: run the workflow machine, route completed work to the next
  agent, enforce the state machine, surface checkpoint/audit data.
- NON-RESPONSIBILITIES: **decision authority** — it does NOT decide GO/NO-GO,
  does NOT approve, does NOT override human gates.
- TOOLS: workflow engine, checkpoint persistence, telemetry.
- PERMISSIONS: orchestration only; **never substitutes for AI Council, Human,
  Quality, Security, or Deployment decisions.**
- FORBIDDEN ACTIONS: auto-continue past a human gate; make a release decision;
  bypass quality, security, or approval; skip a state.
- HANDOFF: routes the pipeline and preserves continuity across agents.
- ERROR HANDLING: invalid transition → `WorkflowStateError`; failure → recover
  without rewriting project files.
- EVIDENCE REQUIREMENTS: state transitions, actor, timestamps, audit trail.
- STATUS MODEL: workflow state machine states (see `docs/WORKFLOW.md`).
- ESCALATION: to human at bound-exceeded retries and at every human gate.

---

## PERMISSION MODEL (least privilege)

```text
AI Council        → research / analysis          → no deployment
Project Council   → plan / assign                → no code release
Coding Agent      → code modification            → no production release
Handoff Agent     → verify continuity            → no code modification
Quality Guardian  → read-only audit              → no code modification
Security Gate     → security verification        → release blocking authority
Deployment        → deployment execution         → only after approval
Human             → approval authority           → final human gates
OpenClaw          → orchestration                → no decision authority
```

---

# PHASE 3 CHECKPOINT — PASS

- All eight agents have a contract: AI Council, Project Council, Coding Agent,
  Handoff Agent, Quality Guardian, Security Gate, Deployment Check, OpenClaw.
- Input/output clear for every agent.
- Permissions clear: least privilege, validated against
  `docs/DEVELOPMENT-F.md` and `docs/SECURITY-ARCHITECTURE.md`.
- Forbidden actions clear for every agent.
- Escalation clear (to human at gates and at bound-exceeded retries).
- Evidence requirement clear (structured output + evidence requirements).
- No authority conflict: no agent can pass its own work through its own gate,
  and only human holds approval authority over release.

---

# PHASE 3 TEST

### Contract Completeness Test — PASS
Every one of the eight agent contracts contains all 14 required fields
(verified below for each agent section).

### Permission Test — PASS
No overly-large privilege: coding cannot release; quality is read-only;
security holds blocking only; deployment needs approval; OpenClaw cannot
decide; human holds the approvals.

### Authority Test — PASS
No agent accidentally holds another department's authority; approval powers
reside with the human; AI Council has no release path; OpenClaw orchestrates
only.

### Handoff Test — PASS
Each agent's OUTPUT is a validated INPUT of the next agent in the flow
(decision → Work Order → checkpoint → verified handoff → quality verdict →
security decision → deployment verdict → release report).

### Consistency Test — PASS
Consistent with `docs/DEVELOPMENT-F.md` (org, roles, flow) and
`docs/SECURITY-ARCHITECTURE.md` (security boundary, evidence model, human
approval).

### Git Test — PASS
`git diff --check` clean; only `docs/AGENT-CONTRACT.md` staged.

---

# PHASE 3 FINAL CHECKPOINT

```text
PHASE: 3
STATUS: PASS

DOCUMENT:
docs/AGENT-CONTRACT.md

AGENTS:
- AI Council: PASS
- Project Council: PASS
- Coding Agent: PASS
- Handoff Agent: PASS
- Quality Guardian: PASS
- Security Gate: PASS
- Deployment Check: PASS
- OpenClaw: PASS

CHECKS:
- Contract Completeness: PASS
- Permissions: PASS
- Authority: PASS
- Handoff: PASS
- Evidence: PASS
- Consistency: PASS
- Git Integrity: PASS

FINDINGS:
None.

REPAIRS:
None required.

RETEST:
All contract tests PASS.

CONCLUSION:
Standard agent contracts established with least-privilege permissions, clear
forbidden actions, structured output, and a clean handoff chain, consistent
with the architecture and security documents and the implemented role model.

NEXT:
PHASE 4
```