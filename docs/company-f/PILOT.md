# Company F Pilot — Supervised End-to-End Proof (Phase 38A)

Status: **PASS**

## 1. Pilot project (non-critical)

| Field | Value |
|---|---|
| project_id | `company-f-pilot` |
| name | Company F Pilot (non-critical) |
| project owner | Human Approver |
| human approver | `human-approver` |
| non-critical note | Internal helper utility — no production data, no PII, no customer release. Safe for a supervised pilot of the Company F flow. |

The pilot runs against a real throwaway git repository created for the proof.
The supervised flow is executed by `src/handoff_agent/pilot.py`
(`CompanyFPilot.run()`).

## 2. Flow executed

```
Company F
  -> AI Council            GO   (rationale recorded)
  -> Planning Council      fully-specified Work Order + risk register
  -> Coding Agent          capability-verified, owned, leased, scoped action
  -> Handoff Agent         Git-state-verified checkpoint, ACCEPT
  -> Quality Guardian      PASS
  -> Deployment Check      REQUIRE_APPROVAL (human gate) then PASS
  -> Human Approval        granted by human approver (cannot be bypassed)
  -> release               RELEASED
```

## 3. Governance guarantees proven

- AI Council decisions: GO / NO-GO / REQUIRE_REVIEW all valid.
- Planning without GO rejected.
- Work Orders must carry scope, owner, acceptance criteria, risk,
  constraint, and rollback consideration or they are rejected.
- Capability mismatch is refused (no silent fallback).
- Out-of-scope actions are rejected and recorded as `action.out_of_scope`
  audit events.
- Checkpoints require valid ownership; stale checkpoints rejected.
- Git state divergence at handoff is handled safely (checkpoint rejected).
- Duplicate work orders are detected.
- CONFLICT state may only be exited by a human reviewer.
- No silent overwrite (lease + ownership enforced).
- Agent crash / coordinator restart never loses state (persistence).
- Deployment without Quality PASS is blocked.
- Deployment without human approval yields REQUIRE_APPROVAL and release is
  blocked.
- Every transition is recorded as immutable audit evidence.