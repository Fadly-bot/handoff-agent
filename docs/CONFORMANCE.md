# Universal Conformance Suite

The conformance suite (`handoff_agent.conformance`) certifies that any
adapter — file, CLI, API, or a future operation adapter — satisfies the
Universal Handoff protocol and its security invariants. The same 21 checks
apply to every adapter.

## Running

```python
from handoff_agent.adapters import create_adapter
from handoff_agent.conformance import run_conformance_suite

adapter = create_adapter("file", project_root=".")
adapter.start()

report = run_conformance_suite(adapter, {"repo": ".", "tmp": "/tmp/ext"})
if not report.clean:
    for check in report.failed:
        print(f"[FAIL] {check.name}: {check.reason}")
```

`report.to_dict()` produces a machine-readable result:

```json
{
  "suite_version": "1",
  "adapter": "file",
  "protocol": {"name": "universal-handoff-protocol", "version": 1},
  "total": 21,
  "passed": 21,
  "failed": 0,
  "skipped": 0,
  "clean": true,
  "checks": []
}
```

Each check reports `passed`, `failed`, or `skipped` (a skip is only used
when the adapter legitimately cannot exercise a path, e.g. a read-only
adapter with no checkpoint to read — never as a way to dodge a security
guarantee).

## Check catalog

| Check                   | Verifies                                                         |
|-------------------------|------------------------------------------------------------------|
| checkpoint.creation     | checkpoint can be created through the adapter                    |
| checkpoint.read         | current checkpoint is readable                                   |
| checkpoint.update       | update from a known base advances the identity                   |
| checkpoint.validation   | current checkpoint passes protocol + schema validation           |
| identity.verification   | checkpoint identity can be verified                              |
| capability.negotiation  | capability negotiation is safe and deterministic                 |
| permission.boundary     | every known capability negotiates cleanly                        |
| read_only               | read-only adapters refuse writes                                 |
| write.authorization     | writes require explicit authorization                           |
| containment             | no path escapes the project root                                 |
| symlink.safety          | symlinked escapes are rejected                                   |
| secret.filtering        | secret-looking content is refused on write                       |
| git.safety              | Git access is read-only / unavailable                            |
| state.consistency       | checkpoint state is consistent + verified                        |
| ai.switching            | different agent identities read identical bytes                  |
| multi_agent.workflow    | a second agent can adopt the checkpoint                          |
| conflict.detection      | two-sided divergence is flagged                                  |
| stale.detection         | stale writers are detectable                                     |
| corrupted.checkpoint    | corrupted checkpoints fail validation                            |
| protocol.version        | unsupported protocol versions are rejected                       |
| adapter.failure_isolation | one adapter's failure never cascades                          |

## The conformance contract

The suite is built exclusively on the universal adapter interface:

- `read_handoff()`, `write_checkpoint(content, expected_base=...)`,
  `project_state()`, `validate_checkpoint()`, `read_changelog()`,
  `negotiate(...)`, `can(...)`.

That is why one suite certifies every adapter, including future ones. A
platform must not satisfy these checks by accident — the suite runs the
real adapter against a real Git repository.

## Security checks are executable, not aspirational

- `containment` and `symlink.safety` attempt real escapes and assert they
  are refused.
- `secret.filtering` feeds a secret-shaped payload and asserts the write is
  refused.
- `git.safety` asserts Git operations are read-only by contract.
- `adapter.failure_isolation` creates an unknown adapter and asserts the
  registry is unaffected.

## Failure policy

A single failed check makes the report `clean: false`. Conformance checks
never raise out of the suite — failures are captured with the underlying
exception type and message in the check's `reason`, so the suite can be used
in CI and in test suites without crashing the runner.