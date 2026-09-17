# Development F — Architecture

Status: **PASS** (Phase 1 architecture foundation).

Development F is a virtual software company run by AI agents under mandatory
human authority. This document defines its identity, organization,
responsibilities, authority boundaries, high-level flow, and strict separation
of concerns. It is the foundation (Phase 1) of the Development F roadmap and is
consistent with the existing implementation in `src/handoff_agent/company_f.py`
(`CompanyFCoordinator`) and `src/handoff_agent/pilot.py` (`CompanyFPilot`).

## A. Identity

- **Name:** Development F — a virtual software company.
- **Purpose:** Develop, verify, and release software through a supervised
  pipeline of AI councils, planning, coding, handoff, quality, security, and
  deployment — never without human authority at the decision gates that belong
  to humans.
- **Scope:** End-to-end software delivery for adopted project baselines:
  proposal → research → review → approval → planning → implementation →
  handoff → quality → security → deployment check → human release approval →
  deploy → post-deploy verify → production.
- **Working principles:**
  1. Deny-by-default: an agent may only act within the capabilities, scope,
     lease, and permissions granted to its role.
  2. Evidence over assumption: every gate decision is grounded in test/scan
     evidence, never in agent assertion alone.
  3. Human is the final authority for project, release, and production
     decisions; AI cannot auto-bypass those gates.
  4. Separation of concerns: planning, coding, handoff, quality, security, and
     deployment are distinct, non-overlapping responsibilities.
  5. Audit integrity: every transition is recorded as immutable audit evidence.
  6. Least privilege: no role holds permission outside its stated contract.

## B. Organization

```text
Development F
├── Strategy
│   └── AI Council
├── Planning
│   └── Project Council
├── Engineering
│   └── Coding Agents
├── Continuity
│   └── Handoff Agent
├── Quality
│   └── Quality Guardian
├── Security
│   └── Security Gate
├── Operations
│   ├── Deployment
│   ├── Rollback
│   └── Monitoring
└── Orchestration
    └── OpenClaw
```

> Terminology note: the implemented orchestration layer
> (`src/handoff_agent/company_f.py`) models the *Project Council* as the
> `planning_council` role and the *AI Council* as the `ai_council` role.
> This document uses the organization names above; the mapping to the code
> roles is explicit in `AGENT-CONTRACT.md` (Phase 3).

## C. Responsibilities

| Department | Agent/Role | Core responsibilities |
|---|---|---|
| Strategy | AI Council (`ai_council`) | Research/analysis; GO / NO-GO / REQUIRE_REVIEW decisions; conflict review; never deploys. |
| Planning | Project Council (`planning_council`) | Produce fully-specified Work Orders + risk register **only after GO**; assign coding agents; scope/owner/criteria/constraints/rollback defined. |
| Engineering | Coding Agents (`coding_agent`) | Implement an assigned, capability-verified Work Order it owns and has leased; no production release. |
| Continuity | Handoff Agent (`handoff_agent`) | Verify checkpoint validity, Git state, non-staleness, and ownership; accept/reject handoffs; no production release. |
| Quality | Quality Guardian (`quality_guardian`) | Read-only quality audit: tests, regression, security evidence, release readiness; PASS / FAIL / REQUIRE_FIX; no code modification. |
| Security | Security Gate | Independent security boundary: secret/scan, static, dependency, sandbox, network, approval-boundary verification; PASS / BLOCK / NEEDS_REVIEW; release-blocking authority. |
| Operations | Deployment | Execute deployment only after required approvals; dry-run before real run. |
| Operations | Rollback | Authorised rollback plan + rollback readiness mandated before release. |
| Operations | Monitoring | Post-deploy verification and observability of the released state. |
| Orchestration | OpenClaw | Coordinates the pipeline; **orchestrates, never decides** — not a decision authority. |

## D. Human Authority

Humans are the only authority for the three release-critical decisions. AI
cannot take them over, override them, or auto-continue past them:

```text
Project Approval      — approve a project/proposal before planning
Release Approval      — approve a release (Quality + Security + Deployment gates passed)
Production Approval   — approve promoting to production
```

