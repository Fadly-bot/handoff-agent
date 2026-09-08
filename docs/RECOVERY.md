# Recovery

Handoff checkpoints are designed so that any failure — a crashed agent, an
interrupted write, a corrupted file, a provider timeout — can be rolled back
non-destructively.

## Guarantees

- **Atomic writes** — `docs/HANDOFF.md` is written to a temp file and renamed
  into place. A reader never sees a half-written checkpoint.
- **History first** — before the current checkpoint is replaced it is
  archived into `docs/CHANGELOG.md`. If archiving fails, the current
  checkpoint is left untouched.
- **Non-destructive recovery** — recovery never rewrites project files; it
  restores the workflow *logical state* to a usable point.
- **Verifiable restore** — any restored checkpoint still validates against the
  protocol schema and identity before it is accepted.

## Recovery procedure (per failure class)

| failure | symptom | recovery |
| --- | --- | --- |
| interrupted write | `HANDOFF.md` missing / truncated | restore last block from `CHANGELOG.md`; re-validate |
| corrupted block | malformed/partial protocol JSON in `HANDOFF.md` | `parse_handoff_document` raises; restore from changelog archive |
| unsupported schema/version | `ProtocolValidationError` / `ProtocolVersionError` | do not rewrite; escalate to human, archive is intact |
| provider timeout / MCP disconnect / CLI interruption | adapter raises `AdapterError` | `workflow.fail()` → `workflow.recover()` → `working` |
| stale base | writer observed old identity | refuse write (`AdapterPermissionError`); re-read current and retry |
| divergent heads | two writers from one base | `detect_conflict` reports; do NOT force-overwrite; human resolution required |
| disk full / permissions | write raises `HandoffWriteError` | previous checkpoint and changelog stay intact (history-first) |
| abandoned session | workflow in `abandoned` | `recover()` reactivates to `working`; audit trail kept |

## Code

```python
from handoff_agent.workflow import WorkflowManager

mgr = WorkflowManager(adapter=adapter)
record = mgr.begin(producer, consumer, project_root)
# ... work ...
mgr.fail(record, reason="provider timeout")
mgr.recover(record)               # → working, files untouched
mgr.checkpoint(record, objective="resume after recovery")
```

Restoring a corrupted checkpoint from history:

```python
import handoff_agent.protocol as protocol
changelog = adapter.read_changelog()
block = changelog[changelog.rfind("```handoff-protocol"):]
cp = protocol.parse_handoff_document(block)   # raises if history is corrupt
assert protocol.verify_identity(cp)
adapter.write_checkpoint(block)               # re-persist the archived checkpoint
```