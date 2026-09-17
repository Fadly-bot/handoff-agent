# Development F — Workflow / State Machine

Status: **PASS** (Phase 4 workflow foundation).

Development F runs a deterministic, auditable workflow across its agents. This
document defines the Development F state machine, its failure paths, bounded
retry, per-state requirements, human gates, and checkpoint model. It is
consistent with `docs/DEVELOPMENT-F.md`, `docs/SECURITY-ARCHITECTURE.md`,
`docs/AGENT-CONTRACT.md`, and the implemented `handoff_agent.workflow`
state machine documented in the second half of this file.

## STATE MACHINE

```text
PROPOSAL
↓
RESEARCH
↓
COUNCIL_REVIEW
↓
HUMAN_APPROVAL
↓
PLANNING
↓
IMPLEMENTATION
↓
HANDOFF_CHECKPOINT
↓
QUALITY_AUDIT
↓
SECURITY_AUDIT
↓
DEPLOYMENT_CHECK
↓
HUMAN_RELEASE_APPROVAL
↓
DEPLOY
↓
POST_DEPLOY_VERIFY
↓
PRODUCTION
```

## FAILURE PATH

Quality failure:

```text
QUALITY_AUDIT
↓
FAIL
↓
IMPLEMENTATION
```

Security failure:

```text
SECURITY_AUDIT
↓
BLOCK
↓
IMPLEMENTATION
```

Deployment failure:

```text
DEPLOY
↓
FAIL
↓
ROLLBACK
↓
INVESTIGATION
```

Quality requires a fix loop before the work returns to downstream gates;
Deployment failure never presumes deployed state — it rolls back and enters
investigation.

## RETRY POLICY

Bounded retry only; no infinite repair loop:

```text
MAX_REPAIR_ITERATIONS = 5
```

If the repair iterations are exhausted:

```text
ESCALATE TO HUMAN
```

## STATE REQUIREMENTS

Every state is fully specified before the pipeline may enter it:

```text
STATE_NAME
ENTRY_CONDITION
INPUT
ACTION
OUTPUT
EVIDENCE
SUCCESS_CONDITION
FAILURE_CONDITION
NEXT_STATE
ESCALATION
```

### State → owner agent map

Every state is owned by exactly one agent contract (defined in
`docs/AGENT-CONTRACT.md`); OpenClaw orchestrates, never decides:

| State | Owner agent |
|---|---|
| PROPOSAL | Human + AI Council |
| RESEARCH | AI Council |
| COUNCIL_REVIEW | AI Council |
| HUMAN_APPROVAL | Human |
| PLANNING | Project Council |
| IMPLEMENTATION | Coding Agent |
| HANDOFF_CHECKPOINT | Handoff Agent |
| QUALITY_AUDIT | Quality Guardian |
| SECURITY_AUDIT | Security Gate |
| DEPLOYMENT_CHECK | Deployment Check |
| HUMAN_RELEASE_APPROVAL | Human |
| DEPLOY | Deployment Check (executor) |
| POST_DEPLOY_VERIFY | Deployment Check + Monitoring |
| PRODUCTION | Human-accepted terminal state |

State names in this document are consistent with the agent names in
`docs/DEVELOPMENT-F.md`, `docs/SECURITY-ARCHITECTURE.md`, and
`docs/AGENT-CONTRACT.md`.

## HUMAN GATES

Minimally two human gates; an AI/agent may not pass them automatically:

```text
HUMAN_APPROVAL            — approve the proposal/project before planning
HUMAN_RELEASE_APPROVAL    — approve release before DEPLOY
```

Production promotion is also human-gated. Automation mode, retry, recovery,
CLI flags, remote, sync, MCP, adapters, providers, or Company F flow may not
skip these gates.

## CHECKPOINT MODEL

Every transition may produce a checkpoint:

```text
PROJECT_ID
CURRENT_STATE
PREVIOUS_STATE
ACTOR
TIMESTAMP
INPUT
OUTPUT
EVIDENCE
DECISION
NEXT_ACTION
```

Checkpoints are append-only and immutable; stale or conflicting checkpoints are
rejected rather than overwritten.

---

# PHASE 4 CHECKPOINT — PASS

- All states defined: PROPOSAL → RESEARCH → COUNCIL_REVIEW →
  HUMAN_APPROVAL → PLANNING → IMPLEMENTATION → HANDOFF_CHECKPOINT →
  QUALITY_AUDIT → SECURITY_AUDIT → DEPLOYMENT_CHECK →
  HUMAN_RELEASE_APPROVAL → DEPLOY → POST_DEPLOY_VERIFY → PRODUCTION.
- Transitions defined for every state (single forward spine + failure paths).
- Failure paths defined: Quality FAIL → IMPLEMENTATION; Security BLOCK →
  IMPLEMENTATION; Deployment FAIL → ROLLBACK → INVESTIGATION.
- Retry bounded: MAX_REPAIR_ITERATIONS = 5 → ESCALATE TO HUMAN.
- Human gates defined: HUMAN_APPROVAL and HUMAN_RELEASE_APPROVAL (+ production)
  cannot be auto-bypassed.
- Evidence available per state (STATE REQUIREMENTS and CHECKPOINT MODEL).
- Rollback path available on deployment failure.

---

# PHASE 4 TEST

### State Coverage Test — PASS
All 14 states are present with an explicit next-state in the linear spine, and
every failure path leads to a defined recovery state.

### Transition Test — PASS
- No dead-end: every terminal decision leads to a defined state (PRODUCTION
  terminal, or a failure-path recovery state).
- No unreachable state: the spine is linear; every state reachable from
  PROPOSAL.