Implementation contract: in `company_f.py` the human role is `human` (trust
level 5, mandatory approval for deployment/release). A deployment without a
satisfied human approval returns `REQUIRE_APPROVAL`, and release is blocked
(the pilot proves this guarantee; see `docs/company-f/PILOT.md`). The human
approval gate is enforced inside the product pipeline and cannot be bypassed by
automation mode, retry, recovery, CLI flag, remote request, sync, MCP, adapter,
or provider.

## E. High-Level Flow

```text
AI Council
↓
Human Approval
↓
Project Council
↓
Coding
↓
Handoff
↓
Quality Guardian
↓
Security Gate
↓
Deployment Check
↓
Human Release Approval
↓
Deploy
↓
Post Deploy Verification
↓
Production
```

The formal, deterministic state machine of this flow (with failure paths,
bounded retry, and rollback) is defined in `docs/WORKFLOW.md` (Phase 4).

## F. Separation of Concerns

```text
Planning  ≠  Coding  ≠  Handoff  ≠  Quality  ≠  Security  ≠  Deployment
```

- **Planning** decides *what and how* (scope, plan, acceptance criteria). It
  does not write implementation code.
- **Coding** implements the approved plan. It does not approve its own work,
  does not pass itself through quality/security, and does not deploy.
- **Handoff** verifies continuity between coding and downstream gates
  (checkpoint validity, Git state, ownership). It does not perform quality or
  security audits.
- **Quality** assesses quality (tests, regression, release readiness) in
  read-only fashion. It is **not** the security authority.
- **Security** is an **independent boundary** verifying security (secrets,
  sandbox, network, policy, approval boundaries). It is not merged into Quality
  and it holds release-blocking authority.
- **Deployment** executes release **only after** Quality, Security, Deployment
  Check, and Human Release Approval all pass. It owns rollback and monitoring,
  not the decision to approve.

This separation prevents a single agent from controlling both the work and its
own acceptance, and it guarantees a human authority remains the final gate.

---

# PHASE 1 CHECKPOINT — PASS

- Architecture complete: identity, organization, responsibilities, human
  authority, high-level flow, separation of concerns all defined.
- All departments defined: Strategy, Planning, Engineering, Continuity,
  Quality, Security, Operations, Orchestration.
- Responsibilities do not overlap: planning/coding/handoff/quality/security/
  deployment each have a distinct, non-overlapping scope.
- Human approval explicit: Project, Release, Production approvals reserved to
  human; cannot be auto-bypassed.
- Security is separate from Quality: Security Gate is an independent boundary
  with release-blocking authority.
- Deployment has a boundary: requires Quality + Security + Deployment Check +
  Human Release Approval before any release; rollback mandated.
- OpenClaw is not a decision authority: orchestration only.

---

# PHASE 1 TEST

### Structural Test — PASS
All required sections (A Identity, B Organization, C Responsibilities,
D Human Authority, E High-Level Flow, F Separation of Concerns) present and
complete.

### Consistency Test — PASS
- Agent names consistent across sections: AI Council / Project Council /
  Coding Agents / Handoff Agent / Quality Guardian / Security Gate /
  Deployment / Rollback / Monitoring / OpenClaw.
- No responsibility conflict: each role's scope is disjoint (see F).
- Flow is a single linear pipeline with human gates at Project Approval,
  Release Approval, and Production Approval.
- Terminology consistent with `src/handoff_agent/company_f.py`
  (`ai_council`, `planning_council`, `coding_agent`, `handoff_agent`,
  `quality_guardian`, `human`, `deployment_check`) and the Company F pilot.

### Git Test — PASS
`git diff --check` clean; only `docs/DEVELOPMENT-F.md` staged.

---

# PHASE 1 FINAL CHECKPOINT

```text
PHASE: 1
STATUS: PASS

DOCUMENT:
docs/DEVELOPMENT-F.md

CHECKS:
- Structure: PASS
- Responsibilities: PASS
- Authority: PASS
- Separation of Concerns: PASS
- Flow: PASS
- Git Integrity: PASS

FINDINGS:
None.

REPAIRS:
None required.

RETEST:
Structural/consistency/git tests all PASS on first run.

CONCLUSION:
Development F architecture foundation established and consistent with the
implemented Company F coordination layer.

NEXT:
PHASE 2
```