# Release Notes — v0.4.0 (release candidate)

**Universal Handoff — production workflow release.**

Protocol: `universal-handoff-protocol` v1 (unchanged, backward compatible).
Runtime: Python 3.11+, standard-library only.

## Highlights

- **Phase 19 — Real AI Integration Validation** (`integration.py`)
  - Certifies all 12 platforms + generic fallback against their *actual*
    capability/interfaces (never assumed).
  - Environment-based credential loading with isolation-first handling:
    only the env-var *name* ever leaves the process.
  - Model-override validation, protocol version negotiation, capability
    mapping, checkpoint create/update/validate, CHANGELOG lifecycle, Git-state
    verification, limitation detection, unsupported-interface handling,
    safe provider failure, and no automatic fallback.
  - Mock mode is the CI default; live API tests are strictly opt-in
    (`HANDOFF_LIVE_TESTS=1` + credential present).
  - Comprehensive machine-readable integration report + actual integration
    matrix.
- **Phase 20 — Universal AI-to-AI Workflow** (`workflow.py`)
  - Producer → handoff → consumer state machine with explicit transitions.
  - Agent/session/project identity, checkpoint ownership, explicit
    acknowledgement with token, human-approval boundary, duplicate/idempotent
    checkpoint operations, stale rejection, conflict detection.
  - Seven-field continuity verification (task, context, constraints,
    decisions, validation, artifacts, Git) before continuation.
  - Non-destructive recovery from failed/abandoned/interrupted workflows;
    append-only audit trail; ASCII state-machine/flow visualization.
- **Phase 21 — Stress, Conflict & Recovery** (`tests/test_stress.py`)
  - Sequential/parallel agents, simultaneous reads, stale-write races,
    checkpoint collisions, divergent objectives, concurrent Git mutations
    (dirty/staged/untracked/deleted/renamed/branch/HEAD), corruption,
    invalid schema, unsupported version, interrupted writes, provider/MCP/CLI/
    filesystem/disk failure injection, fuzz protocol validation, large
    checkpoint/repo, 60-step workflow — with STRESS INVARIANTS enforced.
- **Phase 22 — Release hardening**
  - Version bumped to 0.4.0 across every surface (package, constants, MCP,
    adapters, installer, README, docs).
  - New audits: protocol/capability/adapter/workflow contract,
    concurrency/recovery, credential isolation, MCP/Skill security, API
    transport, package integrity, temp-file/cache/log/secret-output.
  - New docs: `WORKFLOW`, `RECOVERY`, `CONFLICT_RESOLUTION`, `SECURITY`,
    `CLI_GUIDE`, `TROUBLESHOOTING`, `RELEASE_NOTES`.

## Install

```bash
bash install.sh
```

## Verify

```bash
PYTHONPATH=$PWD/src:$PWD/tests .venv/bin/python -m pytest tests/ -q
```

## Release status

This is a **release candidate**. Tagging, pushing, and publishing are human
actions and were **not** performed automatically.