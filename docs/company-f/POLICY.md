# Phase 32 — Policy, Identity, Trust & Approval Engine

Status: **DONE** — checkpoint Phase 32 PASS.

## 1. Tujuan phase

Membangun mesin kebijakan (policy engine) yang memastikan setiap aksi agen,
perangkat, endpoint, workflow, tool, dan deployment hanya berjalan sesuai
identitas, kapabilitas, trust, scope, dan approval. Desain deny-by-default,
most-restrictive-wins (REQUIRE_APPROVAL > DENY > ALLOW), hard gates tidak
bisa di-bypass oleh ALLOW rules, destructive ops wajib approval, deterministik,
ter-audit, dan terintegrasi dengan telemetry Phase 31.

## 2. File yang diperiksa

- `src/handoff_agent/policy_engine.py` (modul baru Phase 32)
- `tests/test_policy_engine.py` (25 test Phase 32)
- `src/handoff_agent/company_f.py` (Phase 30/31 — integrasi future)
- `src/handoff_agent/telemetry.py` (Phase 31 — domain POLICY)
- `REVISIPHASE30-36.md` (instruksi Phase 32)

## 3. File yang dibuat atau diubah

Dibuat:
- `src/handoff_agent/policy_engine.py` — modul mesin kebijakan (ActionRequest,
  ApprovalTicket/Grant, PolicyDecision, PolicyRule, SubjectProfile, PolicyEngine,
  protect_default_sensitive_files, allow_default_git_operations)
- `tests/test_policy_engine.py` — 25 test komprehensif
- `docs/company-f/POLICY.md`
- `docs/company-f/reports/PHASE32.md` (ini)

Tidak ada file existing yang diubah untuk Phase 32.

## 4. Arsitektur / contract yang ditambahkan

### 4.1 Data Model

- `ActionRequest(subject_id, action, resource, scope)` — request yang akan
  dievaluasi. `canonical()` menghasilkan JSON deterministik (sorted keys).
- `ApprovalTicket(ticket_id, signature, subject_id, action, resource, scope)` —
  tiket approval dengan SHA-256 signature dari canonical request.
- `ApprovalGrant(ticket_id, approver, note, granted_at_ms)` — bukti approval
  diberikan.
- `PolicyDecision(allowed, effect, action, subject_id, resource, scope, rule_id,
  reasons, policy_version, approval_required, approval_ticket)` — hasil
  evaluasi deterministik.
- `PolicyRule(rule_id, action, effect, scope, note, immutable)` — aturan
  kebijakan. Scope mendukung inheritance (scope="prod" berlaku untuk
  "prod.eu"). Action mendukung wildcard "filesystem.*".
- `SubjectProfile(subject_id, kind, trust_level, capabilities, revoked)` —
  profil subjek (agent/device/endpoint/workflow/tool/human).

### 4.2 PolicyEngine

Metod utama:
- `register_subject()` / `revoke_subject()` / `grant_capability()` /
  `revoke_capability()` — manajemen identitas dan trust
- `add_rule()` / `remove_rule()` — aturan kebijakan (immutable rules cannot
  be removed)
- `protect_paths()` / `grant_path()` — proteksi path sensitif
- `allow_git()` / `allow_network()` / `grant_secret()` — allowlist
- `require_approval_for_destructive()` — destructive action gating
- `request_approval()` / `grant_approval()` / `is_approved()` — lifecycle
  approval
- `evaluate(request, approval_ticket=)` — evaluasi deterministik
- `assert_allowed(request)` — evaluate + raise PolicyDenied
- `audit_trail()` / `policy_report()` / `explain(decision)` — audit

### 4.3 Evaluation Pipeline (deny-by-default)

```
Layer 0: Subject resolution → unknown/revoked → DENY (return early)
Layer 1: Capability gate → missing → DENY; granted → ALLOW
Layer 2: Protected path gate → unwritable → DENY; granted → ALLOW
Layer 3: Git allowlist → not in list → DENY; allowed → ALLOW
Layer 4: Network allowlist → not allowed → DENY; allowed → ALLOW
Layer 5: Secret gate → not granted → DENY; granted → ALLOW
Layer 6: Destructive gate → ALWAYS → REQUIRE_APPROVAL
Layer 7: Explicit rules → most-restrictive-wins (only if uncategorized or gate=ALLOW)
Approval: REQUIRE_APPROVAL + satisfied signature-bound ticket → ALLOW
```

Hard gates (subject/protected/path/capability-missing/git/network/secret) CANNOT
be overridden by ALLOW rules. Destructive REQUIRE_APPROVAL CANNOT be overridden.

### 4.4 Default Sensitive Files

`.git/**`, `**/.env`, `**/.env.*`, `**/*.pem`, `**/*.key`, `**/*.p12`,
`**/id_rsa`, `**/id_ed25519`, `**/credentials*`, `**/secrets/*`,
`**/*secret*`, `**/*token*`

### 4.5 Default Git Allowlist

`git.status`, `git.diff`, `git.log`, `git.show`, `git.branch`,
`git.rev-parse`, `git.ls-files` (read-only operations)

### 4.6 Approval Binding

Approval tickets carry a SHA-256 signature of the canonical request. The ticket
is bound to the exact `(subject_id, action, resource, scope)` tuple. Forging
a ticket for a different resource/request does NOT satisfy the approval gate.

## 5. Daftar test yang dijalankan

