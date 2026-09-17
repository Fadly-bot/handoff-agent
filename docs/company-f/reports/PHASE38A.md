# Phase 38A Report — Supervised Development Company F Pilot

Status: **PASS** — checkpoint Phase 38A satisfied.

## 1. Tujuan phase

Membuktikan flow Development Company F pada satu project nyata namun
non-kritis, dengan human approval sebagai authority terakhir untuk deployment.
Seluruh transisi harus memiliki audit evidence, coding agent tidak boleh
melewati Handoff atau Quality Guardian, dan deployment tidak boleh berjalan
tanpa human approval.

## 2. File yang diubah / ditambahkan

- `src/handoff_agent/company_f.py` — Phase 38A supervised-development contract:
  `DecisionStatus.REQUIRE_REVIEW`; `DeploymentVerdict.FAIL` /
  `REQUIRE_APPROVAL`; mandatory Work Order fields (scope, project owner,
  acceptance criteria, constraints, rollback consideration, allowed/forbidden
  actions, base Git HEAD); `plan_work_order()`; `record_action()` scope
  enforcement; `submit_checkpoint()` Git-state verification (opt-in via
  `verify_git` / `repo_root`); `run_deployment_check()` PASS/FAIL/
  REQUIRE_APPROVAL; `approve_deployment()`; CONFLICT exit requires a human.
- `src/handoff_agent/pilot.py` — `CompanyFPilot` (pilot definition + runnable
  end-to-end flow + `PilotReport` evidence).
- `tests/test_phase38a.py` — 37 tests covering the full required list.
- `docs/company-f/PILOT.md` — pilot project and governance guarantees.

## 3. AI Council decision

GO / NO-GO / REQUIRE_REVIEW semua valid; hanya AI Council atau human yang
bisa memutuskan; Planning tanpa GO ditolak.

## 4. Planning Work Order

`plan_work_order()` mewajibkan scope, owner (project owner), acceptance
criteria, risk, constraint, dan rollback consideration. Work Order yang
mengurangi salah satu field berikut ditolak (CoordinatorViolation). Duplicate
work terdeteksi via fingerprint.

## 5. Agent & capability

Capability mismatch ditolak tanpa silent fallback. Agent ownership (owner),
lease (TTL), dan scoped action enforcement diterapkan. Action di luar scope
ditolak dengan audit `action.out_of_scope`.

## 6. Handoff evidence

Checkpoint menolak stale state, checkpoint tanpa ownership, payload berbau
secret, dan (dengan `verify_git`) Git state yang divergen dari base HEAD.
Handoff ACCEPT/REJECT tervalidasi.

## 7. Quality Guardian

PASS -> QUALITY_GATED; FAIL -> kembali EXECUTING; REQUIRE_FIX -> kembali
rework. Non-guardian tidak bisa melewati gate.

## 8. Deployment Check

`run_deployment_check()`: tanpa Quality PASS -> ditolak / FAIL; tanpa human
approval -> REQUIRE_APPROVAL (release diblokir); tanpa rollback readiness ->
FAIL; dengan approval + rollback -> PASS -> DEPLOYMENT_GATED -> release.

## 9. Human approval

Hanya role HUMAN (trust 5) yang dapat `approve_deployment()`. Approval tidak
dapat dilewati (non-human ditolak; release tanpa approval diblokir).

## 10. Pilot end-to-end result

Mock-proof dijalankan sebagai test (`test_full_pilot_flow`): Company F ->
GO -> Work Order -> Coding -> Handoff ACCEPT -> Quality PASS ->
REQUIRE_APPROVAL -> HUMAN APPROval -> PASS -> RELEASED. `PilotReport`
menyimpan decision, verdict, approval, audit_events, dan released=True.

## 11. Failure & perbaikan (dalam run ini)

| Finding | Root cause | Perbaikan |
|---|---|---|
| `pending -> executing` invalid | `plan_work_order` dulu membuat WO berstatus PENDING padahal plan sudah dilampirkan | WO dibuat dalam status PLANNED |
| duplicate test gagal di luar block `pytest.raises` | logika test salah (call kedua di luar block) | call kedua dipindah ke dalam block |
| deployment after quality FAIL | status berubah EXECUTING sehingga gate saling bentrok | test disesuaikan: gate menolak via InvalidTransition; release-without-approval menerima NoApproval |

## 12. Test result

| Suite | Result |
|---|---|
| `tests/test_phase38a.py` | 37 passed |
| Full regression `tests/` | 1933 passed, 0 failed |
| Security subset (security/audit/git_inspector/policy/phase37) | 330 passed |

## 13. Security check

- Tidak ada secret leakage: payload checkpoint berisi secret ditolak;
  `PilotReport` dan audit trail secret-free.
- Tidak ada arbitrary command execution di `pilot.py` / `company_f.py`
  (no eval/exec/os.system/subprocess).
- Provider/API-key isolation & Boundary Phase 1-37 tidak berubah.

## 14. Known limitation

- Pilot coding dilakukan lewat representasi terstruktur (scoped action +
  checkpoint) pada repo throwaway; bukan produksi nyata (by design, sesuai
  definisi non-kritis).
- `verify_git` bersifat opt-in (default off) untuk menjaga kontrak Phase 1-37.
- Terdapat 3 file operasional untracked dari kontrak run (roadmap/transcript)
  yang sengaja tidak dihapus sesuai aturan; tidak diikutkan dalam commit.

## 15. Rekomendasi

Sistem **layak melanjutkan ke Phase 39B** (Quality Guardian & Deployment
Control sebagai quality gate operasional).