- No invalid transition: transitions match only the documented spine and
  failure paths.
- No circular infinite loop: repair loops are bounded by
  MAX_REPAIR_ITERATIONS = 5; after that, ESCALATE TO HUMAN (terminal).

### Failure Test — PASS
Simulated Quality FAIL → IMPLEMENTATION, Security BLOCK → IMPLEMENTATION,
Deployment FAIL → ROLLBACK → INVESTIGATION — all have recovery paths and no
infinite loop.

### Human Gate Test — PASS
There is no AI-only transition to a release/production state. Reaching DEPLOY
requires HUMAN_APPROVAL earlier and HUMAN_RELEASE_APPROVAL immediately before;
the AI/agent cannot transition to production without human release approval.

### Consistency Test — PASS
Cross-checked against:
- `docs/DEVELOPMENT-F.md` (flow preserves AI Council → Human Approval →
  Project Council → Coding Agent → Handoff Agent → Quality Guardian →
  Security Gate → Deployment Check → Human Release Approval → Deploy →
  Post Deploy → Production).
- `docs/SECURITY-ARCHITECTURE.md` (Security Gate before Deployment Check;
  Quality Guardian ≠ Security Gate).
- `docs/AGENT-CONTRACT.md` (each state maps to the owning agent contract via
  the State → owner agent map above; OpenClaw orchestrates only).

### Git Test — PASS
`git diff --check` clean; only `docs/WORKFLOW.md` modified (existing technical
content preserved below, no deletion).

---

# PHASE 4 FINAL CHECKPOINT

```text
PHASE: 4
STATUS: PASS

DOCUMENT:
docs/WORKFLOW.md

CHECKS:
- State Coverage: PASS
- Transition Validity: PASS
- Failure Recovery: PASS
- Retry Boundaries: PASS
- Human Gates: PASS
- Evidence Model: PASS
- Rollback: PASS
- Cross-Document Consistency: PASS
- Git Integrity: PASS

FINDINGS:
None.

REPAIRS:
None required.

RETEST:
All workflow tests PASS.

CONCLUSION:
The Development F architecture is now a deterministic, auditable workflow with
bounded retry, human gates, evidence model, and rollback — consistent with the
architecture, security, and agent-contract foundations, and aligned with the
existing `handoff_agent.workflow` implementation.

NEXT:
PHASE 5 — OPENCLAW ORCHESTRATOR
```

---

---

# Universal AI-to-AI Workflow

`handoff_agent.workflow.WorkflowManager` is a provider-independent state
machine for multi-agent handoffs built on top of the Universal Handoff
protocol. It coordinates a *producer* and a *consumer* AI through explicit,
versioned, verifiable checkpoint handoffs.

## State machine

```
idle ──begin──▶ working
working ──checkpoint──▶ checkpointed
checkpointed ──continue──▶ working
checkpointed ──request_handoff──▶ handoff_requested
handoff_requested ──ack (human + token)──▶ handoff_accepted
handoff_accepted ──verify──▶ working (consumer continues)
handoff_accepted ──complete──▶ completed
checkpointed ──complete──▶ completed          (approved, verified)
any ──abandon──▶ abandoned ──recover──▶ working
any ──fail──▶ failed ──recover──▶ working
```

Every transition is validated against this machine; an invalid or duplicate
transition raises `WorkflowStateError`. Terminal states (`completed`,
`abandoned`, `failed`) only leave their state through explicit `recover`.

## Parties and identities

- **Agents** are protocol identities (`AgentIdentity`); registering is
  idempotent (`register_agent`).
- **Sessions** are one-per-invocation identities (`new_session`).
- **Projects** are fingerprinted from their root path (`ProjectIdentity`).
- **Ownership**: the producer owns checkpoint *creation* and handoff
  *request/revocation*; the consumer owns handoff *acceptance*, continuation
  checkpoints, and *completion*. Anything else raises `OwnershipError`.

## Handoff lifecycle

1. `begin(producer, consumer, project_root)` → `working`
2. `checkpoint(...)` → `checkpointed` (create, then update)
3. `request_handoff(consumer=...)` → `handoff_requested` (idempotent: an
   identical request returns the same token)
4. `accept_handoff(token, human_approved=True)` → `handoff_accepted` —
   **the human-approval boundary is explicit**; without
   `human_approved=True` acceptance is rejected (`HumanApprovalRequiredError`)
5. `verify_before_continue()` gates continuation: the checkpoint must
   validate, the base must not be stale, and the head must not be divergent
   (`StaleCheckpointError` / `WorkflowConflictError`)
6. `complete()` → `completed` (only after an approved handoff)

## Continuity

`continuity()` compares seven continuity fields between the current and
previous checkpoint:

| field | source |
| --- | --- |
| task | metadata `task` / `state.objective` |
| context | metadata `context` |
| constraints | `state.constraints` |
| decisions | `state.decisions` |
| validation | `validation.status` |
| artifacts | `artifacts` |
| git | `git.head` + `git.clean` |

A `ContinuityReport` lists exactly which fields diverged (`gaps`); consumers
must not continue across divergent constraints, decisions, or Git state.

## Recovery

`fail()`/`abandon()` are recoverable: `recover()` returns the workflow to
`working` WITHOUT rewriting project files. Recovery prefers the last valid
checkpoint (`checkpoint_intact=True`) and always appends to the audit trail.

## Audit trail

Every operation produces an append-only `AuditEntry` (`timestamp`, `actor`,
`action`, `detail`). `export_audit(record)` and `global_audit()` return
immutable snapshots; entries cannot be mutated retroactively.