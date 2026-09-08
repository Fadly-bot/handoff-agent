# Task Delegation & Agent Routing

`handoff_agent.delegation` (Phase 25) provides the universal task model and the
routing engine that hands work to registry agents: capability-based
deterministic routing, assignment confirmation, approval gates, escalation,
reassignment, and result acceptance with stale/duplicate detection.

## Universal task model

`TaskDefinition` carries `task_id`, `parent_task_id`, `workflow_id`,
`project_id`, `requester_agent_id`, `assigned_agent_id`, `task_type`,
description/context, `requirements`, `constraints`, `priority`, `deadline`,
`dependencies`, and capability/platform/provider/model requirements
(`required_*` strict joins, `preferred_*` ranking hints), plus
`permission_scope`, `trust_min`, and `approval_required`.

- `validate()` rejects missing ids, unknown capability ids, self-dependencies,
  invalid deadlines, and secret-like values.
- `normalized()` produces canonical, deterministic field values.

```python
from handoff_agent.delegation import Delegator, TaskDefinition

delegator = Delegator(registry)                 # requires an AgentRegistry
task = delegator.create_task(TaskDefinition(
    task_id="lint", description="run ruff",
    required_capabilities={"checkpoint.create"},
), actor="requester")
```

Task state is persisted atomically to `~/.handoff/delegation/delegations.json`
(`HANDOFF_HOME` overrides the base) and reloaded on construction.

## Lifecycle

```
created → pending → routed → assigned → confirmed → running → success
pending/blocked ──require_approval──▶ waiting_approval ──approve(human)──▶ pending/confirmed
waiting_approval ──reject_approval──▶ cancelled
running ──submit_result──▶ success      running ──fail/timeout──▶ failed
failed/cancelled/refused ──retry──▶ pending        failed ──reassign──▶ assigned
any (non-terminal) ──block──▶ blocked ──unblock──▶ pending
any ──escalate──▶ escalated
```

## Routing

`route(task_id)` builds a `RoutingDecision`:

1. Detects stale registry agents.
2. Filters the registry on required capabilities (or permission scope),
   required provider/platform/model, trust, task/project scope, and
   availability.
3. Boosts agents matching `preferred_*` hints ahead of the strictly-ranked
   pool; ranking is `(priority, -trust_level, name, agent_id)` — deterministic.
4. Returns the best match with a human-readable `reason`, the full ranked list,
   and records an audit event.

No available match raises `NoAgentAvailableError` (callers escalate rather
than bypass).

## Assignment and confirmation

`assign(task_id, agent_id)` verifies the agent is registered, healthy (fresh
heartbeat), in scope, and capable before assigning. `confirm_assignment` /
`reject_assignment` mirror the two-phase commit; `accept_task` starts execution
with a fresh `execution_id`; `refuse_task` returns the task to the pool.
Retries and reassignments always mint a new execution id, so results against
the previous execution are rejected as stale.

## Approval gate

A task that is `approval_required` (or a delegator running with
`require_human_approval=True`) is blocked at routing until
`require_approval` plus `approve(task_id, human="alice")`. Approval without a
named human raises `ApprovalRequiredError`; `reject_approval` cancels the task.
There is no path that advances a gated task without a human actor.

## Failure handling, escalation, reassignment

- `fail_task` / `timeout` mark failure and propagate failure to dependents.
- `retry` requeues a failed/cancelled/refused/blocked task.
- `reassign(task_id, agent_id)` points the task at a new agent.
- `reassign_failed_agent` (and `reassign_unavailable_agent`) find an available
  alternative and, when none exists, escalate to a human instead of
  downgrading the constraints.
- `block`/`unblock` cover external holds; `escalate` raises the task to human
  review.

## Results

`submit_result(task_id, agent_id, result, execution_id, partial=...)` returns a
`ResultAcceptance`:

| outcome | condition |
| --- | --- |
| accepted → `success` | running task, correct agent, fresh execution id |
| `partial` accepted, stays `running` | incremental fields merge into the stored result |
| idempotent no-op | identical result re-delivered for a completed execution |
| `DuplicateResultError` | different result for an already-successful execution |
| `StaleResultError` | execution id differs from the current one |
| rejected (not accepted) | result delivered by a non-assigned agent |
| `DelegationError` | result contains secret-like values |

Structured results, partial handling, and conflict/stale/duplicate detection
are all covered. `reject_result` fails a task whose submitted result failed
verification.

## Propagation and handoffs

- `decompose(parent, subtasks)` creates a chained subtask DAG and propagates
  workflow/project/requester/`correlation_id`/`request_id` to each subtask.
- `_propagate_success` unblocks dependent tasks when their dependencies all
  succeed; `_propagate_failure` fails dependents.
- `checkpoint_propagation(task_id)` assembles the context/handoff fragment.
- `execution_handoff(task_id)` builds a Universal Handoff-protocol checkpoint
  (validated, identity-derived) and returns it alongside correlation/request
  and workflow ids.

## Security perimeter

- Permission-boundary enforcement: `create_task` requires
  `checkpoint.create`; executing/accepting results requires
  `checkpoint.update`. A read-only or `deny_all` boundary raises
  `CapabilityDeniedError` before any mutation.
- No secret propagation: task definitions and results containing secret-like
  values are refused, and audit detail fields are redacted if they ever
  contain one.
- No credential storage, no unrestricted Git operations, no `eval`/`exec`,
  no shells, and no arbitrary agent execution (agents are only ever selected
  through the registry and invoked via structured results).
- Approval gates cannot be bypassed; availability cannot be forced.

## Reporting and health

`report(task_id=None)` summarizes status counts and audit event volume;
`diagnostics()` lists running/failed/escalated tasks; `health_check()` reports
whether any task is failing and whether the registry is alive; `events()`
returns the append-only audit trail.