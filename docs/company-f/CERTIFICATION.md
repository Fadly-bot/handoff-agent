# CERTIFICATION.md — Production Certification Guide

Guide untuk operator yang menjalankan production certification terhadap
Development Company F sebelum deployment.

## 1. Ringkasan

Production certification adalah validasi end-to-end yang membuktikan
seluruh komponen Development Company F siap digunakan. Certification
dihasilkan oleh `handoff_agent.certification` dan mengembalikan verdict
`production_ready` atau `blocked` dengan evidence-based gate results.

## 2. Prasyarat

- Python 3.10+ (stdlib-only runtime)
- Seluruh source code di `src/handoff_agent/` terinstal atau tersedia
  di `PYTHONPATH`
- Tidak ada dependency eksternal yang diperlukan (hanya stdlib)

## 3. Menjalankan Certification

```bash
cd /path/to/handoff-agent
PYTHONPATH=src python3 -c "
import handoff_agent.certification as c
report = c.run_production_certification()
print(c.certification_summary(report))
"
```

## 4. Struktur Report

Report adalah dict dengan field:

| Field | Deskripsi |
|-------|-----------|
| `verdict` | `"production_ready"` atau `"blocked"` |
| `gate_count` | Jumlah gates (12) |
| `passed_gates` | Jumlah gates yang passed |
| `resolved_blockers` | Gates yang sudah pass |
| `unresolved_blockers` | Blocker messages dari gates yang gagal |
| `handoff_version` | Versi package (`0.4.0`) |
| `evidence_by_gate` | Detail per-gate: ok, evidence, notes, blocker |
| `risk_register` | Risk list per gate dengan severity |
| `release_recommendation` | APPROVED / NOT approved |

## 5. Certification Gates

### Gate 1: Feature Freeze
Memastikan `OPS_COMMANDS` stabil (15 commands) — tidak ada scope creep.

### Gate 2: Architecture Audit
12 core modules dapat diimport: ops, policy_engine, telemetry, registry,
workflow, sync, remote, tool_registry, git_helper, reliability,
messaging, sandbox.

### Gate 3: Security Language
Stdlib-only, tidak ada `shell=True`, tidak ada `eval`/`exec` calls,
tidak ada raw `socket.socket`, subprocess hanya di git_helper/sandbox.

### Gate 4: Secret and Credential Audit
Scan src/ + docs/ untuk assignment patterns (`api_key=`, `secret=`, dll.)
dengan exemption untuk probe values (`thisisasecret` + lowercase) dan
regex detection patterns. Token scan untuk `sk-` prefixed values
(brace patterns exempt).

### Gate 5: Identity, Trust, Policy, Approval
- Unknown subject denied
- `git.push` denied
- `destructive.purge` requires approval
- Capability gating enforced (`tool.purge` not allowed without capability)

### Gate 6: Boundaries (Filesystem/Process/Git/Network/SSRF)
- Tool conformance passes for generic adapter
- GitRunner blocks all FORBIDDEN commands
- Literal verb placement enforced by release audit

### Gate 7: Reliability (Queue/Recovery/Backup/DR)
- Reliability conformance suite passes
- Recovery applies stale lease cleanup
- Checkpoint save/restore roundtrip
- Backup snapshot/verify/restore

### Gate 8: Adapters and Tools
- All ADAPTER_KINDS conformance passes (mcp, cli, skill, generic)
- Sandbox refuses secret-shaped writes

### Gate 9: Messaging/Workflow/Remote/Sync
- MessageBroker lifecycle: send→deliver→acknowledge→process→complete
- Workflow: begin→checkpoint→request_handoff→accept_handoff→complete
- Sync: multi-device registration, session start, lock acquire
- Remote: health report

### Gate 10: Scenarios (Multi-agent/Multi-device/Failure)
- Flaky handler retry succeeds
- Duplicate suppression after completion
- NonRetryableError → dead-letter queue
- 300-message performance baseline < 10s

### Gate 11: Install/Upgrade/Compatibility
- Install simulation (importable)
- Uninstall simulation (not importable)
- Upgrade simulation (re-import ok)
- Ops compatibility matrix (33 proven, 0 unproven)

### Gate 12: Documentation/Release Artifacts
19 required docs present:
README, LICENSE, CHANGELOG, HANDOFF, 8 company-f guides,
7 phase reports (PHASE30-36).

## 6. Interpreting Results

### All 12 gates pass → `production_ready`
- Certification APPROVED for production use
- Risk register shows all gates at severity `info`

### Any gate fails → `blocked`
- `unresolved_blockers` lists the failing gate(s) with reason
- `release_recommendation` says NOT approved
- Fix the blocker and re-run certification

### Gate crashes → `blocked`
- If a gate throws an unexpected exception, the orchestrator catches it
- The gate is marked blocked with `gate crashed: <type>: <msg>`
- This prevents partial certification from passing

## 7. Troubleshooting

### "missing artifacts"
Certification requires all 19 docs to exist and be non-empty. Create the
missing files.

### "reliability conformance failed"
The reliability conformance suite found a regression. Check queue/backup/
checkpoint behavior in `reliability.py`.

### "sandbox allowed a secret-shaped write"
Sandbox must reject writes containing credential-like content. The
detection pattern matches `api_key=`, `access_token=`, `secret=`,
`password=` with values >= 6 chars.

### "gate crashed: <Exception>"
Check the exception traceback and fix the underlying issue.

## 8. Re-running After Fixes

After fixing a blocker, re-run the full certification:

```bash
PYTHONPATH=src python3 -c "
import handoff_agent.certification as c
report = c.run_production_certification()
print(c.certification_summary(report))
assert report['verdict'] == 'production_ready', 'Still blocked'
"
```

## 9. Exit Codes

Certification does not define exit codes — it is a library function.
For CLI integration, use the `ops` subcommand:

```bash
PYTHONPATH=src python3 -m handoff_agent ops health
PYTHONPATH=src python3 -m handoff_agent ops compatibility
```

## 10. Version History

- `0.4.0` — Phase 36: 12 gates, exception-safe orchestrator
- Previous versions: see CHANGELOG.md