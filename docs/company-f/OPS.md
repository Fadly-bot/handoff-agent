# Phase 35 — Operations, Compatibility, and Developer Experience

Status: **PASS** — unified ops interface for operator, developer, dan agent.

## 1. Tujuan phase

Menyediakan interface aman dan konsisten bagi operator, developer, dan agent
untuk menjalankan, memeriksa, mendiagnosis, dan mengaudit Development Company F:
CLI hierarchy seragam, output JSON stabil, exit code stabil, kesalahan
secret-safe, mode dry-run (zero-write, zero-network), konsistensi API/MCP,
panduan operator, panduan integrasi agent, matriks kompatibilitas, matriks
validasi kapabilitas nyata, dan laporan konformance.

## 2. File yang diperiksa

- `src/handoff_agent/cli.py` (subcommand + dispatch)*
- `src/handoff_agent/tool_registry.py` — `ADAPTER_KINDS`, `run_tool_conformance`
- `src/handoff_agent/reliability.py` — `ReliabilityEngine`, `CheckpointStore`,
  `RecoveryJournal`, `run_reliability_conformance`
- `src/handoff_agent/{policy_engine,telemetry,registry,sync,workflow,remote}.py`
- `tests/test_release.py`, `tests/test_security.py`
- `REVISIPHASE30-36.md` (instruksi)

## 3. File yang dibuat atau diubah

Dibuat:
- `src/handoff_agent/ops.py`
- `tests/test_phase35_ops.py` (53 test)
- `docs/company-f/OPS.md` (ini)
- `docs/company-f/reports/PHASE35.md`

Diubah:
- `src/handoff_agent/cli.py` — subcommand `ops` + dispatch ke `run_ops_cli`.

## 4. Arsitektur / contract yang ditambahkan

### 4.1 Command hierarchy (OPS_COMMANDS)

`health`, `status`, `doctor`, `trace`, `audit`, `checkpoint`, `workflow`,
`agent`, `policy-explain`, `tool`, `remote`, `sync`, `recovery`,
`compatibility`, `conformance` — 15 command.

### 4.2 Exit code stabil

| Kode | Arti |
|------|------|
| 0 | ok |
| 1 | operation error |
| 2 | usage error (command/arg tidak dikenal) |
| 3 | policy block (denied / butuh approval) |
| 4 | degraded |
| 5 | not configured / prerequisite missing |

### 4.3 Payload JSON stabil

Semua interface (CLI/API/MCP) menghasilkan skema tingkat-atas yang identik:
`handoff_ops_version`, `command`, `ok`, `exit_code`, `dry_run`, `messages`,
`warnings`, `errors`, `payload`. `assert_consistent_schema()` menjaminnya.

### 4.4 Secret-safe

`redact_value` diterapkan pada seluruh payload sebelum keluar; kesalahan
di-sanitasi; tidak ada nilai bentuk-secret yang lolos lewat interface mana pun.

### 4.5 Dry-run

Handlers mutating (recovery, conformance, checkpoint, remote, sync, trace)
mengembalikan rencana apa yang akan dilakukan; **zero-write** (tidak ada file
state ditulis) dan **zero-network** (tidak ada koneksi remote dibuat).

### 4.6 Kompatibilitas & validasi kapabilitas nyata

`compatibility` memprobe 11 komponen × 3 interface (`cli`, `api`, `mcp`).
Sebuah kapabilitas hanya di-`claimed` bila module importable dan seluruh symbol
resolvable — tidak ada klaim aspirasional. Di lingkungan ini seluruh 33 entry
proven (`proven_total=33`, `unproven=[]`).

### 4.7 Conformance report

`conformance` menjalankan tool boundary (`run_tool_conformance`) untuk seluruh
`ADAPTER_KINDS` (`mcp`, `cli`, `skill`, `generic`) dan reliability suite
(`run_reliability_conformance`, 11 checks) di workspace terpisah.

