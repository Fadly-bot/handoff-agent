# Phase 31 Report — Observability, Trace & Audit Evidence

Status: **PASS** — checkpoint Phase 31 satisfied.

## 1. Tujuan phase

Membangun observability aman yang membuktikan perjalanan kerja dari AI Council
sampai Deployment Check tanpa membocorkan data sensitif: trace end-to-end,
penghubung antar-stage (Work Order, checkpoint, review, approval), metrik
retry/timeout/failure/cancel, deteksi degraded state, redaksi secret, mode
disabled, dan audit export secret-free.

## 2. File yang diperiksa

- `src/handoff_agent/telemetry.py` (event model, span, trace, redaction,
  metrics, health, report)
- `src/handoff_agent/company_f.py` (state machine Phase 30, audit evidence)
- `src/handoff_agent/security.py` (pattern redaksi secret untuk parity)
- `tests/test_telemetry.py` (48 test telemetry lama)
- `tests/test_company_f.py` (38 test kontrak Phase 30)
- `REVISIPHASE30-36.md` (instruksi Phase 31)

## 3. File yang dibuat atau diubah

Dibuat:
- `src/handoff_agent/audit.py` — build/export audit trace secret-free
- `tests/test_phase31_observability.py` — 10 test Phase 31
- `docs/company-f/OBSERVABILITY.md`
- `docs/company-f/reports/PHASE31.md` (ini)

Diubah:
- `src/handoff_agent/telemetry.py` — domain baru (project/decision/workorder/
  quality/deployment), parity redaksi (token=, private_key=, quoted sk-/ghp_/
  xox), pruning span saat trace di-evict
- `src/handoff_agent/company_f.py` — param opsional `tracer=`, helper `_trace`,
  emit event per stage (project.register, decision.make, workorder.create/
  plan/assign/checkpoint/approve/release/failed/reject/cancel/resume,
  handoff.accept/reject, quality.gate, deployment.gate)

## 4. Arsitektur / contract yang ditambahkan

- Satu project trace berisi seluruh stage dari decision sampai deployment gate
  (shared trace_id di bawah span project).
- Envelope event kanonikal tetap `TelemetryEvent` (trace_id, span_id,
  parent_span_id, event_chain hash-chain, domain, operation, status, metrik,
  actor, resource, error taxonomy, metadata ter-redaksi).
- `build_company_trace()` menghasilkan dict JSON-ready; `export_audit_json()`
  menulis ke path yang diberikan dan menolak menulis bila masih ada konten
  secret-like setelah redaksi.
- Parity redaksi telemetry == security.py (token=, private_key=, quoted
  `ghp_`/`sk-`/`xox`).
- Eviction trace juga menghapus span-nya (perbaikan kebocoran memori).

## 5. Daftar test yang dijalankan

- `tests/test_phase31_observability.py` — trace E2E decision→deployment,
  linking WO/checkpoint/review/approval, konflik → blocked, metrik
  retry/timeout/failure/cancellation, degraded detection, audit export bebas
  secret, parity redaksi, disabled mode, pruning span
- `tests/test_company_f.py` (regresi kontrak)
- `tests/test_telemetry.py` (regresi telemetry lama)
- `tests/test_release.py::TestDependencyAudit` + `TestSecretLeakAudit`,
  `tests/test_security.py`
- Full suite `tests/`

## 6. Hasil test sebelum perbaikan

- Phase 31 baru: 9 pass, 3 run berubah; perbaikan kecil iteratif (import
  absolut, trace checkpoint belum ada, handoff ter-icon ke trace terpisah,
  build_company_trace belum ter-redaksi, `test_runtime_is_stdlib_only` gagal
  karena import relatif `from .telemetry`)
- Full suite setelah fase 31 tanpa perbaikan terakhir: `1615 passed, 1 failed`

## 7. Daftar masalah yang ditemukan

1. Import relatif `from .telemetry`/`.persistence` di `audit.py` ditolak oleh
   dependency audit (`test_runtime_is_stdlib_only`).
2. `submit_checkpoint` belum memancarkan event `workorder.checkpoint`.
3. Event `handoff.accept` dibuat dengan `project_id=""` sehingga ter-icon ke
   trace terpisah (putus continuity trace).
4. `build_company_trace()` mengembalikan dict mentah tanpa redaksi — nilai
   secret muncul di JSON audit.

## 8. Perbaikan yang dilakukan

1. Import absolut `handoff_agent.*` di `audit.py` (mengikuti gaya company_f.py).
2. Trace `workorder.checkpoint` ditambahkan setelah transisi CHECKPOINTED.
3. `evaluate_handoff` kini menurunkan `handoff_project` dari work order dan
   memancarkan handoff pada project trace yang sama.
4. `build_company_trace()` menerapkan `_scrub` (redaksi semua string leaf).

## 9. Hasil test setelah perbaikan

- `tests/test_phase31_observability.py` + `test_company_f.py` +
  `test_telemetry.py`: **96 passed**
- `tests/test_release.py::TestDependencyAudit` (+ fase-31): **12 passed**
- Full suite: **1616 passed, 0 failed** (~91s)

## 10. Hasil security check

- `tests/test_security.py` + `TestSecretLeakAudit`: **PASS** (206 test).
- Scan manual `audit.py`, `company_f.py`, `telemetry.py`: bebas pola secret
  (sk-/ghp_/xox/token/api_key assignment).
- Audit export menolak menulis bila redaksi masih menyisakan konten
  secret-like; string leaf semua melalui `redact_text`.
- Tidak ada credential leakage pada event/log/report/JSON (diuji).

## 11. Hasil full regression

- `tests/`: **1616 passed, 0 failed** — termasuk Phase 30 (38) + Phase 31 (10)
  dan seluruh regresi Phase 1–29.
- Messaging, workflow, remote handoff, synchronization: semua PASS.
- `docs/` tidak termutasi (git status bersih dari perubahan docs).

## 12. Known limitation dan risiko

- Trace dalam memori oleh desain; persistensi telemetry penuh (bukan hanya
  ring) belum ada — dipertimbangkan untuk Phase 35/36 (ops). Kehilangan proses
  menghilangkan event stream dalam memori; audit evidence koordinator tetap
  bertahan di state JSON `company_f`.
- Sampling head-based; saat sampling < 1.0 hanya trace bermasalah yang selalu
  disimpan. Default tetap 1.0 (local-only).
- Audit export menyertakan ringkasan health telemetry, bukan dump event penuh
  (boundary data).

## 13. Checkpoint PASS / FAIL

**PASS.** Semua item: test Phase 31 green, trace hubungan antar-stage
tervalidasi (decision→release dalam satu trace; WO↔checkpoint↔review↔approval
terhubung), audit evidence tervalidasi (build_company_trace/export), secret
filtering tervalidasi (parity + redaction), tanpa credential leakage, full
regression PASS, working tree bersih, commit + push.

## 14. Git status

Tracked: `telemetry.py`, `company_f.py` (modif) + `audit.py`,
`test_phase31_observability.py`, `docs/company-f/*` (baru). File instruksi/
audit lama tetap untracked di luar scope.

## 15. Commit hash

`9ea6ca4 feat(phase-31): observability, trace, and audit evidence`

## 16. Push status

`git push origin master` sukses — `a90267d..9ea6ca4`.

## 17. Rekomendasi / blocker untuk phase berikutnya

Wajib input Phase 32: telemetry trace dan audit evidence Phase 31 dipakai
sebagai bukti keputusan policy (setiap permit/deny/require_approval harus
ter-trace ke project yang sama). **Blocker: tidak ada.**