# Conflict Resolution

The Universal Handoff protocol is built to make conflicts explicit and
**detectable before they overwrite state**, not to hide them.

## When a conflict exists

Two checkpoints `A` and `B` conflict when **both** were produced from the
same base checkpoint `base` and diverged (different state → different
protocol identities). A single writer extending the current head is a safe
fast-forward and is **not** a conflict.

`interop.detect_conflict(base_identity, ours, theirs)`:

| situation | result |
| --- | --- |
| both sides empty | no conflict |
| one `ours` fast-forwards `base` | no conflict (fast-forward) |
| `theirs` fast-forwards `base` | no conflict (their head is the base's successor) |
| both diverged from `base` with different identities | **conflict** |

## Detection points

- **write-time**: `write_checkpoint(..., expected_base=...)` refuses a write
  when the on-disk identity differs from the expected base
  (`AdapterPermissionError`).
- **continuation-time**: `workflow.verify_before_continue()` re-checks the
  current head against the record's identity and raises
  `StaleCheckpointError` (superseded) or `WorkflowConflictError` (divergent)
  before a consumer is allowed to continue.
- **record-time**: the workflow audit trail records every
  `checkpoint.create` / `checkpoint.update` with the identity observed, so a
  conflict can be reconstructed deterministically.

## Resolution path

Automatic conflict arbitration is deliberately **not** attempted. The protocol
exposes the deterministic `ConflictReport` (base, ours, theirs, reason) and
leaves the resolution to the machine owner:

1. `detect_conflict(base, ours, theirs)` produces the report.
2. A human (or policy) selects the surviving objective / decides.
3. The producer re-checkpoints the agreed state and the workflow resumes via
   `recover` → `working` → `checkpoint`.

```python
from handoff_agent.interop import detect_conflict

report = detect_conflict(base_identity, ours_text, theirs_text)
if report.conflicting:
    # human resolution; never force-overwrite
    producer.checkpoint(objective=agreed_objective)
```

## Integrity rules

- Never write without an `expected_base` when the checkpoint may already exist.
- Never restore a checkpoint that fails `validate_checkpoint` or
  `verify_identity`.
- Never mutate the audit trail; append a resolution entry instead.