# Development Company F — Coordination Contract (Phase 30)

Status: **PASS**. Implements the coordination layer of the revised roadmap.

Implementation: `src/handoff_agent/company_f.py`
Contract tests: `tests/test_company_f.py` (38 tests).

## Actors and role model

| Role | Trust minimum | Responsibilities |
|---|---|---|
| `ai_council` (AI Council) | 3 | Make GO/NO-GO decisions; conflict review |
| `human` (Human) | 5 | Mandatory approval; conflict review; high-trust veto |
| `planning_council` | 2 | Create plans and Work Orders **only after GO**; assign coding agents |
| `coding_agent` | 2 | Execute a Work Order it was assigned and whose required capabilities it has |
| `handoff_agent` | 2 | Accept/reject checkpoints; hand ownerless, valid, non-stale work on |
| `quality_guardian` | 3 | PASS / FAIL / REQUIRE_FIX quality gates |
| `deployment_check` | 3 | Deployment gate; requires prior Quality PASS and satisfied approval |

Every role is a `RoleIdentity` with `role`, `agent_id`, `name`, capabilities
(frozenset), and `trust_level`. Trust is monotonic; a lowered trust never grants
authority it did not have.

## Capability rules

- A coding agent receives a Work Order **only if** `required_capabilities ⊆
  agent.capabilities`. Missing capability raises a violation that explicitly
  refuses silent fallback to a different agent.
- Roles are capability-checked per action (`plan`, `checkpoint`, `decide`,
  `approve`, `quality`, `deploy`).

## Minimum trust by action

| Action | Trust minimum |
|---|---|
| `onboard`, `project` | 1 |
| `plan`, `checkpoint` | 2 |
| `decide` | 3 |
| `approve` | 5 |

## Projects, decisions, plans

- `ProjectIdentity` (`project_id`, `name`, `root`).
- `Decision` — `decision_id`, `project_id`, status (GO/NO-GO), rationale,
  timestamp, conflict flag. Only AI Council or Human may produce a decision.
- `Plan` — `work_order_id`, strategy, `Risk` list (severity/probability).
  Only Planning Council may attach a plan.
- `create_work_order` refuses to run without a prior GO for the project
  (`CoordinatorViolation: no GO decision`).

## Work Order state machine

```
PENDING → PLANNED → EXECUTING → CHECKPOINTED → QUALITY_GATED
          → DEPLOYMENT_GATED → RELEASED
PENDING → CANCELLED / CONFLICT
EXECUTING → FAILED / CHECKPOINTED
CHECKPOINTED → EXECUTING   (Quality REQUIRE_FIX returns to rework)
QUALITY_GATED → DEPLOYMENT_GATED   (requires PASS)
DEPLOYMENT_GATED → RELEASED        (requires approval satisfied + rollback ready)
any → CONFLICT   (decision conflict detected)
```

`InvalidTransition` is raised on any disallowed transition. The state machine is
authoritative; no action mutates a Work Order outside it.

## Checkpoint and handoff

- `submit_checkpoint` requires the **owner** (assigned coding agent).
  An ownerless or different-owner checkpoint transitions Work Order to
  `CONFLICT` (review required); a stale checkpoint (no lease, no valid state)
  is `REJECTED`.
- `evaluate_handoff` ACCEPTs only handoffs whose checkpoint is owned by the
  Work Order owner and is non-stale; uses the **last checkpoint**, never a
  missing or orphaned one.
- An agent failure does not destroy the Work Order: it remains `FAILED` and
  can be resumed/re-assigned with full audit history (see below).

## Quality and deployment gates

- `quality_gate(verdict)` — Quality Guardian only (trust ≥ 3):
  - PASS → `QUALITY_GATED`
  - FAIL / REQUIRE_FIX → back to `EXECUTING` with recorded issues
- `deployment_gate(rollback_ready)` — Deployment Check only (trust ≥ 3);
  requires `QUALITY_GATED` **and** a satisfied approval requirement;
  missing Quality PASS or unmet approval raises.

## Human approval boundary

- Work Orders may be created with `approval_required=True`.
- `approve_work_order` requires Human (trust ≥ 5) and marks the requirement
  satisfied.
- Approval is **not bypassable**: assignment, deployment, and release all
  enforce the requirement; a missing approval raises `NoApproval`.

## Decision conflict and duplicate prevention

- Two conflicting decisions for the same project with no clean resolution mark
  the latest Work Orders `CONFLICT` — a safe state requiring review, never a
  vehicle for unguarded progress.
- Duplicate Work Orders are prevented by a SHA-256 fingerprint over
  `(title, description)`; a duplicate maintains a `duplicate_of` pointer and is
  rejected with its original reference.

## Audit evidence

Every transition appends an `AuditEvidence` entry: timestamp, actor, action,
work_order_id, from→to, and a short detail message. The full ordered list is
exposed via `audit_trail()`; each entry is secret-free by construction (actors
are role IDs, not credentials).

## Persistence

- With `state_dir`, the coordinator snapshots roles, projects, decisions,
  work orders, plans, risks, approvals, checkpoints (state excluded by design),
  audit evidence, and fingerprints to `state.json` (load on construction).
- Persistence is optional and never stores secrets or arbitrary payload state.

## Lease and ownership

- `lease(agent, work_order_id)` grants the owner lease before checkpointing.
- Ownership is set at assignment; a checkpoint from a non-owner is a conflict,
  a checkpoint from an unknown agent is rejected.

## Safe relaxation note

The contract permits a work order with a mandatory approval to be planned and
executed, but it **cannot be deployed or released** until the Human approval
requirement is satisfied — the "cannot be bypassed" guarantee lives at the
release boundary where it matters.

## Conformance evidence

The checks above are exercised by `tests/test_company_f.py`, which covers all
Phase 30 checklist items: GO/NO-GO, ordering of plans after GO, capability
gating, stale/ownerless checkpoint rejection, PASS/FAIL/REQUIRE_FIX, release
without Quality PASS, non-bypassable approval, conflict safe-state, and
work-order survival on agent failure.