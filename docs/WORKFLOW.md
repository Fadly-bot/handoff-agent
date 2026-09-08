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