- `tests/test_policy_engine.py` — 25 test:
  - Identity & Trust (5): unknown agent denied, revoked device denied, unknown
    endpoint denied, capability revocation, trust level recorded
  - Protected Paths (2): write denied without grant, granted path allows
  - Git/Network/Secret (4): git push denied default, git status allowed,
    network not allowlisted denied, secret access denied/granted
  - Destructive Approval (3): requires approval, ticket unlocks, forgery blocked
  - Deterministic Conflicts (3): conflicting rules deny, scope inheritance,
    repeated evaluation determinism
  - Explanation & Audit (3): explanation available, audit trail recorded,
    assert_allowed raises on deny
  - Telemetry Integration (1): policy events emitted to tracer
  - No Bypass (3): protected path not overridden by ALLOW rule, revoked subject
    not overridden, policy version recorded
  - Policy Report (1): report shape valid
- `tests/test_company_f.py` (regresi kontrak Phase 30)
- `tests/test_phase31_observability.py` (regresi Phase 31)
- `tests/test_telemetry.py` (regresi telemetry)
- `tests/test_security.py` + `TestDependencyAudit` + `TestSecretLeakAudit`
- Full suite `tests/`

## 6. Hasil test sebelum perbaikan

- `policy_engine.py` ditulis: smoke test 10 assertions pass
- `test_policy_engine.py` awal: **7 fail, 18 pass**
  - Fail: reason substring checks (tuple membership vs substring), scope
    inheritance inverted, destructive approval_required wrong, default-sensitive
    missing `secret.txt` pattern

## 7. Daftar masalah yang ditemukan

1. Reason assertion `"unknown subject" in d.reasons` fails because `in` on tuple
   checks element membership, not substring — reasons are `("unknown subject —
   deny by default",)` where substring present but full string not exact.
2. Scope inheritance: `PolicyRule.matches()` correctly checks
   `scope.startswith(self.scope + ".")` but evaluate had uncategorized logic
   starting from `Effect.DENY` and never raising to ALLOW when explicit ALLOW
   rule matched.
3. `approval_required` field set to `(resolved_effect == ALLOW and approved)`
   which is True only when approved — should be True when NOT approved.
4. Default protected file set lacked `**/*secret*` and `**/*token*` patterns.
5. Approval ticket forgery: `evaluate()` compared `ticket.signature !=
   request.canonical()` — digest vs JSON string. Should compare digest to
   `sha256(request.canonical())`.

## 8. Perbaikan yang dilakukan

1. Fixed reason assertions to use `any("X" in r for r in d.reasons)`.
2. Rewrote uncategorized evaluation branch: when no gate fired and explicit
   rules match, pick most-restrictive via `min()` on precedence index +
   rule_id; when no rules match, deny by default with reason.
3. Fixed `approval_required` to `effect == REQUIRE_APPROVAL and not approved`.
4. Added `**/*secret*` and `**/*token*` to default protected file patterns.
5. Fixed approval comparison: `ticket.signature !=
   hashlib.sha256(request.canonical().encode("utf-8")).hexdigest()`.

## 9. Hasil test setelah perbaikan

- `tests/test_policy_engine.py`: **25 passed**
- Targeted regressions (policy + company_f + phase31 + telemetry): **120 passed**
- Security + dependency + secret leak audits: **209 passed**
- Full suite `tests/`: **1642 passed, 0 failed**

## 10. Hasil security check

- `tests/test_security.py` + `TestSecretLeakAudit` + `TestDependencyAudit`:
  **209 PASS**
- `policy_engine.py` uses only `hashlib`, `json`, `time`, `uuid`, `dataclasses`,
  `enum`, `fnmatch` — all stdlib
- No hardcoded tokens, keys, or credentials anywhere in module
- Protected paths matched by pattern only; subjects are identifiers only
- Approval signatures are SHA-256 hashes of canonical request JSON — not reversible
- Audit trail and policy report are secret-free by construction

## 11. Hasil full regression

- `tests/`: **1642 passed, 0 failed**
- Includes Phase 30 (38) + Phase 31 (10) + Phase 32 (25) + all earlier phases
- `docs/` unmutated (git status clean on docs)

## 12. Known limitation dan risiko

- Policy engine is in-memory; persistence (save/load policy state to disk) is
  not implemented — considered for Phase 33/34/35.
- Approval lifecycle is synchronous; no timeout/expiry for pending approvals
  (could be added later).
- Subject trust_level is recorded but not used as a policy gate in evaluation
  (foundation for future trust-scored rules).
- No wildcard subject matching in rules (e.g. `subject: "agent-*"`); all
  subjects referenced by exact ID.
- Policy rules stored in dict; very large rule sets (>10k) may degrade
  performance (not expected in local-only deployment).

## 13. Checkpoint PASS / FAIL

**PASS.** Semua item: test Phase 32 (25) green, deny-by-default verified,
hard-gate no-bypass verified (protected path / revoked subject / missing
capability / git/network/secret), destructive approval + ticket binding
verified (forgery blocked), scope inheritance verified, deterministic
evaluation verified, audit trail + explanation available, policy report
generated, telemetry integration (POLICY domain events emitted), security
audit passed, full regression 1642 passed, no credential leakage.

## 14. Git status

Tracked: `policy_engine.py`, `test_policy_engine.py`, `docs/company-f/POLICY.md`,
`docs/company-f/reports/PHASE32.md` (baru). File instruksi/audit lama tetap
untracked di luar scope.

## 15. Commit hash

`<pending — fill after push>`

## 16. Push status

`<pending — fill after push>`

## 17. Rekomendasi / blocker untuk phase berikutnya

- Wajib input Phase 33+: policy engine digunakan sebagai gate utama untuk
  Universal Adapter/Tool/Sandbox Boundary. Setiap aksi adapter/tool harus
  melewati `PolicyEngine.assert_allowed()`.
- Destructive approval tickets harus ter-trace ke project trace (Phase 31)
  untuk bukti audit end-to-end.
- Subject trust_level dapat dikembangkan menjadi trust-scored rules untuk
  Phase 35/36 (adaptive trust).
- **Blocker: tidak ada.**
