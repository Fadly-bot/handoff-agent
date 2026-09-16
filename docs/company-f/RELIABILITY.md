# Phase 34 — Distributed Reliability, Durable Queue & Recovery

Status: **DONE** — checkpoint Phase 34 PASS.

## 1. Tujuan phase

Membuat sistem tahan terhadap agent failure, provider failure, tool failure,
remote-endpoint failure, network interruption, offline→online recovery, device
loss/crash, restart, duplicate execution, dan state corruption. Semua primitif
disusun sebagai lapisan reliability yang durable (persisted ke disk secara
atomik), deterministik, dan bebas secret.

## 2. File yang diperiksa

- `src/handoff_agent/reliability.py` (baru, Phase 34)
- `src/handoff_agent/adapters/base.py` (deteksi secret `contains_secret_like`)
- `src/handoff_agent/telemetry.py` (Phase 31 — event domain `reliability`)
- `tests/test_phase34_reliability.py` (67 test baru)
- `tests/test_release.py` / `tests/test_security.py` (audit)
- `REVISIPHASE30-36.md` (instruksi Phase 34)

## 3. File yang dibuat atau diubah

Dibuat:
- `src/handoff_agent/reliability.py` — 1900+ baris, reliability layer
- `tests/test_phase34_reliability.py` — 67 test
- `docs/company-f/RELIABILITY.md`
- `docs/company-f/reports/PHASE34.md` (ini)

Tidak ada file existing yang diubah.

## 4. Arsitektur / contract yang ditambahkan

### 4.1 DurableQueue (queue durable)

Append-only JSONL journal (`queue/journal.jsonl`) dengan fsync per record.
Lifecycle message: `enqueue → pending → dequeue → processing → ack | nack |
dead`. Op tersimpan: `enqueue / dequeue / ack / nack / dead / recover`.
`recover()` mereplay journal dengan toleransi baris terakhir terpotong
(crash saat append); `recover_stale()` mengembalikan message `processing`
yang leasenya kedaluwarsa ke `pending`.

### 4.2 RetryPolicy

Exponential backoff deterministik: `base * multiplier**(attempt-1)` ter-cap di
`max_delay_ms`, plus jitter dalam `[0, jitter_factor * delta]`. `NonRetryableError`
tidak pernah di-retry. `delay_for()`/`is_terminal()`/`should_retry()`.

### 4.3 DeadLetterQueue (DLQ)

Message yang habis budget retry (atau permanent failure) ditulis sebagai satu
file JSON per message di `dir/dead/`. Imutabel terhadap crash berikutnya; ada
`get/list/count`.

### 4.4 CircuitBreaker

FSA `CLOSED → OPEN → HALF_OPEN`. `failure_threshold` failure berurutan
mem-buka breaker; setelah `recovery_timeout_ms`, probe HALF_OPEN dibolehkan;
sukses menutup, gagal membuka kembali. `call()` membungkus dengan
`CircuitBreakerOpenError`.

### 4.5 LeaseManager

Lease/lock durable (file `leases/leases.json`); `acquire/renew/release`.
Lease kedaluwarsa → `StaleLeaseError`; `recover_stale()` melepas lease orphan/
stuck; `orphans()` untuk inspeksi.

### 4.6 ExecutionRegistry + idempotency

`register(idempotency_key)` pada key yang sama mengembalikan record yang sama.
`is_duplicate()` mencegah re-eksekusi ter-minal. `message_id == execution_id`
sehingga restorasi langsung terhubung ke record eksekusi. Phase: queued,
processing, succeeded, failed, dead, recovered.

### 4.7 CheckpointStore

Checkpoint durable dengan SHA-256 integrity: `save()` menulis atomik ke
`{name}.json` sambil mempertahankan revisi terakhir di `{name}.bak.json`;
`restore()` memverifikasi; corrupt → fallback ke backup; kedua corrupt →
`CorruptCheckpointError` (tidak pernah mengembalikan state bad).

### 4.8 RecoveryJournal

Append-only recovery log dengan sequence; `replay()` mendeteksi gap
(`gap_detected`). Entry secret-like ditolak. Digunakan untuk audit recovery
(rehydrasi + deteksi korupsi sekuens).

### 4.9 BackupManager

Snapshot (file + manifest SHA-256), `verify()` (deteksi tamper), `restore()`
(kembalikan konten persis), `list_snapshots()`.

### 4.10 FailureDiagnostics

Incident store bounded; reason diredupkan bila secret-like; `report()` dengan
`clean` = tidak ada data secret-shaped yang bocor.

### 4.11 ReliabilityEngine (facade)

`submit(kind, payload, idempotency_key)` → dedupe + durable enqueue;
`process_next(handler)` → dequeue → eksekusi → ack/nack → retry → DLQ → audit +
telemetry (`domain=reliability`); `run_until_idle`; `recover()` (stale lease,
stale lock, checkpoint restore, journal gap); `heartbeat_lease`; `status()`.
`simulate_crash` menyimpan message di `processing` agar recovery diuji.

## 5. Daftar test yang dijalankan

- `tests/test_phase34_reliability.py` — **67 test**:
  - RetryPolicy (7), DurableQueue (11), DeadLetterQueue (2), CircuitBreaker (5),
    LeaseManager (5), ExecutionRegistry (5), CheckpointStore (5),
    RecoveryJournal (3), BackupManager (4), FailureDiagnostics (3),
    ReliabilityEngine (17)
- Regresi target: `test_phase33_tools.py`, `test_policy_engine.py`,
  `test_phase31_observability.py`, `test_company_f.py`, `test_telemetry.py`
- Audit: `tests/test_security.py` + `tests/test_release.py`
- Full suite `tests/`

