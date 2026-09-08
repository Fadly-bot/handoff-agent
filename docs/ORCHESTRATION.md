# Persistent Multi-Agent Orchestration

`handoff_agent.orchestration.PersistentOrchestrator` is a file-backed
orchestration engine (Phase 23) that manages multi-agent workflows: task DAGs,
dispatch policies, human-approval gates, stale/concurrency protection,
recovery, and persistence.

State is stored as one JSON record per workflow under `~/.handoff/orchestration`
(`HANDOFF_HOME` overrides the base). Every mutating operation reloads the
record, validates it, and atomically rewrites it — a dropped or torn write
never corrupts the previous version.

## Workflow status

```
created ──dispatch──▶ running ──complete last──▶ completed
created / running / waiting_approval ──pause──▶ paused ──resume──▶ running
any (non-terminal) ──cancel──▶ cancelled
any ──task failure──▶ failed ──recover──▶ running
```

| status | meaning |
| --- | --- |
| `created` | tasks may still be added |
| `running` | at least one task runnable/running |
| `paused` | dispatch rejected until `resume` |
| `waiting_approval` | an unapproved `approval_required` task gates progress |
| `cancelled` / `failed` / `completed` | terminal |

## Task status

```
pending ──▶ queued ──▶ running ──▶ success
pending/queued/blocked ──flag_approval──▶ approval_required ──approve (human)──▶ queued
queued/blocked ──block──▶ blocked ──unblock──▶ queued
running/queued ──fail──▶ failed ──retry──▶ queued (or approval_required)
non-terminal ──cancel──▶ cancelled ──retry──▶ queued
```

- `waiting` marks a task whose dependencies are not yet satisfied.
- Terminal tasks only leave their state through `retry_task`.

## Building a workflow

```python
from handoff_agent.orchestration import PersistentOrchestrator

orch = PersistentOrchestrator()                      # ~/.handoff/orchestration
rec = orch.create_workflow("handoff-agent", "add lint")
rec = orch.add_task(rec.workflow_id, task_id="lint", name="run ruff")
rec = orch.add_task(rec.workflow_id, task_id="test", name="run tests",
                    dependencies=("lint",), approval_required=True)
rec = orch.assign_agent(rec.workflow_id, "lint", "agent-cline")
```

Dependencies are validated against missing tasks and cycles
(`InvalidTaskDependencyError`). Assigning an agent runs capability
negotiation: the agent's capabilities must satisfy the task's
`required_capabilities` or `CapabilityDeniedError` is raised.

## Dispatch policies

- **Sequential** (`dispatch(parallel=False)`, default): at most one task runs
  at a time, in deterministic order (priority, then creation time, then id).
- **Parallel** (`dispatch(parallel=True)`): every independently-ready task is
  dispatched at once.
- Approval-gated tasks are never dispatched until `approve_task(..., human=...)`
  clears the gate; `approve_task` is the only code path that sets `approved_by`.
  With `require_human_approval=True`, every task without a pre-existing
  approval is gated.

## Stale and idempotency protection

- Every record carries a canonical `state_hash`. Callers may pass
  `expected_base=...`; a superseded base raises `StaleWorkflowError`.
- Tasks carry unique `execution_id`s. Completing with an execution id that
  differs from the current one raises `StaleTaskError`.
- Re-delivering the identical result for a completed execution is an idempotent
  no-op; a *different* result for the same execution raises `StaleTaskError`.
- Retrying a task issues a fresh execution id; results against the old id are
  rejected.

## Approval boundary

```python
rec = orch.approve_task(rec.workflow_id, "publish", human="alice")
```

The human-actor requirement is enforced in-process: an empty or absent human
actor raises `ApprovalRequiredError`, and `approve_task` re-evaluates the
workflow status so an approved task becomes runnable again. Read-only
`PermissionBoundary` instances (no `checkpoint.create`/`checkpoint.update`)
cannot emit checkpoints (`CapabilityDeniedError`).

## Timeouts and deadlines

`apply_timeouts(workflow_id, now=None)` fails tasks whose `started_at` exceeds
`timeout_seconds`, and tasks whose `deadline` has passed. Timeout failures
propagate to dependents exactly like normal failures.

## Recovery

- `recover(workflow_id)` requeues any RUNNING task with a fresh execution id.
- `recover_agent_failure(..., failed_agent=...)` requeues tasks assigned to a
  dead agent.
- `recover_provider_failure` / `recover_adapter_failure` / `recover_mcp_failure`
  are typed aliases for infra-level interruption recovery.
- `graceful_shutdown` pauses the workflow; `graceful_restart` resumes it.
- `health_check()` and `diagnostics()` report writability and any failed
  workflows.

## Checkpoint emission

`emit_checkpoint(workflow_id, ...)` folds the workflow's objective, completed/
in-progress/planned tasks, decisions, constraints, and agents into a Universal
Handoff-protocol checkpoint and writes `docs/HANDOFF.md` plus a `CHANGELOG.md`
entry through a file adapter. `set_checkpoint_adapter(adapter)` installs the
adapter; without one, emission validates the assembled document.

## Persistence and security

- Writes are atomic (temp file + rename); a failed write leaves the previous
  record intact.
- Loads validate structure, dependency integrity, and the content hash;
  malformed, truncated, or missing-key records raise `CorruptStateError`.
- Task results and records containing secret-like values are refused.
  The module never shells out, executes code dynamically, or mutates Git.
- State also mirrors task/agent correlation and request ids so delegated work
  can be traced back to its originating workflow.

## Error taxonomy

| exception | raised when |
| --- | --- |
| `WorkflowNotFoundError` | no persisted record for the id |
| `DuplicateWorkflowError` | workflow id already taken |
| `DuplicateTaskError` | task id already present |
| `TaskNotFoundError` | no task for the id |
| `InvalidTaskDependencyError` | missing/self/cyclic dependency |
| `TaskStateError` | illegal state transition or dispatch from terminal state |
| `StaleWorkflowError` / `StaleTaskError` | superseded base or execution id |
| `ApprovalRequiredError` | approval gate invoked without a human |
| `CorruptStateError` | persisted record cannot be parsed or validated |
| `UnauthorizedOperationError` | actor is not the assigned agent |