## 5. Panduan operator

- `handoff ops health` — ringkasan kesehatan (providers, telemetry, reliability).
- `handoff ops status` — status reliability, agents, workflows, telemetry.
- `handoff ops doctor` — deteksi masalah konfigurasi (repo, provider, registry,
  checkpoint, telemetry); exit 1/4 bila ada masalah.
- `handoff ops trace [--trace-id ID]` — timeline eksekusi.
- `handoff ops audit` — ringkasan jejak kebijakan/telemetry/recovery (secret-free).
- `handoff ops checkpoint` — verifikasi integritas checkpoint (SHA-256).
- `handoff ops recovery run` — jalankan recovery stale queue + lease.
- `handoff ops --dry-run ...` — preview tanpa efek.
- `handoff ops --json ...` — output machine-readable.

## 6. Panduan integrasi agent

- API: `from handoff_agent.ops import run_ops_command; report, code = run_ops_command(...)`.
- CLI: `handoff ops ...` (posisi argumen apa pun).
- MCP: `ops_mcp_payload(report)` — subset summary dengan skema sama.
- Selalu cek `report.exit_code`; `report.ok` adalah `exit_code == 0`.
- Jangan pernah menyuntikkan nilai secret ke `action`/`resource` — payload
  di-redact di sisi server.

## 7. Daftar test yang dijalankan

Phase 35 (53) + targeted + audit (282) + full suite (1820).

## 8. Hasil test sebelum perbaikan

Iterasi 1: 2 fail — `doctor` melaporkan masalah provider sehingga exit 1 di
lingkungan CI (harus terdeteksi, bukan gagal), dan tanda `dry_run` tidak naik
ke output JSON tingkat-atas.

## 9. Perbaikan yang dilakukan

1. Tes `doctor` difokuskan pada hasil check individu (git repo = ok; exit 1
   karena `provider_configuration`).
2. `redacted_dict()` membaca `dry_run`/`_dry_run` dari payload tanpa membuang
   key.

## 10. Hasil test setelah perbaikan

Phase 35: **53 passed**; full suite: **1820 passed**; audits: **282 passed**.

## 11. Hasil security check

- stdlib-only PASS; no third-party imports PASS.
- Secret-leak PASS (probe bentuk-secret di-redact; tidak ada assignment
  secret/API key dalam source atau docs).
- Git-safety PASS; Filesystem-boundary PASS (tidak ada shell/exec/eval).
- JSON export bersih: `test_run_ops_cli_health_dry_run_json` memverifikasi tidak
  ada nilai ter-redact yang bocor.

## 12. Hasil full regression

`tests/`: **1820 passed, 0 failed** (~90s) seluruh phase 1–35.

## 13. Known limitation dan risiko

- Trace memerlukan telemetry aktif; tanpa kolektor exit 5.
- Recovery non-dry-run hanya bekerja in-process single-writer (stale-leader
  recovery menangani konflik).
- Matriks kompatibilitas memvalidasi importability, bukan perilaku penuh
  (perilaku penuh dijamin lewat conformance suite).
- Data directory default dialokasikan sementara (temp) bila `--data-dir`
  tidak diberikan.

## 14. Checkpoint PASS / FAIL

**PASS.** Seluruh scope Phase 35 terpenuhi.

## 15. Git status

New: `src/handoff_agent/ops.py`, `tests/test_phase35_ops.py`,
`docs/company-f/OPS.md`, `docs/company-f/reports/PHASE35.md`.
Diubah: `src/handoff_agent/cli.py`.

## 16. Commit hash

`943108f` — `feat(phase-35): unified operations, compatibility, and developer experience`

## 17. Rekomendasi / blocker untuk phase berikutnya

- Phase 36 dapat memanfaatkan ops `compatibility`/`conformance` sebagai dasar
  sertifikasi rilis. **Blocker: tidak ada.**