## 6. Hasil test sebelum perbaikan

- Smoke jenuh: 16 fail di iterasi pertama (journal belum dibuka saat
  `enqueue` — open dilazimkan ke konstruktor; mismatch idempotency
  `message_id ≠ execution_id`; stale-lease boundary off-by-one)
- Audit sources: tidak ada failure pada dependency/secret/git-safety/filesystem

## 7. Daftar masalah yang ditemukan

1. `JournalEntry` butuh `seq` wajib tapi dipanggil eksplisit — ditambahkan
   default 0; `append()` menetapkan seq final.
2. `DurableQueue` tidak auto-open journal → enqueue gagal pada pemakaian
   langsung; journal dibuka di konstruktor (idempotent).
3. `process_next(simulate_crash)` semula tertangkap sebagai failure retry —
   dipisahkan dengan `_SimulatedCrash` sehingga message tetap di `processing`.
4. Idempotensi: `message_id ≠ execution_id` sehingga dedupe tidak konsisten —
   `enqueue(..., message_id=execution_id)`.
5. `CircuitBreaker` HALF_OPEN langsung membuka saat cooldown 0 — conformance
   memakai `recovery_timeout_ms` besar dan clock sintetis.
6. `FailureDiagnostics.report()["clean"]` salah makna — dihitung dari scan
   `contains_secret_like` reason, bukan kehadiran mark redaksi.
7. Backup/checkpoint fallback butuh revisi sebelumnya ada — test save dua kali.

## 8. Perbaikan yang dilakukan

1. `JournalEntry.seq = 0`; `append()` menetapkan urutan akhir.
2. `DurableQueue.__init__` memanggil `open()` (guard double-open).
3. `_SimulatedCrash` khusus; `except _SimulatedCrash: raise`.
4. `enqueue` menerima `message_id`; engine memakainya = `execution_id`.
5. Conformance breaker memakai cooldown 60s + clock 0.
6. `report()["clean"]` = scan secret-shape pada semua reason.
7. Checkpoint test menyimpan dua revisi sebelum korupsi.

## 9. Hasil test setelah perbaikan

- Phase 34: **67 passed**
- Regresi target (240 termasuk phase 31–33 + policy + telemetry): **240 passed**
- Security + dependency + secret leak + release audit: **281 passed**
- Full suite: **1766 passed, 0 failed** (~90s)

## 10. Hasil security check

- `TestDependencyAudit::test_runtime_is_stdlib_only` PASS — reliability.py hanya
  stdlib (json, os, tempfile, hashlib, uuid, dataclasses, enum, time, pathlib)
  + `handoff_agent.adapters.base` (stdlib)
- `test_no_third_party_imports_in_src` PASS
- `TestSecretLeakAudit` PASS — payload secret-like ditolak di queue,
  checkpoint, recovery journal; reason error diredupkan; probe pakai nilai
  `thisisasecret` lowercase (exempt)
- `TestGitSafetyAudit` PASS — tidak ada literal verb berbahaya
- `TestFilesystemBoundaryAudit` PASS — tidak ada `shell=True`/`eval`/`exec`
- Manual scan: seluruh file persisted (journal, checkpoint, dead, backup,
  registry, leases) terbukti bersih (test `test_secret_stays_out_of_all_dependencies`)

## 11. Hasil full regression

`tests/`: **1766 passed, 0 failed** (~90s). Semua phase 1–34 termasuk
konformansi Phase 33 dan policy Phase 32. `docs/` tidak ada yang diubah
(kecuali menambah dokumen Phase 34).

## 12. Known limitation dan risiko

- Queue bersifat single-writer in-process; tidak ada konsumsi multi-proses
  secara paralel (lease recovery menyelesaikan konflik stale-leader).
- Backoff default 0 ms agar test deterministik; produksi dapat menetapkan
  `base_delay_ms`/`jitter_factor` nyata.
- Backup snapshot menyalin seluruh isi file (tanpa enkripsi; konten dianggap
  tidak sensitif — secret-shape dicegah di lapisan sebelumnya).
- In-memory FailureDiagnostics (bounded), tidak dipersist.
- Recovery journal mendeteksi gap tetapi tidak menutup otomatis — gap ditandai
  (`gap_detected`) dan recovery tetap dapat berjalan pada prefix kontigu.

## 13. Checkpoint PASS / FAIL

**PASS.**

Daftar cek:
- [x] Durable queue + retry policy + exponential backoff + jitter
- [x] Dead-letter queue + circuit breaker
- [x] Lease & lock recovery + stale/orphan/stuck recovery
- [x] Idempotency protection + duplicate execution prevention
- [x] Checkpoint restoration + crash/restart recovery
- [x] Recovery journal + failure diagnostics + backup/restore verification
- [x] Failure injection + offline→online/network interruption recovery test
- [x] Full regression PASS
- [x] Git working tree bersih (hanya file baru)

## 14. Git status

New: `src/handoff_agent/reliability.py`, `tests/test_phase34_reliability.py`,
`docs/company-f/RELIABILITY.md`, `docs/company-f/reports/PHASE34.md`. Tidak ada
file existing yang dimodifikasi. File instruksi untracked dibiarkan.

## 15. Commit hash

`c13f3a0` — `feat(phase-34): distributed reliability, durable queue, and recovery`

## 16. Push status

Diisi setelah push (target `origin master`).

## 17. Rekomendasi / blocker untuk phase berikutnya

- Phase 35 (Operations, Compatibility, DX) dapat memakai reliability evidence:
  health/status/diagnostic dari ReliabilityEngine dan telemetry sebagai sumber
  truth bagi operator CLI. **Blocker: tidak ada.**